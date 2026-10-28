"""支持层单元：构造出的期望 hash 必须与一致性向量逐字一致（只经 dpe-hash），
problem 断言对构造规则的把关，以及缺省前缀列举检查各分支的桩覆盖。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from dpe_conformance.checks import http_binding as http_binding_checks
from dpe_conformance.checks import large as large_checks
from dpe_conformance.checks import order as order_checks
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


# ------------------------------------ test_code_status_payload_too_large 的预算分支桩


class _StubPayloadLimit:
    """最小桩 Target：capabilities 按脚本给出 ``max_payload_bytes``，越界 PUT 按脚本应答。

    ``limit`` 超过检查预算时触发 skip 分支；``respond`` 让越界请求返回 201（模拟违例服务端，
    触发判红分支）。「找到即过」分支由 E2E（参考服务端实跑 413）覆盖。
    """

    def __init__(self, limit: int, *, respond: int = 413) -> None:
        self._limit = limit
        self._respond = respond

    def uri(self, path: str) -> str:
        return "dpe://conformance/stub/" + path.lstrip("/")

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, f"http://dpe.test/{path}")
        if path == "capabilities":
            return httpx.Response(
                200, json={"limits": {"max_payload_bytes": self._limit}}, request=request
            )
        return httpx.Response(self._respond, json={}, request=request)


def _run_payload_check(stub: _StubPayloadLimit) -> None:
    http_binding_checks.test_code_status_payload_too_large(cast(Target, stub), _stub_identity())


def test_stub_payload_limit_over_budget_skips() -> None:
    """服务端声明的上限超过预算 → 显式 skip 并计入计数，不构造越界请求、不判红。"""
    with pytest.raises(pytest.skip.Exception, match="预算"):
        _run_payload_check(_StubPayloadLimit(1 << 40))


def test_stub_payload_violation_fails() -> None:
    """越界请求未得到 413 + DPE_PAYLOAD_TOO_LARGE（如返回 201）→ 判失败（真实协议违例）。"""
    with pytest.raises(AssertionError):
        _run_payload_check(_StubPayloadLimit(1024, respond=201))


# ------------------------------------------------ 缺失清单截断容错（#54 追加单元）


def test_missing_layer_strict_when_not_truncated() -> None:
    """未截断：某层清单必须与实际缺失严格相等。"""
    problem = {"missing": {"pages": ["dpe1:a", "dpe1:b"], "content_hashes": [], "blobs": []}}
    large_checks._assert_missing_layer(problem, "pages", {"dpe1:a", "dpe1:b"})
    with pytest.raises(AssertionError):
        large_checks._assert_missing_layer(problem, "pages", {"dpe1:a"})


def test_missing_layer_subset_when_truncated_flag_on_top_level() -> None:
    """截断（``missing_truncated`` 在 problem 体**顶层**）：只断言子集，且容忍后层被截空。"""
    problem = {
        "missing": {"pages": [], "content_hashes": ["dpe1:a"], "blobs": []},
        "missing_truncated": True,
    }
    large_checks._assert_missing_layer(problem, "content_hashes", {"dpe1:a", "dpe1:b"})
    large_checks._assert_missing_layer(problem, "blobs", {"sha256:x"})  # 被截空也可接受
    with pytest.raises(AssertionError):
        large_checks._assert_missing_layer(problem, "content_hashes", {"dpe1:other"})


# ------------------------------------------------ 限额内最大文档对象（#250 追加单元）


class _StubReferenceDocument:
    """最小桩 Target：capabilities 给出限额，PUT documents 按脚本返回缺失清单。"""

    def __init__(
        self,
        limit: int,
        *,
        truncated: bool,
        drop: bool = False,
        empty: bool = False,
        unexpected: bool = False,
    ) -> None:
        self._limit = limit
        self._truncated = truncated
        self._drop = drop
        self._empty = empty
        self._unexpected = unexpected

    def uri(self, path: str) -> str:
        return "dpe://conformance/stub/" + path.lstrip("/")

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, f"http://dpe.test/{path}")
        if path == "capabilities":
            return httpx.Response(
                200, json={"limits": {"max_payload_bytes": self._limit}}, request=request
            )
        pages = json.loads(kwargs["content"])["document"]["pages"]
        if self._empty:
            listed = []
        elif self._unexpected:
            listed = [pages[0], "dpe1:" + "f" * 64]
        elif self._truncated:
            listed = pages[: max(1, len(pages) // 2)]
        else:
            listed = pages
        if self._drop and not self._truncated:
            listed = listed[:-1]
        problem = {
            "code": "DPE_MISSING_CONTENT",
            "type": "urn:dpe:error:missing-content",
            "title": "missing content",
            "retryable": False,
            "missing": {"pages": listed, "content_hashes": [], "blobs": []},
            "missing_truncated": self._truncated,
        }
        return httpx.Response(
            400,
            content=json.dumps(problem).encode(),
            headers={
                "content-type": "application/problem+json",
                "DPE-Error-Code": "DPE_MISSING_CONTENT",
                "Vary": "DPE-Hash-Contract",
            },
            request=request,
        )


def _run_reference_document_check(stub: _StubReferenceDocument) -> None:
    large_checks.test_max_reference_document_within_payload(cast(Target, stub), _stub_identity())


def test_stub_reference_document_over_budget_skips() -> None:
    """服务端声明的上限超过预算 → 显式 skip（不构造大体量请求、不判红）。"""
    with pytest.raises(pytest.skip.Exception, match="预算"):
        _run_reference_document_check(_StubReferenceDocument(1 << 40, truncated=False))


def test_stub_reference_document_tiny_limit_skips() -> None:
    """限额放不下单页文档体 → 显式 skip，不判红。"""
    with pytest.raises(pytest.skip.Exception, match="单页文档体"):
        _run_reference_document_check(_StubReferenceDocument(64, truncated=False))


def test_stub_reference_document_truncated_subset_passes() -> None:
    """截断：三层合计为非空子集即通过。"""
    _run_reference_document_check(_StubReferenceDocument(4096, truncated=True))


def test_stub_reference_document_truncated_empty_fails() -> None:
    """截断却返回空清单 → 判失败（三层合计必须非空）。"""
    with pytest.raises(AssertionError):
        _run_reference_document_check(_StubReferenceDocument(4096, truncated=True, empty=True))


def test_stub_reference_document_truncated_outside_subset_fails() -> None:
    """截断却列出构造集合之外的 hash → 判失败（必须是子集）。"""
    with pytest.raises(AssertionError):
        _run_reference_document_check(_StubReferenceDocument(4096, truncated=True, unexpected=True))


def test_stub_reference_document_untruncated_exact_passes() -> None:
    """未截断：页清单与构造集合严格相等即通过。"""
    _run_reference_document_check(_StubReferenceDocument(4096, truncated=False))


def test_stub_reference_document_untruncated_mismatch_fails() -> None:
    """未截断却少列一页 → 判失败（真实协议违例）。"""
    with pytest.raises(AssertionError):
        _run_reference_document_check(_StubReferenceDocument(4096, truncated=False, drop=True))


# ------------------------------- 求值顺序：先判 blob 的服务端（#259 追加单元）


class _StubBlobFirstTarget:
    """最小桩 Target：模拟「先判 blob、后判前置条件」的服务端。

    seed 的 commit（``If-None-Match: *``）返回 201；任何带 ``If-Match`` 的 commit 直接返回
    ``DPE_MISSING_CONTENT``（400），使求值顺序检查的 412 断言判红。negotiate 与暂存上传按
    正常成功路径应答，保证检查能走到 commit。
    """

    def uri(self, path: str) -> str:
        return "dpe://conformance/stub/" + path.lstrip("/")

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, f"http://dpe.test/{path}")
        if method == "POST" and path == "negotiate":
            return httpx.Response(
                200,
                json={
                    "staging_session": {
                        "id": "st-session",
                        "expires_at": "2026-10-28T00:00:00Z",
                    }
                },
                request=request,
            )
        if method == "PUT" and path.startswith("staging/"):
            return httpx.Response(201, json={}, request=request)
        if method == "PUT" and path == "documents":
            headers = kwargs.get("headers") or {}
            if "If-Match" in headers:
                return _problem_response(400, "DPE_MISSING_CONTENT")
            return httpx.Response(201, json={}, request=request)
        raise AssertionError(f"桩未覆盖：{method} {path}")


def test_stub_blob_first_without_session_fails() -> None:
    """无会话变体：先判 blob 的服务端在错误 If-Match 上返回 MISSING_CONTENT → 检查判红。"""
    with pytest.raises(AssertionError, match=r"预期 412，实际 400"):
        order_checks.test_precondition_precedes_missing_blob_without_session(
            cast(Target, _StubBlobFirstTarget()), _stub_identity()
        )


def test_stub_blob_first_with_session_fails() -> None:
    """会话变体：先判 blob 的服务端在错误 If-Match 上返回 MISSING_CONTENT → 检查判红。"""
    with pytest.raises(AssertionError, match=r"预期 412，实际 400"):
        order_checks.test_precondition_precedes_missing_blob_with_session(
            cast(Target, _StubBlobFirstTarget()), _stub_identity()
        )
