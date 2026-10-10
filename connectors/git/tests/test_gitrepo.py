"""git 访问层单测：仓库发现、默认分支、分支解析、提交图读取与失败语义。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureRepo, git_env, requires_git, run_git

from dpe_git_connector.gitrepo import (
    GitContentError,
    GitError,
    GitRepo,
    discover_repos,
    looks_like_repo,
)

pytestmark = requires_git


def simple_repo(root: Path, *, default_branch: str = "main") -> FixtureRepo:
    """一个小仓库：两个提交 + origin/HEAD 指向默认分支（像一份克隆）。"""
    repo = FixtureRepo(root, default_branch=default_branch)
    repo.commit("第一次提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    repo.commit("第二次提交", date="2026-01-07T10:00:00+00:00", files={"a.txt": "a\na\n"})
    repo.set_origin_head(default_branch)
    return repo


# --------------------------------------------------------------------------- 发现


def test_discovery_is_recursive_and_skips_repo_interiors(tmp_path: Path) -> None:
    """递归发现、按身份字节序；进入一个仓库后不再往里找（子模块 / 工作树目录不是独立仓库）。"""
    root = tmp_path / "root"
    alpha = simple_repo(root / "alpha")
    nested = simple_repo(root / "nested" / "beta")
    # 仓库内部再有形似仓库的目录：不得被发现（发现层不进入已发现的仓库）
    inner = root / "alpha" / "inner"
    inner.mkdir(parents=True)
    (inner / ".git").mkdir()
    plain = root / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("x", encoding="utf-8")

    found, unreadable = discover_repos(str(root))
    assert [item.identifier for item in found] == [b"alpha", b"nested/beta"]
    assert [item.path for item in found] == [str(alpha.root), str(nested.root)]
    assert unreadable == []


def test_discovery_finds_bare_repo(tmp_path: Path) -> None:
    work = simple_repo(tmp_path / "work")
    bare = tmp_path / "bare.git"
    run_git(work.root, "clone", "-q", "--bare", str(work.root), str(bare))
    found, _ = discover_repos(str(tmp_path))
    # 裸仓库（HEAD + objects + refs）也在发现范围内
    assert [item.identifier for item in found] == [b"bare.git", b"work"]


def test_discovery_records_unreadable_subtree(tmp_path: Path) -> None:
    """子孙目录读不了：记录到第二个返回值（出条目级 error），不废掉整轮；root 自身读不了才抛错。"""
    if os.geteuid() == 0:
        pytest.skip("root 无视权限位，chmod 0 造不出不可读目录")
    root = tmp_path / "root"
    repo = simple_repo(root / "alpha")
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "inner").mkdir()
    os.chmod(hidden, 0)
    try:
        found, unreadable = discover_repos(str(root))
        assert [item.identifier for item in found] == [b"alpha"]
        assert [item.identifier for item in unreadable] == [b"hidden"]
        assert unreadable[0].path == str(hidden)
    finally:
        os.chmod(hidden, 0o755)  # 让 pytest 能清理 tmp_path
    # root 自身不可读：没有可枚举的范围，整轮失败（GitError）
    os.chmod(root, 0)
    try:
        with pytest.raises(GitError, match="root"):
            discover_repos(str(root))
    finally:
        os.chmod(root, 0o755)
    assert repo.root.exists()


def test_looks_like_repo_heuristic(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo")
    assert looks_like_repo(repo.root) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert looks_like_repo(plain) is False
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    assert looks_like_repo(fake) is True  # 候选；可用性由 open 判定


# --------------------------------------------------------------------------- 打开与默认分支


def test_open_validates_repo_root(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo")
    GitRepo(str(repo.root)).open()
    with pytest.raises(GitError, match="不是可用的 git 仓库"):
        GitRepo(str(tmp_path / "nope")).open()
    sub = repo.root / "sub"
    sub.mkdir()
    with pytest.raises(GitError, match="仓库根"):
        GitRepo(str(sub)).open()
    with pytest.raises(GitError, match="不是可用的 git 仓库"):
        GitRepo(str(tmp_path / "nope-dir")).open()


def test_bare_repository_is_supported(tmp_path: Path) -> None:
    work = simple_repo(tmp_path / "work")
    bare = tmp_path / "bare.git"
    run_git(work.root, "clone", "-q", "--bare", str(work.root), str(bare))
    repo = GitRepo(str(bare))
    repo.open()
    assert repo.resolve_branch("main") == work.sha("main")
    assert [record.message for record in repo.log(work.sha("main"))] == [
        "第一次提交\n",
        "第二次提交\n",
    ]


def test_has_refs_reports_empty_repository(tmp_path: Path) -> None:
    empty = FixtureRepo(tmp_path / "empty")
    assert GitRepo(str(empty.root)).has_refs() is False
    repo = simple_repo(tmp_path / "repo")
    assert GitRepo(str(repo.root)).has_refs() is True


def test_default_branch_comes_from_origin_head(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo", default_branch="trunk")
    assert GitRepo(str(repo.root)).default_branch(None) == "trunk"
    # 显式配置优先
    assert GitRepo(str(repo.root)).default_branch("main") == "main"


def test_default_branch_ignores_local_head(tmp_path: Path) -> None:
    """工作区 checkout 改动本地 HEAD 不得改变默认分支（它属于运行环境状态，不是源内容）。"""
    repo = simple_repo(tmp_path / "repo")
    repo.branch("dev")
    repo.checkout("dev")
    assert GitRepo(str(repo.root)).default_branch(None) == "main"


def test_default_branch_missing_origin_head_raises(tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "repo")
    repo.commit("提交", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    with pytest.raises(GitError, match="默认分支"):
        GitRepo(str(repo.root)).default_branch(None)
    # 显式配置后可用
    assert GitRepo(str(repo.root)).default_branch("main") == "main"


# --------------------------------------------------------------------------- 分支与提交解析


def test_resolve_branch_prefers_origin_ref(tmp_path: Path) -> None:
    """``origin/<name>`` 优先于本地 ``refs/heads/<name>``（README 写明优先级）。"""
    repo = FixtureRepo(tmp_path / "repo")
    first = repo.commit("一", date="2026-01-06T10:00:00+00:00", files={"a.txt": "1\n"})
    second = repo.commit("二", date="2026-01-07T10:00:00+00:00", files={"a.txt": "2\n"})
    repo.branch("dev", at=first)
    repo.checkout("main")
    repo.add_remote_branch("dev", at=second)  # 远端 dev 领先于本地 dev
    assert GitRepo(str(repo.root)).resolve_branch("dev") == second


def test_resolve_branch_falls_back_to_local_and_reports_missing(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo")
    repo.branch("dev")
    repo.checkout("main")
    git = GitRepo(str(repo.root))
    assert git.resolve_branch("dev") == repo.sha("dev")
    assert git.resolve_branch("nope") is None
    assert git.resolve_branch("bad name..") is None  # 非法名字按解析不到处理，不抛异常


def test_resolve_ref_and_is_ancestor(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo")
    first = repo.sha("HEAD~1")
    git = GitRepo(str(repo.root))
    assert git.resolve_ref(first) == first
    assert git.resolve_ref("does-not-exist") is None
    assert git.is_ancestor(first, repo.sha("HEAD")) is True
    assert git.is_ancestor(repo.sha("HEAD"), first) is False


# --------------------------------------------------------------------------- 提交图


def test_first_parent_chain_and_log_order(tmp_path: Path) -> None:
    """first-parent 链旧在前；log 是拓扑序（旧在前），被合并的提交排在其合并提交之前。"""
    repo = FixtureRepo(tmp_path / "repo")
    c1 = repo.commit("一", date="2026-01-06T10:00:00+00:00", files={"a.txt": "1\n"})
    repo.branch("side", at=c1)
    s1 = repo.commit("侧枝", date="2026-01-07T10:00:00+00:00", files={"b.txt": "1\n"})
    repo.checkout("main")
    c2 = repo.commit("二", date="2026-01-08T10:00:00+00:00", files={"a.txt": "2\n"})
    merge = repo.merge("side", "合并 side", date="2026-01-09T10:00:00+00:00")

    git = GitRepo(str(repo.root))
    assert git.first_parent_chain(merge) == [c1, c2, merge]
    # 拓扑序（旧在前）：主线提交在前，被合并的侧枝排在其合并提交之前。git 的 --topo-order
    # 对独立分支的次序是确定的（实测主线优先），页内元素序因此稳定。
    assert [record.sha for record in git.log(merge)] == [c1, c2, s1, merge]
    # 空范围：空列表（分支已并入默认分支时没有独有提交）
    assert git.log(f"{merge}..{s1}") == []


def test_log_parses_metadata_and_preserves_message(tmp_path: Path) -> None:
    repo = simple_repo(tmp_path / "repo")
    repo.commit(
        "带正文的提交\n\n第二段。\n",
        date="2026-02-03T04:05:06+08:00",
        author="Bob",
        email="bob@example.invalid",
        files={"b.txt": "x\n"},
    )
    record = GitRepo(str(repo.root)).log("HEAD")[-1]
    assert record.author == "Bob <bob@example.invalid>"
    # 带偏移的时间归一为 RFC 3339 UTC
    assert record.authored_at == "2026-02-02T20:05:06Z"
    assert record.committed_at == "2026-02-02T20:05:06Z"
    assert record.message == "带正文的提交\n\n第二段。\n"
    assert record.parents == (repo.sha("HEAD~1"),)


def test_log_rejects_non_utf8_message(tmp_path: Path) -> None:
    """说明不是合法 UTF-8 时如实报错（进不了 JSON 字符串），不做替换或猜测编码。

    抛 ``GitContentError``（内容类失败）而不是普通 ``GitError``：条目级归属是
    ``content_invalid``、不可重试（§6.5 的封闭枚举按定义区分）。

    ``commit-tree`` 会把不是 UTF-8 的说明转码成 UTF-8（实测），因此这里用 ``hash-object``
    直接写入对象——模拟历史里真实存在的非 UTF-8 提交（无 ``encoding`` 头）。
    """
    repo = simple_repo(tmp_path / "repo")
    tree = run_git(repo.root, "rev-parse", "HEAD^{tree}").strip()
    raw = (
        f"tree {tree}\n"
        "author T <t@example.invalid> 1767225600 +0000\n"
        "committer T <t@example.invalid> 1767225600 +0000\n"
        "\n"
    ).encode() + b"\xff\xfe not utf8\n"
    completed = subprocess.run(
        ["git", "-C", str(repo.root), "hash-object", "-w", "-t", "commit", "--stdin"],
        capture_output=True,
        check=False,
        env=git_env(),
        input=raw,
    )
    assert completed.returncode == 0
    sha = completed.stdout.decode().strip()
    run_git(repo.root, "update-ref", "refs/heads/bad", sha)
    with pytest.raises(GitContentError, match="UTF-8"):
        GitRepo(str(repo.root)).log("bad")


def test_plugin_environment_is_isolation_safe() -> None:
    """git 子进程不继承会改变行为的变量（这里只断言关键项在构造时被覆盖）。"""
    from dpe_git_connector.gitrepo import _git_env

    env = _git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_inherited_git_env_does_not_redirect_repo(tmp_path: Path, monkeypatch: Any) -> None:
    """继承的 GIT_DIR / GIT_WORK_TREE 不得改写仓库发现（从 git hook 启动的场景）。"""
    target = simple_repo(tmp_path / "target")
    other = simple_repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other.root))
    assert GitRepo(str(target.root)).resolve_branch("main") == target.sha("main")
