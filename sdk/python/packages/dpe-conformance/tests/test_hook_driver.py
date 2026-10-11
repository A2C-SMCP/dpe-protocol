"""钩子客户端：命令与 HTTP 两个入口的成功/失败判据，以及载荷契约。

HTTP 入口用 httpx 的同步 MockTransport 做单元级覆盖；对参考服务端真实钩子端点的联通
（HTTP 入口的端到端行为）由 ``test_cli`` 的 ``smoke::test_self_write_hook_is_usable``
以独立进程覆盖。
"""

from __future__ import annotations

import json
import sys

import httpx
from dpe_conformance.config import HookEndpoint
from dpe_conformance.hooks import HookDriver, self_write_payload

_HOOKS = "/__hooks__"


def _command(source: str) -> HookEndpoint:
    return HookEndpoint(command=(sys.executable, "-c", source))


def _http_client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport, base_url="http://dpe.test/")


def test_command_hook_success_and_body() -> None:
    endpoint = _command("import sys; sys.stdin.read(); print('{\"expired\": true}')")
    result = HookDriver(endpoint, httpx.Client()).call({"session_id": "s-1"})
    assert result.ok
    assert result.body == {"expired": True}


def test_command_hook_receives_payload_on_stdin() -> None:
    endpoint = _command(
        "import sys, json; payload = json.loads(sys.stdin.read()); print(json.dumps(payload))"
    )
    result = HookDriver(endpoint, httpx.Client()).call({"session_id": "s-9"})
    assert result.ok
    assert result.body == {"session_id": "s-9"}


def test_command_hook_non_json_stdout_gives_no_body() -> None:
    endpoint = _command("import sys; sys.stdin.read(); print('not json')")
    result = HookDriver(endpoint, httpx.Client()).call({})
    assert result.ok and result.body is None


def test_command_hook_nonzero_exit_is_failure() -> None:
    endpoint = _command("import sys; sys.stdin.read(); sys.stderr.write('boom'); sys.exit(3)")
    result = HookDriver(endpoint, httpx.Client()).call({"session_id": "s-1"})
    assert not result.ok
    assert "退出码 3" in result.detail and "boom" in result.detail


def test_command_hook_missing_binary_is_failure() -> None:
    result = HookDriver(HookEndpoint(command=("/nonexistent/hook",)), httpx.Client()).call({})
    assert not result.ok and "执行失败" in result.detail


def test_command_hook_timeout_is_failure() -> None:
    endpoint = _command("import time; time.sleep(5)")
    driver = HookDriver(endpoint, httpx.Client(), timeout=0.2)
    result = driver.call({})
    assert not result.ok and "执行失败" in result.detail


def test_http_hook_expire_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"{_HOOKS}/expire-session"
        assert json.loads(request.content) == {"session_id": "s-1"}
        return httpx.Response(200, json={"expired": False})

    driver = HookDriver(
        HookEndpoint(http=f"http://dpe.test{_HOOKS}/expire-session"),
        _http_client(httpx.MockTransport(handler)),
    )
    result = driver.call({"session_id": "s-1"})
    assert result.ok, result.detail
    assert result.body == {"expired": False}


def test_http_hook_self_write() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "created"})

    driver = HookDriver(
        HookEndpoint(http=f"http://dpe.test{_HOOKS}/self-write"),
        _http_client(httpx.MockTransport(handler)),
    )
    payload = self_write_payload(
        file_uri="dpe://conf/self-write",
        document={"file_type": "empty", "pages": []},
        if_absent=True,
    )
    result = driver.call(payload)
    assert result.ok, result.detail
    assert result.body == {"status": "created"}
    assert seen == [payload]


def test_http_hook_non_2xx_is_failure() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="no such hook"))
    driver = HookDriver(HookEndpoint(http="http://dpe.test/hook"), _http_client(transport))
    result = driver.call({})
    assert not result.ok and "404" in result.detail


def test_http_hook_non_json_body_gives_no_body() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="plain", headers={"content-type": "text/plain"})
    )
    driver = HookDriver(HookEndpoint(http="http://dpe.test/hook"), _http_client(transport))
    result = driver.call({})
    assert result.ok and result.body is None


def test_http_hook_unreachable_is_failure() -> None:
    transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )
    driver = HookDriver(HookEndpoint(http="http://dpe.test/hook"), _http_client(transport))
    result = driver.call({})
    assert not result.ok and "调用失败" in result.detail


def test_self_write_payload_members() -> None:
    payload = self_write_payload(
        file_uri="dpe://conf/x",
        document={"file_type": "md", "pages": []},
        if_absent=True,
        contract="dpe1",
    )
    assert payload == {
        "file_uri": "dpe://conf/x",
        "document": {"file_type": "md", "pages": []},
        "if_absent": True,
        "contract": "dpe1",
    }
