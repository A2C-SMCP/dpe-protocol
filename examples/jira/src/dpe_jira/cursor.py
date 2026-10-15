"""增量游标的编码（connector 契约 §6.8）：不透明、自包含，解释不了即回退全量轮。

游标记录**上一轮成功产出时范围内的每个工单**——数字 id → ``[key, updated]``，外加数据源摘要
（实例前缀与配置）。下一轮据此只取 updated 变了的工单，并把消失的工单表达为 delete、key 变了的
工单表达为 move（``server``）。

形态：``j1.`` + base64url（无填充）的 zlib 压缩 JSON
``{"v": 1, "src": <数据源摘要>, "issues": {<id>: [<key>, <updated>]}}``。``updated`` 原样保存
搜索返回的字符串，只用于比较相等。

- **不透明**：运行器只原样保存与回传（§6.8 MUST NOT 解析）；
- **自包含**：插件重启后仍可解释，不依赖进程内状态；
- **解释不了即回退**：前缀、编码、压缩、JSON 结构、版本任一不符，或数据源摘要与本次配置不同
  （配置变化本应由运行器丢弃游标，这里再挡一层），``decode_cursor`` 返回 ``None``，调用方按
  全量轮处理并标 ``incremental: false``——MUST NOT 因此报错（§6.8）。
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

from dpe_hash import jcs

__all__ = ["Marks", "decode_cursor", "encode_cursor", "is_issue_id", "source_digest"]

#: 工单 id → (key, updated)
Marks = dict[str, tuple[str, str]]

_PREFIX = "j1."
_VERSION = 1
_ISSUE_ID = re.compile(r"[0-9]{1,19}")
#: 解压后的上限：工单记录重复度高、压缩比可达十几倍，按压缩长度的倍数放宽但仍有界，被篡改的
#: 游标（压缩炸弹）不能解压成超大数据
_MIN_DECOMPRESSED = 1 << 20
_EXPANSION = 64


def is_issue_id(value: str) -> bool:
    """工单 id 的形态（ASCII 十进制、至多 19 位）；枚举与游标用同一判据，免得写进游标的 id
    下一轮解释不了、每轮都回退全量。"""
    return _ISSUE_ID.fullmatch(value) is not None


def source_digest(uri_prefix: str, config: Mapping[str, Any]) -> str:
    """数据源摘要：实例前缀 + 实例配置（JCS）。游标只在同一数据源下有效。"""
    canonical = jcs({"uri_prefix": uri_prefix, "config": dict(config)})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    # validate=True：非字母表字符一律拒绝（缺省会静默丢弃，被篡改的游标可能被「解释」出来）
    padded = (text + "=" * (-len(text) % 4)).encode("ascii")
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def encode_cursor(source: str, marks: Mapping[str, tuple[str, str]]) -> str:
    """把各工单的记录编码成游标；同一输入总是得到同一字符串（键排序、紧凑编码）。"""
    body = {
        "v": _VERSION,
        "src": source,
        "issues": {issue_id: [key, updated] for issue_id, (key, updated) in marks.items()},
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _PREFIX + _b64encode(zlib.compress(raw.encode("utf-8"), 9))


def decode_cursor(token: str, source: str) -> Marks | None:
    """解释游标，返回各工单的记录；任何不符都返回 ``None``（调用方回退全量轮，不报错）。"""
    if not token.startswith(_PREFIX):
        return None
    try:
        compressed = _b64decode(token[len(_PREFIX) :])
        decompressor = zlib.decompressobj()
        limit = max(_MIN_DECOMPRESSED, _EXPANSION * len(compressed))
        raw = decompressor.decompress(compressed, limit)
        # 只接受恰好一个完整的压缩流：截断、超限或流后多出字节（被篡改）都不解释
        if decompressor.unconsumed_tail or decompressor.unused_data or not decompressor.eof:
            return None
        body: Any = json.loads(raw)
    except (binascii.Error, ValueError, zlib.error, RecursionError, MemoryError):
        return None
    if (
        not isinstance(body, dict)
        or set(body) != {"v", "src", "issues"}
        or type(body["v"]) is not int
        or body["v"] != _VERSION
        or body["src"] != source
        or not isinstance(body["issues"], dict)
    ):
        return None
    marks: Marks = {}
    for issue_id, value in body["issues"].items():
        if (
            not is_issue_id(issue_id)
            or not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(part, str) and part for part in value)
        ):
            return None
        marks[issue_id] = (value[0], value[1])
    return marks
