"""支持层单元：构造出的期望 hash 必须与一致性向量逐字一致（只经 dpe-hash），
problem 断言对构造规则的把关，以及缺省前缀列举检查各分支的桩覆盖。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from dpe_conformance.checks import read as read_checks
from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import assert_problem, build_document, text_element


def _vector(vectors_dir: Path, name: str) -> dict[str, Any]:
    vector: dict[str, Any] = json.loads((vectors_dir / f"{name}.json").read_text(encoding="utf-8"))
    return vector


def test_element_hash_matches_vector(vectors_dir: Path) -> None:
    """构造的单元素页，其元素 hash 与 vectors/element_text_basic 逐字一致。"""
    vector = _vector(vectors_dir, "element_text_basic")
    expected = vector["expected"]["doc"]["dpe1"]["pages"][0]["elements"]
    document = build_document([[{"category": "NarrativeText", "text": "Hello, DPE."}]])
    assert document.pages[0]["elements"] == expected
    assert document.objects == [{"category": "NarrativeText", "text": "Hello, DPE."}]


def test_empty_document_hash_matches_vector(vectors_dir: Path) -> None:
    """空文档（0 页）的 doc_hash 与 vectors/empty_document 的 no_pages 期望一致。"""
    vector = _vector(vectors_dir, "empty_document")
    expected = vector["expected"]["no_pages"]["dpe1"]["doc_hash"]
    document = build_document([], file_type="empty")
    assert document.doc_hash == expected
    assert document.commit_body() == {
        "document": {"file_type": "empty", "pages": []},
        "pages": [],
        "objects": [],
    }


def test_metadata_and_title_change_hash() -> None:
    base = build_document([[text_element("x")]])
    richer = build_document([[text_element("x")]], title="t", doc_metadata={"a": 1})
    assert base.doc_hash != richer.doc_hash
    assert base.document != richer.document


def test_content_multiset_counts_across_pages() -> None:
    """delta 的计数单位：跨页展平的元素 content_hash 多重集（重复元素计多次）。"""
    document = build_document([[text_element("a"), text_element("b")], [text_element("a")]])
    first, second = document.pages[0]["elements"]
    assert document.content_multiset() == Counter({first: 2, second: 1})


def _problem_response(
    status: int, code: str, *, documents_path: bool = True, **overrides: Any
) -> httpx.Response:
    kebab = code.removeprefix("DPE_").lower().replace("_", "-")
    body: dict[str, Any] = {
        "type": f"urn:dpe:error:{kebab}",
        "title": kebab.replace("-", " "),
        "status": status,
        "code": code,
        "retryable": code in ("DPE_RATE_LIMITED", "DPE_UNAVAILABLE"),
    }
    body.update(overrides)
    headers = {"DPE-Error-Code": code, "content-type": "application/problem+json"}
    if documents_path:
        headers["Vary"] = "DPE-Hash-Contract"
    request = httpx.Request("PUT", "http://dpe.test/documents?uri=x")
    return httpx.Response(status, json=body, headers=headers, request=request)


def test_assert_problem_accepts_conforming_response() -> None:
    response = _problem_response(400, "DPE_VALIDATION")
    assert assert_problem(response, status=400, code="DPE_VALIDATION")["code"] == "DPE_VALIDATION"


@pytest.mark.parametrize(
    ("overrides", "documents_path"),
    [
        ({"title": "Wrong Title"}, True),  # title 不符构造规则
        ({"type": "about:blank"}, True),  # type 不符构造规则
        ({}, False),  # documents 路径的错误响应缺 Vary
    ],
)
def test_assert_problem_rejects_violations(overrides: dict[str, Any], documents_path: bool) -> None:
    response = _problem_response(400, "DPE_VALIDATION", documents_path=documents_path, **overrides)
    with pytest.raises(AssertionError):
        assert_problem(response, status=400, code="DPE_VALIDATION")


# ------------------------------------------------ test_list_missing_prefix_lists_all 的分支桩


_OTHER_HASH = "dpe1:" + "f" * 64
_OURS = "dpe://conformance/stub/read/all"


def _other_entry(index: int) -> dict[str, str]:
    return {"file_uri": f"dpe://other/{index}", "doc_hash": _OTHER_HASH}


class _StubListTarget:
    """最小桩 Target：list 按脚本分页应答；PUT 与 capabilities 定答。

    ``endless=True`` 时持续产出新游标与新条目（模拟规模超出预算的服务端）。
    """

    def __init__(
        self, pages: list[tuple[str | None, list[dict[str, str]]]], *, endless: bool = False
    ) -> None:
        self._pages = list(pages)
        self._endless = endless
        self._counter = 0

    def uri(self, path: str) -> str:
        return "dpe://conformance/stub/" + path.lstrip("/")

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, f"http://dpe.test/{path}")
        if method == "PUT":
            return httpx.Response(201, json={}, request=request)
        if path == "capabilities":
            return httpx.Response(200, json={"limits": {"list_page_max": 10}}, request=request)
        if method == "GET" and path == "documents":
            if self._endless:
                cursor: str | None = f"c{self._counter}"
                entries = [_other_entry(self._counter)]
                self._counter += 1
            else:
                cursor, entries = self._pages.pop(0)
            return httpx.Response(
                200,
                json={"documents": entries, "next_cursor": cursor},
                headers={"Vary": "DPE-Hash-Contract"},
                request=request,
            )
        raise AssertionError(f"桩未覆盖：{method} {path}")


def _stub_identity() -> Identity:
    return Identity(role="full", authorization="Bearer stub")


def _run_stub_check(stub: _StubListTarget) -> None:
    read_checks.test_list_missing_prefix_lists_all(cast(Target, stub), _stub_identity())


def test_stub_list_finds_document_within_budget() -> None:
    """翻页途中见到本次写入的文档与一致的 doc_hash 即通过（不需翻到 null）。"""
    ours = {"file_uri": _OURS, "doc_hash": read_checks._document().doc_hash}
    _run_stub_check(_StubListTarget([("c1", [_other_entry(1)]), (None, [ours])]))


def test_stub_list_null_without_document_fails() -> None:
    """游标到 null 仍未列出该文档 → 判失败（缺省前缀必须列出全部）。"""
    with pytest.raises(AssertionError, match="游标已到 null"):
        _run_stub_check(_StubListTarget([(None, [_other_entry(1)])]))


def test_stub_list_duplicate_entry_fails() -> None:
    """同一条目出现两次 → 真实协议违例，判失败。"""
    entry = _other_entry(1)
    with pytest.raises(AssertionError, match="重复条目"):
        _run_stub_check(_StubListTarget([(None, [entry, entry])]))


def test_stub_list_duplicate_cursor_fails() -> None:
    """``next_cursor`` 重复 → 真实协议违例，判失败。"""
    with pytest.raises(AssertionError, match="next_cursor 出现重复"):
        _run_stub_check(_StubListTarget([("c1", [_other_entry(1)]), ("c1", [_other_entry(2)])]))


def test_stub_list_budget_exhausted_skips() -> None:
    """规模超出预算仍未见到该文档 → 显式 skip 并计入计数，不判失败。"""
    with pytest.raises(pytest.skip.Exception, match="预算"):
        _run_stub_check(_StubListTarget([], endless=True))
