"""章：契约升级期（#58 全量 5/6）。

服务端同时声明两个 hash 契约（过渡期，契约 1 §6）时：

- HTTP §3.1：请求体中的 hash 按 ``DPE-Hash-Contract`` 计算，响应中的 hash 按它给出——
  batch_head、head、get_skeleton（含骨架里的子 hash）与 commit 的回报都按请求契约；
- HTTP §3.1 + 契约 1 §6：``If-Match`` 按值所带前缀的契约比较当前内容，新旧契约的值都能匹配，
  与请求声明的契约无关；GET 的 ``If-None-Match`` 则只与**请求契约**下的 doc_hash 比较（§3.3）；
- core §2.8 第 4 步 + 契约 1 §5：对象引用另一受支持契约的子 hash 是契约混用 → ``DPE_VALIDATION``
  （不是 ``DPE_CONTRACT_UNSUPPORTED``：受支持与否按服务端声明的集合判定，core §3.1）。

期望值只经 dpe-hash 按各契约重算。第二契约取服务端声明的、dpe-hash 能计算的契约（目前只有
演练契约 ``dpe2``，vectors/README.md）；服务端没有同时声明它时整章如实 skip——真实服务端不会
声明演练契约，不因此判失败。
"""

from __future__ import annotations

import dpe_hash
import pytest

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WireDocument,
    assert_commit_success,
    assert_doc_headers,
    assert_problem,
    build_document,
    commit,
    has_dpe_vary,
    seed,
    text_element,
)

PRIMARY = dpe_hash.CONTRACT


def _document(text: str, contract: str = PRIMARY) -> WireDocument:
    """同一份内容在 ``contract`` 下的线上形态与期望 hash（内容相同、只换契约，契约 1 §6）。"""
    return build_document(
        [[text_element(text), text_element(f"{text}-2")]], title="契约升级", contract=contract
    )


def _declared(target: Target, identity: Identity) -> set[str]:
    response = target.send("GET", "capabilities", contract=None, identity=identity)
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    declared: set[str] = set(response.json()["hash_contracts"])
    return declared


@pytest.fixture(scope="module")
def declared_other(conformance_target: Target, full_identity: Identity) -> str:
    """服务端与主契约同时声明的任一其他契约；没有则 skip。契约混用只需两个契约都已声明，
    不需要计算期望值，dpe-hash 不认识的未来契约同样适用。"""
    declared = _declared(conformance_target, full_identity)
    others = sorted(declared - {PRIMARY})
    if PRIMARY not in declared or not others:
        pytest.skip(f"服务端未同时声明 {PRIMARY} 与其他契约（hash_contracts={sorted(declared)}）")
    return others[0]


@pytest.fixture(scope="module")
def other_contract(conformance_target: Target, full_identity: Identity) -> str:
    """服务端同时声明的第二契约（dpe-hash 能计算期望值的）；没有则整章 skip。"""
    response = conformance_target.send("GET", "capabilities", contract=None, identity=full_identity)
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    declared: set[str] = set(response.json()["hash_contracts"])
    candidates: list[str] = sorted((declared & set(dpe_hash.KNOWN_CONTRACTS)) - {PRIMARY})
    if PRIMARY not in declared or not candidates:
        pytest.skip(
            f"服务端未同时声明 {PRIMARY} 与可计算期望值的第二契约"
            f"（hash_contracts={sorted(declared)}）"
        )
    return candidates[0]


def test_batch_head_answers_in_the_requested_contract(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """HTTP §3.1 + §4.2：``batch_head`` 按请求契约给值——以主契约请求得到与首次提交回报相同的
    值（未变文档不触发重推），以第二契约请求得到该契约下的值。"""
    uri = conformance_target.uri("contracts/heads")
    document = seed(conformance_target, uri, _document("heads"), identity=full_identity)
    for contract, expected in (
        (PRIMARY, document.doc_hash),
        (other_contract, _document("heads", other_contract).doc_hash),
    ):
        response = conformance_target.send(
            "POST", "heads", identity=full_identity, contract=contract, json={"uris": [uri]}
        )
        assert response.status_code == 200, f"heads（{contract}）返回 {response.status_code}"
        assert response.json()["heads"] == [{"doc_hash": expected}], (
            f"以 {contract} 请求应得 {expected}：{response.json()!r}"
        )


def test_head_and_skeleton_answer_in_the_requested_contract(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """HTTP §3.1 + §3.3 + §4.3：以第二契约读取——``DPE-Doc-Hash`` / ``ETag`` / 体内 doc_hash
    与骨架中的页 hash、元素 hash 都按请求契约给出，响应带 ``Vary: DPE-Hash-Contract``。"""
    uri = conformance_target.uri("contracts/read")
    seed(conformance_target, uri, _document("read"), identity=full_identity)
    expected = _document("read", other_contract)

    head = conformance_target.send(
        "HEAD", "documents", identity=full_identity, contract=other_contract, params={"uri": uri}
    )
    assert head.status_code == 200, f"head 返回 {head.status_code}"
    assert_doc_headers(head, expected.doc_hash)

    skeleton = conformance_target.send(
        "GET", "documents", identity=full_identity, contract=other_contract, params={"uri": uri}
    )
    assert skeleton.status_code == 200, f"get_skeleton 返回 {skeleton.status_code}"
    assert_doc_headers(skeleton, expected.doc_hash)
    body = skeleton.json()
    assert body["doc_hash"] == expected.doc_hash
    assert body["document"]["pages"] == expected.page_hashes, "骨架的页 hash 须按请求契约给出"
    assert [page["elements"] for page in body["pages"]] == [
        page["elements"] for page in expected.pages
    ], "骨架的元素 hash 须按请求契约给出"


def test_if_match_in_the_other_contract_matches(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """HTTP §3.1 + 契约 1 §6：以主契约声明、``If-Match`` 写当前内容在第二契约下的值 → 匹配，
    提交新内容得 ``updated``（比较对象是该值前缀所示契约下的 doc_hash，与请求契约无关）。"""
    uri = conformance_target.uri("contracts/if-match-other")
    seed(conformance_target, uri, _document("v1"), identity=full_identity)
    updated = _document("v2")
    response = commit(
        conformance_target,
        uri,
        updated,
        identity=full_identity,
        if_match=_document("v1", other_contract).doc_hash,
    )
    assert_commit_success(response, status=200, status_text="updated", doc_hash=updated.doc_hash)


def test_commit_in_the_other_contract_with_primary_if_match(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """反向：以第二契约声明、请求体按它计算、``If-Match`` 写主契约下的当前值 → ``updated``，
    回报的 doc_hash 与响应头按第二契约给出。"""
    uri = conformance_target.uri("contracts/if-match-primary")
    current = seed(conformance_target, uri, _document("v1"), identity=full_identity)
    updated = _document("v2", other_contract)
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        contract=other_contract,
        params={"uri": uri},
        headers={"If-Match": f'"{current.doc_hash}"'},
        json=updated.commit_body(),
    )
    assert_commit_success(response, status=200, status_text="updated", doc_hash=updated.doc_hash)


def test_unchanged_answers_in_the_requested_contract(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """core §3.3 第 4 步 + HTTP §3.1：以第二契约提交与当前相同的内容 → ``unchanged``，回报的
    doc_hash 为该契约下的值（内容等价跨契约成立，契约 1 §6）。"""
    uri = conformance_target.uri("contracts/unchanged")
    seed(conformance_target, uri, _document("same"), identity=full_identity)
    same = _document("same", other_contract)
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        contract=other_contract,
        params={"uri": uri},
        headers={"If-Match": f'"{same.doc_hash}"'},
        json=same.commit_body(),
    )
    assert_commit_success(response, status=200, status_text="unchanged", doc_hash=same.doc_hash)


def test_get_if_none_match_compares_only_in_the_requested_contract(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """HTTP §3.3：GET 的 ``If-None-Match`` 只与请求契约下的当前 doc_hash 比较——以主契约声明、
    条件头写当前内容在第二契约下的值 → 不命中，返回 200 与骨架（不是 304）。"""
    uri = conformance_target.uri("contracts/if-none-match")
    current = seed(conformance_target, uri, _document("inm"), identity=full_identity)
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": f'"{_document("inm", other_contract).doc_hash}"'},
    )
    assert response.status_code == 200, (
        f"跨契约的 If-None-Match 不得命中，应为 200，实际 {response.status_code}"
    )
    assert_doc_headers(response, current.doc_hash)


def test_list_answers_in_the_requested_contract(
    conformance_target: Target, full_identity: Identity, other_contract: str
) -> None:
    """core §3.1 + HTTP §4.4：``list`` 同为读接口，按请求契约给出各条目的 doc_hash；
    响应带 ``Vary: DPE-Hash-Contract``。"""
    namespace = conformance_target.uri("contracts/list/")
    expected: dict[str, str] = {}
    for name in ("one", "two"):
        seed(conformance_target, f"{namespace}{name}", _document(name), identity=full_identity)
        expected[f"{namespace}{name}"] = _document(name, other_contract).doc_hash
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        contract=other_contract,
        params={"prefix": namespace},
    )
    assert response.status_code == 200, f"list 返回 {response.status_code}"
    assert has_dpe_vary(response), "list 响应带 Vary: DPE-Hash-Contract（HTTP §3.1）"
    body = response.json()
    assert body["next_cursor"] is None, "两条目不应分页"
    listed = {entry["file_uri"]: entry["doc_hash"] for entry in body["documents"]}
    assert listed == expected, f"list 须按请求契约给值：{listed!r}"


@pytest.mark.parametrize("direction", ["primary-refs-other", "other-refs-primary"])
def test_mixed_contract_child_is_validation(
    conformance_target: Target, full_identity: Identity, declared_other: str, direction: str
) -> None:
    """core §2.8 第 4 步 + 契约 1 §5：对象引用另一个**受支持**契约的子 hash 是契约混用 →
    ``DPE_VALIDATION``，不是 ``DPE_CONTRACT_UNSUPPORTED``（两个契约都已声明，core §3.1）。
    hash 用占位值：这是 schema 层判定，先于任何 hash 重算与可得性判定。"""
    declared, foreign = (
        (PRIMARY, declared_other)
        if direction == "primary-refs-other"
        else (declared_other, PRIMARY)
    )
    body = {
        "document": {"file_type": "md", "pages": [f"{declared}:" + "0" * 64]},
        "pages": [{"elements": [f"{foreign}:" + "0" * 64]}],
    }
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        contract=declared,
        params={"uri": conformance_target.uri(f"contracts/mixed-{direction}")},
        headers={"If-None-Match": "*"},
        json=body,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")
