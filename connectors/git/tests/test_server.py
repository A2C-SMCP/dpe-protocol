"""线协议层单测：NDJSON 分帧、握手与配置校验、续批 token、分段与错误路径。

用 ``Serve`` 直接吃行（sans-IO），不起子进程；子进程路径由 ``test_e2e`` 覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import History, ReposRoot
from helpers import FixtureRepo, requires_git

from dpe_git_connector import PLUGIN_NAME, __version__
from dpe_git_connector.server import Serve

pytestmark = requires_git

PREFIX = "git://s/"


def initialize_params(config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "protocol_versions": ["dpe-connector/1"],
        "max_message_bytes": 1024 * 1024,
        "instance": {"uri_prefix": PREFIX, "config": config},
    }
    params.update(extra)
    return params


def request(id_: Any, method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})


def parse(line: str) -> dict[str, Any]:
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def handle(serve: Serve, message: str) -> dict[str, Any]:
    """发出一行请求并解析响应；插件对请求必须应答（``None`` 只用于通知）。"""
    line = serve.handle(message)
    assert line is not None
    return parse(line)


def initialized_serve(config: dict[str, Any], **extra: Any) -> Serve:
    serve = Serve()
    result = handle(serve, request(1, "initialize", initialize_params(config, **extra)))
    assert result["result"]["plugin"]["name"] == PLUGIN_NAME
    return serve


def scan(serve: Serve, id_: Any = 2, params: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = handle(serve, request(id_, "scan", params or {"cursor": None}))[
        "result"
    ]
    return result


def collect(serve: Serve, **first: Any) -> tuple[list[dict[str, Any]], int]:
    """按续批流程拉完整轮；返回（全部条目, 批次数）。"""
    items: list[dict[str, Any]] = []
    batches = 0
    params: dict[str, Any] = {"cursor": None, **first}
    while True:
        result = scan(serve, 2 + batches, params)
        items.extend(result["items"])
        batches += 1
        if "next" not in result:
            assert result["cursor"] is None
            assert result["incremental"] is False
            return items, batches
        params = {"next": result["next"]}


# --------------------------------------------------------------------------- 握手


def test_initialize_handshake_shape(history: History) -> None:
    serve = Serve()
    response = handle(
        serve, request(1, "initialize", initialize_params({"repo": str(history.path)}))
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocol_version": "dpe-connector/1",
            "plugin": {"name": PLUGIN_NAME, "version": __version__},
            "capabilities": {"supports_cursor": False},
        },
    }


def test_handshake_must_come_first() -> None:
    serve = Serve()
    response = handle(serve, request(1, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32600
    assert "握手" in response["error"]["message"]


def test_version_negotiation_rejects_no_common_version(history: History) -> None:
    serve = Serve()
    params = initialize_params({"repo": str(history.path)})
    params["protocol_versions"] = ["dpe-connector/9"]
    response = handle(serve, request(1, "initialize", params))
    assert response["error"]["code"] == -32004


def test_initialize_rejects_bad_params(history: History) -> None:
    serve = Serve()
    for bad in (
        {"protocol_versions": "x", "max_message_bytes": 1, "instance": {}},
        {"protocol_versions": ["dpe-connector/1"], "max_message_bytes": 0, "instance": {}},
        {
            "protocol_versions": ["dpe-connector/1"],
            "max_message_bytes": 1,
            "instance": {"uri_prefix": PREFIX},
        },
    ):
        response = handle(serve, request(1, "initialize", bad))
        assert response["error"]["code"] == -32602, bad


def test_initialize_rejects_config_problems(tmp_path: Path, history: History) -> None:
    """schema 无法表达的语义约束在 initialize 拒绝（-32005）：配置二选一、since 的适用范围等。"""
    no_origin = FixtureRepo(tmp_path / "no-origin")
    no_origin.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    as_root = FixtureRepo(tmp_path / "as-root")
    as_root.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    cases: list[tuple[str, dict[str, Any]]] = [
        ("必须且只能", {}),
        ("必须且只能", {"repo": str(history.path), "root": str(tmp_path)}),
        ("root 不是目录", {"root": str(tmp_path / "nope")}),
        ("root 指向的是仓库本身", {"root": str(as_root.root)}),
        ("since 只在单仓库模式", {"root": str(tmp_path), "since": history.sha("c1")}),
        ("since 无法解析", {"repo": str(history.path), "since": "does-not-exist"}),
        ("since 不是默认分支", {"repo": str(history.path), "since": history.sha("d1")}),
        ("默认分支", {"repo": str(no_origin.root)}),
        ("branches 必须", {"repo": str(history.path), "branches": "dev"}),
    ]
    for match, config in cases:
        serve = Serve()
        response = handle(serve, request(1, "initialize", initialize_params(config)))
        assert response["error"]["code"] == -32005, config
        assert match in response["error"]["message"], config
        assert serve.fatal is True


def test_uri_prefix_must_end_with_separator(history: History) -> None:
    """前缀不以分隔字符结尾时拒绝（否则拼接会改写 authority：git://docs + repo）。"""
    serve = Serve()
    params = initialize_params({"repo": str(history.path)})
    params["instance"]["uri_prefix"] = "git://docs"
    response = handle(serve, request(1, "initialize", params))
    assert response["error"]["code"] == -32005
    assert serve.fatal is True


def test_handshake_failure_is_fatal(tmp_path: Path) -> None:
    """握手失败后进程进入致命状态：此后对任何请求一律回错、不产出内容（§6.3、§7.1）。"""
    serve = Serve()
    params = initialize_params({"repo": str(tmp_path / "missing")})
    assert "error" in handle(serve, request(1, "initialize", params))
    response = handle(serve, request(2, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32600


# --------------------------------------------------------------------------- 全量枚举


def test_scan_emits_one_document_per_repository(history: History) -> None:
    serve = initialized_serve({"repo": str(history.path), "branches": ["dev"]})
    result = scan(serve)
    assert [item["kind"] for item in result["items"]] == ["document"]
    item = result["items"][0]
    assert item["file_uri"] == "git://s/repo"
    assert item["document"] == {"file_type": "git_repo"}
    assert [page["page_metadata"] for page in item["pages"]] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
        {"ref": "dev"},
    ]
    assert result["cursor"] is None
    assert result["incremental"] is False
    assert "next" not in result


def test_scan_root_mode_discovers_and_reports_item_errors(repos_root: ReposRoot) -> None:
    """root 模式：每个仓库一篇文档（按身份字节序）；坏仓库与空仓库如实分列。"""
    serve = initialized_serve({"root": str(repos_root.root)})
    items = scan(serve)["items"]
    assert [(item["kind"], item["file_uri"]) for item in items] == [
        ("document", "git://s/alpha"),
        ("error", "git://s/broken"),
        ("document", "git://s/empty_repo"),
        ("document", "git://s/nested/beta"),
    ]
    assert items[1]["code"] == "source_unavailable"
    assert items[1]["retryable"] is True
    # 空仓库：零页文档；嵌套仓库的身份是相对 root 的路径
    assert items[2]["pages"] == []
    assert [page["page_metadata"] for page in items[3]["pages"]] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
    ]


def test_unreadable_subtree_is_item_error(tmp_path: Path) -> None:
    """发现层读不了的子树：条目级 source_unavailable，其余仓库照常（不废掉整轮，§6.5）。"""
    if os.geteuid() == 0:
        pytest.skip("root 无视权限位，chmod 0 造不出不可读目录")
    root = tmp_path / "root"
    root.mkdir()
    repo = FixtureRepo(root / "alpha")
    repo.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    repo.set_origin_head("main")
    hidden = root / "hidden"
    hidden.mkdir()
    os.chmod(hidden, 0)
    try:
        serve = initialized_serve({"root": str(root)})
        items = scan(serve)["items"]
        assert [(item["kind"], item["file_uri"]) for item in items] == [
            ("document", "git://s/alpha"),
            ("error", "git://s/hidden"),
        ]
        assert items[1]["code"] == "source_unavailable"
        assert items[1]["retryable"] is True
    finally:
        os.chmod(hidden, 0o755)  # 让 pytest 能清理 tmp_path


def test_too_small_budget_is_request_error(history: History) -> None:
    """单条预算连最小条目都装不下（上限过小 / id 过长）：-32602 应答，不发超限消息（§6.5）。"""
    serve = Serve()
    params = initialize_params({"repo": str(history.path)})
    params["max_message_bytes"] = 1200
    assert "result" in handle(serve, request(1, "initialize", params))
    # 预算 = 1200 − 信封预留 1024 − id 152 = 24 字节：远小于最小 error 条目，_BudgetTooSmall 必触发
    long_id = "x" * 150
    line = serve.handle(
        json.dumps({"jsonrpc": "2.0", "id": long_id, "method": "scan", "params": {"cursor": None}})
    )
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= serve.max_message_bytes  # 错误响应本身装得下
    response = json.loads(line)
    assert response["error"]["code"] == -32602
    assert "单条预算" in response["error"]["message"]


def test_error_response_is_trimmed_when_id_is_near_the_limit(history: History) -> None:
    """id 逼近上限的**合法**请求：错误响应按整行校验，装不下完整消息时只留错误码（§6.5）。

    （回归：替代响应只校验了消息正文，id 落在约 140 字节的窄窗口时响应仍会超 max_message_bytes。）
    """
    serve = Serve()
    params = initialize_params({"repo": str(history.path)})
    params["max_message_bytes"] = 1200
    assert "result" in handle(serve, request(1, "initialize", params))
    long_id = "x" * 1000  # 请求本身 ≈1063 字节，合法；预算为负 → _BudgetTooSmall
    request_line = json.dumps(
        {"jsonrpc": "2.0", "id": long_id, "method": "scan", "params": {"cursor": None}}
    )
    assert len(request_line.encode("utf-8")) + 1 <= serve.max_message_bytes
    line = serve.handle(request_line)
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= serve.max_message_bytes  # 整行仍在上限内
    error = json.loads(line)["error"]
    assert error["code"] == -32602
    assert "message" not in error  # 消息正文被裁掉，保留 id 与错误码


def test_continuation_budget_shrink_degrades_to_item_error(tmp_path: Path) -> None:
    """续批的预算随 id 变长而缩水：批首装不下下一页时该文档整篇失败（条目级 error），
    不得断言崩溃（回归：旧实现此处 assert items，会以 -32603 收场且不收敛）。"""
    from dpe_git_connector.server import _encoded_size

    repo = FixtureRepo(tmp_path / "repo")
    for month in (1, 2, 3):
        repo.commit(
            f"{month} 月提交（等长说明）",
            date=f"2026-0{month}-10T00:00:00+00:00",
            files={f"f{month}.txt": "x\n"},
        )
    config = {"repo": str(repo.root), "default_branch": "main"}
    document = scan(initialized_serve(config))["items"][0]
    pages = document["pages"]
    assert len(pages) == 3
    first_base = _encoded_size(
        {
            "kind": "document",
            "file_uri": document["file_uri"],
            "document": document["document"],
            "pages": [],
            "continued": True,
        }
    )
    cont_base = _encoded_size(
        {"kind": "document", "file_uri": document["file_uri"], "pages": [], "continued": True}
    )
    sizes = [_encoded_size(page) for page in pages]
    # 首批（id "2"，1 字节）的预算刚好装下首页：分段成立（其余页留到续批）
    limit = 1024 + 1 + first_base + sizes[0]
    assert cont_base + max(sizes) <= limit - 1024 - 1  # 每一页当时都装得进续段信封

    serve = Serve()
    params = initialize_params(config)
    params["max_message_bytes"] = limit
    assert "result" in handle(serve, request(1, "initialize", params))
    first = scan(serve, 2)
    assert first["items"][0]["continued"] is True

    # 续批换用长 id：预算缩到「比装下第二页少 1 字节」，批首装不下任何一页
    need_room = cont_base + sizes[1] - 1
    id_length = limit - 1024 - need_room
    assert id_length > 0
    line = serve.handle(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "x" * id_length,
                "method": "scan",
                "params": {"next": first["next"]},
            }
        )
    )
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit
    result = json.loads(line)["result"]
    assert [item["kind"] for item in result["items"]] == ["error"]
    assert result["items"][0]["code"] == "content_invalid"
    assert "分段" in result["items"][0]["message"]
    # 该文档失败不扣住本轮：同一响应即结束轮（§6.8）
    assert result["cursor"] is None and result["incremental"] is False


def test_scan_batches_by_max_items(repos_root: ReposRoot) -> None:
    serve = initialized_serve({"root": str(repos_root.root)})
    items, batches = collect(serve, max_items=2)
    assert len(items) == 4
    assert batches == 2


def test_next_token_semantics(repos_root: ReposRoot) -> None:
    """token 不透明：只认最近回出的值；回退、重复、陌生与带 cursor 的组合一律 -32602。"""
    serve = initialized_serve({"root": str(repos_root.root)})
    first = scan(serve, params={"cursor": None, "max_items": 1})
    token = first["next"]
    # 带 cursor 的组合非法（§6.4：续批 MUST NOT 带 cursor）
    response = handle(serve, request(3, "scan", {"next": token, "cursor": None}))
    assert response["error"]["code"] == -32602
    # 陌生的 token
    for bad in ("99999", "0", token + "0"):
        response = handle(serve, request(3, "scan", {"next": bad}))
        assert response["error"]["code"] == -32602, bad
    # 非十进制
    for bad in ("abc", "²", "1.5", "-1", ""):
        response = handle(serve, request(3, "scan", {"next": bad}))
        assert response["error"]["code"] == -32602, bad
    # 正确的 token 继续本轮
    second = scan(serve, 4, {"next": token, "max_items": 1})
    assert second["next"] != token  # 新批次号
    # 已过期的旧 token 再发一次：拒绝（运行器以原 cursor 重开）
    response = handle(serve, request(5, "scan", {"next": token}))
    assert response["error"]["code"] == -32602


def test_cursor_without_round_is_accepted_as_full(history: History) -> None:
    """带 cursor（缓存丢失等）但本插件不支持游标：如实按全量枚举处理（§6.8）。"""
    serve = initialized_serve({"repo": str(history.path)})
    result = scan(serve, params={"cursor": "c-1"})
    assert result["incremental"] is False
    assert [item["file_uri"] for item in result["items"]] == ["git://s/repo"]


def test_scan_re_resolves_refs_each_round(tmp_path: Path) -> None:
    """轮首重新解析引用：宿主跨轮复用进程时，新提交要能反映到下一轮（§6.3）。"""
    repo = FixtureRepo(tmp_path / "repo")
    repo.commit("一月提交", date="2026-01-10T00:00:00+00:00", files={"a.txt": "1\n"})
    serve = initialized_serve({"repo": str(repo.root), "default_branch": "main"})
    first = scan(serve)
    assert [page["page_metadata"] for page in first["items"][0]["pages"]] == [
        {"ref": "main", "period": "2026-01"}
    ]
    repo.commit("二月提交", date="2026-02-10T00:00:00+00:00", files={"a.txt": "2\n"})
    second = scan(serve, 3)
    assert [page["page_metadata"] for page in second["items"][0]["pages"]] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
    ]


# --------------------------------------------------------------------------- 分段（§6.5）


def segment_lines(serve: Serve, **first: Any) -> list[dict[str, Any]]:
    """拉完整轮并逐批校验响应不超上限；返回全部条目（含分段）。"""
    items: list[dict[str, Any]] = []
    params: dict[str, Any] = {"cursor": None, **first}
    batch = 0
    while True:
        line = serve.handle(request(2 + batch, "scan", params))
        assert line is not None
        assert len(line.encode("utf-8")) + 1 <= serve.max_message_bytes
        result = parse(line)["result"]
        items.extend(result["items"])
        batch += 1
        if "next" not in result:
            return items
        params = {"next": result["next"]}


def assemble(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 §6.5 把分段文档拼回整篇：断言各段连续性规则。"""
    documents: list[dict[str, Any]] = []
    open_document: dict[str, Any] | None = None
    for item in items:
        if item["kind"] != "document":
            continue
        if open_document is not None:
            assert item["file_uri"] == open_document["file_uri"], "分段必须连续"
        if "document" in item:
            assert open_document is None, "上一分段未闭合时不得开新课"
            open_document = {
                "kind": "document",
                "file_uri": item["file_uri"],
                "document": item["document"],
                "pages": list(item["pages"]),
            }
        else:
            assert open_document is not None, "续段必须先有首段"
            open_document["pages"].extend(item["pages"])
        if not item.get("continued", False):
            documents.append(open_document)
            open_document = None
    assert open_document is None, "轮结束时不得有未闭合的分段文档"
    return documents


def test_document_is_segmented_at_page_boundaries(history: History) -> None:
    """整篇装不下时按页边界分段：首段带 document、续段不带，拼回与整篇一致。"""
    config = {"repo": str(history.path), "branches": ["dev"]}
    whole = initialized_serve(config)
    whole_items = scan(whole)["items"]
    assert len(whole_items) == 1 and "continued" not in whole_items[0]

    serve = Serve()
    params = initialize_params(config)
    params["max_message_bytes"] = 2500
    assert "result" in handle(serve, request(1, "initialize", params))
    items = segment_lines(serve)
    assert len(items) > 1
    first = items[0]
    assert "document" in first
    assert first["continued"] is True
    assert "document" not in items[1]
    assert items[-1].get("continued", False) is False
    assert assemble(items) == whole_items


def test_segmented_document_spans_batches(history: History) -> None:
    """max_items=1 时分段跨批：token 续批必须接着发在途分段，而不是重发整篇。"""
    serve = Serve()
    params = initialize_params({"repo": str(history.path), "branches": ["dev"]})
    params["max_message_bytes"] = 2500
    assert "result" in handle(serve, request(1, "initialize", params))
    items = segment_lines(serve, max_items=1)
    whole = scan(initialized_serve({"repo": str(history.path), "branches": ["dev"]}))["items"]
    assert len(items) > 2  # 每批一条，分段跨了多批
    assert assemble(items) == whole


def test_unsegmentable_page_becomes_item_error(tmp_path: Path) -> None:
    """单页自身装不进单条预算（且页不可细分）时：整篇文档条目级 content_invalid（§6.5）。"""
    repo = FixtureRepo(tmp_path / "repo")
    text = "".join(f"第 {index} 行\n" for index in range(60_000))  # 一页约 800 KiB
    repo.commit(text, date="2026-06-01T00:00:00+00:00", message_file=True)
    serve = Serve()
    params = initialize_params({"repo": str(repo.root), "default_branch": "main"})
    params["max_message_bytes"] = 5000
    assert "result" in handle(serve, request(1, "initialize", params))
    result = scan(serve, 2)
    item = result["items"][0]
    assert item["kind"] == "error"
    assert item["code"] == "content_invalid"
    assert item["file_uri"] == "git://s/repo"
    # 消息按实测口径写：报的是单条预算（上限 − 信封预留 − id 长度），不是上限本身
    assert f"单条预算 {5000 - 1024 - 1}" in item["message"]
    assert "消息上限 5000" in item["message"]


def test_remote_limits_guard_reports_error(history: History) -> None:
    """远端 max_payload_bytes 小于固定预算能切出的元素时：如实报错，不按远端值重新切分。"""
    serve = initialized_serve({"repo": str(history.path)}, remote_limits={"max_payload_bytes": 64})
    result = scan(serve, 2)
    assert [item["kind"] for item in result["items"]] == ["error"]
    assert "max_payload_bytes" in result["items"][0]["message"]


# --------------------------------------------------------------------------- 协议级错误路径


def test_unknown_method(history: History) -> None:
    serve = initialized_serve({"repo": str(history.path)})
    response = handle(serve, request(2, "read_blob", {"handle": "h"}))
    assert response["error"]["code"] == -32601


def test_malformed_lines() -> None:
    serve = Serve()
    assert handle(serve, "not json")["error"]["code"] == -32700
    assert handle(serve, "[1,2]")["error"]["code"] == -32600
    assert handle(serve, '{"id":1}')["error"]["code"] == -32600


def test_request_over_max_message_bytes_is_rejected(history: History) -> None:
    """§6.2：插件收到超限请求 MUST 以 -32600 拒绝并尽力丢弃该行。"""
    serve = initialized_serve({"repo": str(history.path)})
    big = request(2, "scan", {"cursor": None, "pad": "x" * (1024 * 1024)})
    response = handle(serve, big)
    assert response["error"]["code"] == -32600
    assert "max_message_bytes" in response["error"]["message"]


def test_cancel_notification_marks_request(history: History) -> None:
    """cancel 是通知（无响应）；被取消的请求以 -32001 应答（§6.7）。"""
    serve = initialized_serve({"repo": str(history.path)})
    notification = json.dumps({"jsonrpc": "2.0", "method": "cancel", "params": {"id": 7}})
    assert serve.handle(notification) is None
    response = handle(serve, request(7, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32001


def test_cancel_does_not_poison_reused_ids(history: History) -> None:
    """JSON-RPC 允许 id 复用：cancel 只对最近一次被指向的 id 生效，不得误伤后续同 id 请求。"""
    serve = initialized_serve({"repo": str(history.path)})
    for id_ in (5, 6):
        notification = json.dumps({"jsonrpc": "2.0", "method": "cancel", "params": {"id": id_}})
        assert serve.handle(notification) is None
        assert handle(serve, request(id_, "scan", {"cursor": None}))["error"]["code"] == -32001
    # id 5 已被应答并清除：再发同 id 的合法请求必须正常处理（不因历史 cancel 而被误判）
    response = handle(serve, request(5, "scan", {"cursor": None}))
    assert "result" in response
    # 但最近一次被取消的 id（6）在轮首清空后同样不再误伤（行为断言，不读私有状态）
    assert "result" in handle(serve, request(6, "scan", {"cursor": None}))


def test_shutdown_sets_flag_and_responds(history: History) -> None:
    serve = initialized_serve({"repo": str(history.path)})
    assert serve.shutdown_requested is False
    response = handle(serve, request(9, "shutdown", {}))
    assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert serve.shutdown_requested is True


def test_unknown_method_echo_is_bounded(history: History) -> None:
    """未知方法名不回显超长内容：接近上限的请求不会让错误响应越限。"""
    serve = initialized_serve({"repo": str(history.path)})
    limit = serve.max_message_bytes
    long_method = "m" * (limit - 200)
    line = serve.handle(
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": long_method, "params": {}})
    )
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit
    assert json.loads(line)["error"]["code"] == -32601


def test_batch_accounting_includes_separators_and_envelope(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """批次记账必须含条目间逗号与信封开销（回归：漏记后接近条目上限的批次会顶破上限）。

    把 ``_ENVELOPE_RESERVE`` 缩小后，用「总条目字节数刚好放行整批、实际响应却越限」的窄窗口
    复现：正确记账下响应一定在上限内。
    """
    from dpe_git_connector import server as server_module
    from dpe_git_connector.server import _encoded_size

    monkeypatch.setattr(server_module, "_ENVELOPE_RESERVE", 32)
    root = tmp_path / "root"
    root.mkdir()
    for index in range(5):
        repo = FixtureRepo(root / f"r{index}")
        repo.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
        repo.set_origin_head("main")

    serve = initialized_serve({"root": str(root)})
    items = scan(serve)["items"]
    assert len(items) == 5
    total = sum(_encoded_size(item) for item in items)

    strict = Serve()
    params = initialize_params({"root": str(root)})
    params["max_message_bytes"] = total + 32 + 3  # 预留 32 + id 与行尾的余量
    assert "result" in handle(strict, request(1, "initialize", params))
    line = strict.handle(request(2, "scan", {"cursor": None}))
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= strict.max_message_bytes
    assert "result" in json.loads(line)


def test_long_id_cannot_push_response_over_limit(tmp_path: Path) -> None:
    """id 由运行器给出：超长 id 的病理输入下响应仍不得越限（§6.2 双方 MUST NOT 发超限消息）。"""
    from dpe_git_connector.server import _ENVELOPE_RESERVE, _encoded_size

    root = tmp_path / "root"
    root.mkdir()
    for index in range(3):
        repo = FixtureRepo(root / f"r{index}")
        repo.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
        repo.set_origin_head("main")

    serve = initialized_serve({"root": str(root)})
    items = scan(serve)["items"]
    total = sum(_encoded_size(item) for item in items)

    limit = total + _ENVELOPE_RESERVE + 2000 + 4  # 余量小于重发超长 id 的开销
    strict = Serve()
    params = initialize_params({"root": str(root)})
    params["max_message_bytes"] = limit
    assert "result" in handle(strict, request(1, "initialize", params))
    long_id_request = json.dumps(
        {"jsonrpc": "2.0", "id": "x" * 2000, "method": "scan", "params": {"cursor": None}}
    )
    assert len(long_id_request.encode("utf-8")) + 1 <= limit  # 请求本身合法（在上限内）
    line = strict.handle(long_id_request)
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit


def test_sigterm_exits_cleanly() -> None:
    """空闲阻塞在 stdin 上时收到 SIGTERM 应尽快退出（PEP 475 下只置标志位不会被检查）。

    先发一行非法请求并读到应答，确认进程已进入读循环——固定 sleep 在慢机器上不可靠
    （信号在解释器启动、信号处理器安装之前到达会按默认处置杀掉进程）。
    """
    import signal
    import subprocess

    from helpers import command_for

    proc = subprocess.Popen(
        command_for("dpe-git-connector"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(b"not json\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["error"]["code"] == -32700
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
