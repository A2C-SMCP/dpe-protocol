"""代理只来自环境变量（#132）：``dpe_sdk._proxy`` 的判定规则，以及客户端经真实 TCP 的行为。

判定规则逐例消费两个 SDK 共用的 ``sdk/testdata/proxy_env.json``（Rust 侧 ``proxy.rs`` 同表）。
客户端行为经真实设施：remote 是子进程参考服务端，代理是本文件起的本地 HTTP 代理（转发、拒绝
隧道、回 502 三种模式，记录收到的请求行）——不 mock 掉承载代理行为的 httpx 传输层。
"""

from __future__ import annotations

import ipaddress
import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from dpe_sdk import protocol as protocol_module
from dpe_sdk._proxy import _check_proxy_url, proxy_for
from dpe_sdk.protocol import Request
from dpe_sdk.transport import AsyncDpeClient, DpeClient, not_sent
from proxy_helpers import ProxyServer, start_proxy
from server_helpers import spawn_server

CASES = Path(__file__).resolve().parents[4] / "testdata" / "proxy_env.json"
_TABLE: list[dict[str, Any]] = json.loads(CASES.read_text(encoding="utf-8"))["cases"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


#: 判定会读到的全部变量：真实设施用例先清掉，避免受运行测试的机器环境影响
_PROXY_VARS = [
    name
    for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    for name in (base, base.lower())
] + ["REQUEST_METHOD"]


@pytest.mark.parametrize("case", _TABLE, ids=[c["name"] for c in _TABLE])
def test_shared_proxy_table(case: dict[str, Any]) -> None:
    if case["expected"] == "error":
        with pytest.raises(ValueError) as excinfo:
            proxy_for(case["remote"], case["env"])
        for value in case["env"].values():  # 消息不回显变量的值（可能带凭证）
            if "://" in value:
                assert value not in str(excinfo.value)
    else:
        assert proxy_for(case["remote"], case["env"]) == case["expected"]
        if case["expected"] is not None:
            _assert_httpx_agrees(case["expected"])


def _assert_httpx_agrees(url: str) -> None:
    """文法接受的代理 URL，httpx 也必须接受，且解析出的主机与端口与文法切出的一致——否则同一
    个值在两个 SDK 里可能连到不同的代理（如 WHATWG 把 ``\\`` 当作 ``/``）。"""
    host, port = _check_proxy_url("HTTPS_PROXY", url)
    httpx.HTTPTransport(proxy=url).close()
    parsed = httpx.Proxy(url).url
    if host.startswith("["):
        assert ipaddress.IPv6Address(parsed.host) == ipaddress.IPv6Address(host[1:-1]), url
    else:
        assert parsed.host == host.lower(), url
    default = 443 if url.lower().startswith("https://") else 80
    assert (parsed.port or default) == (port or default), url


def test_unsupported_scheme_message_does_not_echo_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS_PROXY") as excinfo:
        proxy_for("https://dpe.example.com/r", {"HTTPS_PROXY": "socks5://user:s3cr3t@p:1080"})
    assert "s3cr3t" not in str(excinfo.value)
    # 「scheme」位置不合文法（夹着 userinfo）：只报不合法，不回显
    with pytest.raises(ValueError, match="HTTPS_PROXY") as excinfo:
        proxy_for("https://dpe.example.com/r", {"HTTPS_PROXY": "user:s3cr3t@socks5://p:1080"})
    assert "s3cr3t" not in str(excinfo.value)


@pytest.fixture
def proxy() -> Iterator[ProxyServer]:
    yield from start_proxy("forward")


@pytest.fixture
def remote() -> Iterator[str]:
    with spawn_server("--prefix", "/r/1") as info:
        yield str(info["url"])


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------- 客户端经真实 TCP


def test_env_proxy_is_used(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str
) -> None:
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    with DpeClient(remote) as client:
        client.capabilities()
    assert proxy.seen == [f"GET {remote}/capabilities"]


@pytest.mark.anyio
async def test_env_proxy_is_used_by_async_client(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str
) -> None:
    monkeypatch.setenv("http_proxy", proxy.url)
    async with AsyncDpeClient(remote) as client:
        await client.capabilities()
    assert proxy.seen == [f"GET {remote}/capabilities"]


def test_no_proxy_connects_directly(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str
) -> None:
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with DpeClient(remote) as client:
        client.capabilities()
    assert proxy.seen == []


@pytest.mark.anyio
@pytest.mark.parametrize("client_type", ["sync", "async"])
async def test_system_proxy_settings_are_ignored(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str, client_type: str
) -> None:
    """模拟「系统设了代理、环境变量没设」：httpx 缺省会经 ``getproxies()`` 读到它（macOS /
    Windows 的系统代理设置即由此进入），客户端仍然直连。"""
    system = {"http": proxy.url, "https": proxy.url, "all": proxy.url}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: system)
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: system)
    # 先证明模拟生效：httpx 缺省客户端确实会走这个「系统代理」
    with httpx.Client() as plain:
        plain.get(f"{remote}/capabilities")
    assert proxy.seen == [f"GET {remote}/capabilities"]
    proxy.seen.clear()
    if client_type == "sync":
        with DpeClient(remote) as client:
            client.capabilities()
    else:
        async with AsyncDpeClient(remote) as aclient:
            await aclient.capabilities()
    assert proxy.seen == []


def test_empty_uppercase_no_proxy_falls_back_to_lowercase(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str
) -> None:
    """空的大写 ``NO_PROXY`` 视为未设置，取小写 ``no_proxy`` → 直连（与 Rust 侧
    ``reqwests_own_proxy_detection_is_off`` 同一用例）。"""
    monkeypatch.setenv("http_proxy", proxy.url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with DpeClient(remote) as client:
        client.capabilities()
    assert proxy.seen == []


def test_unreachable_proxy_is_not_sent(monkeypatch: pytest.MonkeyPatch, remote: str) -> None:
    """连不上代理是建连失败：确定未发出（端口 1 在测试机上没有监听者）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with DpeClient(remote, timeout=5) as client, pytest.raises(httpx.ConnectError) as excinfo:
        client.capabilities()
    assert protocol_module._attempt_not_sent(excinfo.value)


@pytest.mark.anyio
@pytest.mark.parametrize("client_type", ["sync", "async"])
async def test_rejected_tunnel_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, client_type: str
) -> None:
    """HTTPS 经代理、隧道被拒（CONNECT 应答 403）：请求还没到 remote，判为确定未发出；
    传输层按「确定未发出」原样重发（每次都先建隧道）。失败发生在 TLS 之前，不需要证书。"""
    monkeypatch.setenv("HTTPS_PROXY", proxy.url)
    remote = "https://dpe.invalid/r/1"
    with pytest.raises(httpx.ProxyError) as excinfo:
        if client_type == "sync":
            with DpeClient(remote, max_retries=1) as client:
                client.capabilities()
        else:
            async with AsyncDpeClient(remote, max_retries=1) as aclient:
                await aclient.capabilities()
    assert not_sent(excinfo.value)
    assert protocol_module._attempt_not_sent(excinfo.value)
    assert proxy.seen == ["CONNECT dpe.invalid:443"] * 2


def test_bad_gateway_from_forwarding_proxy_is_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """明文 HTTP 经代理转发时，代理的 502 是普通响应，不是 ``ProxyError``、不判为确定未发出
    （代理可能已把请求转给了 remote）。"""
    for server in start_proxy("bad_gateway"):
        monkeypatch.setenv("HTTP_PROXY", server.url)
        with DpeClient("http://dpe.invalid/r/1", max_retries=0) as client:
            response = client.send(Request("GET", "capabilities", retry_safe=True))
        assert response.status == 502
        assert server.seen == ["GET http://dpe.invalid/r/1/capabilities"]


def test_unsupported_proxy_scheme_is_rejected_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "socks5://user:s3cr3t@127.0.0.1:1080")
    with pytest.raises(ValueError, match="HTTPS_PROXY") as excinfo:
        DpeClient("https://dpe.example.com/r")
    assert "s3cr3t" not in str(excinfo.value)
    with pytest.raises(ValueError, match="HTTPS_PROXY"):
        AsyncDpeClient("https://dpe.example.com/r")


def test_explicit_transport_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方传入的传输层原样使用：不判定代理（不支持的 scheme 也不报错）。"""
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    with DpeClient("https://dpe.example.com/r", transport=httpx.MockTransport(handler)) as client:
        assert client.send(Request("GET", "capabilities", retry_safe=True)).status == 404
    assert seen == ["https://dpe.example.com/r/capabilities"]
