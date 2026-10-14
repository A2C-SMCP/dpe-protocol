"""按目的地套用代理规则的 httpx 传输层（#194）：跑分器不读操作系统的代理设置。

httpx 缺省（``trust_env=True``）在 macOS / Windows 上环境变量为空时会回退读系统代理，对被测
服务端（含本机回环地址）的请求可能被悄悄送进系统代理。给客户端显式传入本传输层后，httpx 不再
自行探测代理；是否经代理、经哪个，由 ``dpe_conformance._proxy`` 的规则按**每个请求的目的地**
判定——target 与服务端钩子（``hooks``，可在另一个 origin）共用一个客户端，不能只按 target 判定
一次。环境变量在构造时快照一次。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

import httpx

from dpe_conformance._proxy import proxy_for

__all__ = ["EnvProxyTransport", "destination"]


def destination(url: httpx.URL | str) -> str:
    """规则按 scheme 与主机判定：把一个 URL 化成 ``scheme://主机/``（IPv6 带方括号）。

    主机取线上的 ASCII 形式（``raw_host``）：``host`` 会把 ``xn--`` 主机解码成 Unicode，与 SDK
    按原串字符切分的口径不同（``NO_PROXY`` 的 ``xn--`` 条目会因此匹配不上）。
    """
    parsed = httpx.URL(url)
    host = parsed.raw_host.decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}/"


class EnvProxyTransport(httpx.BaseTransport):
    """按目的地选择直连或经代理的 ``httpx.HTTPTransport``（同一代理复用同一个传输层）。

    代理 URL 不合法时 ``proxy_for`` 抛 ``ValueError``；跑分器在配置阶段已对 target 与每个 HTTP
    钩子预先判定过（见 ``plugin``），请求期不会遇到。
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = dict(environ)
        self._transports: dict[str | None, httpx.HTTPTransport] = {}
        self._lock = threading.Lock()  # 并发请求不重复创建（泄漏）同一代理的连接池

    def proxy_for(self, url: httpx.URL | str) -> str | None:
        """该目的地应经的代理 URL；直连为 ``None``。"""
        return proxy_for(destination(url), self._environ)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        proxy = self.proxy_for(request.url)
        with self._lock:
            transport = self._transports.get(proxy)
            if transport is None:
                transport = self._transports[proxy] = httpx.HTTPTransport(proxy=proxy)
        return transport.handle_request(request)

    def close(self) -> None:
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
        for transport in transports:
            transport.close()
