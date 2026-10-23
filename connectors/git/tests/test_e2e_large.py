"""大文档的端到端场景（#68）：暂存路径与「中间插入一页只传新增对象」。

两条用例都按协议保证的性质断言，外部 remote 同样适用：暂存路径看是否出现 ``staging/…`` 上传、
最终 commit 引用暂存会话，且每个请求不超过远端 ``max_payload_bytes``；插页看 negotiate 只交
新页、commit 只带新元素。精确请求序列只在本地参考服务端下断言（#68 决策 5）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dpe_sdk.protocol import SMALL_UPDATE_BYTES
from e2e_harness import (
    Seen,
    ServerOptions,
    big_repo,
    runner,
    sent_element_hashes,
    sent_page_hashes,
)
from helpers import FixtureRepo, requires_git, run_git

pytestmark = [requires_git, pytest.mark.scenarios]

#: 本地参考服务端的单请求上限：小到用几百 KiB 的仓库就能触发暂存路径
LOCAL_MAX_PAYLOAD = 256 * 1024
#: 外部 remote 的上限超过它就跳过暂存用例：按 1.5 倍构造仓库会超出测试预算
MAX_EXTERNAL_PAYLOAD = 32 * 1024 * 1024


def _branch(repo: FixtureRepo, name: str, message: str, date: str) -> None:
    """从主线开出跟踪分支、加一个独有提交，并建 ``origin/<name>``（模拟 fetch）。"""
    repo.branch(name, at="main")
    repo.commit(message, date=date)
    repo.add_remote_branch(name)
    repo.checkout("main")


def _write_requests(requests: list[Seen]) -> list[Seen]:
    return [r for r in requests if r.is_write or r.target == "negotiate"]


def test_staging_path_for_oversized_document(tmp_path: Path) -> None:
    """新建一篇整篇内联超过 ``max_payload_bytes`` 的文档：分段 → 运行器拼装 → 协商上传。

    本地模式把参考服务端的上限调小以加速；外部模式按远端公布的限额生成仓库。
    """
    root = tmp_path / "root"
    with runner(
        tmp_path,
        {"root": str(root), "branches": []},
        local_options=ServerOptions(max_payload_bytes=LOCAL_MAX_PAYLOAD),
    ) as r:
        limit = r.limits().max_payload_bytes
        if limit > MAX_EXTERNAL_PAYLOAD:
            pytest.skip(f"远端 max_payload_bytes={limit} 过大，构造超限文档超出测试预算")
        message_bytes = max(1024, min(limit // 8, 512 * 1024))
        target = limit * 9 // 8 + 1024  # 刚超过上限即可：dpe-run 的 30 秒 HTTP 超时不可配置
        commits = max(2, target // message_bytes)
        per_month = (commits + 1) // 2
        big_repo(root / "big", per_month=per_month, note_bytes=message_bytes)

        first = r.round()
        assert first.report["outcome"] == "succeeded", first.report
        assert first.changed() == {"created": 1}

        staging = [q for q in first.requests if q.target.startswith("staging/")]
        assert staging, "整篇内联超过 max_payload_bytes 却没有走暂存路径"
        writes = [q for q in first.requests if q.is_write]
        assert writes and (writes[-1].json() or {}).get("staging_session")
        for request in first.requests:
            assert len(request.body) <= limit, (request.method, request.target, len(request.body))

        r.assert_converged()


def test_middle_branch_page_uploads_only_new_objects(tmp_path: Path) -> None:
    """大仓库中间插入一个分支页：只传新页与新元素，既有页/元素一次都不重传（#68 决策 6b）。"""
    root = tmp_path / "root"
    repo = big_repo(root / "repo", per_month=150)
    _branch(repo, "zeta", "zeta 的独有提交", "2026-01-12T10:00:00+00:00")
    # 限定：只对整篇内联超过 SMALL_UPDATE_BYTES 的文档要求差量（更小的走 core §3.2 快路径）。
    # 提交说明的字节数是文档内联大小的下限；夹具保证它超过阈值，与远端限额无关。
    assert len(run_git(repo.root, "log", "--format=%B", "main").encode()) > SMALL_UPDATE_BYTES
    with runner(tmp_path, {"root": str(root), "branches": ["zeta", "mid"]}) as r:
        r.round()
        with r.client() as client:
            skeleton = client.get_skeleton(r.uri("repo"))
        assert skeleton is not None
        before = r.snapshot()["repo"]
        old_elements = {element for page in skeleton.pages for element in page.elements}

        # mid 的字节序在 main 与 zeta 之间：新页插在既有分支页之前
        _branch(repo, "mid", "mid 的独有提交", "2026-01-13T10:00:00+00:00")
        step = r.round()
        assert step.changed() == {"updated": 1}
        after = r.assert_converged()["repo"]
        assert len(after[1]) == len(before[1]) + 1
        assert after[1][:2] == before[1][:2] and after[1][3:] == before[1][2:]
        assert after[1][2] not in before[1]

        # 只上传新增对象（规范性质，两种模式都断言；内联与 staging 逐层上传均接受）
        assert sent_page_hashes(step.requests) == {after[1][2]}
        with r.client() as client:
            after_skeleton = client.get_skeleton(r.uri("repo"))
        assert after_skeleton is not None
        new_elements = set(after_skeleton.pages[2].elements) - old_elements
        assert new_elements
        assert sent_element_hashes(step.requests) == new_elements
        for request in step.requests:
            assert not any(element.encode() in request.body for element in old_elements)
        writes = [q for q in step.requests if q.is_write]
        assert writes and all(before[0] in q.headers.get("if-match", "") for q in writes)
        if r.local_remote:
            assert [(q.method, q.target) for q in _write_requests(step.requests)] == [
                ("POST", "negotiate"),
                ("POST", "negotiate"),
                ("PUT", "documents"),
            ]
