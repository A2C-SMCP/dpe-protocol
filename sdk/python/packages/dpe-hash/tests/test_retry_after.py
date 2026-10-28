# vector-kind: retry_after
"""Retry-After 的解析（HTTP 绑定 §5）：``retry_after_seconds`` 逐例通过向量 retry_after.json。"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import retry_after_seconds


@pytest.fixture(scope="module")
def cases(vectors_dir: Path) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(
        (vectors_dir / "retry_after.json").read_text(encoding="utf-8")
    )
    assert data["kind"] == "retry_after"
    assert data["cases"]
    return list(data["cases"])


def test_vector_cases(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        now = datetime.fromisoformat(case["now"])
        got = retry_after_seconds(case["input"], now=now)
        expected = case["seconds"]
        label = f"{case['input']!r} @ {case['now']}"
        if expected is None:
            assert got is None, label
        elif expected == "infinity":
            assert got == math.inf, label
        else:
            assert got == float(expected), label


def test_missing_header_is_no_hint() -> None:
    assert retry_after_seconds(None) is None


def test_naive_now_is_utc() -> None:
    assert (
        retry_after_seconds("Thu, 08 Oct 2026 12:00:30 GMT", now=datetime(2026, 10, 8, 12)) == 30.0
    )


def test_fractional_now_is_kept_for_dates() -> None:
    """HTTP-date 相对精确的 now；50 年规则按整秒比较。"""
    now = datetime.fromisoformat("2026-10-08T12:00:00.250000+00:00")
    assert retry_after_seconds("Thu, 08 Oct 2026 12:00:30 GMT", now=now) == 29.75
