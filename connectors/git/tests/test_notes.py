"""git notes 作内容（#222）：note 元素的位置、只读默认 notes ref、过滤与失败口径、增量。

note 用 ``git notes add`` 现场构造（固定提交者时间，使 notes ref 的提交可复现）；非 UTF-8 的 note
用 ``hash-object`` 写 blob 再 ``notes add -C`` 挂上。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureRepo, requires_git, run_git
from test_incremental import run, serve_for, uri

from dpe_git_connector.extractors import ELEMENT_BUDGET_BYTES
from dpe_git_connector.gitrepo import GitRepo, RepoDir
from dpe_git_connector.mapping import DocumentMapper, RepoSpec

pytestmark = requires_git

PREFIX = "git://s/"


def make_repo(root: Path) -> FixtureRepo:
    repo = FixtureRepo(root)
    repo.commit("一月提交", date="2026-01-06T10:00:00+00:00")
    repo.commit("二月提交", date="2026-02-06T10:00:00+00:00")
    repo.set_origin_head("main")
    return repo


def note(repo: Path, rev: str, text: str, *, ref: str | None = None, force: bool = False) -> None:
    args = ["notes", *([f"--ref={ref}"] if ref else []), "add", *(["-f"] if force else [])]
    run_git(repo, *args, "-m", text, rev)


def scan(repo: FixtureRepo, spec: RepoSpec | None = None) -> Any:
    return DocumentMapper(PREFIX).document(
        GitRepo(str(repo.root)),
        RepoDir(path=str(repo.root), identifier=b"repo"),
        spec or RepoSpec(),
    )


def elements(repo: FixtureRepo, spec: RepoSpec | None = None) -> list[list[dict[str, Any]]]:
    item = scan(repo, spec)
    assert item.kind == "document", item.fields
    return [page["elements"] for page in item.fields["pages"]]


def test_note_follows_the_commit_it_annotates(tmp_path: Path) -> None:
    """note 是独立元素，紧跟被注提交；提交元素本身不变；只有那一页变化。"""
    repo = make_repo(tmp_path / "repo")
    before = elements(repo)
    january = repo.sha("HEAD~1")
    note(repo.root, january, "事后补充：关联单号 X-1")
    after = elements(repo)
    assert after[1] == before[1]  # 二月页不变
    assert after[0] == [
        before[0][0],
        {
            "category": "NarrativeText",
            "text": "事后补充：关联单号 X-1\n",
            "metadata": {"note_of": january},
        },
    ]


def test_only_the_default_notes_ref_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读 refs/notes/commits：其他 notes ref 不读，core.notesRef / GIT_NOTES_REF 也不改变映射。"""
    repo = make_repo(tmp_path / "repo")
    before = elements(repo)
    note(repo.root, "HEAD", "review: LGTM", ref="refs/notes/review")
    run_git(repo.root, "config", "core.notesRef", "refs/notes/review")
    monkeypatch.setenv("GIT_NOTES_REF", "refs/notes/review")
    assert elements(repo) == before


def test_note_after_the_last_fragment_of_a_split_commit(tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "repo")
    line = "x" * 1023 + "\n"
    repo.commit(
        line * (ELEMENT_BUDGET_BYTES // len(line) + 8),
        date="2026-01-06T10:00:00+00:00",
        message_file=True,
    )
    repo.set_origin_head("main")
    note(repo.root, "HEAD", "补充")
    [page] = elements(repo)
    assert [e["metadata"].get("note_of") for e in page] == [None, None, repo.sha()]


def test_shared_commit_carries_its_note_on_every_page(tmp_path: Path) -> None:
    """两条未合并分支共享的提交在两个分支页各出现一次：note 跟着它出现在每一处。"""
    repo = make_repo(tmp_path / "repo")
    repo.branch("a")
    shared = repo.commit("共享提交", date="2026-03-06T10:00:00+00:00")
    repo.branch("b")
    repo.add_remote_branch("a")
    repo.add_remote_branch("b")
    note(repo.root, shared, "共享提交的说明")
    pages = elements(repo, RepoSpec(branches=("a", "b")))
    for branch_page in pages[-2:]:
        assert branch_page[-1]["metadata"] == {"note_of": shared}


def test_notes_outside_the_document_are_ignored(tmp_path: Path) -> None:
    """被注提交不在本文档任何页里（since 之前、已丢弃）或 note 挂在树上：忽略，内容也不必合法。"""
    repo = make_repo(tmp_path / "repo")
    since_cut = repo.sha("HEAD~1")
    dropped = repo.commit("将被丢弃", date="2026-03-06T10:00:00+00:00")
    run_git(repo.root, "reset", "-q", "--hard", "HEAD~1")
    run_git(repo.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    before = elements(repo, RepoSpec(since=repo.sha()))
    bad = run_git(repo.root, "hash-object", "-w", "--stdin", input=b"\xff\xfe\n").strip()
    run_git(repo.root, "notes", "add", "-C", bad, dropped)
    note(repo.root, since_cut, "since 之前的提交")
    note(repo.root, "HEAD^{tree}", "树上的 note")
    assert elements(repo, RepoSpec(since=repo.sha())) == before


def test_non_utf8_note_on_a_shown_commit_is_content_invalid(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    bad = run_git(repo.root, "hash-object", "-w", "--stdin", input="说明".encode("gbk")).strip()
    run_git(repo.root, "notes", "add", "-C", bad, "HEAD")
    item = scan(repo)
    assert item.kind == "error"
    assert item.fields["code"] == "content_invalid" and item.fields["retryable"] is False


# ------------------------------------------------------------------------------- 增量


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    make_repo(base / "alpha")
    return base


def test_adding_editing_and_removing_a_note_are_changes(root: Path) -> None:
    serve = serve_for(root)
    first = run(serve)
    alpha = root / "alpha"

    note(alpha, "HEAD", "第一版说明")
    added = run(serve, first.cursor)
    assert added.kinds() == [("document", uri("alpha"))]
    assert added.items[0]["pages"][-1]["elements"][-1]["text"] == "第一版说明\n"

    note(alpha, "HEAD", "第二版说明", force=True)
    edited = run(serve, added.cursor)
    assert edited.kinds() == [("document", uri("alpha"))]
    assert edited.items[0]["pages"][-1]["elements"][-1]["text"] == "第二版说明\n"

    run_git(alpha, "notes", "remove", "HEAD")
    removed = run(serve, edited.cursor)
    assert removed.kinds() == [("document", uri("alpha"))]
    assert removed.items[0]["pages"] == first.items[0]["pages"]
    assert run(serve, removed.cursor).items == []


def test_notes_on_unrelated_objects_rescan_without_changing_the_document(root: Path) -> None:
    """只动不在本文档里的对象的 note：指纹变（覆盖全部输入），重出的文档与上一轮逐字节相同。"""
    serve = serve_for(root)
    first = run(serve)
    note(root / "alpha", "HEAD^{tree}", "树上的 note")
    second = run(serve, first.cursor)
    assert second.kinds() == [("document", uri("alpha"))]
    assert second.items[0]["pages"] == first.items[0]["pages"]
    assert run(serve, second.cursor).items == []
