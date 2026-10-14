"""增量游标的编码（connector 契约 §6.8）：不透明、自包含，解释不了即回退全量轮。

游标记录**上一轮成功产出时各仓库的状态**——仓库标识 → ``RepoMark``（状态指纹 + 移动键），
外加数据源摘要（实例前缀与配置）与生成游标那一轮的远端单元素上限。下一轮据此只产出有变化
的仓库，并把「消失的仓库」表达为 delete / move（``server``）。

形态：``g1.`` + base64url（无填充）的 zlib 压缩 JSON
``{"v": 2, "src": <数据源摘要>, "limit": <max_payload_bytes | null>,
"repos": {<base64url 标识字节>: [指纹, 移动键 | null]}}``。

远端上限**不进数据源摘要**（#170）：上限变了，同一源状态下的产出可能不同（切分后仍超限的
守卫，``mapping.Limits``），但仓库集合不变——下一轮各仓库都重新扫描（不按指纹跳过），删除与
移动仍按上一轮的仓库集合表达，本轮仍是增量轮。

**v1 兼容**：v1 游标（``{"v": 1, "src", "repos"}``，摘要里含上限）只在「按本次上限算出的 v1
摘要」与之相等时解释，视为在本次上限下生成；上限也变了就解释不了、回退一次全量轮。这样常见
的升级（上限没变）不会在升级那一轮丢掉删除与移动。

- **不透明**：运行器只原样保存与回传（§6.8 MUST NOT 解析）；
- **自包含**：插件重启后仍可解释，不依赖进程内状态；
- **解释不了即回退**：前缀、编码、压缩、JSON 结构、版本任一不符，或数据源摘要与本次配置
  不同（配置变化本应由运行器按内容身份丢弃游标，这里再挡一层），``decode`` 返回 ``None``，
  调用方按全量轮处理并标 ``incremental: false``——MUST NOT 因此报错（§6.8）。

仓库标识是原始路径字节（可能不是 UTF-8），因此按 base64url 存放。摘要只用于比较相等，截断为
128 位（``mapping._digest``）。
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

from dpe_git_connector.mapping import RepoMark

__all__ = ["decode_cursor", "encode_cursor", "legacy_source_digest", "source_digest"]

_PREFIX = "g1."
_VERSION = 2
#: 指纹与移动键的形态（``mapping._digest``：128 位小写十六进制）
_DIGEST = re.compile(r"[0-9a-f]{32}")
#: 解压后的上限：真实游标是随机摘要，几乎压不动（约 2:1）；按压缩长度的倍数收紧，被篡改的
#: 游标（压缩炸弹）不能解压成超大数据
_MIN_DECOMPRESSED = 1 << 20
_EXPANSION = 16


def source_digest(uri_prefix: str, config: Mapping[str, Any]) -> str:
    """数据源摘要：实例前缀 + 实例配置（JCS）。游标只在同一数据源下有效；上限另记（见模块说明）。"""
    canonical = jcs({"uri_prefix": uri_prefix, "config": dict(config)})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def legacy_source_digest(
    uri_prefix: str, config: Mapping[str, Any], max_payload_bytes: int | None
) -> str:
    """v1 游标的数据源摘要（实例前缀 + 配置 + 远端上限），只用于解释升级前留下的游标。"""
    canonical = jcs(
        {"uri_prefix": uri_prefix, "config": dict(config), "max_payload_bytes": max_payload_bytes}
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    # validate=True：非字母表字符一律拒绝（缺省会静默丢弃，被篡改的游标可能被「解释」出来）
    padded = (text + "=" * (-len(text) % 4)).encode("ascii")
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def encode_cursor(source: str, limit: int | None, repos: Mapping[bytes, RepoMark]) -> str:
    """把各仓库的记录编码成游标；同一输入总是得到同一字符串（键排序、紧凑编码）。

    ``limit`` 是本轮使用的远端单元素上限（``remote_limits.max_payload_bytes``，未提供为 ``None``）。
    """
    body = {
        "v": _VERSION,
        "src": source,
        "limit": limit,
        "repos": {
            _b64encode(identifier): [mark.fingerprint, mark.move_key]
            for identifier, mark in repos.items()
        },
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _PREFIX + _b64encode(zlib.compress(raw, 9))


def decode_cursor(
    token: str, source: str, legacy: tuple[str, int | None] | None = None
) -> tuple[dict[bytes, RepoMark], int | None] | None:
    """解释游标，返回（各仓库的记录, 生成游标那一轮的远端上限）；任何不符都返回 ``None``
    （调用方回退全量轮，不报错）。

    ``legacy`` 是（按本次上限算出的 v1 摘要, 本次上限）：给出时，摘要相符的 v1 游标按在本次
    上限下生成解释（见模块说明）。
    """
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
        # RecursionError：深嵌套的 JSON（被篡改）；同样只是「解释不了」，不得让本轮报错
        return None
    if not isinstance(body, dict) or "v" not in body or type(body["v"]) is not int:
        return None
    remote_limit: int | None
    if body["v"] == 1 and legacy is not None:
        if set(body) != {"v", "src", "repos"} or body["src"] != legacy[0]:
            return None
        remote_limit = legacy[1]
    elif (
        body["v"] != _VERSION
        or set(body) != {"v", "src", "limit", "repos"}
        or body["src"] != source
        or not _valid_limit(body["limit"])
    ):
        return None
    else:
        remote_limit = body["limit"]
    if not isinstance(body["repos"], dict):
        return None
    repos: dict[bytes, RepoMark] = {}
    for key, value in body["repos"].items():
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or not _DIGEST.fullmatch(value[0])
            or not (value[1] is None or (isinstance(value[1], str) and _DIGEST.fullmatch(value[1])))
        ):
            return None
        try:
            identifier = _b64decode(key)
        except (binascii.Error, ValueError):
            return None
        if not identifier or identifier in repos:
            return None
        repos[identifier] = RepoMark(value[0], value[1])
    return repos, remote_limit


def _valid_limit(value: Any) -> bool:
    """``limit``：null 或正整数（bool 不算整数）。"""
    return value is None or (type(value) is int and value > 0)
