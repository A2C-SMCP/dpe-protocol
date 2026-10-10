"""映射层单测：月划分（引入提交）、页序、元素与 metadata、since、file_uri、切分与守卫。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from dpe_hash import (
    RECOMMENDED_FILE_TYPES,
    ExpandedDocument,
    document_hashes,
    is_valid_file_type,
    normalize_file_uri,
)
from helpers import FixtureRepo, requires_git

from dpe_git_connector.extractors import ELEMENT_BUDGET_BYTES, element_bytes
from dpe_git_connector.gitrepo import CommitRecord, GitError, GitRepo, RepoDir
from dpe_git_connector.mapping import (
    DocumentMapper,
    Limits,
    RepoSpec,
    build_file_uri,
    month_partition,
)

pytestmark = requires_git

PREFIX = "git://docs/"


def record(
    sha: str,
    parents: tuple[str, ...] = (),
    *,
    committed_at: str = "2026-01-01T00:00:00Z",
    authored_at: str = "2026-01-01T00:00:00Z",
    author: str = "Alice <alice@example.invalid>",
    message: str = "提交\n",
) -> CommitRecord:
    return CommitRecord(
        sha=sha,
        parents=parents,
        author=author,
        authored_at=authored_at,
        committed_at=committed_at,
        message=message,
    )


# --------------------------------------------------------------------------- 月划分（纯函数）


def test_month_partition_linear_history() -> None:
    chain = ["c1", "c2", "c3"]
    records = [
        record("c1", committed_at="2026-01-05T00:00:00Z"),
        record("c2", ("c1",), committed_at="2026-01-20T00:00:00Z"),
        record("c3", ("c2",), committed_at="2026-02-01T00:00:00Z"),
    ]
    months = month_partition(chain, records, None)
    assert {month: [item.sha for item in items] for month, items in months.items()} == {
        "2026-01": ["c1", "c2"],
        "2026-02": ["c3"],
    }


def test_month_partition_merged_commits_follow_the_merge() -> None:
    """被合并带入的提交随其合并提交归月，不按自身提交时间（可早于或晚于合并时间）。"""
    chain = ["c1", "c2", "m"]
    records = [
        record("c1", committed_at="2026-01-01T00:00:00Z"),
        record("s1", ("c1",), committed_at="2025-11-11T00:00:00Z"),  # 早于合并月
        record("s2", ("s1",), committed_at="2026-03-01T00:00:00Z"),  # 晚于合并时间
        record("c2", ("c1",), committed_at="2026-01-20T00:00:00Z"),
        record("m", ("c2", "s2"), committed_at="2026-02-01T00:00:00Z"),
    ]
    months = month_partition(chain, records, None)
    assert {month: [item.sha for item in items] for month, items in months.items()} == {
        "2026-01": ["c1", "c2"],
        "2026-02": ["s1", "s2", "m"],
    }


def test_month_partition_double_merge_takes_the_first_introduction() -> None:
    """同一提交被先后两次合并：归入**最早**那次合并的月份。"""
    chain = ["c1", "m1", "m2"]
    records = [
        record("c1", committed_at="2026-01-01T00:00:00Z"),
        record("s", ("c1",), committed_at="2026-01-02T00:00:00Z"),
        record("m1", ("c1", "s"), committed_at="2026-01-03T00:00:00Z"),
        record("branchnote", ("m1",), committed_at="2026-02-02T00:00:00Z"),
        record("m2", ("m1", "branchnote"), committed_at="2026-02-03T00:00:00Z"),
    ]
    months = month_partition(chain, records, None)
    assert {month: [item.sha for item in items] for month, items in months.items()} == {
        "2026-01": ["c1", "s", "m1"],
        "2026-02": ["branchnote", "m2"],
    }


def test_month_partition_since_keeps_the_starting_bucket() -> None:
    chain = ["c1", "m", "c3"]
    records = [
        record("c1", committed_at="2026-01-01T00:00:00Z"),
        record("s", ("c1",), committed_at="2026-01-02T00:00:00Z"),
        record("m", ("c1", "s"), committed_at="2026-02-01T00:00:00Z"),
        record("c3", ("m",), committed_at="2026-03-01T00:00:00Z"),
    ]
    # since 的粒度是「引入提交」：起点所在的那一批（含被合并带入的提交）整体保留
    # since = m（链上）：m 那一批（s、m）与其后的 c3 保留，c1 被排除
    assert [
        item.sha for items in month_partition(chain, records, "m").values() for item in items
    ] == [
        "s",
        "m",
        "c3",
    ]
    # since = s（不在链上）：s 由 m 引入，起点同样是 m 那一批
    assert [
        item.sha for items in month_partition(chain, records, "s").values() for item in items
    ] == [
        "s",
        "m",
        "c3",
    ]
    # since = c1：全部保留
    assert [
        item.sha for items in month_partition(chain, records, "c1").values() for item in items
    ] == [
        "c1",
        "s",
        "m",
        "c3",
    ]


def test_month_partition_rejects_since_outside_history() -> None:
    with pytest.raises(GitError, match="since"):
        month_partition(["c1"], [record("c1")], "nope")


# --------------------------------------------------------------------------- file_uri


def test_build_file_uri_encodes_identifier_bytes() -> None:
    assert build_file_uri(PREFIX, b"docs") == "git://docs/docs"
    assert build_file_uri(PREFIX, "组/说明 仓库".encode()) == (
        "git://docs/%E7%BB%84/%E8%AF%B4%E6%98%8E%20%E4%BB%93%E5%BA%93"
    )
    # 非 UTF-8 的路径字节同样可表达（百分号编码对字节透明）
    assert build_file_uri(PREFIX, b"bad_\xff") == "git://docs/bad_%FF"
    assert normalize_file_uri(build_file_uri(PREFIX, b"a b")) == "git://docs/a%20b"


def test_build_file_uri_rejects_empty_prefix() -> None:
    with pytest.raises(ValueError):
        build_file_uri("", b"repo")


def test_git_repo_is_a_registered_file_type() -> None:
    assert is_valid_file_type("git_repo")
    assert "git_repo" in RECOMMENDED_FILE_TYPES


# --------------------------------------------------------------------------- 仓库 → 文档


def document_for(
    path: Path, identifier: bytes = b"repo", spec: RepoSpec | None = None
) -> dict[str, Any]:
    mapper = DocumentMapper(PREFIX)
    item = mapper.document(
        GitRepo(str(path)), RepoDir(path=str(path), identifier=identifier), spec or RepoSpec()
    )
    assert item.kind == "document", item.fields
    return item.payload()


def page_shas(page: dict[str, Any]) -> list[str]:
    return [element["metadata"]["commit"] for element in page["elements"]]


def test_document_pages_months_and_branch_pages(history: Any) -> None:
    """验收：月页（含合并归月）、分支页（独有提交、空页）、页序与页内序。"""
    item = document_for(
        history.path, spec=RepoSpec(branches=("dev", "feature/early", "feature/merged"))
    )
    assert "title" not in item["document"]  # 不从路径推导 title（改名即身份变化，见 README）
    assert item["document"] == {"file_type": "git_repo"}
    pages = item["pages"]
    assert [page["page_metadata"] for page in pages] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
        {"ref": "dev"},
        {"ref": "feature/early"},
        {"ref": "feature/merged"},
    ]
    assert [page_shas(page) for page in pages[:3]] == [
        [history.sha("c1"), history.sha("c2")],
        # f1 提交于 2025-11、f2 提交于 2026-03：都随合并提交 m1 归入 2026-02
        [history.sha("f1"), history.sha("f2"), history.sha("m1")],
        [history.sha("c3"), history.sha("g1"), history.sha("m2")],
    ]
    # dev 只有独有提交；已合并未删的分支是空页
    assert page_shas(pages[3]) == [history.sha("d1"), history.sha("d2")]
    assert pages[4]["elements"] == []
    assert pages[5]["elements"] == []
    # 月页之间与页内：旧在前
    assert history.sha("c1") != history.sha("c2")


def test_document_elements_carry_source_metadata(history: Any) -> None:
    item = document_for(history.path)
    first_page = item["pages"][0]
    element = first_page["elements"][0]
    assert element["category"] == "NarrativeText"
    # 说明原样（含结尾换行；正文里的空行保留）
    assert element["text"] == "根提交：初始化仓库\n"
    assert element["metadata"] == {
        "commit": history.sha("c1"),
        "author": "Alice <alice@example.invalid>",
        # 构造时是 +08:00，映射后是 RFC 3339 UTC
        "authored_at": "2026-01-05T00:30:00Z",
        "committed_at": "2026-01-05T00:30:00Z",
    }
    multiline = item["pages"][0]["elements"][1]
    assert multiline["text"] == "一月第二次提交\n\n带一段正文。\n"
    # 分支页上的独有提交带各自的作者
    dev_page = item["pages"][3] if len(item["pages"]) > 3 else None
    assert dev_page is None  # 未配置 branches：没有分支页


def test_document_branch_pages_and_author(history: Any) -> None:
    item = document_for(history.path, spec=RepoSpec(branches=("dev",)))
    pages = item["pages"]
    assert [page["page_metadata"] for page in pages] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
        {"ref": "dev"},
    ]
    assert pages[3]["elements"][1]["metadata"]["author"] == "Bob <bob@example.invalid>"


def test_document_skips_absent_and_default_branches(history: Any) -> None:
    """配置的分支不存在时不产页也不报错；配置里含默认分支名不产生重复页。"""
    spec = RepoSpec(branches=("main", "nope"))
    item = document_for(history.path, spec=spec)
    assert [page["page_metadata"]["ref"] for page in item["pages"]] == ["main", "main", "main"]


def test_document_since(history: Any) -> None:
    spec = RepoSpec(branches=("dev",), since=history.sha("c2"))
    item = document_for(history.path, spec=spec)
    assert [page["page_metadata"] for page in item["pages"]] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
        {"ref": "dev"},
    ]
    # 起点含自身：2026-01 页里只剩 c2（c1 在起点之前）
    assert page_shas(item["pages"][0]) == [history.sha("c2")]
    # since 不在链上（f2 由 m1 引入）：2026-01 整页消失
    item2 = document_for(history.path, spec=RepoSpec(since=history.sha("f2")))
    assert [page["page_metadata"] for page in item2["pages"]] == [
        {"ref": "main", "period": "2026-02"},
        {"ref": "main", "period": "2026-03"},
    ]
    # since = 默认分支 tip（一个合并提交）：起点所在的那一批（被合并带入的 g1 与 m2）保留
    item3 = document_for(history.path, spec=RepoSpec(since=history.sha("m2")))
    assert [page_shas(page) for page in item3["pages"]] == [[history.sha("g1"), history.sha("m2")]]


def test_document_is_deterministic_and_hashes(history: Any) -> None:
    first = document_for(history.path, spec=RepoSpec(branches=("dev",)))
    second = document_for(history.path, spec=RepoSpec(branches=("dev",)))
    assert first == second
    expanded = cast(ExpandedDocument, {"file_type": "git_repo", "pages": first["pages"]})
    hashes = document_hashes(expanded)
    assert hashes["doc_hash"].startswith("dpe1:")
    assert hashes == document_hashes(
        cast(ExpandedDocument, {"file_type": "git_repo", "pages": second["pages"]})
    )


def test_document_explicit_default_branch(tmp_path: Path) -> None:
    """default_branch 显式配置时不依赖 origin/HEAD（游离 HEAD 也能映射）。"""
    repo = FixtureRepo(tmp_path / "repo")
    repo.commit("唯一提交", date="2026-05-01T00:00:00+00:00", files={"a.txt": "a\n"})
    item = document_for(repo.root, spec=RepoSpec(default_branch="main"))
    assert [page["page_metadata"] for page in item["pages"]] == [
        {"ref": "main", "period": "2026-05"}
    ]
    # 没有 origin/HEAD 也没有显式配置：条目级错误（root 模式下不影响其他仓库）
    mapper = DocumentMapper(PREFIX)
    broken = mapper.document(
        GitRepo(str(repo.root)), RepoDir(path=str(repo.root), identifier=b"repo"), RepoSpec()
    )
    assert broken.kind == "error"
    assert broken.fields["code"] == "source_unavailable"
    assert broken.fields["retryable"] is True
    assert "默认分支" in broken.fields["message"]
    # mapper 层也校验 since 的祖先关系（server 在 initialize 已拒过一次，这里是防御）
    mapper = DocumentMapper(PREFIX)
    item_error = mapper.document(
        GitRepo(str(repo.root)),
        RepoDir(path=str(repo.root), identifier=b"repo"),
        RepoSpec(default_branch="main", since="does-not-exist"),
    )
    assert item_error.kind == "error" and item_error.fields["code"] == "source_unavailable"


def test_document_empty_repository_is_zero_pages(tmp_path: Path) -> None:
    """空仓库（无任何提交）：零页文档，不是错误（契约 1 §5 允许空文档）。"""
    empty = FixtureRepo(tmp_path / "empty")
    item = document_for(empty.root, identifier=b"empty_repo")
    assert item["pages"] == []
    expanded = cast(ExpandedDocument, {"file_type": "git_repo", "pages": []})
    assert document_hashes(expanded)["doc_hash"].startswith("dpe1:")


def test_document_broken_repository_is_item_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    mapper = DocumentMapper(PREFIX)
    item = mapper.document(
        GitRepo(str(broken)), RepoDir(path=str(broken), identifier=b"broken"), RepoSpec()
    )
    assert item.kind == "error"
    assert item.fields["file_uri"] == "git://docs/broken"
    assert item.fields["code"] == "source_unavailable"


# --------------------------------------------------------------------------- 预算与守卫


def test_large_commit_message_is_split_with_metadata_copied(tmp_path: Path) -> None:
    """超大提交说明按固定预算切分：类别与 metadata 复制到每个片段，拼回原样。"""
    repo = FixtureRepo(tmp_path / "repo")
    text = "".join(f"第 {index} 行：{'x' * 60}\n" for index in range(20_000))  # 约 1.3 MiB
    assert len(text.encode()) > ELEMENT_BUDGET_BYTES
    sha = repo.commit(
        text,
        date="2026-06-01T00:00:00+00:00",
        author="Bob",
        email="bob@example.invalid",
        message_file=True,
    )
    item = document_for(repo.root, spec=RepoSpec(default_branch="main"))
    elements = [element for page in item["pages"] for element in page["elements"]]
    assert len(elements) > 1
    assert all(element["category"] == "NarrativeText" for element in elements)
    assert all(element_bytes(element) <= ELEMENT_BUDGET_BYTES for element in elements)
    assert "".join(element["text"] for element in elements) == text
    assert {element["metadata"]["commit"] for element in elements} == {sha}
    # 片段共享同一组 metadata（含同一个 SHA 与作者）
    assert {element["metadata"]["author"] for element in elements} == {"Bob <bob@example.invalid>"}


def test_remote_limit_guard_reports_error(tmp_path: Path) -> None:
    """按固定预算切分后仍超过远端 max_payload_bytes：条目级 error，不按远端值重切。"""
    repo = FixtureRepo(tmp_path / "repo")
    repo.commit("x" * 5000, date="2026-06-02T00:00:00+00:00")
    mapper = DocumentMapper(PREFIX, Limits(max_payload_bytes=1024))
    item = mapper.document(
        GitRepo(str(repo.root)),
        RepoDir(path=str(repo.root), identifier=b"repo"),
        RepoSpec(default_branch="main"),
    )
    assert item.kind == "error"
    assert item.fields["code"] == "content_invalid"
    assert "max_payload_bytes" in item.fields["message"]
