"""子模块各成一篇文档（#221）：发现判据、映射等同独立仓库、两种模式的增量判定。

子模块用真实的 ``git submodule add``（本地路径作 url，需放行 file 协议）现场构造；老式子模块
（``.git`` 是目录）、未初始化、不合法路径等用手写的 ``.gitmodules`` 构造——发现只读
``.gitmodules`` 与该路径下有没有 ``.git``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureRepo, requires_git, run_git
from test_incremental import marks_for, run, serve_for, uri
from test_server import handle, initialize_params, request

from dpe_git_connector.gitrepo import GitRepo, RepoDir, discover_repos, expand_submodules
from dpe_git_connector.mapping import DocumentMapper, RepoSpec
from dpe_git_connector.server import Serve

pytestmark = requires_git

PREFIX = "git://s/"

posix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位"
)


def make_repo(path: Path, message: str, *, months: int = 1) -> FixtureRepo:
    repo = FixtureRepo(path)
    for month in range(1, months + 1):
        repo.commit(f"{message} {month}", date=f"2026-0{month}-06T10:00:00+00:00")
    repo.set_origin_head("main")
    return repo


def add_submodule(parent: FixtureRepo, url: Path, path: str, *, date: str) -> None:
    run_git(
        parent.root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(url), path
    )
    parent.commit(f"添加子模块 {path}", date=date)
    run_git(parent.root, "update-ref", "refs/remotes/origin/main", "HEAD")


def write_gitmodules(parent: Path, *paths: str) -> None:
    body = "".join(f'[submodule "{p}"]\n\tpath = {p}\n\turl = ./x\n' for p in paths)
    (parent / ".gitmodules").write_text(body, encoding="utf-8")


def identifiers(found: list[RepoDir]) -> list[bytes]:
    return [item.identifier for item in found]


# ------------------------------------------------------------------------------- 发现


def test_initialized_submodules_are_found_recursively(tmp_path: Path) -> None:
    """已初始化的子模块（含嵌套）各是一个候选，身份 = 父身份 + / + 路径；父仓库照旧在。"""
    inner = make_repo(tmp_path / "up" / "inner", "inner")
    lib = make_repo(tmp_path / "up" / "lib", "lib")
    add_submodule(lib, inner.root, "deps/inner", date="2026-02-06T10:00:00+00:00")
    root = tmp_path / "root"
    big = make_repo(root / "big", "big")
    add_submodule(big, lib.root, "mods/lib", date="2026-02-06T10:00:00+00:00")
    run_git(
        big.root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "-q",
        "--init",
        "--recursive",
    )
    found, unreadable = discover_repos(str(root))
    assert identifiers(found) == [b"big", b"big/mods/lib", b"big/mods/lib/deps/inner"]
    assert unreadable == []


def test_old_style_submodule_with_a_git_directory_is_found(tmp_path: Path) -> None:
    """老式子模块的 ``.git`` 是目录：只要 ``.gitmodules`` 登记了该路径就认。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "old", "old")
    make_repo(parent.root / "stray", "stray")  # 没登记的嵌套仓库照旧不算
    write_gitmodules(parent.root, "old")
    found, _ = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert identifiers(found) == [b"parent/old"]


def test_uninitialized_and_unsafe_submodules_only_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """未初始化、路径不合法或经过符号链接：只记 warn（相对路径），不是候选也不是错误。"""
    parent = make_repo(tmp_path / "parent", "parent")
    (parent.root / "empty").mkdir()
    make_repo(tmp_path / "elsewhere", "elsewhere")
    os.symlink(tmp_path / "elsewhere", parent.root / "linked")
    write_gitmodules(parent.root, "empty", "missing", "../escape", "a/../b", ".git/x", "linked")
    with caplog.at_level("WARNING", logger="dpe_git_connector"):
        found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert found == [] and unreadable == []
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 6
    assert not any(str(tmp_path) in message for message in messages)


def test_absolute_submodule_path_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent = make_repo(tmp_path / "parent", "parent")
    write_gitmodules(parent.root, str(tmp_path / "parent"))
    with caplog.at_level("WARNING", logger="dpe_git_connector"):
        found, _ = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert found == [] and len(caplog.records) == 1


def test_symlinked_gitmodules_is_not_followed(tmp_path: Path) -> None:
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "sub", "sub")
    write_gitmodules(tmp_path, "sub")
    os.symlink(tmp_path / ".gitmodules", parent.root / ".gitmodules")
    found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert found == [] and unreadable == []


def test_malformed_gitmodules_is_unreadable(tmp_path: Path) -> None:
    """``.gitmodules`` 解析失败：记为读不了（调用方出条目级 error，本轮 delete / move 暂缓）。"""
    parent = make_repo(tmp_path / "parent", "parent")
    (parent.root / ".gitmodules").write_text('[submodule "x"\n\tpath = x\n', encoding="utf-8")
    found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert found == [] and identifiers(unreadable) == [b"parent/.gitmodules"]


def test_gitmodules_includes_are_not_followed(tmp_path: Path) -> None:
    """只读这一个文件：``include.path`` 指向的文件不参与发现。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "sub", "sub")
    write_gitmodules(tmp_path, "sub")
    (parent.root / ".gitmodules").write_text(
        f"[include]\n\tpath = {tmp_path / '.gitmodules'}\n", encoding="utf-8"
    )
    found, _ = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert found == []


def test_discovery_does_not_depend_on_the_plugin_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插件进程的当前目录恰好是损坏的仓库（.git 文件指向不存在的目录）：照常发现子模块。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "sub", "sub")
    write_gitmodules(parent.root, "sub")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    monkeypatch.chdir(broken)
    found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert identifiers(found) == [b"parent/sub"] and unreadable == []


def test_duplicate_registrations_of_one_path_yield_one_submodule(tmp_path: Path) -> None:
    """规范化之后去重：``a//b`` 与 ``a/b/`` 是同一个子模块，只有一个候选。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "a" / "b", "sub")
    write_gitmodules(parent.root, "a//b", "a/b/")
    found, _ = expand_submodules([RepoDir(str(parent.root), b"parent")])
    assert identifiers(found) == [b"parent/a/b"]


@posix_permissions
def test_permission_denied_gitmodules_is_unreadable_not_empty(tmp_path: Path) -> None:
    """``.gitmodules`` 没有读权限：记为读不了，而不是「没有子模块」（git 对两者都返回 1）。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "sub", "sub")
    write_gitmodules(parent.root, "sub")
    (parent.root / ".gitmodules").chmod(0)
    try:
        found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    finally:
        (parent.root / ".gitmodules").chmod(0o644)
    assert found == [] and identifiers(unreadable) == [b"parent/.gitmodules"]


@posix_permissions
def test_permission_denied_submodule_directory_is_unreadable_not_uninitialized(
    tmp_path: Path,
) -> None:
    """子模块路径上的目录读不了：记为读不了，而不是「未初始化」（不能让它被当成消失）。"""
    parent = make_repo(tmp_path / "parent", "parent")
    make_repo(parent.root / "mods" / "sub", "sub")
    write_gitmodules(parent.root, "mods/sub")
    (parent.root / "mods").chmod(0)
    try:
        found, unreadable = expand_submodules([RepoDir(str(parent.root), b"parent")])
    finally:
        (parent.root / "mods").chmod(0o755)
    assert found == [] and identifiers(unreadable) == [b"parent/mods/sub"]


# ------------------------------------------------------------------------------- 映射


def test_a_submodule_maps_exactly_like_its_standalone_clone(tmp_path: Path) -> None:
    """子模块只认它自己的 origin/HEAD：文档与同一项目的独立克隆逐字节相同，与父仓库锁定的
    提交、游离的 HEAD、.gitmodules 的 branch 都无关。"""
    lib = make_repo(tmp_path / "up" / "lib", "lib", months=2)
    big = make_repo(tmp_path / "big", "big")
    add_submodule(big, lib.root, "mods/lib", date="2026-03-06T10:00:00+00:00")
    run_git(big.root, "config", "-f", ".gitmodules", "submodule.mods/lib.branch", "elsewhere")
    run_git(big.root / "mods" / "lib", "checkout", "-q", "--detach", "HEAD~1")
    run_git(tmp_path, "clone", "-q", str(lib.root), "standalone")
    mapper = DocumentMapper(PREFIX)

    def pages(path: Path) -> Any:
        item = mapper.document(GitRepo(str(path)), RepoDir(str(path), b"x"), RepoSpec())
        assert item.kind == "document", item.fields
        return item.fields["pages"]

    assert pages(big.root / "mods" / "lib") == pages(tmp_path / "standalone")


# ------------------------------------------------------------------------- 增量：root 模式


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, FixtureRepo, FixtureRepo]:
    lib = make_repo(tmp_path / "up" / "lib", "lib")
    root = tmp_path / "root"
    big = make_repo(root / "big", "big")
    add_submodule(big, lib.root, "mods/lib", date="2026-02-06T10:00:00+00:00")
    return root, big, lib


def test_root_mode_submodule_is_a_document_and_follows_move_and_removal(
    world: tuple[Path, FixtureRepo, FixtureRepo],
) -> None:
    root, big, _ = world
    serve = serve_for(root)
    first = run(serve)
    assert first.kinds() == [("document", uri("big")), ("document", uri("big/mods/lib"))]

    run_git(big.root, "mv", "mods/lib", "mods/renamed")
    big.commit("移动子模块", date="2026-03-06T10:00:00+00:00")
    run_git(big.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    moved = run(serve, first.cursor)
    assert moved.kinds() == [
        ("move", uri("big/mods/lib"), uri("big/mods/renamed")),
        ("document", uri("big")),
        ("document", uri("big/mods/renamed")),
    ]

    run_git(big.root, "rm", "-q", "mods/renamed")
    big.commit("移除子模块", date="2026-04-06T10:00:00+00:00")
    run_git(big.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    removed = run(serve, moved.cursor)
    assert removed.kinds() == [("delete", uri("big/mods/renamed")), ("document", uri("big"))]


def test_unreadable_gitmodules_holds_back_deletes(
    world: tuple[Path, FixtureRepo, FixtureRepo],
) -> None:
    """``.gitmodules`` 暂时解析不了：不把子模块当作已删除（fail-closed），如实上报 error。"""
    root, big, _ = world
    serve = serve_for(root)
    first = run(serve)
    (big.root / ".gitmodules").write_text("[broken\n", encoding="utf-8")
    second = run(serve, first.cursor)
    assert ("error", uri("big/.gitmodules")) in second.kinds()
    assert not any(kind[0] == "delete" for kind in second.kinds())
    assert b"big/mods/lib" in marks_for(second.cursor, {"root": str(root)})


# ------------------------------------------------------------------------- 增量：repo 模式


def repo_mode(path: Path, **config: Any) -> tuple[Serve, dict[str, Any]]:
    serve = Serve()
    config = {"repo": str(path), **config}
    handle(serve, request(1, "initialize", initialize_params(config)))
    return serve, config


def test_repo_mode_expands_submodules_with_the_same_identities(
    world: tuple[Path, FixtureRepo, FixtureRepo],
) -> None:
    """repo 模式同样展开子模块：身份 = 目录名 / 路径；since 只作用于配置的仓库本身。"""
    _, big, _ = world
    serve, _ = repo_mode(big.root, since=big.sha("HEAD"))
    first = run(serve)
    assert first.kinds() == [("document", uri("big")), ("document", uri("big/mods/lib"))]
    by_uri = {item["file_uri"]: item for item in first.items}
    assert len(by_uri[uri("big")]["pages"]) == 1  # since 切掉了一月
    sub_messages = [
        element["text"]
        for page in by_uri[uri("big/mods/lib")]["pages"]
        for element in page["elements"]
    ]
    assert sub_messages == ["lib 1\n"]  # 子模块不受父仓库的 since 影响


def test_repo_mode_shares_delete_and_move_with_root_mode(
    world: tuple[Path, FixtureRepo, FixtureRepo],
) -> None:
    """repo 模式下子模块被移除即 delete（与 root 模式同一套判定）；配置的仓库本身不会消失。"""
    _, big, _ = world
    serve, config = repo_mode(big.root)
    first = run(serve)
    run_git(big.root, "rm", "-q", "mods/lib")
    big.commit("移除子模块", date="2026-03-06T10:00:00+00:00")
    run_git(big.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    second = run(serve, first.cursor)
    assert second.kinds() == [("delete", uri("big/mods/lib")), ("document", uri("big"))]
    assert set(marks_for(second.cursor, config)) == {b"big"}


@posix_permissions
def test_repo_mode_holds_back_deletes_when_gitmodules_is_unreadable(
    world: tuple[Path, FixtureRepo, FixtureRepo],
) -> None:
    """repo 模式同样 fail-closed：``.gitmodules`` 读不了时子模块不被当作已删除。"""
    _, big, _ = world
    serve, config = repo_mode(big.root)
    first = run(serve)
    (big.root / ".gitmodules").chmod(0)
    try:
        second = run(serve, first.cursor)
    finally:
        (big.root / ".gitmodules").chmod(0o644)
    assert ("error", uri("big/.gitmodules")) in second.kinds()
    assert not any(kind[0] == "delete" for kind in second.kinds())
    assert b"big/mods/lib" in marks_for(second.cursor, config)


def test_repo_mode_submodule_move_is_a_move(world: tuple[Path, FixtureRepo, FixtureRepo]) -> None:
    _, big, _ = world
    serve, _ = repo_mode(big.root)
    first = run(serve)
    run_git(big.root, "mv", "mods/lib", "mods/renamed")
    big.commit("移动子模块", date="2026-03-06T10:00:00+00:00")
    run_git(big.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    second = run(serve, first.cursor)
    assert second.kinds() == [
        ("move", uri("big/mods/lib"), uri("big/mods/renamed")),
        ("document", uri("big")),
        ("document", uri("big/mods/renamed")),
    ]
