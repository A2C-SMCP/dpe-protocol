"""增量游标与删除 / 移动意图（Issue #66，connector 契约 §6.4、§6.5、§6.8）。

``Serve`` 直接吃行（sans-IO）：一轮按续批拉完，结束批的游标交给下一轮。夹具是 root 模式下的
真实仓库（现场构造），源的变化用真实的 git 操作与目录操作完成。
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureRepo, requires_git, run_git
from test_server import handle, initialize_params, request

from dpe_git_connector.cursor import decode_cursor, encode_cursor, source_digest
from dpe_git_connector.mapping import RepoMark
from dpe_git_connector.server import Serve

pytestmark = requires_git

PREFIX = "git://s/"
_SERVER = "dpe_git_connector.server"


@dataclass
class Round:
    items: list[dict[str, Any]]
    cursor: str | None
    incremental: bool

    def kinds(self) -> list[tuple[str, ...]]:
        """条目的（kind, URI…）摘要：document / delete / error 带 file_uri，move 带两侧。"""
        out: list[tuple[str, ...]] = []
        for item in self.items:
            if item["kind"] == "move":
                out.append(("move", item["from_uri"], item["to_uri"]))
            else:
                out.append((item["kind"], item.get("file_uri") or ""))
        return out


def run(serve: Serve, cursor: str | None = None, **extra: Any) -> Round:
    """拉完一轮（续批直到结束批）。"""
    items: list[dict[str, Any]] = []
    params: dict[str, Any] = {"cursor": cursor, **extra}
    id_ = 10
    while True:
        response = handle(serve, request(id_, "scan", params))
        result = response["result"]
        items.extend(result["items"])
        if "next" not in result:
            return Round(items, result["cursor"], result["incremental"])
        params = {"next": result["next"]}
        id_ += 1


def make_repo(path: Path, message: str) -> FixtureRepo:
    repo = FixtureRepo(path)
    repo.commit(message, date="2026-01-06T10:00:00+00:00", files={"f.txt": message + "\n"})
    repo.set_origin_head("main")
    return repo


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    base = tmp_path / "root"
    make_repo(base / "alpha", "alpha 初始化")
    make_repo(base / "beta", "beta 初始化")
    yield base


def serve_for(root: Path) -> Serve:
    serve = Serve()
    handle(serve, request(1, "initialize", initialize_params({"root": str(root)})))
    return serve


def uri(name: str) -> str:
    return PREFIX + name


def marks(serve: Serve, cursor: str | None, root: Path) -> dict[bytes, Any]:
    assert cursor is not None
    decoded = decode_cursor(cursor, source_digest(PREFIX, {"root": str(root)}))
    assert decoded is not None
    return decoded


# --------------------------------------------------------------------------- 增量轮


def test_first_round_is_full_and_unchanged_round_is_empty(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    assert first.incremental is False
    assert first.kinds() == [("document", uri("alpha")), ("document", uri("beta"))]
    second = run(serve, first.cursor)
    assert second.incremental is True and second.items == []
    assert second.cursor == first.cursor  # 状态未变：游标逐字相同（确定性）


def test_new_commit_only_emits_that_repository(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    alpha = FixtureRepo.__new__(FixtureRepo)
    alpha.root = root / "alpha"
    alpha.commit("alpha 二月", date="2026-02-06T10:00:00+00:00")
    run_git(alpha.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    second = run(serve, first.cursor)
    assert second.incremental is True
    assert second.kinds() == [("document", uri("alpha"))]
    assert second.cursor != first.cursor
    # 再跑一轮收敛：没有变化
    assert run(serve, second.cursor).items == []


def test_new_repository_is_a_document(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    make_repo(root / "gamma", "gamma 初始化")
    second = run(serve, first.cursor)
    assert second.kinds() == [("document", uri("gamma"))]


def test_removed_repository_is_a_delete(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    shutil.rmtree(root / "beta")
    second = run(serve, first.cursor)
    assert second.kinds() == [("delete", uri("beta"))]
    assert set(marks(serve, second.cursor, root)) == {b"alpha"}


def test_renamed_repository_is_a_move(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    os.rename(root / "alpha", root / "renamed")
    second = run(serve, first.cursor)
    assert second.kinds() == [("move", uri("alpha"), uri("renamed"))]  # 内容未变：只有 move
    assert set(marks(serve, second.cursor, root)) == {b"beta", b"renamed"}
    assert run(serve, second.cursor).items == []


def test_renamed_and_changed_is_a_move_then_a_document(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    os.rename(root / "alpha", root / "renamed")
    repo = FixtureRepo.__new__(FixtureRepo)
    repo.root = root / "renamed"
    repo.commit("改名后的新提交", date="2026-02-06T10:00:00+00:00")
    run_git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    second = run(serve, first.cursor)
    assert second.kinds() == [
        ("move", uri("alpha"), uri("renamed")),
        ("document", uri("renamed")),
    ]


def test_ambiguous_renames_are_delete_and_create(tmp_path: Path) -> None:
    """同一仓库的两份克隆同时改名：同键多对多，无法确定配对 → delete + 新建（#96）。"""
    base = tmp_path / "root"
    make_repo(base / "a", "共同的根")
    run_git(base, "clone", "-q", str(base / "a"), "b")
    serve = serve_for(base)
    first = run(serve)
    os.rename(base / "a", base / "x")
    os.rename(base / "b", base / "y")
    second = run(serve, first.cursor)
    assert sorted(second.kinds()) == [
        ("delete", uri("a")),
        ("delete", uri("b")),
        ("document", uri("x")),
        ("document", uri("y")),
    ]


def test_empty_repository_never_pairs_as_a_move(root: Path) -> None:
    serve = serve_for(root)
    FixtureRepo(root / "void")
    first = run(serve)
    os.rename(root / "void", root / "void2")
    second = run(serve, first.cursor)
    assert sorted(second.kinds()) == [("delete", uri("void")), ("document", uri("void2"))]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位")
def test_incomplete_discovery_defers_deletes_and_moves(root: Path) -> None:
    """有读不了的子树：不产出 delete / move（被藏起来的可能正是移动目标），可能是移动目标的
    新仓库先以 error 上报；消失仓库的记录原样带入新游标（§6.8 不越过未产出的变更）。"""
    serve = serve_for(root)
    first = run(serve)
    os.rename(root / "alpha", root / "renamed")
    shutil.rmtree(root / "beta")
    hidden = root / "hidden"
    hidden.mkdir()
    hidden.chmod(0o000)
    try:
        second = run(serve, first.cursor)
    finally:
        hidden.chmod(0o700)
    kinds = second.kinds()
    assert ("error", uri("renamed")) in kinds and ("error", uri("hidden")) in kinds
    assert not [kind for kind in kinds if kind[0] in ("move", "delete", "document")]
    assert set(marks(serve, second.cursor, root)) == {b"alpha", b"beta"}
    # 子树恢复可读后（运行器以旧游标重跑）：发现完整，正常产出 move 与 delete
    third = run(serve, first.cursor)
    assert third.kinds() == [("move", uri("alpha"), uri("renamed")), ("delete", uri("beta"))]


def test_unreadable_or_foreign_cursor_falls_back_to_full(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    assert first.cursor is not None
    other = Serve()
    handle(
        other,
        request(1, "initialize", initialize_params({"root": str(root), "branches": ["dev"]})),
    )
    for cursor in (first.cursor + "x", "g1.%%%", "garbage"):
        assert run(serve, cursor).incremental is False, cursor
    # 另一份配置下的游标（数据源摘要不同）：不在本配置下解释
    foreign = run(other, first.cursor)
    assert foreign.incremental is False and len(foreign.items) == 2


def test_cursor_that_does_not_fit_gets_its_own_batch(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结束批放不下游标：先以 next 结束本批，下一批单独给出空 items 与游标；游标本身超过单条
    预算时回 cursor: null（本轮不提供游标），不发超限消息（§6.5）。"""
    limit = 64 * 1024
    serve = Serve()
    handle(
        serve,
        request(
            1,
            "initialize",
            initialize_params({"root": str(root)}, max_message_bytes=limit),
        ),
    )
    budget = limit - 1024 - len("10")
    # 两篇小文档之后放不下、单独一批却放得下的游标
    big = "g1." + "A" * (budget - 200)

    monkeypatch.setattr(f"{_SERVER}.encode_cursor", lambda *args: big)
    first = handle(serve, request(10, "scan", {"cursor": None}))["result"]
    assert "next" in first and len(first["items"]) == 2
    second = handle(serve, request(11, "scan", {"next": first["next"]}))["result"]
    assert second["items"] == [] and second["cursor"] == big

    monkeypatch.setattr(f"{_SERVER}.encode_cursor", lambda *args: "g1." + "A" * limit)
    final = handle(serve, request(12, "scan", {"cursor": None}))
    lines = [final]
    while "next" in lines[-1]["result"]:
        lines.append(handle(serve, request(13, "scan", {"next": lines[-1]["result"]["next"]})))
    assert lines[-1]["result"]["cursor"] is None


def test_failed_repository_keeps_its_previous_mark(root: Path) -> None:
    """已有记录的仓库本轮失败（默认分支解析不到）：保留上一轮的记录，不越过这个变更（§6.8）。"""
    serve = serve_for(root)
    first = run(serve)
    before = marks(serve, first.cursor, root)[b"alpha"]
    advance = FixtureRepo.__new__(FixtureRepo)
    advance.root = root / "alpha"
    advance.commit("新提交", date="2026-02-06T10:00:00+00:00")
    run_git(root / "alpha", "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    second = run(serve, first.cursor)
    assert second.kinds() == [("error", uri("alpha"))]
    assert marks(serve, second.cursor, root)[b"alpha"] == before


def test_incremental_round_across_many_batches(root: Path) -> None:
    """每批一条：批次边界上的仓库只扫一次，结果与一批拉完相同。"""
    make_repo(root / "gamma", "gamma 初始化")
    serve = serve_for(root)
    first = run(serve, max_items=1)
    shutil.rmtree(root / "beta")
    os.rename(root / "alpha", root / "alpha2")
    advance = FixtureRepo.__new__(FixtureRepo)
    advance.root = root / "gamma"
    advance.commit("gamma 二月", date="2026-02-06T10:00:00+00:00")
    run_git(root / "gamma", "update-ref", "refs/remotes/origin/main", "HEAD")
    batched = run(serve, first.cursor, max_items=1)
    whole = run(serve, first.cursor)
    assert (
        batched.kinds()
        == whole.kinds()
        == [
            ("move", uri("alpha"), uri("alpha2")),
            ("delete", uri("beta")),
            ("document", uri("gamma")),
        ]
    )
    assert batched.cursor == whole.cursor


def test_remote_limits_change_falls_back_to_full(root: Path) -> None:
    """远端单元素上限变化：守卫的结论可能不同而指纹看不出来——回退一次全量轮。"""
    serve = serve_for(root)
    first = run(serve)
    limited = Serve()
    handle(
        limited,
        request(
            1,
            "initialize",
            initialize_params(
                {"root": str(root)}, remote_limits={"max_payload_bytes": 8 * 1024 * 1024}
            ),
        ),
    )
    again = run(limited, first.cursor)
    assert again.incremental is False and len(again.items) == 2


def test_repo_mode_cursor_with_foreign_identifiers_is_reported(tmp_path: Path) -> None:
    """单仓库模式下游标里出现别的仓库（防御路径，正常走不到）：如实上报 error，不静默。"""
    repo = make_repo(tmp_path / "solo", "solo 初始化")
    serve = Serve()
    config = {"repo": str(repo.root)}
    handle(serve, request(1, "initialize", initialize_params(config)))
    first = run(serve)
    previous = decode_cursor(first.cursor or "", source_digest(PREFIX, config))
    assert previous is not None
    forged = encode_cursor(
        source_digest(PREFIX, config), {**previous, b"ghost": RepoMark("0" * 32, None)}
    )
    second = run(serve, forged)
    assert second.kinds() == [("error", "")]
    assert set(marks_for(second.cursor, config)) == {b"solo", b"ghost"}


def marks_for(cursor: str | None, config: dict[str, Any]) -> dict[bytes, RepoMark]:
    assert cursor is not None
    decoded = decode_cursor(cursor, source_digest(PREFIX, config))
    assert decoded is not None
    return decoded


def test_unchanged_repositories_are_skipped_in_bounded_batches(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跳过的仓库也计入单批工作量：到上限即以空 items + next 结束本批，结果不变。"""
    make_repo(root / "gamma", "gamma 初始化")
    serve = serve_for(root)
    first = run(serve)
    monkeypatch.setattr(f"{_SERVER}._BATCH_MAX_SKIPPED", 1)
    batches = []
    params: dict[str, Any] = {"cursor": first.cursor}
    for id_ in range(20, 30):
        result = handle(serve, request(id_, "scan", params))["result"]
        batches.append(result)
        if "next" not in result:
            break
        params = {"next": result["next"]}
    assert [len(batch["items"]) for batch in batches] == [0, 0, 0]
    assert batches[-1]["cursor"] == first.cursor and batches[-1]["incremental"] is True


def test_document_that_does_not_fit_is_scanned_once(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本批放不下而留到下一批的仓库复用同一次扫描：一轮内每个仓库只扫一次。"""
    from dpe_git_connector.mapping import DocumentMapper

    calls: list[bytes] = []
    real = DocumentMapper.scan

    def counting(self: DocumentMapper, repo: Any, entry: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(entry.identifier)
        return real(self, repo, entry, *args, **kwargs)

    monkeypatch.setattr(DocumentMapper, "scan", counting)
    serve = Serve()
    # 单条预算（上限 − 1024 信封预留 − id）约 570 字节：每篇约 400 字节，第二篇在第一批装不下、
    # 留到下一批
    handle(
        serve,
        request(1, "initialize", initialize_params({"root": str(root)}, max_message_bytes=1600)),
    )
    first = run(serve)
    assert first.kinds() == [("document", uri("alpha")), ("document", uri("beta"))]
    assert calls == [b"alpha", b"beta"]


def test_skip_bound_with_items_in_the_same_batch(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一批先产出条目、再达到跳过上限（非空 items + next）：拼起来与不设上限时相同。"""
    for name in ("gamma", "delta"):
        make_repo(root / name, f"{name} 初始化")
    serve = serve_for(root)
    first = run(serve)
    advance = FixtureRepo.__new__(FixtureRepo)
    advance.root = root / "alpha"
    advance.commit("alpha 二月", date="2026-02-06T10:00:00+00:00")
    run_git(root / "alpha", "update-ref", "refs/remotes/origin/main", "HEAD")
    whole = run(serve, first.cursor)
    monkeypatch.setattr(f"{_SERVER}._BATCH_MAX_SKIPPED", 1)
    bounded = run(serve, first.cursor)
    assert bounded.kinds() == whole.kinds() == [("document", uri("alpha"))]
    assert bounded.cursor == whole.cursor
