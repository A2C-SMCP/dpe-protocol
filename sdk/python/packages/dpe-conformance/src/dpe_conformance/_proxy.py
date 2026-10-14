"""代理只来自环境变量（#194）：判定规则的**自留副本**，原件是 ``dpe_sdk._proxy``。

跑分器不依赖 dpe-sdk（#26 的边界），代理判定又不是规范定义的契约、不进 dpe-hash（#194 架构
裁决），所以在这里保留一份。本文件除模块说明外与原件**逐字相同**：测试比较两者的 AST，并逐例
消费两个 SDK 共用的用例表 ``sdk/testdata/proxy_env.json``——改规则须先改原件、再同步这里。

规则（详见原件的模块说明）：只读 ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` /
``NO_PROXY``，从不读操作系统的代理设置；大写优先、ASCII 空白去掉后为空视为未设置；存在
``REQUEST_METHOD`` 时全部忽略；``NO_PROXY`` 支持 ``*``、域名及子域、IP 与 CIDR；代理 URL 按
固定文法校验，不合法抛 ``ValueError``（消息不回显值）。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping

__all__ = ["proxy_for"]

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

#: 两个 SDK 共用的空白字符集（Python 的 ``str.strip()`` 还会去掉 ``\x1c``–``\x1f`` 等，Rust 的
#: ``trim()`` 按 Unicode，二者都不用）
_WHITESPACE = " \t\n\r\x0b\x0c"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*", re.ASCII)
_LABEL = re.compile(r"[A-Za-z0-9\-]+", re.ASCII)
_PORT = re.compile(r"[0-9]{1,5}", re.ASCII)
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX].*", re.ASCII)
#: 代理 URL 的长度上限（字符数，此时都是 ASCII）：reqwest 连接时经 ``http::Uri`` 重新解析，超过
#: 其 65534 字节上限会静默直连——两侧都在构造时拒绝过长的值
_MAX_PROXY_URL = 4096
#: RFC 3986：URI 允许的字符（unreserved、gen-delims、sub-delims）与 pct-encoded
_URI = re.compile(r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*", re.ASCII)
#: RFC 3986 userinfo：unreserved、pct-encoded、sub-delims、``:``（没有 ``@``）
_USERINFO = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:]|%[0-9A-Fa-f]{2})*", re.ASCII)


def _lower(text: str) -> str:
    return text.translate(_ASCII_LOWER)


def _lookup(environ: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """先大写、后小写，取第一个非空值：返回（变量名, 去掉首尾 ASCII 空白的值）。"""
    for key in (name.upper(), name.lower()):
        value = environ.get(key, "").strip(_WHITESPACE)
        if value:
            return key, value
    return None


def _remote_scheme_and_host(remote: str) -> tuple[str, str]:
    """已按 HTTP 绑定 §1 校验过的 remote 的 scheme 与主机（IPv6 去掉方括号）：纯字符切分。"""
    scheme, _, rest = remote.partition("://")
    authority = rest.split("/", 1)[0]
    if authority.startswith("["):
        host = authority[1 : authority.find("]")]
    else:
        host = authority.partition(":")[0]
    return _lower(scheme), _lower(host)


def _address(text: str) -> _Address | None:
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if "%" in text:  # 带 zone 的 IPv6 不按地址处理（两个 SDK 同一口径）
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _network(entry: str) -> _Network | None:
    """IP 或 ``addr/前缀长度``（前缀只接受十进制数字，不接受掩码写法）。"""
    text, slash, prefix = entry.partition("/")
    address = _address(text)
    if address is None:
        return None
    if not slash:
        return ipaddress.ip_network(address)
    if not prefix.isascii() or not prefix.isdigit():
        return None
    # 先去掉前导零再看位数：去零后超过 3 位必然大于 128，直接判不匹配——不把任意长的数字串交给
    # int()（Python 对超过 4300 位的转换直接抛错，#206）。与 Rust 按 u32 解析的结论逐例一致：
    # 前导零照常接受，越界（含 u32 溢出）都不匹配
    digits = prefix.lstrip("0") or "0"
    if len(digits) > 3 or int(digits) > address.max_prefixlen:
        return None
    return ipaddress.ip_network((address, int(digits)), strict=False)


def _bypass(host: str, no_proxy: str) -> bool:
    """remote 的主机是否命中 ``NO_PROXY``。"""
    host_address = _address(host)
    for raw in no_proxy.split(","):
        entry = _lower(raw.strip(_WHITESPACE))
        if not entry:
            continue
        if entry == "*":
            return True
        network = _network(entry)
        if network is not None:
            if host_address is not None and host_address in network:
                return True
            continue
        domain = entry.removeprefix(".")
        if domain and host_address is None and (host == domain or host.endswith("." + domain)):
            return True
    return False


def _valid_host(host: str) -> bool:
    """代理主机：方括号内的 IPv6、点分十进制 IPv4，或 LDH 标签组成的域名（见模块说明）。"""
    if host.startswith("["):
        inner = host[1:-1] if host.endswith("]") else ""
        if not inner or "%" in inner:
            return False
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            return False
        return True
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return False  # 含空标签（空主机、尾点、连续点）与 LDH 之外的字符
    if any(_lower(label).startswith("xn--") for label in labels):
        return False
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
    return True


def _check_proxy_url(variable: str, url: str) -> tuple[str, int | None]:
    """按模块说明第 5 条校验补全 scheme 之后的代理 URL，返回（主机, 端口）（IPv6 带方括号，
    端口没写为 ``None``）；不合法抛 ``ValueError``。"""
    invalid = ValueError(f"环境变量 {variable} 的代理 URL 不合法")
    if len(url) > _MAX_PROXY_URL or not _URI.fullmatch(url):
        raise invalid
    scheme, _, rest = url.partition("://")
    if not _SCHEME.fullmatch(scheme):
        raise invalid  # 不回显：不合法的「scheme」里可能夹着凭证
    if _lower(scheme) not in ("http", "https"):
        raise ValueError(
            f"环境变量 {variable} 的代理 scheme {scheme!r} 不受支持（只支持 http / https）"
        )
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority, tail = rest[:end], rest[end:]
    if tail not in ("", "/"):
        raise invalid
    userinfo, at, hostport = authority.rpartition("@")
    if at and not _USERINFO.fullmatch(userinfo):
        raise invalid
    if hostport.startswith("["):
        close = hostport.find("]")
        host, port_part = (hostport[: close + 1], hostport[close + 1 :]) if close >= 0 else ("", "")
    else:
        host, colon, port = hostport.partition(":")
        port_part = colon + port
    if not _valid_host(host):
        raise invalid
    if not port_part:
        return host, None
    port = port_part[1:]
    if not port_part.startswith(":") or not _PORT.fullmatch(port) or int(port) > 65535:
        raise invalid
    return host, int(port)


def proxy_for(remote: str, environ: Mapping[str, str]) -> str | None:
    """按模块说明的规则判定 ``remote`` 应经的代理 URL；直连返回 ``None``。

    代理 URL 不合法或 scheme 不受支持时抛 ``ValueError``（消息不回显可能带凭证的值）。
    """
    if "REQUEST_METHOD" in environ:
        return None
    scheme, host = _remote_scheme_and_host(remote)
    found = _lookup(environ, f"{scheme}_proxy") or _lookup(environ, "all_proxy")
    if found is None:
        return None
    no_proxy = _lookup(environ, "no_proxy")
    if no_proxy is not None and _bypass(host, no_proxy[1]):
        return None
    variable, value = found
    url = value if "://" in value else "http://" + value
    _check_proxy_url(variable, url)
    return url
