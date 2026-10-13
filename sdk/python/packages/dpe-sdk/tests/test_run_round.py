"""一轮编排（connector 契约 §6.4–§6.8、§7.1–§7.3）：脚本化插件（真实子进程）× 进程内参考服务端。

插件行为由实例配置给出（``run_scan_plugin.py``）；协议路径经真实参考服务端
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

import dpe_hash
import httpx
import pytest
from client_helpers import PREFIX, REMOTE, in_process_client
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
from dpe_sdk.testing import create_app
from dpe_sdk.transport import AsyncDpeClient
from engine_helpers import make_engine

pytestmark = pytest.mark.anyio

PLUGIN = Path(__file__).with_name("run_scan_plugin.py")
MANIFEST = parse_manifest(
    {
        "manifest_version": 1,
        "name": "scan",
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
            return await run_round(
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
        finally:
            await host.close()

    def finished_level(self) -> str:
        return next(level for event, level, _ in self.events if event == "round.finished")


@asynccontextmanager
async def harness(
    tmp_path: Path,
    transport: Callable[[httpx.AsyncBaseTransport], httpx.AsyncBaseTransport] | None = None,
) -> AsyncIterator[Harness]:
    app = create_app(make_engine(), prefix=PREFIX)
    inner: httpx.AsyncBaseTransport = httpx.ASGITransport(app=app)
    async with (
        in_process_client(transport=transport(inner) if transport else inner) as (client, _),
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
