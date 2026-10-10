"""端到端：真实子进程 + 运行器的插件宿主（dpe-run 的 ``PluginHost``）跑全量枚举。

验收（Issue #96）：对夹具仓库全量枚举，页的划分与顺序、元素内容与 metadata 符合映射规则；
同一仓库状态重复枚举 ``doc_hash`` 一致；装不下的文档按页分段且拼回一致。这里用运行器自己的
宿主与清单校验驱动插件，与 dpe-run 启动插件的路径一致（启动子进程、握手核对身份、``scan``
拉批、``shutdown`` 收尾）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import History, ReposRoot
from dpe_sdk.run import (
    InstanceFailure,
    PluginHost,
    RpcError,
    load_definition,
    load_manifest,
)
from helpers import command_for, requires_git

from dpe_git_connector import PLUGIN_NAME, __version__

pytestmark = requires_git

PREFIX = "git://t/"
#: 端到端测试用的消息上限（远大于夹具，且不小于 remote_limits 的 max_payload_bytes）
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def manifest_path() -> Path:
    return Path(__file__).resolve().parents[1] / "dpe-connector.json"


def write_definition(tmp_path: Path, config: Mapping[str, Any]) -> Path:
    """写一份实例定义（§4.4）；清单用本包随附的那一份（名/版本与插件一致）。"""
    definition = {
        "definition_version": 1,
        "id": "fixture",
        "remote": {"url": "https://dpe.example.com/remotes/x"},
        "uri_prefix": PREFIX,
        "plugin": {"manifest": str(manifest_path()), "command": command_for("dpe-git-connector")},
        "config": dict(config),
    }
    path = tmp_path / "instance.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    return path


async def handshake(
    host: PluginHost, definition: Path, *, remote_limits: Mapping[str, int] | None = None
) -> None:
    """按运行器的方式启动插件并完成握手（清单校验 + 身份核对）。"""
    spec = load_definition(definition)
    await host.start(spec.plugin.command, {})
    manifest = load_manifest(spec.base_dir / spec.plugin.manifest)
    # §6.2：消息上限 MUST NOT 小于远端 max_payload_bytes；小上限的分段测试要一并调低
    limits = remote_limits
    if limits is None and host.max_message_bytes >= MAX_MESSAGE_BYTES:
        limits = {"max_payload_bytes": MAX_MESSAGE_BYTES}
    result = await host.initialize(
        manifest=manifest,
        uri_prefix=spec.uri_prefix,
        config=spec.config,
        remote_limits=limits,
    )
    assert result.plugin.name == PLUGIN_NAME
    assert result.protocol_version == "dpe-connector/1"
    assert result.capabilities.supports_cursor is False


async def collect(
    host: PluginHost, definition: Path, *, remote_limits: Mapping[str, int] | None = None
) -> list[dict[str, Any]]:
    """握手后按批 scan 到结束批，收集全部产出项（含分段）。"""
    await handshake(host, definition, remote_limits=remote_limits)
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"next": token} if token is not None else {"cursor": None}
        result = await host.request("scan", params, timeout=60.0)
        items.extend(result["items"])
        token = result.get("next")
        if token is None:
            # 结束批：本插件不支持游标（契约 §6.8）
            assert result["cursor"] is None
            assert result["incremental"] is False
            break
    return items


async def enumerate_all(
    definition: Path,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
    remote_limits: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    async with PluginHost(max_message_bytes=max_message_bytes) as host:
        return await collect(host, definition, remote_limits=remote_limits)


def documents_from(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """产出项 → ``file_uri → 整篇文档``（按 §6.5 拼回分段；断言分段连续性）。"""
    documents: dict[str, dict[str, Any]] = {}
    open_document: dict[str, Any] | None = None
    for item in items:
        if item["kind"] != "document":
            continue
        if open_document is not None:
            assert item["file_uri"] == open_document["file_uri"]
        if "document" in item:
            assert open_document is None
            open_document = {
                "kind": "document",
                "file_uri": item["file_uri"],
                "document": item["document"],
                "pages": list(item["pages"]),
            }
        else:
            assert open_document is not None
            open_document["pages"].extend(item["pages"])
        if not item.get("continued", False):
            documents[open_document["file_uri"]] = open_document
            open_document = None
    assert open_document is None  # 轮结束时不得有未闭合的分段文档
    return documents


def doc_hashes(documents: Mapping[str, dict[str, Any]]) -> dict[str, str]:
    """整篇文档 → ``file_uri → doc_hash``（与契约 §8.3 的 F(S) 同口径，hash 由运行器算）。"""
    from dpe_hash import ExpandedDocument, document_hashes

    out: dict[str, str] = {}
    for file_uri, item in documents.items():
        expanded: dict[str, Any] = {
            "file_type": item["document"]["file_type"],
            "pages": item["pages"],
        }
        if "title" in item["document"]:
            expanded["title"] = item["document"]["title"]
        out[file_uri] = document_hashes(cast(ExpandedDocument, expanded))["doc_hash"]
    return out


# --------------------------------------------------------------------------- 全量枚举


def test_full_enumeration_over_plugin_host(tmp_path: Path, history: History) -> None:
    definition = write_definition(tmp_path, {"repo": str(history.path), "branches": ["dev"]})
    items = asyncio.run(enumerate_all(definition))
    documents = documents_from(items)
    assert set(documents) == {"git://t/repo"}
    document = documents["git://t/repo"]
    assert document["document"] == {"file_type": "git_repo"}
    assert [page["page_metadata"] for page in document["pages"]] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
        {"ref": "dev"},
    ]
    # 合并归月：f1（2025-11 提交）与 f2（2026-03 提交）都随合并提交 m1 归入 2026-02
    february = document["pages"][1]
    assert [element["metadata"]["commit"] for element in february["elements"]] == [
        history.sha("f1"),
        history.sha("f2"),
        history.sha("m1"),
    ]
    # 元素内容与 metadata 如实映射（说明原文 + 作者 ident + RFC 3339 UTC 时间）
    first = document["pages"][0]["elements"][0]
    assert first["category"] == "NarrativeText"
    assert first["text"] == "根提交：初始化仓库\n"
    assert first["metadata"] == {
        "commit": history.sha("c1"),
        "author": "Alice <alice@example.invalid>",
        "authored_at": "2026-01-05T00:30:00Z",
        "committed_at": "2026-01-05T00:30:00Z",
    }


def test_root_mode_over_plugin_host(tmp_path: Path, repos_root: ReposRoot) -> None:
    definition = write_definition(tmp_path, {"root": str(repos_root.root)})
    items = asyncio.run(enumerate_all(definition))
    kinds = [(item["kind"], item["file_uri"]) for item in items]
    assert kinds == [
        ("document", "git://t/alpha"),
        ("error", "git://t/broken"),
        ("document", "git://t/empty_repo"),
        ("document", "git://t/nested/beta"),
    ]


def test_repeat_run_same_state_is_stable(tmp_path: Path, history: History) -> None:
    """验收判据：同一仓库状态重复运行，doc_hash 完全稳定（三次枚举，含三次独立握手）。"""
    definition = write_definition(
        tmp_path, {"repo": str(history.path), "branches": ["dev", "feature/merged"]}
    )
    runs = [doc_hashes(documents_from(asyncio.run(enumerate_all(definition)))) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert set(runs[0]) == {"git://t/repo"}
    assert all(value.startswith("dpe1:") for value in runs[0].values())


def test_since_bounds_history(tmp_path: Path, history: History) -> None:
    definition = write_definition(tmp_path, {"repo": str(history.path), "since": history.sha("f2")})
    documents = documents_from(asyncio.run(enumerate_all(definition)))
    assert [page["page_metadata"] for page in documents["git://t/repo"]["pages"]] == [
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
    ]


def test_segments_over_plugin_host(tmp_path: Path, history: History) -> None:
    """小消息上限驱动真实分段：拼回与整篇一致，doc_hash 不因分段而变。"""
    config = {"repo": str(history.path), "branches": ["dev"]}
    definition = write_definition(tmp_path, config)
    whole = asyncio.run(enumerate_all(definition))
    segmented = asyncio.run(
        enumerate_all(definition, max_message_bytes=2500, remote_limits={"max_payload_bytes": 1024})
    )
    assert len(segmented) > len(whole)  # 确实按页分段了
    assert doc_hashes(documents_from(segmented)) == doc_hashes(documents_from(whole))


def test_unknown_next_is_rejected(tmp_path: Path, history: History) -> None:
    """未知或过期的 next → -32602，运行器以原 cursor 重开（契约 §6.4）。"""
    definition = write_definition(tmp_path, {"repo": str(history.path)})

    async def run() -> None:
        async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
            await handshake(host, definition)
            with pytest.raises(RpcError) as excinfo:
                await host.request("scan", {"next": "99999"}, timeout=60.0)
            assert excinfo.value.code == -32602

    asyncio.run(run())


# --------------------------------------------------------------------------- 握手失败路径


def _initialize_failure(definition: Path) -> InstanceFailure | None:
    async def run() -> InstanceFailure | None:
        spec = load_definition(definition)
        async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
            await host.start(spec.plugin.command, {})
            manifest = load_manifest(spec.base_dir / spec.plugin.manifest)
            try:
                await host.initialize(
                    manifest=manifest, uri_prefix=spec.uri_prefix, config=spec.config
                )
            except InstanceFailure as exc:
                return exc
        return None

    return asyncio.run(run())


def test_initialize_rejects_missing_repo(tmp_path: Path) -> None:
    definition = write_definition(tmp_path, {"repo": str(tmp_path / "nope")})
    failure = _initialize_failure(definition)
    assert failure is not None
    assert failure.code == "invalid_config"
    assert failure.source == "plugin"


def test_initialize_rejects_bad_since(tmp_path: Path, history: History) -> None:
    definition = write_definition(
        tmp_path,
        {"repo": str(history.path), "since": history.sha("d1")},  # 不在默认分支历史中
    )
    failure = _initialize_failure(definition)
    assert failure is not None
    assert failure.code == "invalid_config"


def test_initialize_rejects_root_with_since(tmp_path: Path, repos_root: ReposRoot) -> None:
    definition = write_definition(tmp_path, {"root": str(repos_root.root), "since": "HEAD"})
    failure = _initialize_failure(definition)
    assert failure is not None
    assert failure.code == "invalid_config"


def test_shutdown_exits_cleanly(tmp_path: Path, history: History) -> None:
    """响应 shutdown 后以退出码 0 退出（契约 §6.3、§8.1 第 4 条）。"""
    definition = write_definition(tmp_path, {"repo": str(history.path)})

    async def run() -> int | None:
        async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
            await handshake(host, definition)
        return host.returncode

    assert asyncio.run(run()) == 0


def test_stdin_eof_exits(tmp_path: Path, history: History) -> None:
    """读到 stdin EOF 尽快退出（契约 §6.3：运行器崩溃后不残留孤儿进程）。"""
    definition = write_definition(tmp_path, {"repo": str(history.path)})

    async def run() -> int | None:
        host = PluginHost(max_message_bytes=MAX_MESSAGE_BYTES)
        await host.start(load_definition(definition).plugin.command, {})
        await host.close(graceful=False)  # 不发 shutdown，直接关 stdin
        return host.returncode

    assert asyncio.run(run()) == 0


# --------------------------------------------------------------------------- 清单与中立性


def test_plugin_identity_matches_manifest() -> None:
    """插件自报的身份必须与随分销清单一致（契约 §4.1）；清单本身经 dpe-run 校验器验证。"""
    manifest = load_manifest(manifest_path())
    assert manifest.name == PLUGIN_NAME
    assert manifest.version == __version__
    assert manifest.protocol_versions == ["dpe-connector/1"]
    assert manifest.secrets == ()


def test_manifest_config_schema_is_closed_and_exclusive() -> None:
    """config_schema 是封闭 schema：repo / root 必须且只能给出一个；未声明的成员被拒绝。"""
    from dpe_sdk.run import validate_config

    manifest = load_manifest(manifest_path())
    validate_config(manifest, {"repo": "/tmp/x"})
    validate_config(manifest, {"root": "/tmp/x"})
    validate_config(manifest, {"repo": "/tmp/x", "branches": ["dev"], "since": "HEAD"})
    for bad in ({}, {"repo": "/tmp/x", "root": "/tmp/y"}, {"repo": "/tmp/x", "subdir": "docs"}):
        with pytest.raises(InstanceFailure) as excinfo:
            validate_config(manifest, bad)
        assert excinfo.value.code == "config_schema_violation"


def test_no_server_private_concepts() -> None:
    """验收：检索不到服务端私有概念——源码、清单与文档全部检查。"""
    root = Path(__file__).resolve().parents[1]
    forbidden = ("tfrs", "robot", "tenant", "vnd.", "x-tfrs")
    targets = list((root / "src").rglob("*.py"))
    targets += [root / "dpe-connector.json", root / "README.md"]
    for path in targets:
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{path} 含服务端私有概念 {word!r}"
