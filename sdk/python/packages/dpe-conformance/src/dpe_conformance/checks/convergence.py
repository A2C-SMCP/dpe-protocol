"""章：收敛一致性（#59 全量 6/6，#28 的验收）。

- core §3.3 + plan §13 第 3 条（不制造伪变更）：**delta 是规范定义的量**——新旧文档的元素
  content_hash 多重集之差（added = 新∖旧、removed = 旧∖新、retained = 交集）。服务端 MUST 如实
  回报，跑分器以 get_skeleton 前后两次读回的多重集印证：内容未变的元素不得出现在 added / removed
  中（重复元素按次数计、跨页移动与重排不算变化）；快路径与暂存路径同一口径。
- connector §8.3 判据 3（R = F(S)，plan §14 M4 的验收口径）的**服务端一侧**：一串增量写入（快路径
  与暂存路径更新、删除、move、同内容重推、在被删 / 被移走的 URI 上重建）收敛后，remote 状态与把
  最终源状态 S 全量推送到全新命名空间的结果一致——被删与被移走的文档不残留，每篇的 doc_hash 等于
  按 S 计算的期望值，骨架逐字段相等（骨架不含衍生数据，HTTP §4.3）。本章只验证服务端正确执行了
  增量写入；「插件 + 运行器」组合的增量收敛由 dpe-run 的端到端测试覆盖。

期望值只经 dpe-hash 计算。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WireDocument,
    assert_commit_success,
    build_document,
    commit,
    commit_with_session,
    delete,
    expected_delta,
    list_all,
    move,
    negotiate,
    seed,
    session_of,
    skeleton_multiset,
    stage_all,
    text_element,
)


def _doc(*pages: Sequence[str]) -> WireDocument:
    return build_document([[text_element(text) for text in page] for page in pages])


def _skeleton(target: Target, uri: str, *, identity: Identity) -> dict[str, Any]:
    response = target.send("GET", "documents", identity=identity, params={"uri": uri})
    assert response.status_code == 200, f"读回 {uri} 失败：{response.status_code}"
    body: dict[str, Any] = response.json()
    return body


#: (名称, 前一版的页, 后一版的页, 期望 delta)：每种都与读回多重集互证
_TRANSITIONS = [
    # 重复元素按次数计：去掉两份 x 中的一份 → removed 1、retained 1（多重集语义）
    ("duplicate", [["x", "x"]], [["x"]], {"added": 0, "removed": 1, "retained": 1}),
    # 跨页移动：b 从第 1 页挪到第 2 页 → delta 为零（不是伪变更），但 doc_hash 变化
    (
        "cross-page",
        [["a", "b"], ["c"]],
        [["a"], ["b", "c"]],
        {"added": 0, "removed": 0, "retained": 3},
    ),
    # 局部修改：多页中只改一个元素 → 只有它进 added / removed，其余全部 retained
    (
        "local-edit",
        [["a", "b"], ["c", "d"]],
        [["a", "b"], ["c", "d2"]],
        {"added": 1, "removed": 1, "retained": 3},
    ),
    # 整页删除与新增页的混合
    (
        "pages-replaced",
        [["a"], ["b"], ["c"]],
        [["a"], ["c"], ["n1", "n2"]],
        {"added": 2, "removed": 1, "retained": 2},
    ),
]


@pytest.mark.parametrize(
    ("before_pages", "after_pages", "expected"),
    [(before, after, expected) for _, before, after, expected in _TRANSITIONS],
    ids=[name for name, *_ in _TRANSITIONS],
)
def test_fast_path_delta_matches_readback(
    conformance_target: Target,
    full_identity: Identity,
    before_pages: list[list[str]],
    after_pages: list[list[str]],
    expected: dict[str, int],
) -> None:
    """core §3.3：快路径更新的 delta 等于前后两次读回的多重集之差，读回与推送内容一致，
    未变的元素不出现在 added / removed 中。"""
    uri = conformance_target.uri(f"convergence/delta/{uuid.uuid4().hex}")
    before, after = _doc(*before_pages), _doc(*after_pages)
    seed(conformance_target, uri, before, identity=full_identity)
    read_before = skeleton_multiset(conformance_target, uri, identity=full_identity)
    assert read_before == before.content_multiset(), "读回须与推送内容一致"

    response = commit(
        conformance_target, uri, after, identity=full_identity, if_match=before.doc_hash
    )
    body = assert_commit_success(
        response, status=200, status_text="updated", doc_hash=after.doc_hash
    )
    read_after = skeleton_multiset(conformance_target, uri, identity=full_identity)
    assert read_after == after.content_multiset(), "读回须与推送内容一致"
    assert body["delta"] == expected_delta(read_before, read_after) == expected, (
        f"delta 与读回多重集之差不符：服务端报 {body['delta']!r}，期望 {expected!r}"
    )


def test_staging_path_delta_matches_readback(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3：delta 与投递路径无关——经 negotiate、逐层上传、引用会话的 commit 更新，
    delta 同样等于读回多重集之差。"""
    uri = conformance_target.uri(f"convergence/staged-delta/{uuid.uuid4().hex}")
    before, after = _doc(["a", "b"], ["c", "d"]), _doc(["a", "b"], ["c", "d2"], ["e"])
    seed(conformance_target, uri, before, identity=full_identity)
    read_before = skeleton_multiset(conformance_target, uri, identity=full_identity)

    negotiated = negotiate(conformance_target, uri, after, identity=full_identity)
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, after, identity=full_identity)
    response = commit_with_session(
        conformance_target,
        uri,
        after,
        session_id,
        identity=full_identity,
        if_match=before.doc_hash,
    )
    body = assert_commit_success(
        response, status=200, status_text="updated", doc_hash=after.doc_hash
    )
    read_after = skeleton_multiset(conformance_target, uri, identity=full_identity)
    assert read_after == after.content_multiset()
    assert (
        body["delta"]
        == expected_delta(read_before, read_after)
        == {
            "added": 2,
            "removed": 1,
            "retained": 3,
        }
    ), f"暂存路径的 delta 与读回不符：{body['delta']!r}"


# ---------------------------------------------------------------------- R = F(S)


def test_incremental_writes_converge_to_a_full_push(
    conformance_target: Target, full_identity: Identity
) -> None:
    """connector §8.3 判据 3 的服务端一侧（R = F(S)）：增量写入收敛后，remote 状态与把最终源状态
    S 全量推送到全新命名空间的结果一致。

    序列覆盖服务端最易出错的残留状态：快路径与暂存路径更新、删除、move、同内容重推（unchanged、
    delta 为零）、move 到一个刚被删除的 URI、在被 move 走的源 URI 上重建、改成与另一篇相同的内容。
    全量侧只作骨架参照；收敛的判据是增量侧的 ``list`` 映射等于按 S 算出的期望值。
    """
    root = conformance_target.uri(f"convergence/{uuid.uuid4().hex}/")
    incremental, full = f"{root}inc/", f"{root}full/"
    a1, a2 = _doc(["a1"]), _doc(["a2"], ["shared"])
    b1, b2 = _doc(["b1"], ["shared"]), _doc(["b2"], ["shared"], ["b-tail"])
    c1, e1, f1 = _doc(["c1"]), _doc(["e1"]), _doc(["f1"])

    def at(name: str) -> str:
        return f"{incremental}{name}"

    for name, document in (("a", a1), ("b", b1), ("c", c1), ("f", f1)):
        seed(conformance_target, at(name), document, identity=full_identity)
    # a：快路径更新；同内容重推 → unchanged、delta 为零（不是伪变更）
    updated = commit(conformance_target, at("a"), a2, identity=full_identity, if_match=a1.doc_hash)
    assert_commit_success(updated, status=200, status_text="updated", doc_hash=a2.doc_hash)
    replayed = commit(conformance_target, at("a"), a2, identity=full_identity, if_match=a2.doc_hash)
    replay_body = assert_commit_success(
        replayed, status=200, status_text="unchanged", doc_hash=a2.doc_hash
    )
    assert replay_body["delta"] == {"added": 0, "removed": 0, "retained": 2}
    # b：暂存路径更新
    negotiated = negotiate(conformance_target, at("b"), b2, identity=full_identity)
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, b2, identity=full_identity)
    staged = commit_with_session(
        conformance_target, at("b"), b2, session_id, identity=full_identity, if_match=b1.doc_hash
    )
    assert_commit_success(staged, status=200, status_text="updated", doc_hash=b2.doc_hash)
    # c：删除后，a 移到这个刚被删除的 URI（目标不存在即可移入）
    removed = delete(conformance_target, at("c"), identity=full_identity, if_match=c1.doc_hash)
    assert removed.status_code == 204, f"delete 返回 {removed.status_code}"
    moved = move(
        conformance_target, at("a"), at("c"), identity=full_identity, base_hash=a2.doc_hash
    )
    assert moved.status_code == 200, f"move 返回 {moved.status_code}：{moved.text[:200]}"
    # 在被 move 走的源 URI a 上重建新内容
    rebuilt = commit(
        conformance_target, at("a"), e1, identity=full_identity, if_none_match_star=True
    )
    assert_commit_success(rebuilt, status=201, status_text="created", doc_hash=e1.doc_hash)
    # f：改成与 b 相同的内容（跨文档同内容不影响各自身份）
    same = commit(conformance_target, at("f"), b2, identity=full_identity, if_match=f1.doc_hash)
    assert_commit_success(same, status=200, status_text="updated", doc_hash=b2.doc_hash)

    final_state = {"a": e1, "b": b2, "c": a2, "f": b2}  # S
    expected = {f"{incremental}{name}": doc.doc_hash for name, doc in final_state.items()}
    assert list_all(conformance_target, incremental, identity=full_identity) == expected, (
        "增量写入收敛后的 remote 状态须等于 F(S)：被删、被移走的文档不得残留"
    )
    for name, document in final_state.items():
        seed(conformance_target, f"{full}{name}", document, identity=full_identity)
        left = _skeleton(conformance_target, f"{incremental}{name}", identity=full_identity)
        right = _skeleton(conformance_target, f"{full}{name}", identity=full_identity)
        assert (left["doc_hash"], left["document"], left["pages"]) == (
            right["doc_hash"],
            right["document"],
            right["pages"],
        ), f"{name} 的骨架与全量推送不一致"
