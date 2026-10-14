"""游标编码（connector 契约 §6.8：不透明、自包含，解释不了即回退全量轮）。"""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from dpe_git_connector.cursor import decode_cursor, encode_cursor, source_digest
from dpe_git_connector.mapping import RepoMark

SRC = source_digest("git://s/", {"root": "/srv"})
MARKS = {
    b"alpha": RepoMark("0" * 32, "1" * 32),
    b"\xff\xfe/non-utf8": RepoMark("2" * 32, None),  # 标识是原始路径字节
}


def test_round_trip_and_determinism() -> None:
    token = encode_cursor(SRC, MARKS)
    assert token.startswith("g1.")
    assert decode_cursor(token, SRC) == MARKS
    assert encode_cursor(SRC, dict(reversed(list(MARKS.items())))) == token  # 与插入顺序无关
    assert decode_cursor(encode_cursor(SRC, {}), SRC) == {}


def test_source_digest_covers_prefix_and_config() -> None:
    assert source_digest("git://s/", {"root": "/srv"}) == SRC
    assert source_digest("git://t/", {"root": "/srv"}) != SRC
    assert source_digest("git://s/", {"root": "/srv", "branches": ["dev"]}) != SRC
    assert decode_cursor(encode_cursor(SRC, MARKS), source_digest("git://t/", {})) is None


def _token(body: object) -> str:
    raw = json.dumps(body).encode()
    return "g1." + base64.urlsafe_b64encode(zlib.compress(raw)).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "g1.",
        "g2." + encode_cursor(SRC, MARKS)[3:],
        encode_cursor(SRC, MARKS)[:-4],  # 截断
        "g1.@@@@",
        "g1." + base64.urlsafe_b64encode(b"not zlib").decode(),
        _token([]),
        _token({"v": 2, "src": SRC, "repos": {}}),
        _token({"v": True, "src": SRC, "repos": {}}),
        _token({"v": 1.0, "src": SRC, "repos": {}}),
        _token({"v": 1, "src": SRC, "repos": {}, "extra": 1}),
        _token({"v": 1, "src": SRC, "repos": {"YQ": ["short", None]}}),
        _token({"v": 1, "src": SRC, "repos": {"YQ": ["0" * 32]}}),
        _token({"v": 1, "src": SRC, "repos": {"YQ": ["0" * 32, "X" * 32]}}),
        _token({"v": 1, "src": SRC, "repos": {"": ["0" * 32, None]}}),
        _token({"v": 1, "src": SRC, "repos": {"Y Q": ["0" * 32, None]}}),
    ],
)
def test_undecodable_cursors_are_rejected(token: str) -> None:
    assert decode_cursor(token, SRC) is None


def test_trailing_bytes_after_the_stream_are_rejected() -> None:
    """压缩流之后多出的字节（被篡改）不被静默忽略。"""
    raw = json.dumps({"v": 1, "src": SRC, "repos": {}}).encode()
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
