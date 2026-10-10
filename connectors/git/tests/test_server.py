"""线协议层单测：NDJSON 分帧、错误码、握手先行、续批 token 与失败路径。

用 ``Serve`` 直接吃行（sans-IO），不起子进程；子进程路径由 ``test_e2e`` 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helpers import init_repo, requires_git

from dpe_git_connector import PLUGIN_NAME, __version__
from dpe_git_connector.server import Serve

pytestmark = requires_git

PREFIX = "git://s/"


def initialize_params(repo: Path, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "protocol_versions": ["dpe-connector/1"],
        "max_message_bytes": 1024 * 1024,
        "instance": {"uri_prefix": PREFIX, "config": {"repo": str(repo), "ref": "main"}},
    }
    params.update(extra)
    return params


def request(id_: int, method: str, params: dict[str, Any]) -> str:
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


def initialized_serve(repo: Path, **extra: Any) -> Serve:
    serve = Serve()
    result = handle(serve, request(1, "initialize", initialize_params(repo, **extra)))
    assert result["result"]["plugin"]["name"] == PLUGIN_NAME
    return serve


def test_initialize_handshake_shape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# x\n"})
    serve = Serve()
    response = handle(serve, request(1, "initialize", initialize_params(repo)))
    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocol_version": "dpe-connector/1",
            "plugin": {"name": PLUGIN_NAME, "version": __version__},
            "capabilities": {"supports_cursor": False},
        },
    }


def test_handshake_must_come_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# x\n"})
    serve = Serve()
    response = handle(serve, request(1, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32600
    assert "握手" in response["error"]["message"]


def test_version_negotiation_rejects_no_common_version(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# x\n"})
    serve = Serve()
    params = initialize_params(repo)
    params["protocol_versions"] = ["dpe-connector/9"]
    response = handle(serve, request(1, "initialize", params))
    assert response["error"]["code"] == -32004


def test_initialize_rejects_unavailable_repo(tmp_path: Path) -> None:
    serve = Serve()
    params = initialize_params(tmp_path / "missing")
    response = handle(serve, request(1, "initialize", params))
    assert response["error"]["code"] == -32005


def test_initialize_rejects_bad_params(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# x\n"})
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


def test_scan_emits_documents_and_ends_without_cursor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n", "b.txt": b"b\n"})
    serve = initialized_serve(repo)
    response = handle(serve, request(2, "scan", {"cursor": None}))
    result = response["result"]
    assert [item["file_uri"] for item in result["items"]] == ["git://s/a.md", "git://s/b.txt"]
    assert result["cursor"] is None
    assert result["incremental"] is False
    assert "next" not in result


def test_scan_batches_by_max_items(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {f"f{index}.txt": b"x\n" for index in range(5)})
    serve = initialized_serve(repo)
    response = handle(serve, request(2, "scan", {"cursor": None, "max_items": 2}))["result"]
    batches = [len(response["items"])]
    assert response["next"] == "2"
    while "next" in response:
        # 中间批：不带 cursor / incremental
        assert "cursor" not in response and "incremental" not in response
        params = {"next": response["next"], "max_items": 2}
        response = handle(serve, request(3, "scan", params))["result"]
        batches.append(len(response["items"]))
    assert batches == [2, 2, 1]
    assert response["cursor"] is None and response["incremental"] is False


def test_rejects_bad_next_and_cursor_combo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    serve.handle(request(2, "scan", {"cursor": None}))
    for params in (
        {"next": "abc"},
        {"next": "999"},
        {"next": "0", "cursor": None},
    ):
        response = handle(serve, request(3, "scan", params))
        assert response["error"]["code"] == -32602, params


def test_cursor_without_round_is_accepted_as_full(tmp_path: Path) -> None:
    """首轮就带 cursor（缓存丢失等）：如实按全量枚举处理（§6.8）。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    response = handle(serve, request(2, "scan", {"cursor": "c-1"}))
    assert response["result"]["incremental"] is False
    assert len(response["result"]["items"]) == 1


def test_unknown_method(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    response = handle(serve, request(2, "read_blob", {"handle": "h"}))
    assert response["error"]["code"] == -32601


def test_malformed_lines(tmp_path: Path) -> None:
    serve = Serve()
    assert handle(serve, "not json")["error"]["code"] == -32700
    assert handle(serve, "[1,2]")["error"]["code"] == -32600
    assert handle(serve, '{"id":1}')["error"]["code"] == -32600


def test_cancel_notification_marks_request(tmp_path: Path) -> None:
    """cancel 是通知（无响应）；被取消的请求以 -32001 应答（§6.7）。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    notification = json.dumps({"jsonrpc": "2.0", "method": "cancel", "params": {"id": 7}})
    assert serve.handle(notification) is None
    response = handle(serve, request(7, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32001


def test_shutdown_sets_flag_and_responds(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    assert serve.shutdown_requested is False
    response = handle(serve, request(9, "shutdown", {}))
    assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert serve.shutdown_requested is True


def test_unsupported_types_are_skipped_without_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n", "x.png": b"\x89PNG", "y.html": b"<html>"})
    serve = initialized_serve(repo)
    result = handle(serve, request(2, "scan", {"cursor": None}))["result"]
    assert [item["file_uri"] for item in result["items"]] == ["git://s/a.md"]


def test_remote_limits_guard_reports_error(tmp_path: Path) -> None:
    """远端 max_payload_bytes 小于固定预算时：如实报错，不按远端值重新切分。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"big.txt": b"x" * 4096})
    serve = initialized_serve(repo, remote_limits={"max_payload_bytes": 1024})
    result = handle(serve, request(2, "scan", {"cursor": None}))["result"]
    assert [item["kind"] for item in result["items"]] == ["error"]
    assert "max_payload_bytes" in result["items"][0]["message"]


def test_oversize_entry_becomes_error_not_oversize_message(tmp_path: Path) -> None:
    """§6.5：单条装不进 max_message_bytes 时以条目级 error 上报，MUST NOT 发送超限消息。"""
    repo = tmp_path / "repo"
    # 每行 3000 字符、共 900 行 ≈ 2.7 MB：固定预算把它切成多个元素，但整篇条目仍装不下小上限
    init_repo(repo, {"big.txt": "abc\n".join("x" * 3000 for _ in range(900)).encode()})
    serve = Serve()
    params = initialize_params(repo)
    params["max_message_bytes"] = 200_000
    assert "result" in handle(serve, request(1, "initialize", params))
    result = handle(serve, request(2, "scan", {"cursor": None}))["result"]
    assert [item["kind"] for item in result["items"]] == ["error"]
    assert result["items"][0]["code"] == "content_invalid"
    assert "单条预算" in result["items"][0]["message"]
    # 响应本身必须在上限内
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 200_000


def test_request_over_max_message_bytes_is_rejected(tmp_path: Path) -> None:
    """§6.2：插件收到超限请求 MUST 以 -32600 拒绝并尽力丢弃该行。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    big = request(2, "scan", {"cursor": None, "pad": "x" * (1024 * 1024)})
    response = handle(serve, big)
    assert response["error"]["code"] == -32600
    assert "max_message_bytes" in response["error"]["message"]


def test_next_token_must_be_ascii_digits(tmp_path: Path) -> None:
    """next token 只认 ASCII 十进制：``str.isdigit`` 会放行全角/上标数字，不能用它判定。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    for token in ("²", "٣", "1.5", "-1", ""):
        response = handle(serve, request(3, "scan", {"next": token}))
        assert response["error"]["code"] == -32602, token


def test_uri_prefix_must_end_with_separator(tmp_path: Path) -> None:
    """前缀不以分隔字符结尾时拒绝（否则拼接会改写 authority：git://docs + a.md）。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = Serve()
    params = initialize_params(repo)
    params["instance"]["uri_prefix"] = "git://docs"
    response = handle(serve, request(1, "initialize", params))
    assert response["error"]["code"] == -32005
    assert serve.fatal is True


def test_handshake_failure_is_fatal(tmp_path: Path) -> None:
    """握手失败后进程进入致命状态：此后对任何请求一律回错，不产出内容（§6.3、§7.1）。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = Serve()
    params = initialize_params(tmp_path / "missing")
    assert "error" in handle(serve, request(1, "initialize", params))
    response = handle(serve, request(2, "scan", {"cursor": None}))
    assert response["error"]["code"] == -32600


def test_scan_re_resolves_ref_each_round(tmp_path: Path) -> None:
    """轮首重新解析 ref：宿主跨轮复用进程时，分支前进要能反映到下一轮（§6.3）。"""
    from helpers import run_git

    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# one\n"})
    serve = initialized_serve(repo)
    first = handle(serve, request(2, "scan", {"cursor": None}))["result"]
    assert [item["file_uri"] for item in first["items"]] == ["git://s/a.md"]

    (repo / "b.md").write_bytes(b"# two\n")
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "second")
    second = handle(serve, request(3, "scan", {"cursor": None}))["result"]
    assert [item["file_uri"] for item in second["items"]] == ["git://s/a.md", "git://s/b.md"]


def test_cancel_does_not_poison_reused_ids(tmp_path: Path) -> None:
    """JSON-RPC 允许 id 复用：cancel 只对最近一次被指向的 id 生效，不得误伤后续同 id 请求。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    for id_ in (5, 6):
        notification = json.dumps({"jsonrpc": "2.0", "method": "cancel", "params": {"id": id_}})
        assert serve.handle(notification) is None
        assert handle(serve, request(id_, "scan", {"cursor": None}))["error"]["code"] == -32001
    # id 5 已被应答并清除：再发同 id 的合法请求必须正常处理（不因历史 cancel 而被误判）
    response = handle(serve, request(5, "scan", {"cursor": None}))
    assert "result" in response
    # 但最近一次被取消的 id（6）在轮首清空后同样不再误伤
    assert serve._cancel_last is None


def test_sigterm_exits_cleanly(tmp_path: Path, fixture_files: Any) -> None:
    """空闲阻塞在 stdin 上时收到 SIGTERM 应尽快退出（PEP 475 下只置标志位不会被检查）。"""
    import signal
    import subprocess
    import time

    from helpers import command_for

    proc = subprocess.Popen(
        command_for("dpe-git-connector"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)  # 让插件进入阻塞读
    proc.send_signal(signal.SIGTERM)
    try:
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_batch_accounting_includes_separators_and_envelope(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """批次记账必须含条目间逗号与信封开销（回归：漏记后接近条目上限的批次会顶破上限）。

    真实窗口出现在 ~1000 条、合计尺寸落在预留余量下方的窄区间；这里把 ``_ENVELOPE_RESERVE``
    缩小到 32 字节，用 30 个文件复现同一形状——否则驱动 1000 个文件要多跑约 80 秒（每个 blob
    一次 git 子进程，见 README「已知限制」）。
    """
    from dpe_git_connector import server as server_module
    from dpe_git_connector.server import _encoded_size

    monkeypatch.setattr(server_module, "_ENVELOPE_RESERVE", 32)
    reserve = 32
    repo = tmp_path / "repo"
    count = 30
    init_repo(repo, {f"f{index:03d}.txt": b"x\n" for index in range(count)})

    serve = Serve()
    params = initialize_params(repo)
    params["max_message_bytes"] = 1024 * 1024
    handle(serve, request(1, "initialize", params))
    items = handle(serve, request(2, "scan", {"cursor": None}))["result"]["items"]
    assert len(items) == count
    total = sum(_encoded_size(item) for item in items)

    # 上限刚好让「旧记账」（budget = limit − reserve ≥ total）放行整批、而实际响应超限
    limit = total + reserve + 2
    strict = Serve()
    strict_params = initialize_params(repo)
    strict_params["max_message_bytes"] = limit
    handle(strict, request(1, "initialize", strict_params))
    line = strict.handle(request(2, "scan", {"cursor": None}))
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit  # 含行尾 LF
    assert "result" in json.loads(line)


def test_oversize_error_message_reports_actual_numbers(tmp_path: Path) -> None:
    """超限条目的消息按实测口径写：报的是单条预算，不写「N 字节超过上限 M」这种失真关系。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"big.txt": "abc\n".join("x" * 3000 for _ in range(900)).encode()})
    from dpe_git_connector.server import _ENVELOPE_RESERVE

    serve = Serve()
    params = initialize_params(repo)
    params["max_message_bytes"] = 200_000
    handle(serve, request(1, "initialize", params))
    items = handle(serve, request(2, "scan", {"cursor": None}))["result"]["items"]
    assert items[0]["kind"] == "error"
    message = items[0]["message"]
    # 单条预算 = 上限 − 信封预留 − 响应里回显的 id 编码长度（这里 id=2 占 1 字节）
    assert f"单条预算 {200_000 - _ENVELOPE_RESERVE - 1}" in message
    assert "消息上限 200000" in message


def test_long_id_cannot_push_response_over_limit(tmp_path: Path) -> None:
    """id 由运行器给出：超长 id 的病理输入下响应仍不得越限（§6.2 双方 MUST NOT 发超限消息）。"""
    from dpe_git_connector.server import _ENVELOPE_RESERVE, _encoded_size

    repo = tmp_path / "repo"
    count = 30
    init_repo(repo, {f"f{index:03d}.txt": b"x\n" for index in range(count)})

    serve = Serve()
    params = initialize_params(repo)
    params["max_message_bytes"] = 1024 * 1024
    handle(serve, request(1, "initialize", params))
    items = handle(serve, request(2, "scan", {"cursor": None}))["result"]["items"]
    total = sum(_encoded_size(item) for item in items)

    # 上限落在「旧记账会放行整批、响应却因回显 2000 字节 id 而越限」的窗口
    limit = total + _ENVELOPE_RESERVE + 2
    strict = Serve()
    strict_params = initialize_params(repo)
    strict_params["max_message_bytes"] = limit
    handle(strict, request(1, "initialize", strict_params))
    long_id_request = json.dumps(
        {"jsonrpc": "2.0", "id": "x" * 2000, "method": "scan", "params": {"cursor": None}}
    )
    assert len(long_id_request.encode("utf-8")) + 1 <= limit  # 请求本身合法（在上限内）
    line = strict.handle(long_id_request)
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit


def test_unknown_method_echo_is_bounded(tmp_path: Path) -> None:
    """未知方法名不回显超长内容：接近上限的请求不会让错误响应越限。"""
    repo = tmp_path / "repo"
    init_repo(repo, {"a.md": b"# a\n"})
    serve = initialized_serve(repo)
    limit = serve.max_message_bytes
    long_method = "m" * (limit - 200)
    line = serve.handle(
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": long_method, "params": {}})
    )
    assert line is not None
    assert len(line.encode("utf-8")) + 1 <= limit
    assert json.loads(line)["error"]["code"] == -32601
