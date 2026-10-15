"""附注标签作内容（#220）：标签页的映射、签名切分、页内顺序与失败口径。

夹具标签都用固定的 tagger 时间（``GIT_COMMITTER_DATE``）现场构造；签名标签与无 tagger 的老标签
用 ``hash-object -t tag`` 直接写对象（不需要真实的签名密钥）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import FixtureRepo, requires_git, run_git

from dpe_git_connector.extractors import ELEMENT_BUDGET_BYTES
from dpe_git_connector.gitrepo import GitRepo, RepoDir, strip_signature
from dpe_git_connector.mapping import DocumentMapper, RepoSpec

pytestmark = requires_git

PREFIX = "git://s/"

_PGP = b"-----BEGIN PGP SIGNATURE-----\n\niQEzBAABCAAdFiEE\n-----END PGP SIGNATURE-----\n"
_SSH = b"-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQ==\n-----END SSH SIGNATURE-----\n"
_X509 = b"-----BEGIN SIGNED MESSAGE-----\nMIAGCSqGSIb3DQEH\n-----END SIGNED MESSAGE-----\n"


def make_repo(root: Path) -> FixtureRepo:
    repo = FixtureRepo(root)
    repo.commit("第一次提交", date="2026-01-06T10:00:00+00:00")
    repo.commit("第二次提交", date="2026-02-06T10:00:00+00:00")
    repo.set_origin_head("main")
    return repo


def annotate(repo: FixtureRepo, name: str, message: str, *, date: str, at: str = "HEAD") -> str:
    """打一个附注标签（tagger 时间固定为 ``date``），返回标签对象 SHA。"""
    run_git(
        repo.root,
        "-c",
        "tag.gpgsign=false",
        "tag",
        "-a",
        name,
        "-m",
        message,
        at,
        env={"GIT_COMMITTER_DATE": date},
    )
    return run_git(repo.root, "rev-parse", f"refs/tags/{name}").strip()


def raw_tag(repo: FixtureRepo, name: str, body: bytes, *, tagger: bool = True) -> str:
    """直接写一个标签对象（可带签名块、可没有 tagger 头），挂到 ``refs/tags/<name>``。"""
    header = f"object {repo.sha()}\ntype commit\ntag {name}\n".encode()
    if tagger:
        header += b"tagger Bob <bob@example.invalid> 1767225600 +0800\n"
    # 没有 tagger 的老标签过不了新版 git 的 fsck，只能 --literally 写入
    literally = () if tagger else ("--literally",)
    sha = run_git(
        repo.root,
        "hash-object",
        *literally,
        "-w",
        "-t",
        "tag",
        "--stdin",
        input=header + b"\n" + body,
    ).strip()
    run_git(repo.root, "update-ref", f"refs/tags/{name}", sha)
    return sha


def scan(repo: FixtureRepo) -> Any:
    mapper = DocumentMapper(PREFIX)
    return mapper.document(
        GitRepo(str(repo.root)), RepoDir(path=str(repo.root), identifier=b"repo"), RepoSpec()
    )


def pages_of(repo: FixtureRepo) -> list[dict[str, Any]]:
    item = scan(repo)
    assert item.kind == "document", item.fields
    pages: list[dict[str, Any]] = item.fields["pages"]
    return pages


# ------------------------------------------------------------------------------- 映射


def test_annotated_tags_become_the_last_page(tmp_path: Path) -> None:
    """附注标签成为最后一页 ``{kind: tags}``；轻量标签不产元素；元素字段如规定。"""
    repo = make_repo(tmp_path / "repo")
    first = repo.sha("HEAD~1")
    run_git(repo.root, "tag", "light")
    v1 = annotate(repo, "v1.0", "第一次发版\n\n修了很多问题\n", date="2026-02-07T10:00:00+08:00")
    annotate(repo, "v0.9", "预发版\n", date="2026-01-07T10:00:00+00:00", at=first)
    pages = pages_of(repo)
    assert [page["page_metadata"] for page in pages] == [
        {"ref": "main", "period": "2026-01"},
        {"ref": "main", "period": "2026-02"},
        {"kind": "tags"},
    ]
    assert pages[-1]["elements"] == [
        {
            "category": "NarrativeText",
            "text": "预发版\n",
            "metadata": {
                "tag": "v0.9",
                "target": first,
                "tagger": "Alice <alice@example.invalid>",
                "tagged_at": "2026-01-07T10:00:00Z",
            },
        },
        {
            "category": "NarrativeText",
            "text": "第一次发版\n\n修了很多问题\n",
            "metadata": {
                "tag": "v1.0",
                "target": repo.sha(),
                "tagger": "Alice <alice@example.invalid>",
                "tagged_at": "2026-02-07T02:00:00Z",
            },
        },
    ]
    assert v1 != repo.sha()  # 附注标签的 ref 指向标签对象


def test_tags_page_leaves_the_other_pages_untouched(tmp_path: Path) -> None:
    """打标签只多出标签页：其余页对象逐字节不变（page_hash 不变）。"""
    repo = make_repo(tmp_path / "repo")
    before = pages_of(repo)
    annotate(repo, "v1", "发版\n", date="2026-02-07T10:00:00+00:00")
    after = pages_of(repo)
    assert after[:-1] == before and after[-1]["page_metadata"] == {"kind": "tags"}


def test_only_lightweight_tags_make_no_page(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    before = pages_of(repo)
    run_git(repo.root, "tag", "light")
    assert pages_of(repo) == before


def test_a_branch_named_tags_does_not_collide_with_the_tags_page(tmp_path: Path) -> None:
    """分支可以叫 ``tags``：分支页是 ``{ref: tags}``，标签页是 ``{kind: tags}``，键集合不同。"""
    repo = make_repo(tmp_path / "repo")
    repo.add_remote_branch("tags", at=repo.sha())
    annotate(repo, "v1", "发版\n", date="2026-02-07T10:00:00+00:00")
    item = DocumentMapper(PREFIX).document(
        GitRepo(str(repo.root)),
        RepoDir(path=str(repo.root), identifier=b"repo"),
        RepoSpec(branches=("tags",)),
    )
    metadata = [page["page_metadata"] for page in item.fields["pages"]]
    assert metadata[-2:] == [{"ref": "tags"}, {"kind": "tags"}]


def test_nested_tag_targets_the_tag_it_points_to(tmp_path: Path) -> None:
    """``target`` 是对象里直接记录的 SHA，不剥到底层：嵌套标签指向那个标签对象。"""
    repo = make_repo(tmp_path / "repo")
    inner = annotate(repo, "inner", "内层\n", date="2026-02-07T10:00:00+00:00")
    annotate(repo, "outer", "外层\n", date="2026-02-08T10:00:00+00:00", at="inner")
    elements = pages_of(repo)[-1]["elements"]
    assert [e["metadata"]["tag"] for e in elements] == ["inner", "outer"]
    assert elements[1]["metadata"]["target"] == inner


def test_tag_without_tagger_omits_the_keys_and_sorts_first(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    annotate(repo, "a-new", "新标签\n", date="2026-02-07T10:00:00+00:00")
    raw_tag(repo, "z-old", "老标签\n".encode(), tagger=False)
    elements = pages_of(repo)[-1]["elements"]
    assert elements[0]["metadata"] == {"tag": "z-old", "target": repo.sha()}
    assert elements[1]["metadata"]["tag"] == "a-new"


def test_same_time_tags_order_by_name_bytes(tmp_path: Path) -> None:
    """页内顺序只依赖标签对象与 ref 名：同一时间按名字字节序，与创建顺序无关。"""
    repo = make_repo(tmp_path / "repo")
    # 不用只差大小写的名字：大小写不敏感的文件系统上 loose ref 会撞
    for name in ("b", "Z", "a", "é"):
        annotate(repo, name, f"{name}\n", date="2026-02-07T10:00:00+00:00")
    names = [e["metadata"]["tag"] for e in pages_of(repo)[-1]["elements"]]
    assert names == ["Z", "a", "b", "é"]


def test_the_ref_name_wins_over_the_tag_header(tmp_path: Path) -> None:
    """``tag`` 取 ref 名（用户认的名字），对象里的 ``tag`` 头与之不同也一样。"""
    repo = make_repo(tmp_path / "repo")
    sha = annotate(repo, "old-name", "发版\n", date="2026-02-07T10:00:00+00:00")
    run_git(repo.root, "update-ref", "refs/tags/new-name", sha)
    run_git(repo.root, "update-ref", "-d", "refs/tags/old-name")
    [element] = pages_of(repo)[-1]["elements"]
    assert element["metadata"]["tag"] == "new-name"


def test_long_tag_message_is_split_like_commits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    line = "x" * 1023 + "\n"
    body = line * (ELEMENT_BUDGET_BYTES // len(line) + 8)
    raw_tag(repo, "big", body.encode())
    elements = pages_of(repo)[-1]["elements"]
    assert len(elements) == 2
    assert "".join(e["text"] for e in elements) == body
    assert elements[0]["metadata"] == elements[1]["metadata"]


@pytest.mark.parametrize("body", [b"\xff\xfe bad\n", "发版".encode("gbk") + b"\n"])
def test_non_utf8_tag_message_is_content_invalid(tmp_path: Path, body: bytes) -> None:
    repo = make_repo(tmp_path / "repo")
    raw_tag(repo, "bad", body)
    item = scan(repo)
    assert item.kind == "error"
    assert item.fields["code"] == "content_invalid"
    assert item.fields["retryable"] is False


# ------------------------------------------------------------------------------- 签名


@pytest.mark.parametrize("signature", [_PGP, _SSH, _X509], ids=["pgp", "ssh", "x509"])
def test_signature_is_not_content(tmp_path: Path, signature: bytes) -> None:
    """三种签名块都不进 text；重签（签名变、说明不变）不改变元素。"""
    repo = make_repo(tmp_path / "repo")
    raw_tag(repo, "v1", "发版\n".encode() + signature)
    [element] = pages_of(repo)[-1]["elements"]
    assert element["text"] == "发版\n"
    resigned = signature.replace(b"-----END", b"cmVzaWduZWQ=\n-----END", 1)
    raw_tag(repo, "v1", "发版\n".encode() + resigned)
    assert pages_of(repo)[-1]["elements"] == [element]


@pytest.mark.parametrize("signature", [_PGP, _SSH, _X509], ids=["pgp", "ssh", "x509"])
def test_signature_boundary_matches_git(tmp_path: Path, signature: bytes) -> None:
    """切分边界与 git 的 ``%(contents:signature)`` 一致（本机 git 识别该格式时交叉核对）。"""
    repo = make_repo(tmp_path / "repo")
    message = b"subject\n\nbody line\n"
    raw_tag(repo, "v1", message + signature)
    reported = run_git(
        repo.root, "for-each-ref", "--format=%(contents:signature)", "refs/tags/v1"
    ).encode()
    if not reported.strip():
        pytest.skip("本机 git 不识别这种签名格式（SSH 签名自 git 2.34 起）")
    assert reported.rstrip(b"\n") == signature.rstrip(b"\n")
    assert strip_signature(message + signature) == message


def test_marker_not_at_line_start_or_not_last_stays_in_the_text() -> None:
    """反例：标记不在行首不算签名；正文行首出现标记、末尾另有真签名时只切末尾那块。"""
    quoted = b"see `-----BEGIN PGP SIGNATURE-----` above\n"
    assert strip_signature(quoted) == quoted
    body = b"how to verify:\n-----BEGIN SSH SIGNATURE-----\n(example)\n\n"
    assert strip_signature(body + _PGP) == body
    assert strip_signature(b"no signature\n") == b"no signature\n"
