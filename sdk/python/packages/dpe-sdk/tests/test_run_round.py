"""一轮编排（connector 契约 §6.4–§6.8、§7.1–§7.3）：脚本化插件（真实子进程）× 进程内参考服务端。

插件行为由实例配置给出（§8.4 的线协议夹具 ``conformance/connector-fixture/``，脚本模式）；
协议路径经真实参考服务端
（``client_helpers.in_process_client``），``httpx`` transport 层只做故障注入（外部并发写入、
响应丢失）。状态目录是真实的临时目录。
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import dpe_hash
import httpx
import pytest
from client_helpers import PREFIX, REMOTE, in_process_client
from dpe_sdk.credentials import CredentialProvider, StaticCredentialProvider
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, IfAbsent
from dpe_sdk.pusher import AsyncStatefulPusher
from dpe_sdk.run import (
    InstanceStore,
    ItemFailure,
    PluginHost,
    RoundOptions,
    RoundResult,
    StateDirectory,
    run_round,
)
from dpe_sdk.run import state as state_module
from dpe_sdk.run.definition import ConflictPolicy
from dpe_sdk.run.manifest import parse_manifest
from dpe_sdk.run.round import finished_level
from dpe_sdk.testing import Engine, PrefixAuthorizer, create_app
from dpe_sdk.transport import AsyncDpeClient
from engine_helpers import make_engine

pytestmark = pytest.mark.anyio

#: §8.4 的 connector 线协议夹具（仓库 conformance/connector-fixture/，脚本模式）
PLUGIN = (
    Path(__file__).resolve().parents[5]
    / "conformance"
    / "connector-fixture"
    / "dpe_connector_fixture.py"
)
MANIFEST = parse_manifest(
    {
        "manifest_version": 1,
        "name": "connector-fixture",
        "version": "1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {"type": "object"},
    }
)
P = "dpe://src/"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(*texts: str) -> list[dict[str, Any]]:
    return [{"category": "NarrativeText", "text": t} for t in texts]


def doc_item(name: str, *texts: str, **extra: Any) -> dict[str, Any]:
    """一页文本的 document 产出项。"""
    return {
        "kind": "document",
        "file_uri": P + name,
        "document": {"file_type": "md"},
        "pages": [{"elements": _text(*texts)}],
        **extra,
    }


def expected(*texts: str) -> str:
    """``doc_item`` 产出内容的 doc_hash。"""
    return Document.model_validate(
        {"file_type": "md", "pages": [{"elements": _text(*texts)}]}
    ).doc_hash()


class Harness:
    """一个实例：状态目录 + 远端客户端；每次 ``round`` 启动一个新插件进程跑一轮。"""

    def __init__(self, tmp: Path, client: AsyncDpeClient, foreign: AsyncDpeClient) -> None:
        self.dir = StateDirectory(tmp / "state")
        self.record = tmp / "requests.ndjson"
        self.client = client
        self.foreign = foreign
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.store: InstanceStore | None = None
        self.result: RoundResult | None = None

    def requests(self, method: str) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        lines = [json.loads(line) for line in self.record.read_text().splitlines()]
        return [entry["params"] for entry in lines if entry["method"] == method]

    async def round(
        self,
        config: dict[str, Any],
        *,
        policy: ConflictPolicy | None = None,
        options: RoundOptions | None = None,
        max_message_bytes: int = 1 << 20,
        client: AsyncDpeClient | None = None,
    ) -> RoundResult:
        self.events.clear()
        host = PluginHost(exit_grace=1.0, max_message_bytes=max_message_bytes)
        await host.start([sys.executable, str(PLUGIN)], {"PATH": os.environ.get("PATH", "")})
        try:
            init = await host.initialize(
                manifest=MANIFEST, uri_prefix=P, config={**config, "record": str(self.record)}
            )
            self.store = self.dir.open_instance("inst", "identity-1")
            pusher = AsyncStatefulPusher(client or self.client, state=self.store)
            self.result = await run_round(
                host=host,
                pusher=pusher,
                store=self.store,
                uri_prefix=P,
                policy=policy or ConflictPolicy(),
                supports_cursor=init.capabilities.supports_cursor,
                blob_max_bytes=None,
                options=options,
                on_event=lambda event, level, fields: self.events.append((event, level, fields)),
            )
            return self.result
        finally:
            await host.close()

    def finished_level(self) -> str:
        """``round.finished`` 由调用方按最终结果发出（#161）：这里按同一规则从结果算级别。"""
        assert self.result is not None
        result = self.result
        return finished_level(
            result.outcome, cursor_given=result.cursor_given, incremental=result.incremental
        )


@asynccontextmanager
async def harness(
    tmp_path: Path,
    transport: Callable[[httpx.AsyncBaseTransport], httpx.AsyncBaseTransport] | None = None,
    *,
    engine: Engine | None = None,
    credential: CredentialProvider | None = None,
) -> AsyncIterator[Harness]:
    app = create_app(engine or make_engine(), prefix=PREFIX)
    inner: httpx.AsyncBaseTransport = httpx.ASGITransport(app=app)
    async with (
        in_process_client(
            transport=transport(inner) if transport else inner, credential=credential
        ) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        yield Harness(tmp_path, client, foreign)


def codes(result: RoundResult) -> list[str]:
    return [failure.code for failure in result.failures]


# ---------------------------------------------------------------------- 产出项与落地


async def test_each_item_kind_lands_and_the_cursor_advances(tmp_path: Path) -> None:
    """document（含跨批分段）、delete、move 逐类落地；整轮成功后持久化游标，下一轮带上它。"""
    async with harness(tmp_path) as h:
        first = await h.round(
            {
                "batches": [
                    [doc_item("a.md", "A"), doc_item("b.md", "B1", continued=True)],
                    [
                        {
                            "kind": "document",
                            "file_uri": P + "b.md",
                            "pages": [{"elements": _text("B2")}],
                        },
                        doc_item("x.md", "X"),
                    ],
                ],
                "cursor": "c1",
            }
        )
        assert first.outcome == "succeeded" and first.failures == []
        assert first.counts.as_dict()["created"] == 3
        assert first.cursor_persisted and h.store is not None and h.store.cursor == "c1"
        assert first.incremental is False  # 首轮未给游标
        two_pages = Document.model_validate(
            {
                "file_type": "md",
                "pages": [{"elements": _text("B1")}, {"elements": _text("B2")}],
            }
        )
        assert await h.foreign.head(P + "b.md") == two_pages.doc_hash()

        second = await h.round(
            {
                "batches": [
                    [
                        {"kind": "delete", "file_uri": P + "x.md"},
                        {"kind": "move", "from_uri": P + "a.md", "to_uri": P + "c.md"},
                    ]
                ],
                "cursor": "c2",
            }
        )
        assert second.outcome == "succeeded" and second.incremental is True
        assert (second.counts.deleted, second.counts.moved) == (1, 1)
        assert await h.foreign.head(P + "x.md") is None
        assert await h.foreign.head(P + "a.md") is None
        assert await h.foreign.head(P + "c.md") == expected("A")
        assert [p.get("cursor") for p in h.requests("scan") if "next" not in p] == [None, "c1"]
        assert h.store.cursor == "c2"


async def test_out_of_prefix_and_invalid_items_are_rejected_and_hold_the_cursor(
    tmp_path: Path,
) -> None:
    """越界、封闭 schema 违例、未知 category、插件 error 条目：条目失败、均未投递、本轮 partial。"""
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        {**doc_item("ok.md", "fine")},
                        {**doc_item("x", "out"), "file_uri": "dpe://other/x.md"},
                        {"kind": "move", "from_uri": P + "ok.md", "to_uri": "dpe://other/y.md"},
                        {"kind": "rename", "file_uri": P + "r.md"},
                        {**doc_item("u.md", "u"), "extra": 1},
                        {
                            "kind": "document",
                            "file_uri": P + "c.md",
                            "document": {"file_type": "md"},
                            "pages": [{"elements": [{"category": "Nope", "text": "?"}]}],
                        },
                        {
                            "kind": "error",
                            "file_uri": P + "e.md",
                            "code": "source_unavailable",
                            "message": "boom",
                            "retryable": True,
                        },
                    ]
                ],
                "cursor": "c1",
            }
        )
    assert result.outcome == "partial"
    assert codes(result) == [
        "out_of_prefix",
        "out_of_prefix",
        "DPE_VALIDATION",
        "DPE_VALIDATION",
        "DPE_CATEGORY_UNKNOWN",
        "source_unavailable",
    ]
    sources = [f.source for f in result.failures]
    assert sources == ["prefix", "prefix", "validation", "validation", "validation", "plugin"]
    move_failure = result.failures[1]
    assert isinstance(move_failure, ItemFailure)
    assert move_failure.to_report()["to_uri"] == "dpe://other/y.md"
    assert result.counts.created == 1 and result.counts.failed == 6
    assert not result.cursor_persisted and h.store is not None and h.store.cursor is None
    assert result.cursor_held_since == result.started_at
    assert h.finished_level() == "warn"
    async with harness(tmp_path) as check:
        assert await check.foreign.head("dpe://other/x.md") is None


async def test_unclosed_and_interleaved_segments_fail_the_whole_document(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        doc_item("i.md", "I1", continued=True),
                        doc_item("j.md", "J"),  # 插入其他条目：i.md 整篇失败
                        doc_item("k.md", "K1", continued=True),  # 轮结束仍未闭合
                    ]
                ]
            }
        )
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_VALIDATION", "segment_unclosed"]
        assert await h.foreign.head(P + "i.md") is None
        assert await h.foreign.head(P + "k.md") is None
        assert await h.foreign.head(P + "j.md") == expected("J")


# ---------------------------------------------------------------------- blob


PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(200_000)


def image_item(name: str, blob: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "document",
        "file_uri": P + name,
        "document": {"file_type": "md"},
        "pages": [{"elements": [{"category": "Image", "mime_type": "image/png", "blob": blob}]}],
    }


def image_hash(data: bytes) -> str:
    element = {"category": "Image", "mime_type": "image/png", "blob": dpe_hash.blob_ref(data)}
    return Document.model_validate(
        {"file_type": "md", "pages": [{"elements": [element]}]}
    ).doc_hash()


async def test_inline_and_multi_chunk_handle_blobs_are_uploaded(tmp_path: Path) -> None:
    small = b"tiny-bytes"
    png = tmp_path / "image.png"
    png.write_bytes(PNG)
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        image_item("inline.md", {"inline": base64.b64encode(small).decode()}),
                        image_item("handle.md", {"handle": "h1", "size": len(PNG)}),
                    ]
                ],
                "blobs": {"h1": {"file": str(png)}},
            },
            max_message_bytes=64 * 1024,  # 迫使句柄分多块读取
        )
        assert result.outcome == "succeeded", result.failures
        assert await h.foreign.head(P + "inline.md") == image_hash(small)
        assert await h.foreign.head(P + "handle.md") == image_hash(PNG)
        offsets = [p["offset"] for p in h.requests("read_blob")]
        assert len(offsets) > 1 and offsets[0] == 0 and offsets == sorted(offsets)


async def test_blob_failures_fail_the_document(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        image_item("size.md", {"handle": "h1", "size": 3}),
                        image_item("gone.md", {"handle": "missing"}),
                        image_item("hang.md", {"handle": "slow"}),
                    ]
                ],
                "blobs": {"h1": base64.b64encode(b"abcdef").decode(), "slow": "AAAA"},
                "hang": ["slow"],
            },
            options=RoundOptions(blob_timeout=0.5),
        )
        assert result.outcome == "partial"
        assert codes(result) == ["blob_size_mismatch", "blob_read_failed", "blob_read_failed"]
        retryable = [f.retryable for f in result.failures]
        assert retryable == [False, False, True]  # 句柄失效不可重试，取消可重试
        for name in ("size.md", "gone.md", "hang.md"):
            assert await h.foreign.head(P + name) is None


# ---------------------------------------------------------------------- 保序


async def test_same_uri_items_take_effect_in_stream_order(tmp_path: Path) -> None:
    """链式 move 与「先写后删」在同一批：按流中顺序生效（拆成保序波次）。"""
    async with harness(tmp_path) as h:
        assert (await h.round({"batches": [[doc_item("a.md", "A")]]})).outcome == "succeeded"
        result = await h.round(
            {
                "batches": [
                    [
                        {"kind": "move", "from_uri": P + "a.md", "to_uri": P + "b.md"},
                        {"kind": "move", "from_uri": P + "b.md", "to_uri": P + "c.md"},
                        doc_item("d.md", "D"),
                        {"kind": "delete", "file_uri": P + "d.md"},
                    ]
                ]
            }
        )
        assert result.outcome == "succeeded", result.failures
        assert (result.counts.moved, result.counts.created, result.counts.deleted) == (2, 1, 1)
        assert await h.foreign.head(P + "c.md") == expected("A")
        for name in ("a.md", "b.md", "d.md"):
            assert await h.foreign.head(P + name) is None


# ---------------------------------------------------------------------- 冲突策略


async def test_commit_conflict_source_wins_and_report(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "v1")]]})
        theirs = Document.model_validate(
            {"file_type": "md", "pages": [{"elements": _text("theirs")}]}
        )
        await h.foreign.commit(P + "a.md", theirs, BaseHash(expected("v1")))

        reported = await h.round(
            {"batches": [[doc_item("a.md", "v2")]]},
            policy=ConflictPolicy(commit="report"),
        )
        assert reported.outcome == "partial"
        assert codes(reported) == ["DPE_PRECONDITION_FAILED"]
        assert reported.failures[0].retryable is False
        assert reported.cursor_held_since is not None
        assert await h.foreign.head(P + "a.md") == theirs.doc_hash()  # 未覆盖

        won = await h.round({"batches": [[doc_item("a.md", "v2")]]})  # 缺省 source_wins
        assert won.outcome == "succeeded" and won.counts.updated == 1
        assert await h.foreign.head(P + "a.md") == expected("v2")
        assert won.cursor_held_since is None


class _AlwaysRaced(httpx.AsyncBaseTransport):
    """每个 commit 发出之前，他人先改写目标文档（持续竞态）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport, before: Callable[[], Awaitable[None]]):
        self._inner = inner
        self._before = before
        self.armed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.armed and request.method == "PUT" and request.url.path.endswith("/documents"):
            await self._before()
        return await self._inner.handle_async_request(request)


async def test_conflict_retries_are_bounded(tmp_path: Path) -> None:
    """source_wins 下持续冲突：本轮至多重提 2 次，之后条目失败（retryable: true）。"""
    counter = {"n": 0}
    raced: list[_AlwaysRaced] = []
    holder: dict[str, AsyncDpeClient] = {}

    async def foreign_write() -> None:
        counter["n"] += 1
        current = await holder["foreign"].head(P + "a.md")
        doc = Document.model_validate(
            {"file_type": "md", "pages": [{"elements": _text(f"t{counter['n']}")}]}
        )
        await holder["foreign"].commit(
            P + "a.md", doc, BaseHash(current) if current else IfAbsent()
        )

    def wrap(inner: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        raced.append(_AlwaysRaced(inner, foreign_write))
        return raced[0]

    async with harness(tmp_path, wrap) as h:
        holder["foreign"] = h.foreign
        await h.round({"batches": [[doc_item("a.md", "v1")]]})
        raced[0].armed = True
        result = await h.round({"batches": [[doc_item("a.md", "v2")]]})
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_PRECONDITION_FAILED"]
        assert result.failures[0].retryable is True
        assert counter["n"] == 3  # 首次写入 + 2 次重提，每次都被抢先


async def test_move_target_occupied_is_a_permanent_item_failure(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
        result = await h.round(
            {"batches": [[{"kind": "move", "from_uri": P + "a.md", "to_uri": P + "b.md"}]]}
        )
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_ALREADY_EXISTS"]
        assert result.failures[0].retryable is False
        report = result.failures[0].to_report()
        assert (report["from_uri"], report["to_uri"]) == (P + "a.md", P + "b.md")
        assert await h.foreign.head(P + "a.md") == expected("A")  # 不退化为 commit + 删源
        assert await h.foreign.head(P + "b.md") == expected("B")


# ------------------------------------------------------------ move 未达成后的目标 document（#178）

_ERROR = {"kind": "error", "code": "internal", "message": "x", "retryable": False}


def _move(source: str, target: str) -> dict[str, Any]:
    return {"kind": "move", "from_uri": P + source, "to_uri": P + target}


def _is_move(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/move")


async def _deleted_in_a_failed_round(h: Harness) -> None:
    """a 已同步；随后一轮删除 a，但同轮另有条目出错，游标停在 c1（§6.8）。"""
    first = await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
    assert first.outcome == "succeeded"
    held = await h.round(
        {"batches": [[{"kind": "delete", "file_uri": P + "a.md"}, _ERROR]], "cursor": "c2"}
    )
    assert held.outcome == "partial" and h.store is not None and h.store.cursor == "c1"
    assert await h.foreign.head(P + "a.md") is None  # 删除已生效


def _synced(h: Harness) -> dict[str, str]:
    snapshot = h.store.load(REMOTE) if h.store else None
    assert snapshot is not None
    return dict(snapshot.synced)


async def test_move_after_own_delete_is_achieved_by_the_target_document(tmp_path: Path) -> None:
    """§7.2：判定时源与目标都不存在的 move，其后的目标 document 以 if_absent 建出目标
    （created）→ 这条 move 按已达成计（moved），本轮 succeeded、游标推进。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "succeeded", codes(result)
        assert result.counts.as_dict() == {
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
            "moved": 1,
            "failed": 0,
        }
        assert result.cursor_persisted and h.store is not None and h.store.cursor == "c3"
        assert await h.foreign.head(P + "a.md") is None
        assert await h.foreign.head(P + "b.md") == expected("A")
        assert _synced(h) == {P + "b.md": expected("A")}


async def test_move_onto_absent_target_without_target_document_still_fails(tmp_path: Path) -> None:
    """没有给出目标的 document：轮末仍按「源已不存在，目标未达成」计条目失败、游标扣住。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        result = await h.round({"batches": [[_move("a.md", "b.md")]], "cursor": "c3"})
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_NOT_FOUND"]
        report = result.failures[0].to_report()
        assert (report["from_uri"], report["to_uri"]) == (P + "a.md", P + "b.md")
        assert result.counts.failed == 1 and not result.cursor_persisted


async def test_move_whose_source_others_deleted_is_also_achieved(tmp_path: Path) -> None:
    """判定只看 R：源被他人删除（本实例仍有源的 L）同样适用——目标由 document 建出即按已达成计，
    并以「确认不存在」清掉源残留的 L（不发写）。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.delete(P + "a.md", expected("A"))
        result = await h.round({"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]})
        assert result.outcome == "succeeded", codes(result)
        assert (result.counts.moved, result.counts.created) == (1, 1)
        assert _synced(h) == {P + "b.md": expected("A")}  # 源的 L 已清掉
        assert await h.foreign.head(P + "a.md") is None


async def test_target_document_does_not_overwrite_a_target_that_appeared(tmp_path: Path) -> None:
    """判定时源已不存在、目标却已被他人建出：目标 document 以 if_absent 提交被拒，不覆盖、不按
    策略重提（source_wins 也不例外），这条 move 计「目标已被占用」。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_ALREADY_EXISTS"]
        assert result.counts.as_dict()["created"] == result.counts.as_dict()["updated"] == 0
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()  # 未被覆盖


async def test_target_document_after_occupied_target_is_not_written(tmp_path: Path) -> None:
    """move 因目标被占用失败（源仍在）：其后的目标 document 不发写、不另计失败——绝不借它覆盖
    占用目标的文档（§7.2「永久冲突」）。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())
        result = await h.round({"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]})
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_ALREADY_EXISTS"]
        assert result.counts.failed == 1 and result.counts.updated == 0
        assert await h.foreign.head(P + "a.md") == expected("A")
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()


async def test_target_document_after_a_source_deleted_mid_write_is_not_written(
    tmp_path: Path,
) -> None:
    """判定时源仍在、读写之间被他人删除（写入期得到「不存在」）：不属于「判定时源与目标都
    不存在」，move 照常失败，其后的目标 document 不发写。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})

        async def sweep() -> None:
            await h.foreign.delete(P + "a.md", expected("A"))

        made[0].before.append((_is_move, sweep))
        result = await h.round({"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]})
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_NOT_FOUND"]
        assert result.counts.created == 0
        assert await h.foreign.head(P + "b.md") is None


async def test_move_onto_a_target_already_equal_to_the_document_is_achieved(
    tmp_path: Path,
) -> None:
    """判定时源已不存在、目标已被建成与目标 document 相同的内容（如另一写入者做了同一次改名）：
    不发写，这条 move 按已达成计并清掉源残留的 L，不会每轮重复失败、永久扣住游标。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.delete(P + "a.md", expected("A"))
        await h.foreign.commit(P + "b.md", _doc_of("A"), IfAbsent())
        result = await h.round({"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]})
        assert result.outcome == "succeeded", codes(result)
        assert (result.counts.moved, result.counts.unchanged) == (1, 1)
        assert _synced(h) == {P + "b.md": expected("A")}


async def test_target_document_after_a_move_whose_read_was_rejected_is_not_written(
    tmp_path: Path,
) -> None:
    """move 的读被确定拒绝（逐条定位后记条目失败，没有推送结果）：同样是未达成，其后的目标
    document 不发写——不得以「无 L，R 存在」覆盖占用目标的文档。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())
        made[0].fail = _reject_reads_of({P + "a.md"}, _problem(403, "DPE_FORBIDDEN"))
        result = await h.round({"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]})
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_FORBIDDEN"]
        assert result.counts.updated == 0
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()


async def test_failure_while_clearing_the_source_keeps_the_wave_accounted(tmp_path: Path) -> None:
    """目标建出后清源 L 的读遇到网络失败：同一波的其余结果先全部入账，再以轮级失败结束。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await _deleted_in_a_failed_round(h)

        async def cut_reads() -> None:
            def fail(request: httpx.Request) -> httpx.Response | None:
                if _is_heads(request) or request.method == "HEAD":
                    raise httpx.ConnectError("断网", request=request)
                return None

            made[0].fail = fail

        made[0].after.append((_is_commit, cut_reads))
        made[0].after.append((_is_commit, cut_reads))
        result = await h.round(
            {
                "batches": [[_move("a.md", "b.md"), doc_item("b.md", "A"), doc_item("c.md", "C")]],
                "cursor": "c3",
            }
        )
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"
        assert (result.counts.moved, result.counts.created) == (1, 2)


async def test_writes_touching_an_unachieved_target_are_not_sent(tmp_path: Path) -> None:
    """move 因目标被占用而失败：其后对目标的 delete、以它为源的 move 同样不发写，不删除也不
    移走占用者。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())
        result = await h.round(
            {
                "batches": [
                    [
                        _move("a.md", "b.md"),
                        {"kind": "delete", "file_uri": P + "b.md"},
                        _move("b.md", "d.md"),
                    ]
                ]
            }
        )
        assert codes(result) == ["DPE_ALREADY_EXISTS"]
        assert (result.counts.deleted, result.counts.moved) == (0, 0)
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()
        assert await h.foreign.head(P + "d.md") is None


async def test_target_document_before_the_move_is_written_normally(tmp_path: Path) -> None:
    """目标 document 出现在 move 之前：那时还没有未达成的 move，按普通文档落地。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        result = await h.round({"batches": [[doc_item("b.md", "A"), _move("a.md", "b.md")]]})
        assert result.outcome == "succeeded", codes(result)
        assert (result.counts.created, result.counts.moved) == (1, 1)
        assert await h.foreign.head(P + "a.md") is None


async def test_reread_failure_after_a_rejected_target_document_ends_the_round(
    tmp_path: Path,
) -> None:
    """目标 document 的 if_absent 写入遇写后冲突、重读又断网：按读失败终止本轮（轮级），不把
    网络失败当作目标被占用吞掉。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        hooks = made[0]
        await _deleted_in_a_failed_round(h)

        async def occupy_then_cut_reads() -> None:
            await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())

            def cut(request: httpx.Request) -> httpx.Response | None:
                if request.method == "HEAD" or _is_heads(request):
                    raise httpx.ConnectError("断网", request=request)
                return None

            hooks.fail = cut

        hooks.before.append((_is_commit, occupy_then_cut_reads))
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()


async def test_move_rejudged_under_source_wins_awaits_the_target_document(
    tmp_path: Path,
) -> None:
    """source_wins 下 move 写后冲突（源被改写）、重读时源已被删除：重新判定得出「源已不存在，
    目标未达成」，同样由其后的目标 document 建出目标。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})

        async def rewrite() -> None:
            await h.foreign.commit(P + "a.md", _doc_of("theirs"), BaseHash(expected("A")))

        async def remove() -> None:
            await h.foreign.delete(P + "a.md", _doc_of("theirs").doc_hash())

        made[0].before.append((_is_move, rewrite))
        made[0].after.append((_is_move, remove))
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]]},
            policy=ConflictPolicy(commit="source_wins"),
        )
        assert result.outcome == "succeeded", codes(result)
        assert (result.counts.moved, result.counts.created) == (1, 1)
        assert await h.foreign.head(P + "b.md") == expected("A")


async def test_second_move_onto_the_same_target_is_skipped(tmp_path: Path) -> None:
    """两条 move 指向同一个不存在的目标：第一条未达成时，第二条涉及未达成的目标、不发写，也使
    其后的目标 document 不发写——两个源谁该达成无从判断，第一条照常计失败、游标扣住。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A"), doc_item("c.md", "C")]]})
        await h.foreign.delete(P + "a.md", expected("A"))
        await h.foreign.delete(P + "c.md", expected("C"))
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), _move("c.md", "b.md"), doc_item("b.md", "A")]]}
        )
        assert result.outcome == "partial"
        assert codes(result) == ["DPE_NOT_FOUND"]
        assert result.failures[0].to_report()["from_uri"] == P + "a.md"
        assert (result.counts.moved, result.counts.created) == (0, 0)
        assert await h.foreign.head(P + "b.md") is None


async def test_writes_touching_an_awaiting_target_are_not_sent(tmp_path: Path) -> None:
    """move 在等待目标 document 时，其后对目标的 delete、以它为源的 move 不发写：判定后目标
    可能已被他人建出，不得删除或移走它。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())
        result = await h.round(
            {
                "batches": [
                    [
                        _move("a.md", "b.md"),
                        {"kind": "delete", "file_uri": P + "b.md"},
                        _move("b.md", "d.md"),
                    ]
                ]
            }
        )
        assert codes(result) == ["DPE_NOT_FOUND"]
        assert (result.counts.deleted, result.counts.moved) == (0, 0)
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()
        assert await h.foreign.head(P + "d.md") is None


async def test_skipped_move_also_holds_its_own_target(tmp_path: Path) -> None:
    """move 因涉及未达成的目标而跳过：它自己的目标同样未达成，其后的目标 document 不覆盖
    占用者。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("c.md", "C")]]})
        await h.foreign.commit(P + "a.md", _doc_of("theirs"), IfAbsent())
        await h.foreign.commit(P + "b.md", _doc_of("other"), IfAbsent())
        result = await h.round(
            {"batches": [[_move("c.md", "a.md"), _move("a.md", "b.md"), doc_item("b.md", "A")]]}
        )
        assert codes(result) == ["DPE_ALREADY_EXISTS"]
        assert result.counts.updated == 0
        assert await h.foreign.head(P + "a.md") == _doc_of("theirs").doc_hash()
        assert await h.foreign.head(P + "b.md") == _doc_of("other").doc_hash()


async def test_awaiting_move_is_retryable_when_the_target_document_failed(tmp_path: Path) -> None:
    """目标 document 没能产出、可重试（插件暂时读不出目标）：等它的 move 下一轮可能达成，记为
    可重试；目标的失败不可重试（如文档层校验）时，重跑结果相同，move 同样不可重试。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        unreadable = {
            "kind": "error",
            "file_uri": P + "b.md",
            "code": "source_unavailable",
            "message": "x",
            "retryable": True,
        }
        result = await h.round({"batches": [[_move("a.md", "b.md"), unreadable]], "cursor": "c3"})
        assert result.outcome == "partial"
        moves = [f for f in result.failures if f.to_report().get("to_uri") == P + "b.md"]
        assert [(f.code, f.retryable) for f in moves] == [("DPE_NOT_FOUND", True)]

        broken = doc_item("b.md", "A")
        broken["document"]["bogus"] = 1
        result = await h.round({"batches": [[_move("a.md", "b.md"), broken]], "cursor": "c3"})
        moves = [f for f in result.failures if f.to_report().get("to_uri") == P + "b.md"]
        assert [(f.code, f.retryable) for f in moves] == [("DPE_NOT_FOUND", False)]


async def test_writes_skipped_while_awaiting_keep_the_move_unresolved(tmp_path: Path) -> None:
    """等待中的目标上有写入被跳过（这里是以它为源的 move）：跳过的写入使它涉及的 URI 都未达成，
    其后的目标 document 不再解除等待的 move——跳过必然扣住游标，不会静默丢掉 d。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        result = await h.round(
            {
                "batches": [
                    [
                        _move("a.md", "b.md"),
                        _move("b.md", "d.md"),
                        doc_item("b.md", "A"),
                        doc_item("d.md", "A"),
                    ]
                ],
                "cursor": "c3",
            }
        )
        assert result.outcome == "partial" and not result.cursor_persisted
        assert codes(result) == ["DPE_NOT_FOUND"]
        assert (result.counts.moved, result.counts.created) == (0, 0)


async def test_target_created_then_removed_during_the_write_is_retryable(tmp_path: Path) -> None:
    """目标 document 的 if_absent 写入时目标刚被他人建出、重读时又已删掉：不重提，等待的 move
    轮末计失败但可重试（竞态，下一轮可能达成）。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await _deleted_in_a_failed_round(h)

        async def occupy() -> None:
            await h.foreign.commit(P + "b.md", _doc_of("theirs"), IfAbsent())

        async def vacate() -> None:
            await h.foreign.delete(P + "b.md", _doc_of("theirs").doc_hash())

        made[0].before.append((_is_commit, occupy))
        made[0].after.append((_is_commit, vacate))
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "partial"
        assert [(f.code, f.retryable) for f in result.failures] == [("DPE_NOT_FOUND", True)]
        assert await h.foreign.head(P + "b.md") is None


async def test_target_document_in_a_later_batch_achieves_the_move(tmp_path: Path) -> None:
    """move 与目标 document 分处不同的 scan 批次（插件常见的产出顺序）：等待跨批保留，目标
    建出后 move 照常按已达成计。"""
    async with harness(tmp_path) as h:
        await _deleted_in_a_failed_round(h)
        result = await h.round(
            {"batches": [[_move("a.md", "b.md")], [doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "succeeded", codes(result)
        assert (result.counts.moved, result.counts.created) == (1, 1)


async def test_state_failure_at_the_target_document_leaves_the_move_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标 document 写入后状态保存失败：本轮以 state_unavailable 结束，等待的 move 记为可重试
    （下一轮可能达成），不误报为永久冲突。"""
    real = state_module._write_atomic
    failures = {"left": 0}

    def flaky(path: Path, data: Any) -> None:
        if failures["left"] and path.name == "state.json":
            failures["left"] -= 1
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await _deleted_in_a_failed_round(h)

        async def arm() -> None:
            failures["left"] = 1

        made[0].after.append((_is_commit, arm))
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c3"}
        )
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "state_unavailable"
        moves = [f for f in result.failures if f.to_report().get("to_uri") == P + "b.md"]
        assert [(f.code, f.retryable) for f in moves] == [("DPE_NOT_FOUND", True)]


async def test_source_state_that_cannot_be_cleared_holds_the_cursor(tmp_path: Path) -> None:
    """目标建出后清源 L 的读被确定拒绝：残留的 L 会让源端之后重现同一 URI 时误判冲突，所以给源
    记一条条目失败、扣住游标；拒绝解除后，下一轮重放 move 时清掉（目标等于源的 base：推送器
    直接判已达成；目标内容不同：经紧跟的目标 document 再次达成）。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
        await h.foreign.delete(P + "a.md", expected("A"))

        async def deny_source() -> None:
            made[0].fail = _reject_reads_of({P + "a.md"}, _problem(403, "DPE_FORBIDDEN"))

        made[0].after.append((_is_commit, deny_source))
        script = {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "A")]], "cursor": "c2"}
        held = await h.round(script)
        assert held.outcome == "partial" and not held.cursor_persisted
        assert [(f.code, f.retryable) for f in held.failures] == [("DPE_FORBIDDEN", False)]
        # 远端已达成（目标建出、move 计 moved），本地 L 没清掉另记一条：两件不同的事
        assert (held.counts.moved, held.counts.created, held.counts.failed) == (1, 1, 1)
        assert held.failures[0].to_report()["file_uri"] == P + "a.md"
        assert P + "a.md" in _synced(h)

        made[0].fail = None
        rerun = await h.round(script)
        assert rerun.outcome == "succeeded", codes(rerun)
        assert _synced(h) == {P + "b.md": expected("A")}


async def test_uncleared_source_state_heals_when_the_target_content_differs(
    tmp_path: Path,
) -> None:
    """同上，但改名时内容也变了（目标 N ≠ 源的 L）：下一轮 move 再次等待，紧跟的目标
    document 判已达成，move 再次达成并清掉源的 L。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
        await h.foreign.delete(P + "a.md", expected("A"))

        async def deny_source() -> None:
            made[0].fail = _reject_reads_of({P + "a.md"}, _problem(403, "DPE_FORBIDDEN"))

        made[0].after.append((_is_commit, deny_source))
        script = {"batches": [[_move("a.md", "b.md"), doc_item("b.md", "B")]], "cursor": "c2"}
        held = await h.round(script)
        assert held.outcome == "partial" and P + "a.md" in _synced(h)

        made[0].fail = None
        rerun = await h.round(script)
        assert rerun.outcome == "succeeded", codes(rerun)
        assert (rerun.counts.moved, rerun.counts.unchanged) == (1, 1)
        assert _synced(h) == {P + "b.md": expected("B")}


async def test_awaiting_move_of_an_aborted_round_is_retryable(tmp_path: Path) -> None:
    """本轮在目标 document 到来之前中止：等待中的 move 记为可重试，不误导为永久冲突。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await _deleted_in_a_failed_round(h)

        def cut_commits(request: httpx.Request) -> httpx.Response | None:
            if _is_commit(request):
                raise httpx.ConnectError("断网", request=request)
            return None

        made[0].fail = cut_commits
        result = await h.round(
            {"batches": [[_move("a.md", "b.md"), doc_item("x.md", "X")]], "cursor": "c3"}
        )
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"
        moves = [f for f in result.failures if f.to_report().get("to_uri") == P + "b.md"]
        assert [(f.code, f.retryable) for f in moves] == [("DPE_NOT_FOUND", True)]


# ---------------------------------------------------------------------- 游标与全量轮


async def test_cursor_is_fail_closed_and_held_since_tracks_the_first_failed_round(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path) as h:
        assert (await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})).outcome == (
            "succeeded"
        )
        error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
        held = await h.round({"batches": [[error]], "cursor": "c2"})
        assert held.outcome == "partial" and not held.cursor_persisted
        assert h.store is not None and h.store.cursor == "c1"
        again = await h.round({"batches": [[error]], "cursor": "c3"})
        assert again.cursor_held_since == held.started_at  # 保持首次失败轮的开始时刻
        ok = await h.round({"batches": [[]], "cursor": "c4"})
        assert ok.outcome == "succeeded" and ok.cursor_held_since is None
        scans = [p.get("cursor") for p in h.requests("scan") if "next" not in p]
        assert scans == [None, "c1", "c1", "c1"]
        assert h.store.cursor == "c4"


async def test_plugin_without_cursor_support_is_never_given_a_cursor(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        for _ in range(2):
            result = await h.round({"batches": [[]], "supports_cursor": False})
            assert result.outcome == "succeeded" and result.incremental is False
            assert not result.cursor_persisted
            assert h.finished_level() == "info"
        assert [p.get("cursor") for p in h.requests("scan")] == [None, None]


async def test_fallback_to_full_round_is_observable(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        result = await h.round({"batches": [[]], "cursor": "c2", "incremental": False})
        assert result.outcome == "succeeded" and result.incremental is False
        assert h.finished_level() == "warn"


async def test_scan_error_fails_the_round(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        result = await h.round(
            {
                "scan_error": {"code": -32003, "message": "down", "data": {"retryable": True}},
                "cursor": "c2",
            }
        )
        assert result.outcome == "failed" and result.incremental is None
        assert result.fatal is not None and result.fatal.code == "source_failed"
        assert result.fatal.retryable is True
        assert h.store is not None and h.store.cursor == "c1"
        assert h.finished_level() == "error"


# ---------------------------------------------------------------------- 结果未知与本地状态


class _DropCommits(httpx.AsyncBaseTransport):
    """commit 照常送达服务端，但响应丢失。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.armed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if self.armed and request.method == "PUT" and request.url.path.endswith("/documents"):
            raise httpx.ReadTimeout("响应丢失", request=request)
        return response


async def test_unknown_outcome_fails_the_round_and_rerun_does_not_conflict(tmp_path: Path) -> None:
    """写入生效、确认前「崩溃」（响应丢失）：本轮 failed、意图保留；重跑经意图对账，不报冲突。"""
    droppers: list[_DropCommits] = []

    def wrap(inner: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        droppers.append(_DropCommits(inner))
        return droppers[0]

    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "v1")]]})
        droppers[0].armed = True
        lost = await h.round({"batches": [[doc_item("a.md", "v2")]]})
        assert lost.outcome == "failed"
        assert lost.fatal is not None and lost.fatal.code == "remote_unavailable"
        assert h.store is not None and h.store.load(REMOTE) is not None
        snapshot = h.store.load(REMOTE)
        assert snapshot is not None and snapshot.intents == {P + "a.md": expected("v2")}

        droppers[0].armed = False
        rerun = await h.round({"batches": [[doc_item("a.md", "v2")]]})
        assert rerun.outcome == "succeeded", rerun.failures
        assert rerun.counts.unchanged == 1  # 自己已生效的写入：已达成，不是冲突


async def test_state_write_failure_is_state_unavailable_and_resaved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """意图落盘失败：本轮 failed（state_unavailable）、未发写；退出前补存使磁盘与内存一致。"""
    real = state_module._write_atomic
    failures = {"left": 0}

    def flaky(path: Path, data: Any) -> None:
        if failures["left"] and path.name == "state.json":
            failures["left"] -= 1
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "v1")]], "cursor": "c1"})
        failures["left"] = 1  # 写前持久化那一次失败
        result = await h.round({"batches": [[doc_item("a.md", "v2")]], "cursor": "c2"})
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "state_unavailable"
        assert result.fatal.level == "round" and result.fatal.retryable is True
        assert await h.foreign.head(P + "a.md") == expected("v1")  # 未发写
        reloaded = h.dir.open_instance("inst", "identity-1")
        snapshot = reloaded.load(REMOTE)
        assert snapshot is not None and snapshot.intents == {}  # 无幽灵意图
        assert reloaded.cursor == "c1"  # 游标未推进

        failures["left"] = 100  # 补存也失败：仍 failed，游标不推进
        stuck = await h.round({"batches": [[doc_item("a.md", "v3")]], "cursor": "c3"})
        assert stuck.outcome == "failed" and not stuck.cursor_persisted
        failures["left"] = 0
        assert h.dir.open_instance("inst", "identity-1").cursor == "c1"


# ---------------------------------------------------------------------- 冲突处置的其余分支


class _Hooks(httpx.AsyncBaseTransport):
    """按请求谓词在请求前 / 响应后执行外部动作，或直接以异常顶替（故障注入）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.before: list[
            tuple[Callable[[httpx.Request], bool], Callable[[], Awaitable[None]]]
        ] = []
        self.after: list[tuple[Callable[[httpx.Request], bool], Callable[[], Awaitable[None]]]] = []
        self.fail: Callable[[httpx.Request], httpx.Response | None] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            replaced = self.fail(request)
            if replaced is not None:
                return replaced
        for match, action in list(self.before):
            if match(request):
                self.before.remove((match, action))
                await action()
        response = await self._inner.handle_async_request(request)
        for match, action in list(self.after):
            if match(request):
                self.after.remove((match, action))
                await action()
        return response


def _hooks() -> tuple[list[_Hooks], Callable[[httpx.AsyncBaseTransport], httpx.AsyncBaseTransport]]:
    made: list[_Hooks] = []

    def wrap(inner: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        made.append(_Hooks(inner))
        return made[0]

    return made, wrap


def _is_commit(request: httpx.Request) -> bool:
    return request.method == "PUT" and request.url.path.endswith("/documents")


def _is_delete(request: httpx.Request) -> bool:
    return request.method == "DELETE" and request.url.path.endswith("/documents")


def _doc_of(*texts: str) -> Document:
    return Document.model_validate({"file_type": "md", "pages": [{"elements": _text(*texts)}]})


async def test_delete_conflict_follows_the_delete_policy(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "v1")]]})
        await h.foreign.commit(P + "a.md", _doc_of("theirs"), BaseHash(expected("v1")))
        delete = {"batches": [[{"kind": "delete", "file_uri": P + "a.md"}]]}

        reported = await h.round(delete)  # 缺省 report：delete 不可逆，不以源为准
        assert reported.outcome == "partial" and codes(reported) == ["DPE_PRECONDITION_FAILED"]
        assert reported.failures[0].retryable is False
        assert await h.foreign.head(P + "a.md") == _doc_of("theirs").doc_hash()

        won = await h.round(delete, policy=ConflictPolicy(delete="source_wins"))
        assert won.outcome == "succeeded" and won.counts.deleted == 1
        assert await h.foreign.head(P + "a.md") is None


async def test_conflict_already_achieved_on_reread_wins_over_report(tmp_path: Path) -> None:
    """§7.2：冲突后重读已是目标状态（commit 同内容、delete 已不存在）即已达成，与策略无关。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        hooks = made[0]
        await h.round({"batches": [[doc_item("a.md", "v1"), doc_item("b.md", "b1")]]})

        async def same_content() -> None:  # 他人抢先写下与我们相同的内容
            await h.foreign.commit(P + "a.md", _doc_of("v2"), BaseHash(expected("v1")))

        hooks.before.append((_is_commit, same_content))
        report = ConflictPolicy(commit="report", delete="report")
        result = await h.round({"batches": [[doc_item("a.md", "v2")]]}, policy=report)
        assert result.outcome == "succeeded", result.failures
        assert result.counts.unchanged == 1
        snapshot = h.store.load(REMOTE) if h.store else None
        assert snapshot is not None and snapshot.synced[P + "a.md"] == expected("v2")

        async def rewrite() -> None:  # 使我们的 delete 得 412
            await h.foreign.commit(P + "b.md", _doc_of("b2"), BaseHash(expected("b1")))

        async def remove() -> None:  # 重读之前他人已删除
            await h.foreign.delete(P + "b.md", _doc_of("b2").doc_hash())

        hooks.before.append((_is_delete, rewrite))
        hooks.after.append((_is_delete, remove))
        deleted = await h.round(
            {"batches": [[{"kind": "delete", "file_uri": P + "b.md"}]]}, policy=report
        )
        assert deleted.outcome == "succeeded", deleted.failures
        assert deleted.counts.deleted == 1


async def test_move_with_rewritten_source_is_retried_under_source_wins(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[doc_item("a.md", "A")]]})
        await h.foreign.commit(P + "a.md", _doc_of("theirs"), BaseHash(expected("A")))
        result = await h.round(
            {"batches": [[{"kind": "move", "from_uri": P + "a.md", "to_uri": P + "b.md"}]]}
        )
        assert result.outcome == "succeeded", result.failures
        assert result.counts.moved == 1
        assert await h.foreign.head(P + "b.md") == _doc_of("theirs").doc_hash()  # 他人写入随之移动


async def test_reread_failure_after_conflict_fails_the_round(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        hooks = made[0]
        await h.round({"batches": [[doc_item("a.md", "v1")]]})

        async def rewrite_then_cut() -> None:
            await h.foreign.commit(P + "a.md", _doc_of("theirs"), BaseHash(expected("v1")))

            def cut(request: httpx.Request) -> httpx.Response | None:
                if request.method == "HEAD":
                    raise httpx.ConnectError("断网", request=request)
                return None

            hooks.fail = cut

        hooks.before.append((_is_commit, rewrite_then_cut))
        result = await h.round({"batches": [[doc_item("a.md", "v2")]]})
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"


async def test_unauthorized_remote_is_instance_level(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        made[0].fail = lambda request: httpx.Response(401, request=request)
        result = await h.round({"batches": [[doc_item("a.md", "A")]]})
        assert result.outcome == "failed"
        assert result.fatal is not None
        assert (result.fatal.level, result.fatal.code) == ("instance", "remote_unauthorized")


# ---------------------------------------------------------------------- 协议违例与收尾写失败


@pytest.mark.parametrize(
    "config",
    [
        {"batches": [[]], "supports_cursor": False, "cursor": "c"},  # 不支持游标却给出游标
        {"batches": [[]], "incremental": True},  # 未给游标却标增量
    ],
)
async def test_end_batch_protocol_violations_fail_the_round(
    tmp_path: Path, config: dict[str, Any]
) -> None:
    async with harness(tmp_path) as h:
        result = await h.round(config)
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "protocol_error"
        assert not result.cursor_persisted and h.store is not None and h.store.cursor is None


async def test_cursor_write_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = state_module._write_atomic
    failing = {"on": False}

    def flaky(path: Path, data: Any) -> None:
        if failing["on"] and path.name == "state.json":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        failing["on"] = True
        result = await h.round({"batches": [[]], "cursor": "c2"})  # 无条目：唯一的写是收尾
        failing["on"] = False
        assert result.outcome == "failed" and not result.cursor_persisted
        assert result.cursor_held_since == result.started_at  # 失败轮的记录
        assert "state.held_write_failed" not in [e for e, _, _ in h.events]
        assert result.fatal is not None and result.fatal.code == "state_unavailable"
        assert h.dir.open_instance("inst", "identity-1").cursor == "c1"


async def test_each_document_sharing_a_failed_handle_reports_its_own_uri(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        image_item("one.md", {"handle": "missing"}),
                        image_item("two.md", {"handle": "missing"}),
                    ]
                ]
            }
        )
        assert codes(result) == ["blob_read_failed", "blob_read_failed"]
        uris = [f.to_report().get("file_uri") for f in result.failures]
        assert uris == [P + "one.md", P + "two.md"]


def test_incompatible_remote_is_instance_level() -> None:
    from dpe_sdk.errors import ContractUnsupportedError, IncompatibleServerError
    from dpe_sdk.run.round import _classify_remote

    for exc in (ContractUnsupportedError("dpe9"), IncompatibleServerError("dpe/9")):
        failure = _classify_remote(exc, unknown=True)
        assert (failure.level, failure.code) == ("instance", "remote_incompatible")


async def test_achieved_on_reread_is_confirmed_even_without_retry_budget(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "v1")]]})

        async def same_content() -> None:
            await h.foreign.commit(P + "a.md", _doc_of("v2"), BaseHash(expected("v1")))

        made[0].before.append((_is_commit, same_content))
        result = await h.round(
            {"batches": [[doc_item("a.md", "v2")]]},
            policy=ConflictPolicy(commit="report"),
            options=RoundOptions(conflict_retries=0),
        )
        assert result.outcome == "succeeded", result.failures
        assert result.counts.unchanged == 1


async def test_a_handle_referenced_again_in_a_later_batch_is_reread(tmp_path: Path) -> None:
    """暂存按批释放：同一句柄在后续批中再被引用时重新读取，照常上传。"""
    data = b"\x89PNG later batch"
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [image_item("one.md", {"handle": "h"})],
                    [image_item("two.md", {"handle": "h"})],
                ],
                "blobs": {"h": base64.b64encode(data).decode()},
            }
        )
        assert result.outcome == "succeeded", result.failures
        assert await h.foreign.head(P + "two.md") == image_hash(data)
        assert [p["offset"] for p in h.requests("read_blob")] == [0, 0]


def test_local_defects_are_not_disguised_as_remote_failures() -> None:
    from dpe_sdk.run.round import _classify_remote

    with pytest.raises(KeyError):
        _classify_remote(KeyError("sha256:missing"), unknown=True)
    assert _classify_remote(httpx.ConnectError("down"), unknown=True).code == "remote_unavailable"


# ---------------------------------------------------------------------- 远端失败分层（读与写）


def _problem(status: int, code: str, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "application/problem+json", **headers},
            json={"type": "about:blank", "title": code, "status": status, "code": code},
            request=request,
        )

    return respond


def _is_heads(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/heads")


async def test_definite_write_error_is_an_item_failure(tmp_path: Path) -> None:
    """写入得到确定、不可重试的 DPE 错误：条目级、source remote、code 原样，本轮继续。"""
    engine = make_engine(authorizer=PrefixAuthorizer({"Bearer t": (P + "ok/",)}))
    async with harness(
        tmp_path, engine=engine, credential=StaticCredentialProvider("Bearer t")
    ) as h:
        result = await h.round(
            {"batches": [[doc_item("denied.md", "x"), doc_item("ok/b.md", "b")]]}
        )
    assert result.outcome == "partial"
    assert codes(result) == ["DPE_FORBIDDEN"]
    failure = result.failures[0]
    assert (failure.level, failure.source, failure.retryable) == ("item", "remote", False)
    assert failure.to_report()["file_uri"] == P + "denied.md"
    assert result.counts.created == 1


async def test_definite_read_error_fails_each_entry_of_the_call(tmp_path: Path) -> None:
    """读（batch_head）得到确定错误：不是可重试的轮级故障，归条目级、本轮继续（这里每个条目
    的读都被拒，逐条定位后各记一条）。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        heads = _problem(403, "DPE_FORBIDDEN")
        made[0].fail = lambda request: (
            heads(request) if _is_heads(request) or request.method == "HEAD" else None
        )
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
    assert result.outcome == "partial" and result.fatal is None
    assert codes(result) == ["DPE_FORBIDDEN", "DPE_FORBIDDEN"]
    assert [f.to_report()["file_uri"] for f in result.failures] == [P + "a.md", P + "b.md"]
    assert all(f.retryable is False for f in result.failures)


async def test_definite_reread_error_after_conflict_is_an_item_failure(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        hooks = made[0]
        await h.round({"batches": [[doc_item("a.md", "v1")]]})
        forbidden = _problem(403, "DPE_FORBIDDEN")

        async def rewrite_then_forbid_reads() -> None:
            await h.foreign.commit(P + "a.md", _doc_of("theirs"), BaseHash(expected("v1")))
            hooks.fail = lambda request: forbidden(request) if request.method == "HEAD" else None

        hooks.before.append((_is_commit, rewrite_then_forbid_reads))
        result = await h.round({"batches": [[doc_item("a.md", "v2")]]})
    assert result.outcome == "partial" and result.fatal is None
    assert codes(result) == ["DPE_FORBIDDEN"]
    assert result.failures[0].to_report()["file_uri"] == P + "a.md"


async def test_retryable_error_exhausted_aborts_the_round(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        unavailable = _problem(503, "DPE_UNAVAILABLE", **{"retry-after": "0"})
        made[0].fail = lambda request: unavailable(request) if _is_heads(request) else None
        result = await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
    assert result.outcome == "failed"
    assert result.fatal is not None
    assert (result.fatal.level, result.fatal.code, result.fatal.retryable) == (
        "round",
        "remote_unavailable",
        True,
    )


# ---------------------------------------------------------------------- 收尾保存失败（#137）


def _fail_state_writes(monkeypatch: pytest.MonkeyPatch, plan: list[bool]) -> Callable[[], None]:
    """按顺序决定每一次 state.json 写入是否失败（``plan`` 用尽后不再失败）；返回重置函数。"""
    real = state_module._write_atomic
    queue = list(plan)

    def flaky(path: Path, data: Any) -> None:
        if path.name == "state.json" and queue and queue.pop(0):
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    return queue.clear


async def test_final_save_failure_fails_the_round_even_if_resave_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收尾那次 save 失败（写入已生效）、补存成功：本轮仍 failed、游标不推进（推送器整批报错，
    拿不到逐条结果），磁盘上的状态已与内存一致——意图清空、同步状态含已确认的写入。"""
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        # 本轮 state.json 写入顺序：写前持久化意图（成功）→ 推送器收尾（失败）→ 补存（成功）
        _fail_state_writes(monkeypatch, [False, True])
        result = await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c2"})
        assert result.outcome == "failed" and not result.cursor_persisted
        assert codes(result) == ["state_unavailable"]  # 补存成功：不另记
        assert result.fatal is not None and result.fatal.code == "state_unavailable"
        assert result.cursor_held_since == result.started_at
        assert await h.foreign.head(P + "a.md") == expected("A")  # 写入已生效
        reloaded = h.dir.open_instance("inst", "identity-1")
        assert reloaded.cursor == "c1"
        assert reloaded.held_since == result.started_at  # 收尾记录照常落盘
        snapshot = reloaded.load(REMOTE)
        assert snapshot is not None
        assert snapshot.intents == {} and snapshot.synced == {P + "a.md": expected("A")}


async def test_failed_resave_is_recorded_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        reset = _fail_state_writes(monkeypatch, [False] + [True] * 20)
        result = await h.round({"batches": [[doc_item("a.md", "A")]], "cursor": "c2"})
        reset()
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "state_unavailable"
        assert result.failures[0] is result.fatal  # 终止失败在前，补存失败单独一条在后
        assert "补存" in result.failures[1].message and result.failures[1].code == (
            "state_unavailable"
        )
        assert result.cursor_held_since == result.started_at  # 收尾写入也失败：回退到本轮起点
        assert h.dir.open_instance("inst", "identity-1").cursor == "c1"


async def test_continuation_out_of_prefix_is_reported_once(tmp_path: Path) -> None:
    """可识别 file_uri 的分段文档越界：整篇只记一条，续段不重复上报。"""
    outside = "dpe://other/x.md"
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        {**doc_item("x", "1", continued=True), "file_uri": outside},
                        {
                            "kind": "document",
                            "file_uri": outside,
                            "pages": [{"elements": _text("2")}],
                        },
                    ]
                ]
            }
        )
    assert codes(result) == ["out_of_prefix"]


async def test_capability_errors_are_round_level_not_per_entry(tmp_path: Path) -> None:
    """能力获取与条目无关：确定错误是一条轮级失败（code 原样、不可重试），不拆成条目失败。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden = _problem(403, "DPE_FORBIDDEN")
        made[0].fail = lambda request: (
            forbidden(request) if request.url.path.endswith("/capabilities") else None
        )
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
    assert result.outcome == "failed" and result.counts.failed == 0
    assert result.fatal is not None
    assert (result.fatal.level, result.fatal.code, result.fatal.retryable) == (
        "round",
        "DPE_FORBIDDEN",
        False,
    )


async def test_read_error_fails_its_wave_and_later_waves_continue(tmp_path: Path) -> None:
    """一波的 batch_head 确定错误只让该波条目失败（move 带两侧 URI），后续波次照常落地。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("b.md", "B")]]})
        forbidden = _problem(403, "DPE_FORBIDDEN")
        left = {"n": 2}  # 第一波的 commit 与 move 各读一次 heads（连同其逐条读）被拒

        def first_wave_heads(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                if left["n"]:
                    left["n"] -= 1
                    return forbidden(request)
                left["n"] = -1  # 第二波的批量读：此后不再拒绝
                return None
            if request.method == "HEAD" and left["n"] >= 0:
                return forbidden(request)
            return None

        made[0].fail = first_wave_heads
        result = await h.round(
            {
                "batches": [
                    [
                        doc_item("a.md", "v1"),
                        {"kind": "move", "from_uri": P + "b.md", "to_uri": P + "c.md"},
                        doc_item("a.md", "v2"),  # 同 URI：开第二波
                    ]
                ]
            }
        )
        assert result.outcome == "partial"
        reports = [f.to_report() for f in result.failures]
        assert [r["code"] for r in reports] == ["DPE_FORBIDDEN", "DPE_FORBIDDEN"]
        assert reports[0]["file_uri"] == P + "a.md"
        assert (reports[1]["from_uri"], reports[1]["to_uri"]) == (P + "b.md", P + "c.md")
        assert result.counts.created == 1
        assert await h.foreign.head(P + "a.md") == expected("v2")
        assert await h.foreign.head(P + "b.md") == expected("B")  # move 未执行


@pytest.mark.parametrize(
    "segments",
    [
        # 首段信封违例（未定义字段），file_uri 可识别、continued 为 true：后续续段被吞掉
        [{**doc_item("x.md", "1", continued=True), "extra": 1}, "tail"],
        # 首段 continued 非布尔：保守视为未闭合，后续续段被吞掉
        [{**doc_item("x.md", "1"), "continued": "yes"}, "tail"],
        # 已打开的文档收到信封违例的续段
        [doc_item("x.md", "1", continued=True), "bad-tail"],
    ],
)
async def test_segment_envelope_violations_are_reported_once(
    tmp_path: Path, segments: list[Any]
) -> None:
    tail = {"kind": "document", "file_uri": P + "x.md", "pages": [{"elements": _text("2")}]}
    items = [
        {**tail}
        if segment == "tail"
        else {**tail, "unknown": True}
        if segment == "bad-tail"
        else segment
        for segment in segments
    ]
    async with harness(tmp_path) as h:
        result = await h.round({"batches": [items]})
        assert codes(result) == ["DPE_VALIDATION"]
        assert await h.foreign.head(P + "x.md") is None


async def test_failed_open_segment_does_not_swallow_a_later_document_or_other_uri(
    tmp_path: Path,
) -> None:
    """失败后仍打开的分段文档：之后同 URI 带 document 的新文档照常落地；换 URI 时不重复上报。"""
    async with harness(tmp_path) as h:
        result = await h.round(
            {
                "batches": [
                    [
                        {**doc_item("x.md", "bad"), "continued": "no"},  # 非法 continued
                        doc_item("x.md", "again"),  # 同 URI 的新文档（后写覆盖）
                        {**doc_item("y.md", "bad"), "continued": "no"},
                        doc_item("z.md", "Z"),  # 换 URI：y 不重复上报
                    ]
                ]
            }
        )
        assert codes(result) == ["DPE_VALIDATION", "DPE_VALIDATION"]
        assert await h.foreign.head(P + "x.md") == expected("again")
        assert await h.foreign.head(P + "z.md") == expected("Z")
        assert await h.foreign.head(P + "y.md") is None


def _reject_reads_of(
    forbidden_uris: set[str], respond: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response | None]:
    """服务端拒绝读这些 URI：含其中任一 URI 的批量读整体被拒，单篇读该 URI 同样被拒。"""

    def reject(request: httpx.Request) -> httpx.Response | None:
        if _is_heads(request) and any(u.encode() in request.content for u in forbidden_uris):
            return respond(request)
        if request.method == "HEAD" and unquote(str(request.url)).split("uri=")[-1] in (
            forbidden_uris
        ):
            return respond(request)
        return None

    return reject


async def test_batch_read_rejection_is_localized_to_the_offending_entry(tmp_path: Path) -> None:
    """服务端因其中一个 URI 拒绝整个 batch_head：推送器逐条 head 定位（#145），只有该条目失败，
    其余照常落地；运行器把它当普通条目失败入账。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        made[0].fail = _reject_reads_of({P + "bad.md"}, _problem(403, "DPE_FORBIDDEN"))
        result = await h.round(
            {
                "batches": [
                    [doc_item("good.md", "G"), doc_item("bad.md", "B"), doc_item("ok.md", "O")]
                ]
            }
        )
        rejected = [
            fields for event, _, fields in h.events if event == "remote.batch_read_rejected"
        ]
    assert result.outcome == "partial"
    assert codes(result) == ["DPE_FORBIDDEN"]
    assert result.failures[0].to_report()["file_uri"] == P + "bad.md"
    assert result.failures[0].to_report()["source"] == "remote"
    assert result.counts.created == 2
    # 推送器的 warning 转成事件：只带码与计数，不带远端文本（§4.3 不外泄）
    assert rejected == [{"code": "DPE_FORBIDDEN", "entries": 3, "denied": 1}]


async def test_delete_and_move_read_rejection_is_localized(tmp_path: Path) -> None:
    """delete 与 move 同样逐条定位；move 的任一侧被拒都归到该 move（带两侧 URI）。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item(n, n) for n in ("d1.md", "d2.md", "m1.md", "m2.md")]]})
        made[0].fail = _reject_reads_of(
            {P + "d2.md", P + "secret.md"}, _problem(403, "DPE_FORBIDDEN")
        )
        result = await h.round(
            {
                "batches": [
                    [
                        {"kind": "delete", "file_uri": P + "d1.md"},
                        {"kind": "delete", "file_uri": P + "d2.md"},
                        {"kind": "move", "from_uri": P + "m1.md", "to_uri": P + "secret.md"},
                        {"kind": "move", "from_uri": P + "m2.md", "to_uri": P + "n2.md"},
                    ]
                ]
            }
        )
        reports = [f.to_report() for f in result.failures]
        assert [r.get("file_uri") or r.get("from_uri") for r in reports] == [
            P + "d2.md",
            P + "m1.md",
        ]
        assert reports[1]["to_uri"] == P + "secret.md"
        assert (result.counts.deleted, result.counts.moved) == (1, 1)
        assert await h.foreign.head(P + "d1.md") is None
        assert await h.foreign.head(P + "n2.md") == expected("m2.md")


async def test_batch_rejection_with_every_single_read_passing_still_lands(tmp_path: Path) -> None:
    """批量读被拒但逐条读都通过（拒绝与具体 URI 无关）：按逐条读到的值照常落地（connector §7.1），
    并发出 remote.batch_read_rejected 事件（只带码与计数），使这类服务端可被察觉。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden = _problem(403, "DPE_FORBIDDEN")
        made[0].fail = lambda request: forbidden(request) if _is_heads(request) else None
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
        events = [fields for event, _, fields in h.events if event == "remote.batch_read_rejected"]
    assert result.outcome == "succeeded" and codes(result) == []
    assert result.counts.created == 2
    assert events == [{"code": "DPE_FORBIDDEN", "entries": 2, "denied": 0}]


async def test_network_failure_while_localizing_aborts_without_writes(tmp_path: Path) -> None:
    """定位期间网络失败：本轮作废（轮级），此时尚未发出任何写入，不会丢失已落地的结果。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden = _problem(403, "DPE_FORBIDDEN")

        def reject_then_cut(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                return forbidden(request)
            if request.method == "HEAD":
                raise httpx.ConnectError("断网", request=request)
            return None

        made[0].fail = reject_then_cut
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"
        assert result.counts.created == 0
        assert await h.foreign.head(P + "a.md") is None


async def test_located_entries_are_not_read_again(tmp_path: Path) -> None:
    """定位后其余条目用逐条读到的值直接判定，不再发批量读：即便服务端拒绝一切多 URI 的批量读，
    也只有真正读被拒的条目失败。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden = _problem(403, "DPE_FORBIDDEN")
        heads_seen = {"n": 0}

        def reject(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                heads_seen["n"] += 1
                if b"," in request.content:  # 多于一个 URI 的批量读一律被拒
                    return forbidden(request)
            if request.method == "HEAD" and "bad.md" in unquote(str(request.url)):
                return forbidden(request)
            return None

        made[0].fail = reject
        result = await h.round(
            {"batches": [[doc_item("g1.md", "1"), doc_item("bad.md", "B"), doc_item("g2.md", "2")]]}
        )
    assert codes(result) == ["DPE_FORBIDDEN"]
    assert result.failures[0].to_report()["file_uri"] == P + "bad.md"
    assert result.counts.created == 2
    assert heads_seen["n"] == 1  # 只有最初那一次批量读


async def test_read_rejection_during_conflict_resubmission_is_localized(tmp_path: Path) -> None:
    """冲突重提那一轮（带 expected_base）里批量读被拒：子集的 expected_base 过滤正确，
    读被拒的条目失败，其余以重读值重提成功。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("a.md", "v1"), doc_item("b.md", "v1")]]})
        for name in ("a.md", "b.md"):
            await h.foreign.commit(P + name, _doc_of("theirs"), BaseHash(expected("v1")))
        forbidden = _problem(403, "DPE_FORBIDDEN")
        heads_seen = {"n": 0}

        def reject_b_after_first_read(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                heads_seen["n"] += 1
                if heads_seen["n"] > 1 and (P + "b.md").encode() in request.content:
                    return forbidden(request)
            if (
                request.method == "HEAD"
                and heads_seen["n"] > 1
                and unquote(str(request.url)).endswith(P + "b.md")
            ):
                return forbidden(request)
            return None

        made[0].fail = reject_b_after_first_read
        result = await h.round({"batches": [[doc_item("a.md", "v2"), doc_item("b.md", "v2")]]})
        assert codes(result) == ["DPE_FORBIDDEN"]
        assert result.failures[0].to_report()["file_uri"] == P + "b.md"
        assert result.counts.updated == 1
        assert await h.foreign.head(P + "a.md") == expected("v2")


async def test_mixed_probe_outcomes_abort_the_round_without_writes(tmp_path: Path) -> None:
    """定位时一部分读被拒、一部分断网：读被拒的结论不被当作异常，本轮按网络失败作废、不写入。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden = _problem(403, "DPE_FORBIDDEN")

        def mixed(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                return forbidden(request)
            if request.method == "HEAD":
                if "bad.md" in unquote(str(request.url)):
                    return forbidden(request)
                raise httpx.ConnectError("断网", request=request)
            return None

        made[0].fail = mixed
        result = await h.round({"batches": [[doc_item("bad.md", "B"), doc_item("ok.md", "O")]]})
        assert result.outcome == "failed"
        assert result.fatal is not None and result.fatal.code == "remote_unavailable"
        assert codes(result) == ["remote_unavailable"]  # 读被拒的结论未提前入账
        assert result.counts.created == 0


# ---------------------------------------------------------------------- 响应不是 DPE 报文（#137）


def _bare(status: int, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    """无 problem 体、无 DPE-Error-Code 的响应（网关、代理、错配的 remote）。"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, text="<html>gateway</html>", request=request)

    return respond


@pytest.mark.parametrize(
    ("target", "respond", "retryable"),
    [
        ("capabilities", _bare(404), False),
        ("heads", _bare(302, location="http://elsewhere.test/"), False),  # 不跟随重定向
        ("heads", _bare(502), True),
    ],
)
async def test_non_dpe_responses_are_round_level_protocol_errors(
    tmp_path: Path,
    target: str,
    respond: Callable[[httpx.Request], httpx.Response],
    retryable: bool,
) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        made[0].fail = lambda request: (
            respond(request) if request.url.path.endswith(f"/{target}") else None
        )
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
    assert result.outcome == "failed" and result.counts.failed == 0  # 不落到条目级
    assert result.fatal is not None
    assert (result.fatal.level, result.fatal.code, result.fatal.source) == (
        "round",
        "remote_protocol_error",
        "remote",
    )
    assert result.fatal.retryable is retryable
    assert "HTTP" in result.fatal.message


async def test_non_dpe_write_response_keeps_the_intent(tmp_path: Path) -> None:
    """写请求得到 2xx 却不是 DPE 报文：结果未知，意图保留；本轮 remote_protocol_error、不可重试。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        html = _bare(200, **{"content-type": "text/html"})
        made[0].fail = lambda request: html(request) if _is_commit(request) else None
        result = await h.round({"batches": [[doc_item("a.md", "A")]]})
        assert result.fatal is not None and result.fatal.code == "remote_protocol_error"
        assert result.fatal.retryable is False
        assert h.store is not None
        snapshot = h.store.load(REMOTE)
        assert snapshot is not None and snapshot.intents == {P + "a.md": expected("A")}


async def test_non_dpe_response_while_localizing_is_round_level(tmp_path: Path) -> None:
    """批量读被确定拒绝后逐条定位，单篇 head 得到裸 404：轮级 remote_protocol_error，不落到条目。"""
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        forbidden, bare = _problem(403, "DPE_FORBIDDEN"), _bare(404)

        def respond(request: httpx.Request) -> httpx.Response | None:
            if _is_heads(request):
                return forbidden(request)
            if request.method == "HEAD":
                return bare(request)
            return None

        made[0].fail = respond
        result = await h.round({"batches": [[doc_item("a.md", "A"), doc_item("b.md", "B")]]})
    assert result.fatal is not None and result.fatal.code == "remote_protocol_error"
    assert result.counts.failed == 0 and result.counts.created == 0


async def test_bare_5xx_on_a_write_is_retryable_and_keeps_the_intent(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        gateway = _bare(502)
        made[0].fail = lambda request: gateway(request) if _is_commit(request) else None
        result = await h.round({"batches": [[doc_item("a.md", "A")]]})
        assert result.fatal is not None and result.fatal.code == "remote_protocol_error"
        assert result.fatal.retryable is True and "502" in result.fatal.message
        assert h.store is not None
        snapshot = h.store.load(REMOTE)
        assert snapshot is not None and snapshot.intents == {P + "a.md": expected("A")}


async def test_bare_response_on_a_delete_is_round_level(tmp_path: Path) -> None:
    made, wrap = _hooks()
    async with harness(tmp_path, wrap) as h:
        await h.round({"batches": [[doc_item("d.md", "D")]]})
        bare = _bare(404)
        made[0].fail = lambda request: bare(request) if _is_delete(request) else None
        result = await h.round({"batches": [[{"kind": "delete", "file_uri": P + "d.md"}]]})
    assert result.fatal is not None and result.fatal.code == "remote_protocol_error"
    assert result.fatal.retryable is False and result.counts.failed == 0


async def test_held_record_write_failure_does_not_change_a_partial_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7.2：游标扣住记录写入失败不影响轮结果、不计入 failures，只记日志。"""
    real = state_module._write_atomic
    failing = {"on": False}

    def flaky(path: Path, data: Any) -> None:
        if failing["on"] and path.name == "state.json" and data.get("held_since"):
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        await h.round({"batches": [[]], "cursor": "c1"})
        failing["on"] = True
        error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
        result = await h.round({"batches": [[error]], "cursor": "c2"})
    assert result.outcome == "partial" and codes(result) == ["internal"]
    assert result.cursor_held_since == result.started_at
    assert [e for e, _, _ in h.events].count("state.held_write_failed") == 1


async def test_clearing_the_held_record_failing_keeps_a_succeeded_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7.2：succeeded 轮在游标不变时（不支持游标），收尾只是清除扣住记录；写失败不影响轮结果。"""
    real = state_module._write_atomic
    failing = {"on": False}

    def flaky(path: Path, data: Any) -> None:
        if failing["on"] and path.name == "state.json" and data.get("held_since") is None:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
        held = await h.round({"batches": [[error]], "supports_cursor": False})
        assert held.outcome == "partial"
        failing["on"] = True
        result = await h.round({"batches": [[]], "supports_cursor": False})
    assert result.outcome == "succeeded" and result.failures == []
    assert result.cursor_held_since is None
    assert [e for e, _, _ in h.events].count("state.held_write_failed") == 1


async def test_unchanged_round_record_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持续被扣住的轮：记录不变即不重写（磁盘只读时也不产生误导性的告警）。"""
    real = state_module._write_atomic
    failing = {"on": False}

    def flaky(path: Path, data: Any) -> None:
        if failing["on"] and path.name == "state.json":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
        first = await h.round({"batches": [[error]], "supports_cursor": False})
        failing["on"] = True
        again = await h.round({"batches": [[error]], "supports_cursor": False})
    assert again.outcome == "partial" and again.cursor_held_since == first.started_at
    assert "state.held_write_failed" not in [e for e, _, _ in h.events]


async def test_clearing_a_stored_cursor_that_fails_to_persist_fails_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存游标被清为 None（插件改为不支持游标）也是游标的变化：写不进去即本轮 failed、游标
    未落盘（§6.8 fail-closed，保守口径）；报告的记录按失败轮取本轮起点，不发扣住记录告警。"""
    real = state_module._write_atomic
    failing = {"on": False}

    def flaky(path: Path, data: Any) -> None:
        if failing["on"] and path.name == "state.json":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    async with harness(tmp_path) as h:
        assert (await h.round({"batches": [[]], "cursor": "c1"})).outcome == "succeeded"
        failing["on"] = True
        result = await h.round({"batches": [[]], "supports_cursor": False})
    assert result.outcome == "failed" and codes(result) == ["state_unavailable"]
    assert result.cursor_held_since == result.started_at
    assert "state.held_write_failed" not in [e for e, _, _ in h.events]
