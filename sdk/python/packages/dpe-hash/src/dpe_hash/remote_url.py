"""remote URL 的文法（HTTP 绑定 §1）：客户端接受 remote URL 的唯一判定。

dpe-sdk 的传输层（``DpeClient`` / ``AsyncDpeClient`` 构造）、connector 运行器（实例定义的
``remote.url``，connector 契约 §4.4）与一致性跑分器（``REMOTE-URL`` 参数）共用本模块；放在
dpe-hash 是因为文法第 1 条就是 core §1.1 规范化的不动点，且跑分器只依赖本包。判定只看输入串
本身，不依赖任何 URL 库；与生成器 ``scripts/gen_vectors.py`` 的参考实现行为一致，由
``vectors/remote_url.json`` 逐例固定。文法消除了同一地址的写法差异（大小写、编码、默认端口、
尾部 ``/``、点段、IP 的不同文本形式），因此两个 remote URL 是否相同按字符串相等比较即可；DNS
别名这类字符串无法判定的重叠由服务端前缀授权兜底。
"""

from __future__ import annotations

import re

from dpe_hash.errors import ValidationError
from dpe_hash.uri import normalize_file_uri

__all__ = ["RemoteUrlError", "check_remote_url"]


class RemoteUrlError(ValueError):
    """remote URL 不符合 HTTP 绑定 §1 的文法。

    这是调用方的配置错误，不是协议错误：没有规范错误码，因此不属于 ``DpeHashError`` 体系。
    消息只说明违反了哪一条，不回显 URL（可能把凭证误写在 userinfo 里）。
    """


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
#: 主机名（第 5 条）：RFC 3986 的 reg-name 去掉 pct-encoded。
_HOST_CHARS = _UNRESERVED | _SUB_DELIMS
#: pchar 中除 pct-encoded 以外的字符（RFC 3986 §3.3）。
_PCHARS = _UNRESERVED | _SUB_DELIMS | frozenset(":@")
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_DEC_OCTET = re.compile(r"25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]|[0-9]")
_H16 = re.compile(r"[0-9A-Fa-f]{1,4}")
#: 数字形式的末标签（WHATWG URL 的 "ends in a number"）：全为十进制数字，或 0x 后接 hex。
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(_DEC_OCTET.fullmatch(part) for part in parts)


def _ipv6_groups(text: str) -> list[int] | None:
    """RFC 3986 §3.2.2 的 IPv6address（不含内嵌 IPv4、IPvFuture 与 zone）解析为 8 个 16 位组。"""
    if text.count("::") > 1:
        return None
    if "::" in text:
        head, tail = text.split("::")
        left = head.split(":") if head else []
        right = tail.split(":") if tail else []
        if len(left) + len(right) > 7:
            return None
        parts = left + ["0"] * (8 - len(left) - len(right)) + right
    else:
        parts = text.split(":")
        if len(parts) != 8:
            return None
    if not all(_H16.fullmatch(part) for part in parts):
        return None
    return [int(part, 16) for part in parts]


def _ipv6_text(groups: list[int]) -> str:
    """RFC 5952 §4 的推荐文本：hex 小写无前导 0，只压缩最长（≥2，并列取首段）的零组串。"""
    best_start, best_len = -1, 1
    i = 0
    while i < 8:
        if groups[i] == 0:
            j = i
            while j < 8 and groups[j] == 0:
                j += 1
            if j - i > best_len:
                best_start, best_len = i, j - i
            i = j
        else:
            i += 1
    parts = [format(group, "x") for group in groups]
    if best_start < 0:
        return ":".join(parts)
    return ":".join(parts[:best_start]) + "::" + ":".join(parts[best_start + best_len :])


def _ipv6(text: str) -> bool:
    """IPv6 字面量须恰为其 RFC 5952 §4 推荐文本（第 5 条）。"""
    groups = _ipv6_groups(text)
    return groups is not None and _ipv6_text(groups) == text


def _hostname(host: str) -> bool:
    """主机名（第 5 条）：非空标签以 "." 连接；末标签是数字形式时须为严格 IPv4。"""
    if any(ch not in _HOST_CHARS for ch in host):
        return False
    labels = host.split(".")
    if not all(labels):
        return False
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        return _ipv4(host)
    return True


def check_remote_url(url: str) -> str:
    """按 HTTP 绑定 §1 判定 remote URL，合法时原样返回。

    不合法抛 ``RemoteUrlError``（``ValueError`` 的子类）；消息只说明违反了哪一条，不回显
    URL（可能把凭证误写在 userinfo 里，connector 契约 §4.3「不外泄」）。
    """
    try:
        normalized = normalize_file_uri(url)
    except ValidationError:
        raise RemoteUrlError("remote URL 不是合法的 URI（core §1.1）") from None
    if normalized != url:
        raise RemoteUrlError(
            "remote URL 不是规范化形式（scheme 与 host 须小写、百分号编码的 hex 须大写、"
            "unreserved 字符不得编码，core §1.1）"
        )
    if "?" in url or "#" in url:
        raise RemoteUrlError("remote URL 不得含查询串或片段")
    scheme, _, rest = url.partition(":")
    if scheme not in _DEFAULT_PORTS:
        raise RemoteUrlError("remote URL 的 scheme 必须是 http 或 https")
    if not rest.startswith("//"):
        raise RemoteUrlError("remote URL 缺少 authority（须形如 http(s)://host…）")
    authority, slash, path = rest[2:].partition("/")
    path = slash + path
    if "@" in authority:
        raise RemoteUrlError("remote URL 不得含 userinfo（凭证经单独的凭证引用提供）")
    port: str | None
    if authority.startswith("["):
        close = authority.find("]")
        if close == -1 or not _ipv6(authority[1:close]):
            raise RemoteUrlError(
                "remote URL 的 IP 字面量须为 IPv6 地址的 RFC 5952 推荐文本"
                "（不接受 IPvFuture、zone 与内嵌 IPv4 的点分写法）"
            )
        after = authority[close + 1 :]
        if after and not after.startswith(":"):
            raise RemoteUrlError("remote URL 的 host 不合法")
        port = after[1:] if after else None
    else:
        host, colon, port_text = authority.partition(":")
        if not _hostname(host):
            raise RemoteUrlError(
                "remote URL 的主机名不合法（须为非空标签、不得百分号编码；"
                "末标签为数字时须为严格的四段 IPv4）"
            )
        port = port_text if colon else None
    if port is not None:
        if not _PORT.fullmatch(port) or int(port) > 65535:
            raise RemoteUrlError("remote URL 的端口须为 1–65535 的十进制数，且无前导 0")
        if port == _DEFAULT_PORTS[scheme]:
            raise RemoteUrlError("remote URL 不得显式写出 scheme 的默认端口（http 80、https 443）")
    if path:
        if path.endswith("/"):
            raise RemoteUrlError("remote URL 的路径不得以 / 结尾")
        for segment in path[1:].split("/"):
            # "%" 后的两位 HEXDIG 已由 core §1.1 的合法性判定保证
            if any(ch not in _PCHARS and ch != "%" for ch in segment):
                raise RemoteUrlError("remote URL 的路径含不允许的字符")
            if segment in (".", ".."):
                raise RemoteUrlError("remote URL 的路径不得含 . 或 .. 段")
    return url
