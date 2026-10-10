"""端到端：真实子进程 + 运行器的插件宿主（dpe-run 的 ``PluginHost``）跑全量枚举。

验收（Issue #65）：对夹具仓库全量枚举，产出的文档经 dpe-hash 算出稳定的 ``doc_hash``——
同一提交重复运行结果一致。这里用运行器自己的宿主与清单校验驱动插件，与 dpe-run 启动插件的
路径一致（启动子进程、握手核对身份、``scan`` 拉批、``shutdown`` 收尾）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from dpe_sdk.run import (
    InstanceFailure,
    PluginHost,
    RpcError,
    load_definition,
    load_manifest,
)
from helpers import command_for, config_for, init_repo, requires_git, run_git

from dpe_git_connector import PLUGIN_NAME, __version__

pytestmark = requires_git

PREFIX = "git://t/"
#: 产出 ``document`` 的夹具文件，按 file_uri 的码点序。注意这是**百分号编码后**的字符串序
#: （大写字母在前），与文件名序不同。不在表内的：img.png / readme.html（映射表外，不产出）、
#: bad.md（映射表内但内容不是 UTF-8 → 条目级 error）。
EXPECTED_URIS = [
    "git://t/Makefile",
    "git://t/config.json",
    "git://t/data.csv",
    "git://t/docs/%E8%AF%B4%E6%98%8E.md",
    "git://t/list.md",
    "git://t/metrics.ndjson",
    "git://t/notes.txt",
    "git://t/script.py",
]

#: 端到端测试用的消息上限（远大于夹具，且不小于 remote_limits 的 max_payload_bytes）
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def manifest_path() -> Path:
    return Path(__file__).resolve().parents[1] / "dpe-connector.json"


def write_definition(
    tmp_path: Path, repo: Path, overrides: Mapping[str, Any] | None = None
) -> Path:
    """写一份实例定义（§4.4）；清单用本包随附的那一份（名/版本与插件一致）。"""
    config: dict[str, Any] = config_for(repo)
    if overrides:
        config.update(overrides)
    definition = {
        "definition_version": 1,
        "id": "fixture",
        "remote": {"url": "https://dpe.example.com/remotes/x"},
        "uri_prefix": PREFIX,
        "plugin": {"manifest": str(manifest_path()), "command": command_for("dpe-git-connector")},
        "config": config,
    }
    path = tmp_path / "instance.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    return path


async def handshake(host: PluginHost, definition: Path) -> None:
    """按运行器的方式启动插件并完成握手（清单校验 + 身份核对）。"""
    spec = load_definition(definition)
    await host.start(spec.plugin.command, {})
    manifest = load_manifest(spec.base_dir / spec.plugin.manifest)
    result = await host.initialize(
        manifest=manifest,
        uri_prefix=spec.uri_prefix,
        config=spec.config,
        remote_limits={"max_payload_bytes": 8 * 1024 * 1024},
    )
    assert result.plugin.name == PLUGIN_NAME
    assert result.protocol_version == "dpe-connector/1"
    assert result.capabilities.supports_cursor is False


async def collect(host: PluginHost, definition: Path) -> list[dict[str, Any]]:
    """握手后按批 scan 到结束批，收集全部产出项。"""
    await handshake(host, definition)
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


async def enumerate_all(definition: Path) -> list[dict[str, Any]]:
    async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
        return await collect(host, definition)


def doc_hashes_from(items: list[dict[str, Any]]) -> dict[str, str]:
    """产出项 → ``file_uri → doc_hash``（与契约 §8.3 的 F(S) 同口径，hash 由运行器算）。"""
    from dpe_hash import ExpandedDocument, document_hashes

    out: dict[str, str] = {}
    for item in items:
        if item["kind"] != "document":
            continue
        document = item["document"]
        expanded: dict[str, Any] = {"file_type": document["file_type"], "pages": item["pages"]}
        if "title" in document:
            expanded["title"] = document["title"]
        out[item["file_uri"]] = document_hashes(cast(ExpandedDocument, expanded))["doc_hash"]
    return out


# --------------------------------------------------------------------------- 全量枚举


def test_full_enumeration_yields_expected_uris(tmp_path: Path, fixture_files: Any) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    items = asyncio.run(enumerate_all(write_definition(tmp_path, repo)))

    documents = [item for item in items if item["kind"] == "document"]
    errors = [item for item in items if item["kind"] == "error"]
    assert sorted(item["file_uri"] for item in documents) == EXPECTED_URIS
    # bad.md 不是合法 UTF-8：如实报条目级错误，不静默跳过
    assert [(item["file_uri"], item["code"]) for item in errors] == [
        ("git://t/bad.md", "content_invalid")
    ]
    # front matter 的 title 进了文档对象；其余文档没有 title 键
    by_uri = {item["file_uri"]: item["document"] for item in documents}
    assert by_uri["git://t/docs/%E8%AF%B4%E6%98%8E.md"]["title"] == "说明"
    assert "title" not in by_uri["git://t/notes.txt"]
    # 元素层也如实映射：md 的第一个元素是标题
    markdown = next(
        item for item in documents if item["file_uri"].endswith("%E8%AF%B4%E6%98%8E.md")
    )
    assert markdown["pages"][0]["elements"][0]["category"] == "Title"
    # 列表项经完整管线产出（回归：容器分支曾把整个列表跳过，ListItem 永不出现）
    listed = next(item for item in documents if item["file_uri"] == "git://t/list.md")
    assert [element["category"] for element in listed["pages"][0]["elements"]] == [
        "Title",
        "ListItem",
        "ListItem",
    ]


def test_repeat_run_same_commit_is_stable(tmp_path: Path, fixture_files: Any) -> None:
    """验收判据：同一提交重复运行，doc_hash 完全稳定（三次枚举，含三次独立握手）。"""
    repo = tmp_path / "repo"
    commit = init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo)

    first = doc_hashes_from(asyncio.run(enumerate_all(definition)))
    second = doc_hashes_from(asyncio.run(enumerate_all(definition)))
    third = doc_hashes_from(asyncio.run(enumerate_all(definition)))
    assert first == second == third
    assert set(first) == set(EXPECTED_URIS)
    assert all(value.startswith("dpe1:") for value in first.values())
    assert len(commit) == 40  # 夹具的提交 id 是确定的（固定作者与时间戳）


def test_ref_selects_commit(tmp_path: Path, fixture_files: Any) -> None:
    """ref 指定提交：把 ref 钉在旧提交上，新提交的内容不出现在枚举里。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# one\n"})
    old = run_git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.md").write_bytes(b"# two\n")
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "second")

    definition = write_definition(tmp_path, repo, {"ref": old})
    items = asyncio.run(enumerate_all(definition))
    assert [item["file_uri"] for item in items] == ["git://t/a.md"]


def test_unknown_next_is_rejected(tmp_path: Path, fixture_files: Any) -> None:
    """未知或过期的 next → -32602，运行器以原 cursor 重开（契约 §6.4）。"""
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo)

    async def run() -> None:
        async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
            await handshake(host, definition)
            await host.request("scan", {"cursor": None}, timeout=60.0)
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


def test_initialize_rejects_missing_repo(tmp_path: Path, fixture_files: Any) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo, {"repo": str(tmp_path / "nope")})
    failure = _initialize_failure(definition)
    assert failure is not None
    assert failure.code == "invalid_config"
    assert failure.source == "plugin"


def test_initialize_rejects_unresolvable_ref(tmp_path: Path, fixture_files: Any) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo, {"ref": "does-not-exist"})
    failure = _initialize_failure(definition)
    assert failure is not None
    assert failure.code == "invalid_config"


def test_shutdown_exits_cleanly(tmp_path: Path, fixture_files: Any) -> None:
    """响应 shutdown 后以退出码 0 退出（契约 §6.3、§8.1 第 4 条）。"""
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo)

    async def run() -> int | None:
        async with PluginHost(max_message_bytes=MAX_MESSAGE_BYTES) as host:
            await handshake(host, definition)
        return host.returncode

    assert asyncio.run(run()) == 0


def test_stdin_eof_exits(tmp_path: Path, fixture_files: Any) -> None:
    """读到 stdin EOF 尽快退出（契约 §6.3：运行器崩溃后不残留孤儿进程）。"""
    repo = tmp_path / "repo"
    init_repo(repo, fixture_files)
    definition = write_definition(tmp_path, repo)

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


def test_manifest_rejects_unlisted_config() -> None:
    """config_schema 是封闭 schema：未声明的成员会被运行器拒绝（§4.1）。"""
    from dpe_sdk.run import validate_config

    manifest = load_manifest(manifest_path())
    validate_config(manifest, {"repo": "/tmp/x"})  # 最小合法配置
    with pytest.raises(InstanceFailure) as excinfo:
        validate_config(manifest, {"repo": "/tmp/x", "subdir": "docs"})
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
