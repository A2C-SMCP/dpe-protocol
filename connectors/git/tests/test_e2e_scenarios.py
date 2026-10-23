"""端到端场景（#215）：把 test 会话 gitdemo 系列手工验证过的行为固化为 CI 用例。

真实 ``dpe-run`` 子进程、真实 Git connector 插件、参考服务端，中间一层记录请求的代理（设施见
``e2e_harness``）。每一步之后都断言 R = F(S)：与「同一源状态在全新空前缀上全量推送」的结果
一致（``Runner.assert_converged``）。比较以页级快照（每篇的 doc_hash 与按序的 page_hash 列表）
呈现：doc_hash 相同即页列表相同，页级快照用于定位差异，并供用例对单页断言。

整组标记为 ``scenarios``，在 CI 中单独一个 job 跑（见 ``connector-git.yml``）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from dpe_sdk.protocol import SMALL_UPDATE_BYTES
from e2e_harness import (
    Runner,
    Seen,
    advance,
    big_repo,
    foreign_delete,
    foreign_modify,
    make_repo,
    runner,
    sent_element_hashes,
    sent_page_hashes,
)
from helpers import FixtureRepo, requires_git, run_git

pytestmark = [requires_git, pytest.mark.scenarios]


def _write_requests(requests: list[Seen]) -> list[Seen]:
    """一轮里的写路径请求：写请求（commit / delete / move）与 negotiate。"""
    return [r for r in requests if r.is_write or r.target == "negotiate"]


def _writes(requests: list[Seen]) -> list[tuple[str, str]]:
    return [(r.method, r.target) for r in _write_requests(requests)]


def test_basic_six_rounds(tmp_path: Path) -> None:
    """基础 6 轮：全量 → 无变化 → 大仓库新提交（#212 流量断言）→ 新分支 → 改名 → 移出 root。"""
    root = tmp_path / "root"
    big = big_repo(root / "big", per_month=150)
    # 夹具不变量：文档内联超过 SMALL_UPDATE_BYTES（提交说明字节数是下限），更新才走差量路径
    assert len(run_git(big.root, "log", "--format=%B", "main").encode()) > SMALL_UPDATE_BYTES
    make_repo(root / "small", "small 初始化")
    with runner(tmp_path, {"root": str(root), "branches": ["dev"]}) as r:
        # ① 全量
        first = r.round()
        assert first.changed() == {"created": 2} and first.report["incremental"] is False
        before = r.assert_converged()

        # ② 无变化：只有 capabilities，counts 为空
        second = r.round()
        assert second.changed() == {}
        # 精确请求序列只在本地参考服务端下断言（#68 决策 5）
        if r.local_remote:
            assert [(q.method, q.target) for q in second.requests] == [("GET", "capabilities")]

        # ③ 大仓库当月（2 月）新提交：只传新增的内容对象（#212）
        with r.client() as client:
            skeleton = client.get_skeleton(r.uri("big"))
        assert skeleton is not None and len(skeleton.pages) == 2
        january = skeleton.document.pages[0]
        january_elements = skeleton.pages[0].elements
        advance(root / "big", "2 月的新提交", "2026-02-27T12:00:00+00:00")
        third = r.round()
        assert third.changed() == {"updated": 1}
        if r.local_remote:
            assert _writes(third.requests) == [
                ("POST", "negotiate"),
                ("POST", "negotiate"),
                ("PUT", "documents"),
            ]
            first_negotiate, second_negotiate, commit = (
                q.json() for q in _write_requests(third.requests)
            )
            assert "pages" not in first_negotiate  # 只交文档对象
            assert len(second_negotiate["pages"]) == 1  # 只附带变化的 2 月页
            assert len(commit["objects"]) == 1 and "pages" not in commit  # 只内联新提交
        after = r.assert_converged()
        assert after["big"][1][0] == january == before["big"][1][0]  # 1 月页的 page_hash 不变
        # 规范性质（外部模式同样断言）：本轮送出的页恰好是变化的 2 月页，送出的元素恰好是
        # 新增元素。内联与 staging 逐层上传都是合规路径，按集合断言，不绑定请求形状。
        assert after["big"][1][1] != before["big"][1][1]
        assert sent_page_hashes(third.requests) == {after["big"][1][1]}
        with r.client() as client:
            new_skeleton = client.get_skeleton(r.uri("big"))
        assert new_skeleton is not None
        old_elements = set(skeleton.pages[0].elements) | set(skeleton.pages[1].elements)
        new_elements = set(new_skeleton.pages[1].elements) - old_elements
        assert len(new_elements) == 1
        assert sent_element_hashes(third.requests) == new_elements
        for request in third.requests:
            assert not any(e.encode() in request.body for e in january_elements)

        # ④ 小仓库新跟踪分支（带独有提交）：小文档快路径，整篇一次 commit，多一页
        small = FixtureRepo.__new__(FixtureRepo)
        small.root = root / "small"
        _branch(small, "dev", "dev 的独有提交", "2026-01-08T10:00:00+00:00")
        fourth = r.round()
        assert fourth.changed() == {"updated": 1}
        if r.local_remote:
            assert _writes(fourth.requests) == [("PUT", "documents")]
            (put,) = [q for q in fourth.requests if q.is_write]
            assert "staging_session" not in put.json()  # 快路径，不开会话
        snapshot = r.assert_converged()
        assert len(snapshot["small"][1]) == len(after["small"][1]) + 1
        with r.client() as client:
            small_skeleton = client.get_skeleton(r.uri("small"))
        assert small_skeleton is not None and len(small_skeleton.pages[-1].elements) == 1

        # ⑤ 目录改名：只有 move，doc_hash 不变（不重传内容）
        os.rename(root / "small", root / "renamed")
        fifth = r.round()
        # 目标 document 总是紧跟 move（#178），内容未变判 unchanged、不发写
        assert fifth.changed() == {"moved": 1, "unchanged": 1}
        if r.local_remote:
            assert _writes(fifth.requests) == [("POST", "move")]
        moved = r.assert_converged()
        assert moved["renamed"] == snapshot["small"]

        # ⑥ 移出 root：delete
        shutil.move(str(root / "big"), str(tmp_path / "outside"))
        sixth = r.round()
        assert sixth.changed() == {"deleted": 1}
        if r.local_remote:
            assert _writes(sixth.requests) == [("DELETE", "documents")]
        assert set(r.assert_converged()) == {"renamed"}


def _branch(repo: FixtureRepo, name: str, message: str, date: str) -> None:
    """从主线开出跟踪分支、加一个独有提交，并建 ``origin/<name>``（模拟 fetch）。"""
    repo.branch(name, at="main")
    repo.commit(message, date=date)
    repo.add_remote_branch(name)
    repo.checkout("main")


def test_branch_pages_s1_s2(tmp_path: Path) -> None:
    """S1：新分支按名字的 UTF-8 字节序插在中间，只多出这一页、其余页 page_hash 不变；
    S2：删掉分支后该页消失，doc_hash 回到插入前。"""
    repo = make_repo(tmp_path / "root" / "repo", "主线初始化")
    _branch(repo, "alpha", "alpha 的独有提交", "2026-01-10T10:00:00+00:00")
    _branch(repo, "zeta", "zeta 的独有提交", "2026-01-11T10:00:00+00:00")
    # UTF-8 字节序排最后（前提：分支名以预组合形式 C3 B6 存入引用；APFS / Linux 如此，会把
    # 文件名分解为 NFD 的文件系统上不成立）
    _branch(repo, "ötest", "非 ASCII 分支", "2026-01-12T10:00:00+00:00")
    # 配置顺序故意与字节序不同：页序按分支名的 UTF-8 字节序，与配置顺序无关
    config = {"root": str(tmp_path / "root"), "branches": ["zeta", "ötest", "mid", "alpha"]}

    def refs(r: Runner) -> list[str]:
        with r.client() as client:
            skeleton = client.get_skeleton(r.uri("repo"))
        assert skeleton is not None
        return [str((page.page_metadata or {})["ref"]) for page in skeleton.pages]

    with runner(tmp_path, config) as r:
        r.round()
        before = r.assert_converged()["repo"]
        assert refs(r) == ["main", "alpha", "zeta", "ötest"]

        # S1：mid 排在 alpha 与 zeta 之间（中间插入一页）
        _branch(repo, "mid", "mid 的独有提交", "2026-01-13T10:00:00+00:00")
        inserted = r.round()
        assert inserted.changed() == {"updated": 1}
        after = r.assert_converged()["repo"]
        assert refs(r) == ["main", "alpha", "mid", "zeta", "ötest"]
        assert after[1][:2] == before[1][:2] and after[1][3:] == before[1][2:]
        assert after[1][2] not in before[1]

        # S2：删掉 mid 分支（本地与远端引用都消失——只删远端引用时连接器回退读本地分支）
        # → 该页消失，doc_hash 回到插入前
        run_git(repo.root, "branch", "-q", "-D", "mid")
        run_git(repo.root, "update-ref", "-d", "refs/remotes/origin/mid")
        removed = r.round()
        assert removed.changed() == {"updated": 1}
        assert r.assert_converged()["repo"] == before


def test_merge_months_and_force_push_s3_s5(tmp_path: Path) -> None:
    """S3：合并带入的提交（自身时间早于或晚于合并）随合并提交归月；S4：跨月新提交新增一页、
    旧月页不变；S5：amend 后 force-push 只改涉及月份那一页。"""
    repo = make_repo(tmp_path / "root" / "repo", "1 月初始化")
    repo.branch("feature", at="main")
    repo.commit("分支提交：时间早于合并", date="2025-11-03T10:00:00+00:00")
    repo.commit("分支提交：时间晚于合并", date="2026-03-20T10:00:00+00:00")
    repo.checkout("main")
    with runner(tmp_path, {"root": str(tmp_path / "root"), "branches": []}) as r:
        r.round()
        one_month = r.assert_converged()["repo"]
        assert len(one_month[1]) == 1

        # S3：2 月合并 → 合并带入的两个提交与合并提交一起进 2 月页（不按自身时间归 11 月 / 3 月）
        repo.merge("feature", "合并 feature", date="2026-02-15T10:00:00+00:00")
        run_git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        r.round()
        merged = r.assert_converged()["repo"]
        assert len(merged[1]) == 2 and merged[1][0] == one_month[1][0]
        with r.client() as client:
            skeleton = client.get_skeleton(r.uri("repo"))
        assert skeleton is not None and len(skeleton.pages[1].elements) == 3

        # S4：3 月的新提交 → 新增 3 月页，1、2 月页不变
        advance(repo.root, "3 月的提交", "2026-03-05T10:00:00+00:00")
        r.round()
        march = r.assert_converged()["repo"]
        assert len(march[1]) == 3 and march[1][:2] == merged[1]

        # S5：amend 3 月提交并 force-push → 只有 3 月页 page_hash 变化
        run_git(
            repo.root,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--amend",
            "--allow-empty",
            "-m",
            "3 月的提交（改写）",
            env={"GIT_COMMITTER_DATE": "2026-03-05T11:00:00+00:00"},
        )
        run_git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        rewritten = r.round()
        assert rewritten.changed() == {"updated": 1}
        after = r.assert_converged()["repo"]
        assert after[1][:2] == march[1][:2] and after[1][2] != march[1][2]


def test_conflict_policies_s6_s8(tmp_path: Path) -> None:
    """S6：他人改写后源再变，source_wins 覆盖；S7：他人删除后源再变，source_wins 以 if_absent
    重建；S8：report 下 partial（DPE_PRECONDITION_FAILED），cursor_held_since 再跑仍保持，改回
    source_wins 后解开。每步之后 R = F(S)（页级）。"""
    root = tmp_path / "root"
    make_repo(root / "a", "a 初始化")
    make_repo(root / "b", "b 初始化")
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.round()
        r.assert_converged()

        # S6
        theirs = foreign_modify(r, "a")
        advance(root / "a", "a：他人改写之后的源变化", "2026-01-07T10:00:00+00:00")
        overwritten = r.round()
        assert overwritten.report["outcome"] == "succeeded"
        assert overwritten.changed() == {"updated": 1}
        # 写前判定发现 R ≠ L（冲突），按 source_wins 以重读到的他人值为前置条件提交——经 CAS，
        # 不 force。单写解包只在本地参考服务端下断言，外部模式对每个写请求断言同一性质。
        writes = [q for q in overwritten.requests if q.is_write]
        assert writes
        for commit in writes:
            assert theirs in commit.headers.get("if-match", "")
            assert not (commit.json() or {}).get("force")
        r.assert_converged()

        # S7
        foreign_delete(r, "a")
        advance(root / "a", "a：他人删除之后的源变化", "2026-01-08T10:00:00+00:00")
        revived = r.round()
        assert revived.report["outcome"] == "succeeded"
        assert revived.changed() == {"created": 1}
        recreates = [q for q in revived.requests if q.is_write and q.status < 400]
        assert recreates
        for recreate in recreates:
            assert recreate.headers.get("if-none-match") == "*"  # 以 if_absent 复活
        r.assert_converged()

        # S8
        r.policy = {"commit": "report"}
        foreign_modify(r, "b")
        advance(root / "b", "b：report 下的源变化", "2026-01-09T10:00:00+00:00")
        held = r.round(expect=1)
        assert held.report["outcome"] == "partial"
        assert held.codes() == ["DPE_PRECONDITION_FAILED"]
        since = held.report["cursor_held_since"]
        assert since is not None and held.report["cursor_persisted"] is False
        again = r.round(expect=1)
        assert again.report["cursor_held_since"] == since  # 保持首次失败轮的开始时刻
        r.policy = None
        resolved = r.round()
        assert resolved.report["outcome"] == "succeeded"
        assert resolved.report["cursor_held_since"] is None
        r.assert_converged()


def test_crash_before_ack_s9a_s9b(tmp_path: Path) -> None:
    """确认前终止（确定性：代理在写入生效后扣住响应，测试再 kill -9 运行器进程组）。

    S9a：commit 生效、未确认 → 重跑判 unchanged，不报冲突（意图日志对账）；
    S9b（#213）：delete 生效、未确认 → 源原样恢复 → 重跑补一次全量刷新，`round.refreshed`
    带 1 个 URI、重建该文档，R = F(S)；再跑一轮不再刷新。
    """
    root = tmp_path / "root"
    make_repo(root / "notes", "notes 初始化")
    make_repo(root / "scratch", "scratch 初始化")
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.round()
        r.assert_converged()

        # S9a
        advance(root / "notes", "notes 新提交", "2026-01-07T10:00:00+00:00")
        crashed = r.crash_on_next_write()
        assert (crashed.method, crashed.target) == ("PUT", "documents")
        rerun = r.round()
        assert rerun.report["outcome"] == "succeeded"
        assert rerun.changed() == {"unchanged": 1} and not rerun.report["failures"]
        r.assert_converged()

        # S9b
        shutil.move(str(root / "scratch"), str(tmp_path / "scratch.away"))
        crashed = r.crash_on_next_write()
        assert (crashed.method, crashed.target) == ("DELETE", "documents")
        shutil.move(str(tmp_path / "scratch.away"), str(root / "scratch"))
        refreshed = r.round()
        assert refreshed.report["outcome"] == "succeeded"
        assert [e["uris"] for e in refreshed.named("round.refreshed")] == [1]
        assert refreshed.changed() == {"created": 1}
        r.assert_converged()
        settled = r.round()
        assert settled.named("round.refreshed") == [] and settled.changed() == {}


def test_crash_after_create_then_source_removes_it(tmp_path: Path) -> None:
    """刷新的另一个方向（#213）：新仓库的 commit 生效、未确认，源随后又没有它 → 重跑刷新时
    全量扫描不再产出它、游标那一刻它不存在 → 以 L 删除，回到游标那一刻的状态，R = F(S)。"""
    root = tmp_path / "root"
    make_repo(root / "notes", "notes 初始化")
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.round()
        make_repo(root / "fresh", "新仓库")
        crashed = r.crash_on_next_write()
        assert (crashed.method, crashed.target) == ("PUT", "documents")
        shutil.rmtree(root / "fresh")
        refreshed = r.round()
        assert refreshed.report["outcome"] == "succeeded"
        assert [e["uris"] for e in refreshed.named("round.refreshed")] == [1]
        assert refreshed.changed() == {"deleted": 1}
        assert set(r.assert_converged()) == {"notes"}


def test_report_partial_then_restore_s9c(tmp_path: Path) -> None:
    """S9c / 固化链（#213）：report 下同一轮一个 delete 成功、另一篇冲突（partial，游标不推进）→
    被删的仓库原样移回 → 冲突未解时仍 partial、remote 仍缺它（已知行为：等全部落地才对账）→
    改回 source_wins 的那一轮先刷新补回、再推进游标，R = F(S)；之后不再刷新。"""
    root = tmp_path / "root"
    make_repo(root / "notes", "notes 初始化")
    make_repo(root / "scratch", "scratch 初始化")
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.round()
        r.policy = {"commit": "report"}
        foreign_modify(r, "notes")
        advance(root / "notes", "notes：report 下的源变化", "2026-01-07T10:00:00+00:00")
        shutil.move(str(root / "scratch"), str(tmp_path / "scratch.away"))
        partial = r.round(expect=1)
        assert partial.report["outcome"] == "partial"
        assert partial.codes() == ["DPE_PRECONDITION_FAILED"]
        assert partial.counts["deleted"] == 1 and partial.report["cursor_persisted"] is False

        shutil.move(str(tmp_path / "scratch.away"), str(root / "scratch"))
        still = r.round(expect=1)
        assert still.report["outcome"] == "partial"
        assert "scratch" not in r.snapshot()  # 冲突未解时不对账（游标仍被扣住，不会固化）

        r.policy = None
        resolved = r.round()
        assert resolved.report["outcome"] == "succeeded"
        assert resolved.report["cursor_persisted"] is True
        assert [e["uris"] for e in resolved.named("round.refreshed")] == [1]
        r.assert_converged()
        settled = r.round()
        assert settled.named("round.refreshed") == [] and settled.changed() == {}


def test_null_cursor_then_source_returns(tmp_path: Path) -> None:
    """插件本轮给出 ``cursor: null``（运行器保留旧游标）后源回到旧游标时的状态（#213）：
    第一轮写入生效但游标不变；第二轮插件按旧游标看不到变化，运行器刷新 2 个 URI，R = F(S)。"""
    root = tmp_path / "root"
    notes = make_repo(root / "notes", "notes 初始化")
    make_repo(root / "scratch", "scratch 初始化")
    flag = tmp_path / "NULLCURSOR"
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.nullcursor_flag = flag
        r.round()
        original = notes.sha()

        flag.touch()
        advance(root / "notes", "notes 新提交", "2026-01-07T10:00:00+00:00")
        shutil.move(str(root / "scratch"), str(tmp_path / "scratch.away"))
        nulled = r.round()
        assert nulled.report["outcome"] == "succeeded"
        assert nulled.report["cursor_persisted"] is False
        assert nulled.changed() == {"updated": 1, "deleted": 1}

        flag.unlink()
        run_git(root / "notes", "reset", "-q", "--hard", original)
        run_git(root / "notes", "update-ref", "refs/remotes/origin/main", original)
        shutil.move(str(tmp_path / "scratch.away"), str(root / "scratch"))
        refreshed = r.round()
        assert refreshed.report["outcome"] == "succeeded"
        assert [e["uris"] for e in refreshed.named("round.refreshed")] == [2]
        r.assert_converged()


def test_repo_mode_and_since(tmp_path: Path) -> None:
    """repo 模式 + ``since``：since 只裁剪默认分支的月页（起点之前的月份不出现），分支页只表达
    独有提交、不受 since 影响；增量轮页级收敛。"""
    repo = make_repo(tmp_path / "solo", "1 月：起点之前")
    repo.branch("dev", at="main")
    repo.commit("dev 的独有提交（时间早于起点）", date="2025-12-01T10:00:00+00:00")
    repo.add_remote_branch("dev")
    repo.checkout("main")
    start = advance(repo.root, "2 月：起点", "2026-02-03T10:00:00+00:00")
    config = {"repo": str(repo.root), "branches": ["dev"], "since": start}
    with runner(tmp_path, config) as r:
        first = r.round()
        assert first.changed() == {"created": 1}
        pages = r.assert_converged()["solo"][1]
        assert len(pages) == 2  # 2 月页 + dev 页（1 月被 since 裁掉）
        with r.client() as client:
            skeleton = client.get_skeleton(r.uri("solo"))
        assert skeleton is not None
        assert [p.page_metadata for p in skeleton.pages] == [
            {"ref": "main", "period": "2026-02"},
            {"ref": "dev"},
        ]
        # 2 月页只有起点那一个提交（1 月的被裁掉）；dev 页保留早于起点的独有提交（不受 since 影响）
        assert len(skeleton.pages[0].elements) == 1 and len(skeleton.pages[1].elements) == 1

        advance(repo.root, "3 月", "2026-03-03T10:00:00+00:00")
        step = r.round()
        assert step.changed() == {"updated": 1}
        after = r.assert_converged()["solo"][1]
        assert after[0] == pages[0] and after[-1] == pages[-1] and len(after) == 3


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位")
def test_unreadable_root_fails_retryably(tmp_path: Path) -> None:
    """root 不可读：scan 以 -32003（retryable）失败 → 本轮 failed、source_failed、可重试、游标
    不推进；恢复权限后照常成功并收敛。"""
    root = tmp_path / "root"
    make_repo(root / "a", "a 初始化")
    with runner(tmp_path, {"root": str(root), "branches": []}) as r:
        r.round()
        advance(root / "a", "a 新提交", "2026-01-07T10:00:00+00:00")
        root.chmod(0o000)
        try:
            failed = r.round(expect=2)
        finally:
            root.chmod(0o700)
        assert failed.report["outcome"] == "failed"
        fatal = failed.report["failures"][-1]
        assert (fatal["code"], fatal["retryable"]) == ("source_failed", True)
        assert failed.report["cursor_persisted"] is False
        recovered = r.round()
        assert recovered.changed() == {"updated": 1}
        r.assert_converged()
