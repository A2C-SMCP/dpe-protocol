"""章：读接口（head / batch_head / get_skeleton / list，#27 子集 1）。

- HTTP §3.3：文档资源成功响应的头（``DPE-Doc-Hash`` / ``ETag`` / ``Vary``）与
  ``get_skeleton`` 的条件请求（``If-None-Match``：entity-tag 列表或 ``*``，命中 → 304 空体）；
- HTTP §4.2（batch_head）：响应与请求一一对应、不存在为 ``null``、超 ``batch_head_max``
  为 ``DPE_VALIDATION``；
- HTTP §4.3（get_skeleton）：读回内容与最近一次写入内容等价、不含服务端衍生数据；
- HTTP §4.4（list）：``cursor`` 不透明、``limit`` 约束、缺省 ``list_page_max``；
  码点前缀匹配不识别 path 段边界（HTTP §4.4 + CORE §3 的 list 行；匹配基于规范化形式
  的前提见 CORE §1.1）；
- HTTP §1：查询参数语法（``uri`` 与 list 参数互斥、``HEAD`` 必须有 ``uri``）。
"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WireDocument,
    assert_doc_headers,
    assert_head_error,
    assert_problem,
    build_document,
    commit,
    has_dpe_vary,
    text_element,
)


def _seed(target: Target, identity: Identity, uri: str, document: WireDocument) -> WireDocument:
    """建一份文档（If-None-Match: *），供读接口检查使用。"""
    response = commit(target, uri, document, identity=identity, if_none_match_star=True)
    assert response.status_code == 201, f"铺垫失败：{response.status_code} {response.text[:200]}"
    return document


def _document() -> WireDocument:
    return build_document(
        [[text_element("first", metadata={"order": 1}), text_element("second")]],
        title="读接口样例",
        doc_metadata={"source": "conformance"},
    )


def test_head_reports_doc_hash(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.3：HEAD 成功时按声明契约给出权威 doc_hash 与 ETag；HEAD 无体。"""
    uri = conformance_target.uri("read/head")
    document = _seed(conformance_target, full_identity, uri, _document())
    response = conformance_target.send(
        "HEAD", "documents", identity=full_identity, params={"uri": uri}
    )
    assert response.status_code == 200, f"HEAD 返回 {response.status_code}"
    # httpx 不读取 HEAD 的响应体；此断言表达规范要求，不作为「无体」的可证伪证据（HTTP §3.3）
    assert response.content == b"", "HEAD 不得带响应体"
    assert_doc_headers(response, document.doc_hash)


def test_head_absent_document(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.3 + §5：不存在的文档 → 404 + DPE_NOT_FOUND（只有错误码头，无体）。"""
    response = conformance_target.send(
        "HEAD",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("read/absent")},
    )
    assert_head_error(response, status=404, code="DPE_NOT_FOUND")


def test_head_missing_uri_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §1：HEAD 缺 ``uri`` → DPE_VALIDATION（无体，只有错误码头）。"""
    response = conformance_target.send("HEAD", "documents", identity=full_identity)
    assert_head_error(response, status=400, code="DPE_VALIDATION")


def test_head_mixed_query_params_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §1：``uri`` 与 list 参数（``prefix`` 等）混用 → DPE_VALIDATION。"""
    response = conformance_target.send(
        "HEAD",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("read/mix"), "prefix": "dpe:"},
    )
    assert_head_error(response, status=400, code="DPE_VALIDATION")


def test_batch_head_positional_correspondence(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.2：``heads`` 与请求一一对应，不存在的文档为 ``null``；该端点不带 Vary。"""
    uri = conformance_target.uri("read/batch")
    document = _seed(conformance_target, full_identity, uri, _document())
    absent = conformance_target.uri("read/batch-absent")
    response = conformance_target.send(
        "POST", "heads", identity=full_identity, json={"uris": [uri, absent]}
    )
    assert response.status_code == 200, f"heads 返回 {response.status_code}"
    heads: list[Any] = response.json()["heads"]
    assert len(heads) == 2, f"heads 必须与 uris 一一对应：{heads!r}"
    assert heads[0] == {"doc_hash": document.doc_hash}, f"存在文档的条目：{heads[0]!r}"
    assert heads[1] is None, f"不存在的文档必须为 null：{heads[1]!r}"
    assert not has_dpe_vary(response), "heads 不带 Vary: DPE-Hash-Contract（HTTP §3.1）"


def test_batch_head_over_limit_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.2：数量超过 capabilities 声明的 ``batch_head_max`` → DPE_VALIDATION。

    若构造出的超限请求体本身超过 ``max_payload_bytes``，服务端按 HTTP §3.1 第 4 步 / CORE §3.3
    第 0 步会先报 413——此时无法用该请求体验证「数量超限」，显式 skip 并计入计数。
    """
    limits = conformance_target.send(
        "GET", "capabilities", contract=None, identity=full_identity
    ).json()["limits"]
    over = limits["batch_head_max"] + 1
    uris = [conformance_target.uri(f"read/over/{index}") for index in range(over)]
    payload = json.dumps({"uris": uris}).encode()
    if len(payload) > limits["max_payload_bytes"]:
        pytest.skip(
            f"超限请求体（{len(payload)} 字节）超过 max_payload_bytes="
            f"{limits['max_payload_bytes']}，会先触发 413"
        )
    response = conformance_target.send("POST", "heads", identity=full_identity, json={"uris": uris})
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


def test_get_skeleton_round_trip(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.3 + CORE §2.7：读回骨架与写入内容**内容等价**且不含衍生数据。

    等价判据用 hash 而非逐字段相等：三层对象各自按契约 1 重算 hash 必须等于期望值——
    任何多出来的、缺掉的或被改写的字段都会改变 hash，因此这同时证明「不含服务端衍生数据」。
    """
    uri = conformance_target.uri("read/skeleton")
    document = _seed(conformance_target, full_identity, uri, _document())
    response = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"uri": uri}
    )
    assert response.status_code == 200, f"get_skeleton 返回 {response.status_code}"
    assert_doc_headers(response, document.doc_hash)
    body = response.json()
    assert body["file_uri"] == uri, f"file_uri 不是规范化形式：{body['file_uri']!r}"
    assert body["doc_hash"] == document.doc_hash, (
        f"体内 doc_hash 是 {body['doc_hash']!r}，按 dpe-hash 期望 {document.doc_hash!r}"
    )
    returned = body["document"]
    assert dpe_hash.object_hash(returned, "document") == document.doc_hash, (
        "读回的文档对象按契约 1 重算的 hash 与期望不符（内容被改动或混入衍生数据）"
    )
    pages = body["pages"]
    assert [page["elements"] for page in pages] == [page["elements"] for page in document.pages], (
        "pages 必须按文档对象 pages 的顺序给出全部页对象（HTTP §4.3）"
    )
    for page, expected_hash in zip(pages, document.page_hashes, strict=True):
        assert dpe_hash.object_hash(page, "page") == expected_hash, (
            "读回的页对象按契约 1 重算的 hash 与期望不符"
        )


def test_get_skeleton_absent_document(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.3 + §5：不存在的文档 → 404 + DPE_NOT_FOUND，problem 体全形。"""
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("read/skeleton-absent")},
    )
    assert_problem(response, status=404, code="DPE_NOT_FOUND")


def test_get_skeleton_conditional_not_modified(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.3：``If-None-Match`` 命中当前 doc_hash → 304，空体，头齐（含 Vary 与 ETag）。"""
    uri = conformance_target.uri("read/conditional")
    document = _seed(conformance_target, full_identity, uri, _document())
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": f'"{document.doc_hash}"'},
    )
    assert response.status_code == 304, f"预期 304，实际 {response.status_code}"
    # httpx 不读取 304 的响应体；此断言表达规范要求，不作为「无体」的可证伪证据（HTTP §3.3）
    assert response.content == b"", "304 不得带响应体"
    assert_doc_headers(response, document.doc_hash)


@pytest.mark.parametrize(
    ("case", "value"), [("bare", "x"), ("unterminated", '"unterminated'), ("weak-orphan", "W/")]
)
def test_get_skeleton_invalid_conditional_is_validation(
    conformance_target: Target, full_identity: Identity, case: str, value: str
) -> None:
    """HTTP §3.3：``If-None-Match`` 的非法取值 → 400 + DPE_VALIDATION（排在「契约 → uri」之后）。"""
    uri = conformance_target.uri(f"read/conditional-invalid/{case}")
    _seed(conformance_target, full_identity, uri, _document())
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": value},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_list_paginates_through_own_documents(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.4：``limit`` 分页经 ``next_cursor`` 推进至 ``null``；cursor 只可原样回传。"""
    namespace = conformance_target.uri("read/list/")
    expected: dict[str, str] = {}
    for name in ("one", "two", "three"):
        uri = f"{namespace}{name}"
        document = _seed(conformance_target, full_identity, uri, _document())
        expected[uri] = document.doc_hash
    seen: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(len(expected) + 1):
        params = {"prefix": namespace, "limit": "1"}
        if cursor is not None:
            params["cursor"] = cursor
        response = conformance_target.send(
            "GET", "documents", identity=full_identity, params=params
        )
        assert response.status_code == 200, f"list 返回 {response.status_code}"
        assert has_dpe_vary(response), "list 响应带 Vary: DPE-Hash-Contract"
        body = response.json()
        for entry in body["documents"]:
            assert entry["file_uri"] not in seen, f"分页出现重复条目：{entry['file_uri']}"
            seen[entry["file_uri"]] = entry["doc_hash"]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError(
            f"分页未以 next_cursor = null 终止（已翻 {len(expected) + 1} 页仍未结束）"
        )
    assert seen == expected, f"分页结果与预期不符：{sorted(seen)} != {sorted(expected)}"


def test_list_prefix_matches_codepoints_not_segments(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.4 + CORE §3（list）：前缀按码点匹配、不识别 path 段边界；带分隔符才按段。"""
    base = conformance_target.uri("read/cp/")
    a = f"{base}a"
    ab = f"{base}ab"
    inside = f"{base}a/x"
    for uri in (a, ab, inside):
        _seed(conformance_target, full_identity, uri, _document())

    def listed(prefix: str) -> set[str]:
        response = conformance_target.send(
            "GET", "documents", identity=full_identity, params={"prefix": prefix}
        )
        assert response.status_code == 200, f"list(prefix={prefix!r}) 返回 {response.status_code}"
        return {entry["file_uri"] for entry in response.json()["documents"]}

    assert listed(a) == {a, ab, inside}, "码点前缀不得识别 path 段边界"
    assert listed(f"{base}a/") == {inside}, "前缀末尾自带分隔符时才按段匹配"


#: 缺省前缀列举的翻页预算：超出仍未见到本次写入的文档时按「规模超出预算」skip——
#: 合规的大规模服务端不得被判失败（判失败只留给真实协议违例）。
_LIST_PAGE_BUDGET = 1000


def test_list_missing_prefix_lists_all(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.4：缺省前缀视同空串（列出全部文档）——在预算内翻页，找到本次写入的文档且
    doc_hash 一致即证通过。

    判失败只取真实协议违例：游标到 ``null`` 仍未出现该文档、条目重复、``next_cursor`` 重复。
    「分页最终以 ``null`` 终止」由 ``test_list_paginates_through_own_documents`` 在本命名
    空间内证明，本检查不重复。规模超出预算（文档数超过预算 × ``list_page_max``）时显式
    skip 并计入计数。
    """
    uri = conformance_target.uri("read/all")
    document = _seed(conformance_target, full_identity, uri, _document())
    limits = conformance_target.send(
        "GET", "capabilities", contract=None, identity=full_identity
    ).json()["limits"]
    seen: dict[str, str] = {}
    cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(_LIST_PAGE_BUDGET):
        params: dict[str, str] = {"limit": str(limits["list_page_max"])}
        if cursor is not None:
            params["cursor"] = cursor
        response = conformance_target.send(
            "GET", "documents", identity=full_identity, params=params
        )
        assert response.status_code == 200, f"list 返回 {response.status_code}"
        assert has_dpe_vary(response), "list 响应带 Vary: DPE-Hash-Contract"
        body = response.json()
        for entry in body["documents"]:
            listed_uri = entry["file_uri"]
            assert listed_uri not in seen, f"分页出现重复条目：{listed_uri}"
            seen[listed_uri] = entry["doc_hash"]
        if uri in seen:
            assert seen[uri] == document.doc_hash, (
                f"列表中 {uri} 的 doc_hash 是 {seen[uri]!r}，按 dpe-hash 期望 {document.doc_hash!r}"
            )
            return
        cursor = body["next_cursor"]
        if cursor is None:
            raise AssertionError(f"游标已到 null，仍未列出本次写入的文档：{uri}")
        if cursor in cursors:
            raise AssertionError(f"next_cursor 出现重复：{cursor!r}")
        cursors.add(cursor)
    pytest.skip(
        f"达到 {_LIST_PAGE_BUDGET} 页预算仍未见到 {uri}（服务端文档规模超出预算，无法判定）"
    )


def test_list_invalid_limit_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.4：``limit`` 不在 1 到 ``list_page_max`` 之间 → DPE_VALIDATION。"""
    response = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"limit": "0"}
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_list_unknown_cursor_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.4：服务端无法识别的 cursor → DPE_VALIDATION（cursor 不透明）。"""
    response = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"cursor": "not-a-cursor"}
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_list_uri_and_prefix_mixed_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §1：``uri`` 与 list 参数混用 → DPE_VALIDATION。"""
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("read/mixed"), "prefix": "dpe:"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")
