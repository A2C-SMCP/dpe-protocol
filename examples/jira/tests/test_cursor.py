"""增量游标：编解码往返、确定性，以及任何不符都回退（返回 None，不抛异常）。"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any

import pytest

from dpe_jira.cursor import decode_cursor, encode_cursor, source_digest

SOURCE = source_digest("jira://x/", {"site": "https://x.atlassian.net", "projects": ["ABC"]})
MARKS = {"10001": ("ABC-1", "2026-10-01T08:00:00.000+0000"), "7": ("ABC-0", "t")}


def raw_cursor(body: Any, prefix: str = "j1.") -> str:
    data = zlib.compress(json.dumps(body).encode())
    return prefix + base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_round_trip_and_determinism() -> None:
    token = encode_cursor(SOURCE, MARKS)
    assert token.startswith("j1.")
    assert decode_cursor(token, SOURCE) == MARKS
    assert encode_cursor(SOURCE, dict(reversed(list(MARKS.items())))) == token
    assert decode_cursor(encode_cursor(SOURCE, {}), SOURCE) == {}


def test_source_digest_depends_on_prefix_and_config() -> None:
    config = {"site": "https://x.atlassian.net", "projects": ["ABC"]}
    assert source_digest("jira://x/", config) == SOURCE
    assert source_digest("jira://y/", config) != SOURCE
    assert source_digest("jira://x/", {**config, "projects": ["ABD"]}) != SOURCE


def test_cursor_from_another_source_is_not_interpreted() -> None:
    other = source_digest("jira://y/", {"site": "https://x.atlassian.net", "projects": ["ABC"]})
    assert decode_cursor(encode_cursor(other, MARKS), SOURCE) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "garbage",
        "g1.abc",
        "j1.***",
        "j1." + base64.urlsafe_b64encode(b"not zlib").decode(),
        raw_cursor([1, 2]),
        raw_cursor({"v": 2, "src": SOURCE, "issues": {}}),
        raw_cursor({"v": True, "src": SOURCE, "issues": {}}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": {}, "extra": 1}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": []}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": {"abc": ["K-1", "t"]}}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": {"1": ["K-1"]}}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": {"1": ["", "t"]}}),
        raw_cursor({"v": 1, "src": SOURCE, "issues": {"1": ["K-1", 5]}}),
        encode_cursor(SOURCE, MARKS)[:-4],  # 截断
        encode_cursor(SOURCE, MARKS) + "AAAA",  # 流后多出字节
    ],
)
def test_malformed_cursors_fall_back(token: str) -> None:
    assert decode_cursor(token, SOURCE) is None


def test_decompression_is_bounded() -> None:
    bomb = zlib.compress(b"[" * (64 << 20), 9)  # 64 MiB 的嵌套括号，压缩后很小
    token = "j1." + base64.urlsafe_b64encode(bomb).decode().rstrip("=")
    assert decode_cursor(token, SOURCE) is None
