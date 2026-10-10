"""git 访问层单测：路径原字节、排序、非 UTF-8 路径与空仓库/坏 ref 的失败语义。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import init_repo, requires_git, run_git

from dpe_git_connector.gitrepo import GitError, GitRepo

pytestmark = requires_git


def test_lists_blobs_sorted_by_path_bytes(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {"z.txt": b"z", "a/b.md": b"b", "Makefile": b"m"})
    repo = GitRepo(str(repo_dir), "main")
    entries = repo.list_blobs()
    assert [entry.path for entry in entries] == [b"Makefile", b"a/b.md", b"z.txt"]
    assert all(entry.object_id and entry.size >= 0 for entry in entries)


def test_non_ascii_and_special_path_bytes_round_trip(tmp_path: Path) -> None:
    """路径按原始字节处理：中文、空格、制表符都原样读回。"""
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {"中文 空格.txt": "内容\n".encode(), "tab\tname.txt": b"t\n"})
    repo = GitRepo(str(repo_dir), "main")
    entries = repo.list_blobs()
    assert [entry.path for entry in entries] == [b"tab\tname.txt", "中文 空格.txt".encode()]
    first = entries[0]
    assert repo.read_blob(first.object_id, first.size) == b"t\n"


@pytest.mark.skipif(
    sys.platform == "darwin", reason="macOS 的 APFS 不接受非 UTF-8 的文件名（Errno 92）"
)
def test_non_utf8_path_bytes_round_trip(tmp_path: Path) -> None:
    """非 UTF-8 路径在 Linux 上按原始字节读回（file_uri 由百分号编码表达，见 test_mapping）。"""
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {b"bad_\xff\xfe.txt": b"x\n"})
    repo = GitRepo(str(repo_dir), "main")
    entries = repo.list_blobs()
    assert [entry.path for entry in entries] == [b"bad_\xff\xfe.txt"]
    entry = entries[0]
    assert repo.read_blob(entry.object_id, entry.size) == b"x\n"


def test_submodule_entries_are_skipped(tmp_path: Path) -> None:
    """子模块在 ls-tree 里是 ``commit`` 类型：不是 blob，不枚举。"""
    inner = tmp_path / "inner"
    init_repo(inner, {"a.txt": b"a\n"})
    outer = tmp_path / "outer"
    init_repo(outer, {"readme.md": b"# x\n"})
    run_git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    run_git(outer, "-c", "commit.gpgsign=false", "commit", "-qm", "add submodule")

    entries = GitRepo(str(outer), "main").list_blobs()
    # 子模块在树里是 mode 160000 的 commit 类型：跳过；.gitmodules 是普通 blob，照常枚举
    assert [entry.path for entry in entries] == [b".gitmodules", b"readme.md"]


def test_repo_must_exist(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="不可用"):
        GitRepo(str(tmp_path / "nope"), "main").open()


def test_directory_without_git_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError, match="不是可用的 git 仓库"):
        GitRepo(str(plain), "main").open()


def test_empty_repo_has_no_commit(tmp_path: Path) -> None:
    repo_dir = tmp_path / "empty"
    repo_dir.mkdir()
    run_git(repo_dir, "init", "-q", "-b", "main", ".")
    with pytest.raises(GitError, match="ref 无法解析为提交"):
        GitRepo(str(repo_dir), "HEAD").open()


def test_unknown_ref_and_wrong_size(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {"a.txt": b"abc\n"})
    with pytest.raises(GitError, match="ref 无法解析为提交"):
        GitRepo(str(repo_dir), "nosuch").open()
    repo = GitRepo(str(repo_dir), "main")
    entry = repo.list_blobs()[0]
    with pytest.raises(GitError, match="长度"):
        repo.read_blob(entry.object_id, entry.size + 1)


def test_tags_and_shas_resolve(tmp_path: Path) -> None:
    """ref 可以是分支、标签或提交 id（``^{commit}`` 解引用）。"""
    repo_dir = tmp_path / "repo"
    commit = init_repo(repo_dir, {"a.txt": b"a\n"})
    run_git(repo_dir, "tag", "v1")
    assert GitRepo(str(repo_dir), "v1").open() == commit
    assert GitRepo(str(repo_dir), commit).open() == commit
    assert GitRepo(str(repo_dir), "HEAD").open() == commit


def test_plugin_environment_is_isolation_safe() -> None:
    """git 子进程不继承会改变行为的变量（这里只断言关键项在构造时被覆盖）。"""
    from dpe_git_connector.gitrepo import _git_env

    env = _git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_symlinks_are_skipped(tmp_path: Path) -> None:
    """符号链接（mode 120000）不枚举：它的内容是链接目标，不是源文件内容。"""
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {"real.md": b"# real\n"})
    (repo_dir / "link.md").symlink_to("real.md")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "-c", "commit.gpgsign=false", "commit", "-qm", "symlink")

    entries = GitRepo(str(repo_dir), "main").list_blobs()
    assert [entry.path for entry in entries] == [b"real.md"]


def test_bare_repository_is_supported(tmp_path: Path) -> None:
    """裸仓库（--bare / --mirror）没有工作树，但 ls-tree/cat-file 完全可用。"""
    work = tmp_path / "work"
    commit = init_repo(work, {"a.md": b"# a\n"})
    bare = tmp_path / "bare.git"
    run_git(work, "clone", "-q", "--bare", str(work), str(bare))

    repo = GitRepo(str(bare), "main")
    assert repo.open() == commit
    entries = repo.list_blobs()
    assert [entry.path for entry in entries] == [b"a.md"]
    assert repo.read_blob(entries[0].object_id, entries[0].size) == b"# a\n"


def test_resolve_can_be_called_again_for_new_commits(tmp_path: Path) -> None:
    """resolve 每次重新解析 ref（轮首调用）：分支前进后能看到新提交。"""
    repo_dir = tmp_path / "repo"
    first = init_repo(repo_dir, {"a.md": b"# a\n"})
    repo = GitRepo(str(repo_dir), "main")
    assert repo.open() == first
    (repo_dir / "b.md").write_bytes(b"# b\n")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "-c", "commit.gpgsign=false", "commit", "-qm", "second")
    assert repo.resolve() != first
    assert len(repo.list_blobs()) == 2


def test_subdirectory_is_rejected(tmp_path: Path) -> None:
    """repo 必须是仓库根：指向子目录会丢掉路径前缀，同一文件得到不同身份（W11 残留修复）。"""
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir, {"docs/b.md": b"# b\n"})
    with pytest.raises(GitError, match="仓库根"):
        GitRepo(str(repo_dir / "docs"), "main").open()


def test_inherited_git_env_does_not_redirect_repo(tmp_path: Path, monkeypatch: Any) -> None:
    """继承的 GIT_DIR / GIT_WORK_TREE 不得改写仓库发现（从 git hook 启动的场景）。"""
    target = tmp_path / "target"
    init_repo(target, {"a.md": b"# a\n"})
    other = tmp_path / "other"
    init_repo(other, {"b.md": b"# b\n"})

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    entries = GitRepo(str(target), "main").list_blobs()
    assert [entry.path for entry in entries] == [b"a.md"]
