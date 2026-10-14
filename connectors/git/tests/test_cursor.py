"""游标编码（connector 契约 §6.8：不透明、自包含，解释不了即回退全量轮）。"""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from dpe_git_connector.cursor import (
    decode_cursor,
    encode_cursor,
    legacy_source_digest,
    source_digest,
)
from dpe_git_connector.mapping import RepoMark

SRC = source_digest("git://s/", {"root": "/srv"})
MARKS = {
    b"alpha": RepoMark("0" * 32, "1" * 32),
    b"\xff\xfe/non-utf8": RepoMark("2" * 32, None),  # 标识是原始路径字节
}


def test_round_trip_and_determinism() -> None:
    token = encode_cursor(SRC, 4096, MARKS)
    assert token.startswith("g1.")
    assert decode_cursor(token, SRC) == (MARKS, 4096)
    # 与插入顺序无关
    assert encode_cursor(SRC, 4096, dict(reversed(list(MARKS.items())))) == token
    assert decode_cursor(encode_cursor(SRC, None, {}), SRC) == ({}, None)


def test_source_digest_covers_prefix_and_config() -> None:
    assert source_digest("git://s/", {"root": "/srv"}) == SRC
    assert source_digest("git://t/", {"root": "/srv"}) != SRC
    assert source_digest("git://s/", {"root": "/srv", "branches": ["dev"]}) != SRC
    assert decode_cursor(encode_cursor(SRC, None, MARKS), source_digest("git://t/", {})) is None


def test_remote_limit_is_recorded_beside_the_digest() -> None:
    """上限不进数据源摘要（#170）：换了上限，游标照样可解释，并带回生成时的上限。"""
    token = encode_cursor(SRC, 1024, MARKS)
    assert decode_cursor(token, SRC) == (MARKS, 1024)


def test_v1_cursors_fall_back_once() -> None:
    """不给 legacy 时 v1 游标（上限算在摘要里）解释不了。"""
    assert decode_cursor(_token({"v": 1, "src": SRC, "repos": {}}), SRC) is None


def test_v1_cursor_under_the_same_limit_is_still_understood() -> None:
    """v1 兼容：按本次上限算出的 v1 摘要相符，就按本次上限下生成解释；上限变了才回退。"""
    legacy = legacy_source_digest("git://s/", {"root": "/srv"}, 4096)
    token = _token({"v": 1, "src": legacy, "repos": {"YWxwaGE": ["0" * 32, "1" * 32]}})
    assert decode_cursor(token, SRC, legacy=(legacy, 4096)) == (
        {b"alpha": RepoMark("0" * 32, "1" * 32)},
        4096,
    )
    other = legacy_source_digest("git://s/", {"root": "/srv"}, 8192)
    assert decode_cursor(token, SRC, legacy=(other, 8192)) is None
    # v1 不带 limit 成员；多出成员一律不解释
    extra = _token({"v": 1, "src": legacy, "limit": 4096, "repos": {}})
    assert decode_cursor(extra, SRC, legacy=(legacy, 4096)) is None


def _token(body: object) -> str:
    raw = json.dumps(body).encode()
    return "g1." + base64.urlsafe_b64encode(zlib.compress(raw)).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "g1.",
        "g2." + encode_cursor(SRC, None, MARKS)[3:],
        encode_cursor(SRC, None, MARKS)[:-4],  # 截断
        "g1.@@@@",
        "g1." + base64.urlsafe_b64encode(b"not zlib").decode(),
        _token([]),
        _token({"v": 2, "src": SRC, "repos": {}}),  # 缺 limit
        _token({"v": 3, "src": SRC, "limit": None, "repos": {}}),
        _token({"v": True, "src": SRC, "limit": None, "repos": {}}),
        _token({"v": 2.0, "src": SRC, "limit": None, "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {}, "extra": 1}),
        _token({"v": 2, "src": SRC, "limit": True, "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": 0, "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": -1, "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": 1.5, "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": "4096", "repos": {}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {"YQ": ["short", None]}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {"YQ": ["0" * 32]}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {"YQ": ["0" * 32, "X" * 32]}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {"": ["0" * 32, None]}}),
        _token({"v": 2, "src": SRC, "limit": None, "repos": {"Y Q": ["0" * 32, None]}}),
    ],
)
def test_undecodable_cursors_are_rejected(token: str) -> None:
    assert decode_cursor(token, SRC) is None


def test_trailing_bytes_after_the_stream_are_rejected() -> None:
    """压缩流之后多出的字节（被篡改）不被静默忽略。"""
    raw = json.dumps({"v": 2, "src": SRC, "limit": None, "repos": {}}).encode()
    for extra in (b"x", b"xy", b"xyz", b"\x00" * 7):
        payload = zlib.compress(raw) + extra
        token = "g1." + base64.urlsafe_b64encode(payload).decode().rstrip("=")
        assert decode_cursor(token, SRC) is None, extra


def _raw_token(raw: bytes) -> str:
    return "g1." + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def test_hostile_cursors_fall_back_instead_of_failing() -> None:
    """被篡改的游标不得让本轮报错或撑爆内存（§6.8：解释不了即回退全量轮）。"""
    deep = _raw_token(b"[" * 200_000)  # 深嵌套：json 解析递归过深
    assert decode_cursor(deep, SRC) is None
    bomb = _raw_token(b"[" + b"0," * (8 << 20) + b"0]")  # 高压缩比：解压上限按长度收紧
    assert len(bomb) < 64 * 1024
    assert decode_cursor(bomb, SRC) is None
