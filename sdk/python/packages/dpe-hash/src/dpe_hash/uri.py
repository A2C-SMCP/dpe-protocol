"""file_uri 的语法规范化（core.md §1.1）：唯一实现，内核与各服务端实现直接引用。

与生成器 ``scripts/gen_vectors.py`` 的参考实现行为一致，由
``vectors/file_uri_normalization.json`` 逐例固定（合法输入的输出、非法输入的错误码、幂等）。
零依赖、纯 ASCII 操作（非 ASCII 字符一律拒绝，见规范）。
"""

from __future__ import annotations

import re

from dpe_hash.errors import ValidationError

__all__ = ["normalize_file_uri"]

#: unreserved（RFC 3986 §2.3）与 reserved（§2.2）字符集；其余字符（含非 ASCII）一律非法。
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_RESERVED = frozenset(":/?#[]@!$&'()*+,;=")
_URI_CHARS = _UNRESERVED | _RESERVED
_HEXDIG = frozenset("0123456789abcdefABCDEF")
#: scheme（RFC 3986 §3.1）：ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) 后接 ":"。
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def normalize_file_uri(uri: str) -> str:
    """返回 ``uri`` 的语法规范化形式（core.md §1.1）；它就是身份的比较形式。

    合法性判定（封闭清单）：scheme 文法、全串字符集（``pct-encoded`` / ``unreserved`` /
    ``reserved``）、``%`` 后两位 HEXDIG；不校验更深的成分文法（host 是否合法 IP 等）。
    不合法（含非 ASCII、空格、控制字符、坏百分号三元组）抛 ``ValidationError``
    （``DPE_VALIDATION``）。

    变换三步：解码表示 unreserved 的百分号三元组 → scheme 与 host 中三元组之外的 ASCII
    字母小写 → 其余三元组的 hex 位大写（reserved 字符不解码）。不做点段移除与基于
    scheme / 协议的规范化（core.md §1.1）。结果幂等：``normalize(normalize(x)) ==
    normalize(x)``。
    """
    match = _SCHEME.match(uri)
    if match is None:
        raise ValidationError(f"file_uri 缺少合法 scheme：{uri!r}")
    scheme_end = match.end()

    # host 成分的范围：':' 后紧跟 "//" 才有 authority；authority 到第一个 "/"、"?"、"#" 为止，
    # host 是最后一个 "@"（userinfo 之后）到 port 之前的成分。三元组解码不改变这些边界
    # （unreserved 不含定界符），所以索引可以直接取自原串。
    host_start = host_end = scheme_end
    if uri.startswith("//", scheme_end):
        start = scheme_end + 2
        authority_end = len(uri)
        for ch in "/?#":
            at = uri.find(ch, start)
            if at != -1:
                authority_end = min(authority_end, at)
        at = uri.rfind("@", start, authority_end)
        host_start = at + 1 if at != -1 else start
        if host_start < authority_end and uri[host_start] == "[":
            close = uri.find("]", host_start, authority_end)
            host_end = authority_end if close == -1 else close + 1
        else:
            colon = uri.find(":", host_start, authority_end)
            host_end = authority_end if colon == -1 else colon

    out: list[str] = []
    i = 0
    n = len(uri)
    while i < n:
        ch = uri[i]
        if ch == "%":
            if i + 2 >= n or uri[i + 1] not in _HEXDIG or uri[i + 2] not in _HEXDIG:
                raise ValidationError(f"file_uri 的百分号编码不合法：{uri!r}")
            decoded = chr(int(uri[i + 1 : i + 3], 16))
            if decoded in _UNRESERVED:
                if host_start <= i < host_end and "A" <= decoded <= "Z":
                    decoded = chr(ord(decoded) + 32)
                out.append(decoded)
            else:
                out.append("%" + uri[i + 1 : i + 3].upper())
            i += 3
            continue
        if ch not in _URI_CHARS:
            raise ValidationError(f"file_uri 含不在 URI 字符集的字符 {ch!r}：{uri!r}")
        if (i < scheme_end or host_start <= i < host_end) and "A" <= ch <= "Z":
            ch = chr(ord(ch) + 32)
        out.append(ch)
        i += 1
    return "".join(out)
