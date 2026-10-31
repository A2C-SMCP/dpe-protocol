"""git notes 作内容（#222）：note 元素的位置、只读默认 notes ref、过滤与失败口径、增量。

note 用 ``git notes add`` 现场构造（固定提交者时间，使 notes ref 的提交可复现）；非 UTF-8 的 note
用 ``hash-object`` 写 blob 再 ``notes add -C`` 挂上。fanout、重复路径等异常树用 ``mktree`` /
``commit-tree`` 自行拼（#233）。

**只读守护**：本模块的每次扫描都断言对象总数不变（``scan``）——连接器对源仓库只读，任何命令
都不得写它的对象库；对象目录只读的用例覆盖整次扫描。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from dpe_hash import ExpandedDocument, document_hashes
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


def blob(repo: Path, text: str) -> str:
    """写入一个 blob，返回 SHA（note 原文与「非 note 条目」都用它）。"""
    return run_git(repo, "hash-object", "-w", "--stdin", input=text.encode()).strip()


def build_tree(repo: Path, files: dict[str, str]) -> str:
    """``路径 → blob SHA`` 表 → 树对象；``a/b/c`` 逐层建目录（fanout 与异常树用）。"""
    children: dict[str, Any] = {}
    for path, object_id in files.items():
        node = children
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = object_id

    def emit(node: dict[str, Any]) -> str:
        entries = [
            (
                f"040000 tree {emit(value)}\t{name}"
                if isinstance(value, dict)
                else f"100644 blob {value}\t{name}"
            )
            for name, value in sorted(node.items())
        ]
        return run_git(repo, "mktree", input=("\n".join(entries) + "\n").encode()).strip()

    return emit(children)


def install_notes_tree(repo: Path, files: dict[str, str]) -> None:
    """把（路径 → blob）表装成 ``refs/notes/commits``：``git notes add`` 造不出异常树。"""
    tree = build_tree(repo, files)
    commit = run_git(repo, "commit-tree", tree, "-m", "notes").strip()
    run_git(repo, "update-ref", "refs/notes/commits", commit)


def object_count(repo: Path) -> int:
    """仓库对象总数（松散 + 已打包）；``count-objects`` 只读，不写对象库。"""
    counts = dict(line.split(": ", 1) for line in run_git(repo, "count-objects", "-v").splitlines())
    return int(counts["count"]) + int(counts["in-pack"])


def scan(repo: FixtureRepo, spec: RepoSpec | None = None) -> Any:
    """整次扫描前后对象总数不变——覆盖扫描里的每条 git 命令，而不只是读 note（#233）。"""
    before = object_count(repo.root)
    item = DocumentMapper(PREFIX).document(
        GitRepo(str(repo.root)),
        RepoDir(path=str(repo.root), identifier=b"repo"),
        spec or RepoSpec(),
    )
    assert object_count(repo.root) == before, "扫描改动了源仓库的对象库"
    return item


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


# ------------------------------------------------------------------- 读 notes 树而不写对象（#233）


def test_mixed_fanout_notes_tree_keeps_hashes(tmp_path: Path) -> None:
    """(a) 平铺 + 单层 + 多层 fanout 混合的**正常** notes 树：page_hash、doc_hash 与状态指纹
    与改用 ``ls-tree`` 自行解析之前逐字节一致（期望值实测于改动前的实现）。"""
    repo = make_repo(tmp_path / "repo")
    january = repo.sha("HEAD~1")
    february = repo.sha("HEAD")
    outside = "0123456789abcdef0123456789abcdef01234567"
    install_notes_tree(
        repo.root,
        {
            january: blob(repo.root, "平铺 note\n"),
            f"{february[:2]}/{february[2:]}": blob(repo.root, "单层 fanout\n"),
            f"{outside[:2]}/{outside[2:4]}/{outside[4:]}": blob(
                repo.root, "两层 fanout（不在文档）\n"
            ),
        },
    )
    item = scan(repo)
    assert item.kind == "document", item.fields
    hashes = document_hashes(
        cast(ExpandedDocument, {"file_type": "git_repo", "pages": item.fields["pages"]})
    )
    assert hashes["doc_hash"] == (
        "dpe1:1635a41e45168011b1e8da4c2448607fd6404674a9203d5ba199acc5a2084382"
    )
    assert [page["page_hash"] for page in hashes["pages"]] == [
        "dpe1:5aa7a6f97091c48d10a310b30c4eb42b6110eb6a44e05bc21ae3a3904056b648",
        "dpe1:e21f2eaab60762a2ca01191b9d7d1c900148f0c788d600ca82c3a720c185038b",
    ]
    # 状态指纹同样不变：全部（被注对象, blob）对，多层 fanout 的不在本文档里也保留
    state = DocumentMapper(PREFIX).resolve(GitRepo(str(repo.root)), RepoSpec())
    assert state.fingerprint() == "ac4fc218f6418871b67a331982151407"


def test_duplicate_note_paths_for_a_shown_commit_are_content_invalid(tmp_path: Path) -> None:
    """(b) 同一被注对象既有 fanout 路径又有平铺路径，且它在本文档里：``content_invalid``、
    不可重试（不替用户合并——``git notes list`` 在这里会写对象）。"""
    repo = make_repo(tmp_path / "repo")
    head = repo.sha("HEAD")
    install_notes_tree(
        repo.root,
        {
            f"{head[:2]}/{head[2:]}": blob(repo.root, "fanout 路径上的说明\n"),
            head: blob(repo.root, "平铺路径上的说明\n"),
        },
    )
    item = scan(repo)  # scan 同时断言对象库未被改动
    assert item.kind == "error"
    assert item.fields["code"] == "content_invalid"
    assert item.fields["retryable"] is False
    assert "多条路径" in item.fields["message"]


def test_duplicate_note_paths_outside_the_document_are_ignored(tmp_path: Path) -> None:
    """(b) 重复路径的被注对象不在本文档里：照旧忽略，文档与没有这些 note 时逐字节相同。"""
    repo = make_repo(tmp_path / "repo")
    before = elements(repo)
    outside = "0123456789abcdef0123456789abcdef01234567"
    install_notes_tree(
        repo.root,
        {
            f"{outside[:2]}/{outside[2:]}": blob(repo.root, "fanout 路径上的说明\n"),
            outside: blob(repo.root, "平铺路径上的说明\n"),
        },
    )
    assert elements(repo) == before


def test_read_only_object_directory_allows_a_full_scan(tmp_path: Path) -> None:
    """(c) 对象目录只读（root 下 skip）：完整扫描成功，且不写任何对象。

    只读让任何写对象的命令直接失败（退出码非 0 → 条目级 error），覆盖整次扫描而不只是读
    note；``scan`` 再做一次对象总数前后比对。
    """
    if os.geteuid() == 0:
        pytest.skip("root 无视权限位，只读目录造不出来")
    repo = make_repo(tmp_path / "repo")
    head = repo.sha("HEAD")
    note(repo.root, head, "只读仓库里的 note")
    objects = repo.root / ".git" / "objects"
    directories = [objects, *(path for path in objects.rglob("*") if path.is_dir())]
    try:
        for directory in directories:
            os.chmod(directory, 0o555)
        item = scan(repo)
    finally:
        for directory in directories:
            os.chmod(directory, 0o755)  # 让 pytest 能清理 tmp_path
    assert item.kind == "document", item.fields
    assert item.fields["pages"][-1]["elements"][-1]["metadata"] == {"note_of": head}


def test_notes_ref_pointing_at_a_blob_is_content_invalid(tmp_path: Path) -> None:
    """(d) notes ref 存在但指不到提交（指向 blob）：``content_invalid``、不可重试，消息点名
    notes ref——源状态确定性地损坏，重跑不会好，也不能静默当作没有 notes。"""
    repo = make_repo(tmp_path / "repo")
    run_git(repo.root, "update-ref", "refs/notes/commits", blob(repo.root, "不是 notes 树\n"))
    item = scan(repo)
    assert item.kind == "error"
    assert item.fields["code"] == "content_invalid"
    assert item.fields["retryable"] is False
    assert "notes ref" in item.fields["message"]
    assert "refs/notes/commits" in item.fields["message"]


def test_non_note_entries_are_ignored_like_git_notes_list(tmp_path: Path) -> None:
    """(e) 树里的非 note 条目（非 40 位名字、非 fanout 目录、非 blob）：与 ``git notes list``
    的结果一致，不报错。"""
    repo = make_repo(tmp_path / "repo")
    head = repo.sha("HEAD")
    install_notes_tree(
        repo.root,
        {
            head: blob(repo.root, "正常的 note\n"),
            "README": blob(repo.root, "README 不是 note\n"),
            head[:39]: blob(repo.root, "39 位名字\n"),
            "g" * 40: blob(repo.root, "40 位但不是十六进制\n"),
            f"zz/{head}": blob(repo.root, "目录名不是 fanout\n"),
        },
    )
    pairs = GitRepo(str(repo.root)).notes()
    listed = {}
    for line in run_git(repo.root, "notes", "list").splitlines():
        listed_blob, annotated = line.split()
        listed[annotated] = listed_blob
    assert dict(pairs) == listed
    assert [annotated for annotated, _ in pairs] == [head]


def sha256_repo(root: Path) -> FixtureRepo:
    """SHA-256 仓库；本机 git 不支持时跳过（``--object-format`` 自 2.29 起）。"""
    try:
        return FixtureRepo(root, object_format="sha256")
    except AssertionError:
        pytest.skip("本机 git 不支持 --object-format=sha256")


def test_sha256_repository_note_paths_are_64_hex(tmp_path: Path) -> None:
    """SHA-256 仓库的 note 名是 64 位（fanout 拼接后），按 64 位判定，不误收 40 位名字。"""
    repo = sha256_repo(tmp_path / "repo")
    repo.commit("唯一提交", date="2026-01-06T10:00:00+00:00")
    repo.set_origin_head("main")
    head = repo.sha("HEAD")
    assert len(head) == 64
    note(repo.root, head, "sha256 仓库的 note")
    pages = elements(repo)
    assert pages[-1][-1]["metadata"] == {"note_of": head}


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


def test_corrupt_notes_tree_is_reported_on_every_round(root: Path) -> None:
    """重复路径是源状态的确定性损坏：增量轮照样报 ``content_invalid``（不重试），不因指纹
    相同被跳过，也不静默当作没有 notes——游标不越过未产出的变更（§6.8）。"""
    serve = serve_for(root)
    first = run(serve)
    alpha = root / "alpha"
    head = run_git(alpha, "rev-parse", "HEAD").strip()
    install_notes_tree(
        alpha,
        {
            f"{head[:2]}/{head[2:]}": blob(alpha, "fanout 路径上的说明\n"),
            head: blob(alpha, "平铺路径上的说明\n"),
        },
    )
    second = run(serve, first.cursor)
    assert second.kinds() == [("error", uri("alpha"))]
    assert second.items[0]["code"] == "content_invalid"
    assert second.items[0]["retryable"] is False
    third = run(serve, second.cursor)
    assert third.kinds() == [("error", uri("alpha"))]
