"""增量游标（connector 契约 §6.8）：不透明、自包含，解释不了即回退全量轮。

游标记录上一轮成功产出时每个文件的内容摘要（``source`` 的说明）。下一轮据此只产出摘要变了
的文件，并把消失的文件表达为 delete / move（``server``）。

形态：``mdd1.`` + base64url（无填充）的 zlib 压缩 JSON
``{"v": 2, "src": <数据源摘要>, "files": {<b64 标识>: <摘要>}}``。

解释不了（前缀、编码、压缩、JSON 结构、版本不符，数据源摘要与本次配置不同，或被篡改成
深嵌套 / 压缩炸弹）一律返回 ``None``，调用方回退全量轮并标 ``incremental: false``——MUST NOT
因此报错（§6.8）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from collections.abc import Mapping
from typing import Any

__all__ = ["decode_cursor", "encode_cursor", "source_digest"]

_PREFIX = "mdd1."
#: 摘要算法变化时递增：旧游标在新算法下不可比较，按「解释不了」回退全量
_VERSION = 2
_DIGEST = re.compile(r"[0-9a-f]{32}")
#: 解压上限：按压缩长度的倍数收紧，被篡改的游标不能解压成超大数据
_MIN_DECOMPRESSED = 1 << 20
_EXPANSION = 16


def source_digest(uri_prefix: str, config: Mapping[str, Any]) -> str:
    """数据源摘要：实例前缀 + 实例配置。游标只在同一数据源下有效。"""
    canonical = json.dumps(
        {"uri_prefix": uri_prefix, "config": dict(config)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padded = (text + "=" * (-len(text) % 4)).encode("ascii")
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def encode_cursor(source: str, files: Mapping[bytes, str]) -> str:
    """把各文件的摘要编码成游标；同一输入总是得到同一字符串（键排序、紧凑编码）。"""
    body = {
        "v": _VERSION,
        "src": source,
        "files": {_b64encode(identifier): digest for identifier, digest in files.items()},
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _PREFIX + _b64encode(zlib.compress(raw, 9))


def decode_cursor(token: str, source: str) -> dict[bytes, str] | None:
    if not token.startswith(_PREFIX):
        return None
    try:
        compressed = _b64decode(token[len(_PREFIX) :])
        decompressor = zlib.decompressobj()
        limit = max(_MIN_DECOMPRESSED, _EXPANSION * len(compressed))
        raw = decompressor.decompress(compressed, limit)
        if decompressor.unconsumed_tail or decompressor.unused_data or not decompressor.eof:
            return None
        body: Any = json.loads(raw)
        return _parse(body, source)
    except (binascii.Error, ValueError, zlib.error, RecursionError, MemoryError):
        return None


def _parse(body: Any, source: str) -> dict[bytes, str] | None:
    if (
        not isinstance(body, dict)
        or set(body) != {"v", "src", "files"}
        or type(body["v"]) is not int
        or body["v"] != _VERSION
        or body["src"] != source
        or not isinstance(body["files"], dict)
    ):
        return None
    files: dict[bytes, str] = {}
    for key, digest in body["files"].items():
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            return None
        identifier = _b64decode(key)
        if not identifier or identifier in files:
            return None
        files[identifier] = digest
    return files
