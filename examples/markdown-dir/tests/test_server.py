"""线协议（sans-IO）：握手、read_blob 快照、增量与 delete / move、发现不完整时推迟、分段。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dpe_markdown_dir import PLUGIN_NAME, __version__
from dpe_markdown_dir.server import Serve

PREFIX = "file://notes/"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # 1 KiB 的「图片」


def request(id_: Any, method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})


def call(serve: Serve, id_: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    line = serve.handle(request(id_, method, params))
    assert line is not None
    if serve.max_message_bytes:  # 每条响应都不超过单条消息上限（含行尾 LF，§6.2）
        assert len(line.encode("utf-8")) + 1 <= serve.max_message_bytes
    response: dict[str, Any] = json.loads(line)
    return response


def started(root: Path, max_message_bytes: int = 1 << 20, prefix: str = PREFIX) -> Serve:
    serve = Serve()
    response = call(
        serve,
        1,
        "initialize",
        {
            "protocol_versions": ["dpe-connector/1"],
            "max_message_bytes": max_message_bytes,
            "instance": {"uri_prefix": prefix, "config": {"root": str(root)}},
        },
    )
    assert "result" in response, response
    return serve


@dataclass
class Round:
    items: list[dict[str, Any]]
    cursor: str | None
    incremental: bool

    def kinds(self) -> list[tuple[str, ...]]:
        out: list[tuple[str, ...]] = []
        for item in self.items:
            if item["kind"] == "move":
                out.append(("move", item["from_uri"], item["to_uri"]))
            else:
                out.append((item["kind"], item.get("file_uri") or ""))
        return out


def run(serve: Serve, cursor: str | None = None, **extra: Any) -> Round:
    items: list[dict[str, Any]] = []
    params: dict[str, Any] = {"cursor": cursor, **extra}
    id_ = 10
    while True:
        result = call(serve, id_, "scan", params)["result"]
        items.extend(result["items"])
        if "next" not in result:
            return Round(items, result["cursor"], result["incremental"])
        params = {"next": result["next"]}
        id_ += 1


def uri(name: str) -> str:
    return PREFIX + name


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "notes"
    (base / "img").mkdir(parents=True)
    (base / "img" / "logo.png").write_bytes(PNG)
    (base / "a.md").write_text("# 甲\n\n正文\n\n![标志](img/logo.png)\n", encoding="utf-8")
    (base / "b.md").write_text("# 乙\n\n另一篇\n", encoding="utf-8")
    (base / ".hidden.md").write_text("不应出现\n", encoding="utf-8")
    (base / "note.txt").write_text("不是 md\n", encoding="utf-8")
    return base


def touch_later(path: Path) -> None:
    status = path.stat()
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))


# --------------------------------------------------------------------------- 握手


def test_initialize_reports_identity_and_cursor_support(root: Path) -> None:
    serve = Serve()
    response = call(
        serve,
        1,
        "initialize",
        {
            "protocol_versions": ["dpe-connector/1"],
            "max_message_bytes": 1 << 20,
            "instance": {"uri_prefix": PREFIX, "config": {"root": str(root)}},
        },
    )
    assert response["result"] == {
        "protocol_version": "dpe-connector/1",
        "plugin": {"name": PLUGIN_NAME, "version": __version__},
        "capabilities": {"supports_cursor": True},
    }


@pytest.mark.parametrize(
    ("prefix", "config"),
    [
        ("file://notes", {"root": "."}),  # 前缀不以分隔符结尾：拼接会越界
        (PREFIX, {"root": "/no/such/dir"}),
        (PREFIX, {"root": ".", "extra": 1}),
        (PREFIX, {"root": ".", "include_hidden": "yes"}),
    ],
)
def test_initialize_rejects_bad_config(prefix: str, config: dict[str, Any]) -> None:
    serve = Serve()
    response = call(
        serve,
        1,
        "initialize",
        {
            "protocol_versions": ["dpe-connector/1"],
            "max_message_bytes": 1 << 20,
            "instance": {"uri_prefix": prefix, "config": config},
        },
    )
    assert response["error"]["code"] == -32005 and serve.fatal


def test_version_negotiation_and_handshake_first(root: Path) -> None:
    serve = Serve()
    assert call(serve, 1, "scan", {})["error"]["code"] == -32600
    response = call(
        serve,
        2,
        "initialize",
        {"protocol_versions": ["dpe-connector/9"], "max_message_bytes": 1 << 20, "instance": {}},
    )
    assert response["error"]["code"] == -32004


# --------------------------------------------------------------------------- 全量与 read_blob


def test_full_round_documents_and_image_handles(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    assert first.incremental is False and isinstance(first.cursor, str)
    assert first.kinds() == [("document", uri("a.md")), ("document", uri("b.md"))]
    image = first.items[0]["pages"][0]["elements"][2]
    assert image["category"] == "Image" and image["mime_type"] == "image/png"
    assert image["blob"]["size"] == len(PNG)
    handle = image["blob"]["handle"]

    # 分块、任意偏移、越界偏移得到空块 + eof
    data = b""
    while True:
        result = call(
            serve, 20, "read_blob", {"handle": handle, "offset": len(data), "max_bytes": 300}
        )["result"]
        data += base64.b64decode(result["data"])
        if result["eof"]:
            break
    assert data == PNG
    middle = call(serve, 21, "read_blob", {"handle": handle, "offset": 500, "max_bytes": 10})
    assert base64.b64decode(middle["result"]["data"]) == PNG[500:510]
    beyond = call(serve, 22, "read_blob", {"handle": handle, "offset": 99999, "max_bytes": 10})
    assert beyond["result"] == {"data": "", "eof": True}
    assert (
        call(serve, 23, "read_blob", {"handle": "nope", "max_bytes": 1})["error"]["code"] == -32002
    )

    # 快照：源文件改了，本轮读到的仍是产出时那份字节
    (root / "img" / "logo.png").write_bytes(b"changed")
    again = call(serve, 24, "read_blob", {"handle": handle, "offset": 0, "max_bytes": 4096})
    assert base64.b64decode(again["result"]["data"]) == PNG

    # 下一轮轮首 scan 起，旧句柄失效
    run(serve, first.cursor)
    gone = call(serve, 25, "read_blob", {"handle": handle, "offset": 0, "max_bytes": 1})
    assert gone["error"]["code"] == -32002


# --------------------------------------------------------------------------- 增量


def test_unchanged_and_touched_files_are_not_emitted(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    assert run(serve, first.cursor).items == []
    touch_later(root / "a.md")  # 只改 mtime：重读后摘要不变，不产出
    touched = run(serve, first.cursor)
    assert touched.items == [] and touched.incremental is True
    assert run(serve, touched.cursor).items == []


def test_changes_deletes_moves(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    (root / "b.md").write_text("# 乙\n\n改过了\n", encoding="utf-8")
    assert run(serve, first.cursor).kinds() == [("document", uri("b.md"))]

    # 只换图片字节（等长、mtime 改回原值）：md 未动，文档仍变化——不靠时间戳判断
    logo = root / "img" / "logo.png"
    before = logo.stat()
    logo.write_bytes(PNG[::-1])
    os.utime(logo, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert run(serve, first.cursor).kinds() == [
        ("document", uri("a.md")),
        ("document", uri("b.md")),
    ]

    second = run(serve, first.cursor)
    os.rename(root / "b.md", root / "renamed.md")
    (root / "a.md").unlink()
    third = run(serve, second.cursor)
    assert third.kinds() == [
        ("move", uri("b.md"), uri("renamed.md")),
        ("delete", uri("a.md")),
        ("document", uri("renamed.md")),  # move 的目标总随后产出（#178）
    ]
    assert run(serve, third.cursor).items == []


def test_rename_with_content_change_is_delete_and_create(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    os.rename(root / "b.md", root / "c.md")
    (root / "c.md").write_text("# 丙\n", encoding="utf-8")
    assert sorted(run(serve, first.cursor).kinds()) == [
        ("delete", uri("b.md")),
        ("document", uri("c.md")),
    ]


def test_ambiguous_copies_are_delete_and_create(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    shutil.copy(root / "b.md", root / "b1.md")
    shutil.copy(root / "b.md", root / "b2.md")
    (root / "b.md").unlink()
    assert sorted(run(serve, first.cursor).kinds()) == [
        ("delete", uri("b.md")),
        ("document", uri("b1.md")),
        ("document", uri("b2.md")),
    ]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位")
def test_incomplete_discovery_defers_deletes_and_moves(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    os.rename(root / "b.md", root / "renamed.md")
    (root / "a.md").unlink()
    hidden = root / "locked"
    hidden.mkdir()
    hidden.chmod(0o000)
    try:
        second = run(serve, first.cursor)
    finally:
        hidden.chmod(0o700)
    kinds = second.kinds()
    assert ("error", uri("renamed.md")) in kinds and ("error", uri("locked")) in kinds
    assert not [kind for kind in kinds if kind[0] in ("move", "delete", "document")]
    hidden.rmdir()
    third = run(serve, first.cursor)  # 运行器不推进游标：以旧游标重跑，发现完整后正常判定
    assert third.kinds() == [
        ("move", uri("b.md"), uri("renamed.md")),
        ("delete", uri("a.md")),
        ("document", uri("renamed.md")),  # move 的目标总随后产出（#178）
    ]


def test_unreadable_or_foreign_cursor_falls_back_to_full(root: Path) -> None:
    serve = started(root)
    first = run(serve)
    assert first.cursor is not None
    for cursor in ("garbage", "mdd1.%%%", first.cursor + "x"):
        assert run(serve, cursor).incremental is False
    other = started(root, prefix="file://other/")
    assert run(other, first.cursor).incremental is False


# --------------------------------------------------------------------------- 上限与分段


def test_large_document_is_segmented_at_page_boundaries(tmp_path: Path) -> None:
    base = tmp_path / "big"
    base.mkdir()
    pages = "\n---\n".join(f"第 {i} 页\n\n" + "字" * 300 for i in range(6))
    (base / "big.md").write_text(pages, encoding="utf-8")
    serve = started(base, max_message_bytes=1024 + 2200)
    result = run(serve)
    segments = [item for item in result.items if item["kind"] == "document"]
    assert len(segments) > 1
    assert "document" in segments[0] and all("document" not in s for s in segments[1:])
    assert all(s.get("continued") for s in segments[:-1]) and "continued" not in segments[-1]
    assert sum(len(s["pages"]) for s in segments) == 6


def test_page_larger_than_budget_is_content_invalid(tmp_path: Path) -> None:
    base = tmp_path / "huge"
    base.mkdir()
    (base / "huge.md").write_text("字" * 5000, encoding="utf-8")
    serve = started(base, max_message_bytes=1024 + 2000)
    result = run(serve)
    assert [(i["kind"], i.get("code")) for i in result.items] == [("error", "content_invalid")]


def test_same_length_edit_with_preserved_mtime_is_detected(root: Path) -> None:
    """等长修改、mtime 原样保留（如 cp -p、同一时间戳精度内的编辑）：照样产出（§6.8）。"""
    serve = started(root)
    first = run(serve)
    note = root / "b.md"
    before = note.stat()
    note.write_text(note.read_text(encoding="utf-8").replace("乙", "丁"), encoding="utf-8")
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert note.stat().st_size == before.st_size
    assert run(serve, first.cursor).kinds() == [("document", uri("b.md"))]


@pytest.mark.skipif(sys.platform == "win32", reason="符号链接")
def test_images_outside_root_or_symlinked_are_not_read(root: Path, tmp_path: Path) -> None:
    """图片路径越出 root、或是符号链接：不读那个文件，整段按普通文本保留。"""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top secret")
    (root / "img" / "link.png").symlink_to(secret)
    (root / "c.md").write_text(
        f"![外](../secret.png)\n\n![绝对]({secret})\n\n![链](img/link.png)\n",
        encoding="utf-8",
    )
    serve = started(root)
    document = next(i for i in run(serve).items if i["file_uri"] == uri("c.md"))
    elements = document["pages"][0]["elements"]
    assert [e["category"] for e in elements] == ["NarrativeText"] * 3
    assert elements[0]["text"] == "![外](../secret.png)"


def test_missing_image_is_an_image_without_bytes(root: Path) -> None:
    """图片文件不存在：不带 blob 的 Image（源确实没有字节，§6.6），文档照常产出；它是新文件时
    也不会扣住其他文件的删除与改名。"""
    serve = started(root)
    first = run(serve)
    (root / "c.md").write_text("![没有](missing.png)\n", encoding="utf-8")
    (root / "a.md").unlink()
    second = run(serve, first.cursor)
    assert second.kinds() == [("delete", uri("a.md")), ("document", uri("c.md"))]
    image = second.items[1]["pages"][0]["elements"][0]
    assert image == {"category": "Image", "text": "没有"}  # 没有字节，也就没有 mime_type
    (root / "missing.png").write_bytes(PNG)  # 图片补上：内容变了
    assert run(serve, second.cursor).kinds() == [("document", uri("c.md"))]


def test_read_blob_chunks_fit_the_message_limit(root: Path) -> None:
    """运行器要的块比单条消息装得下的还大：插件按上限截断（base64 膨胀计入），可续读。"""
    limit = 1024 + 340  # 不截断时 1 KiB 图片的 base64 必然超限
    serve = started(root, max_message_bytes=limit)
    image = next(
        e
        for item in run(serve).items
        if item["kind"] == "document"
        for page in item["pages"]
        for e in page["elements"]
        if e["category"] == "Image"
    )
    handle, data = image["blob"]["handle"], b""
    while True:
        result = call(
            serve, 30, "read_blob", {"handle": handle, "offset": len(data), "max_bytes": 1 << 20}
        )
        data += base64.b64decode(result["result"]["data"])
        if result["result"]["eof"]:
            break
    assert data == PNG


def test_stale_next_token_from_an_earlier_round_is_rejected(root: Path) -> None:
    serve = started(root)
    first = call(serve, 10, "scan", {"cursor": None, "max_items": 1})["result"]
    stale = first["next"]
    call(serve, 11, "scan", {"cursor": None, "max_items": 1})  # 运行器中止并重开一轮
    assert call(serve, 12, "scan", {"next": stale})["error"]["code"] == -32602
    assert call(serve, 13, "scan", {"next": "9" * 5000})["error"]["code"] == -32602


def test_digest_is_unambiguous(tmp_path: Path) -> None:
    """md 字节与图片记录分开拼接：改动后的文件不会碰巧得到改动前的摘要（§6.8 不漏判）。"""
    from dpe_markdown_dir.source import build_document

    base = tmp_path / "d"
    base.mkdir()
    note = base / "a.md"
    note.write_bytes(b"![a](gone.png)")
    before = build_document(str(base), str(note), lambda: "h").digest
    for suffix in (b"M", b"M" + bytes(32), b"missing", b"T" + bytes(32)):
        note.write_bytes(b"![a](gone.png)" + suffix)
        assert build_document(str(base), str(note), lambda: "h").digest != before, suffix


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 特殊文件")
def test_special_and_hidden_targets_are_not_read(root: Path) -> None:
    """不是普通文件（目录、管道）、隐藏路径、含 NUL 的路径：都不读，整段按普通文本；不阻塞、
    不让整轮报错。"""
    (root / "img" / "sub").mkdir()
    os.mkfifo(root / "img" / "pipe.png")
    (root / ".secret").mkdir()
    (root / ".secret" / "k.png").write_bytes(b"key")
    (root / "c.md").write_text(
        "![目录](img/sub)\n\n![管道](img/pipe.png)\n\n![隐藏](.secret/k.png)\n\n![空字符](a\x00.png)\n",
        encoding="utf-8",
    )
    serve = started(root)
    document = next(i for i in run(serve).items if i.get("file_uri") == uri("c.md"))
    assert [e["category"] for e in document["pages"][0]["elements"]] == ["NarrativeText"] * 4


@pytest.mark.skipif(sys.platform == "win32", reason="符号链接")
def test_symlink_inside_root_is_not_read_and_symlinked_root_works(
    root: Path, tmp_path: Path
) -> None:
    (root / "img" / "alias.png").symlink_to(root / "img" / "logo.png")  # 指向 root 之内
    (root / "c.md").write_text("![别名](img/alias.png)\n", encoding="utf-8")
    linked = tmp_path / "linked-root"
    linked.symlink_to(root)  # root 自身是符号链接：图片照常读取
    serve = started(linked)
    items = {i["file_uri"]: i for i in run(serve).items}
    assert items[uri("c.md")]["pages"][0]["elements"][0]["category"] == "NarrativeText"
    a_elements = items[uri("a.md")]["pages"][0]["elements"]
    assert a_elements[2]["category"] == "Image" and "blob" in a_elements[2]


def test_unreadable_new_file_defers_deletes(root: Path) -> None:
    """读不出的新文件（含不是 UTF-8 的——内容问题也会消失，比如文件写到一半或编码被改回去）：
    推迟本轮的删除与改名，推迟本身报一条 error；该文件 content_invalid、不可重试。"""
    serve = started(root)
    first = run(serve)
    (root / "bad.md").write_bytes(b"\xff\xfe")
    (root / "b.md").unlink()
    second = run(serve, first.cursor)
    assert second.kinds() == [("error", ""), ("error", uri("bad.md"))]
    assert "推迟" in second.items[0]["message"]
    error = second.items[1]
    assert (error["code"], error["retryable"]) == ("content_invalid", False)
    (root / "bad.md").unlink()  # 坏文件处理掉之后，删除照常产出
    assert run(serve, first.cursor).kinds() == [("delete", uri("b.md"))]


def test_new_file_state_is_fixed_at_round_start(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮首读失败的新文件，同一轮内不重读：否则它可能被直接建出来，绕过推迟（下一轮配成的
    move 会撞上已存在的目标）。"""
    from dpe_markdown_dir.source import SourceError
    from dpe_markdown_dir.source import build_document as real

    serve = started(root)
    first = run(serve)
    os.rename(root / "b.md", root / "renamed.md")
    calls: list[str] = []

    def flaky(root_: str, path: str, *args: Any) -> Any:
        calls.append(os.path.basename(path))
        if os.path.basename(path) == "renamed.md" and calls.count("renamed.md") == 1:
            raise SourceError("第一次读不出")
        return real(root_, path, *args)

    monkeypatch.setattr("dpe_markdown_dir.server.build_document", flaky)
    second = run(serve, first.cursor)
    assert second.kinds() == [("error", ""), ("error", uri("renamed.md"))]
    assert calls.count("renamed.md") == 1


def test_files_read_for_move_pairing_are_not_read_again(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve = started(root)
    first = run(serve)
    os.rename(root / "b.md", root / "moved.md")
    (root / "new.md").write_text("新\n", encoding="utf-8")
    reads: list[str] = []
    from dpe_markdown_dir.source import build_document as real

    def counting(root_: str, path: str, *args: Any) -> Any:
        reads.append(os.path.basename(path))
        return real(root_, path, *args)

    monkeypatch.setattr("dpe_markdown_dir.server.build_document", counting)
    second = run(serve, first.cursor)
    assert second.kinds() == [
        ("move", uri("b.md"), uri("moved.md")),
        ("document", uri("moved.md")),
        ("document", uri("new.md")),
    ]
    assert sorted(reads) == ["a.md", "moved.md", "new.md"]  # 每个文件只读一次


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位")
def test_temporarily_unreadable_rename_target_defers_deletes(root: Path) -> None:
    """改名目标暂时读不出（无权限）：不得先产出源的 delete——否则本轮因 error 不推进游标，
    下一轮以旧游标配出的 move 会因源已删除而永久失败（§7.2）。"""
    serve = started(root)
    first = run(serve)
    os.rename(root / "b.md", root / "renamed.md")
    (root / "renamed.md").chmod(0o000)
    try:
        second = run(serve, first.cursor)
    finally:
        (root / "renamed.md").chmod(0o600)
    # 没有 delete，也没有 move；推迟本身报一条 error
    assert second.kinds() == [("error", ""), ("error", uri("renamed.md"))]
    third = run(serve, first.cursor)  # 运行器以旧游标重跑
    assert third.kinds() == [
        ("move", uri("b.md"), uri("renamed.md")),
        ("document", uri("renamed.md")),
    ]


def test_deterministic_bad_image_paths_do_not_fail_the_document(root: Path) -> None:
    """写死在 md 里的坏路径（中间段不是目录、文件名超长）：按缺失或普通文本处理，文档照常产出。"""
    (root / "c.md").write_text(
        "![中间段不是目录](a.md/x.png)\n\n![超长](" + "x" * 300 + ".png)\n", encoding="utf-8"
    )
    serve = started(root)
    document = next(i for i in run(serve).items if i.get("file_uri") == uri("c.md"))
    assert document["kind"] == "document"
    elements = document["pages"][0]["elements"]
    assert elements[0] == {"category": "Image", "text": "中间段不是目录"}
    assert elements[1]["category"] == "NarrativeText"


def test_transient_io_error_on_an_image_is_retryable(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图片的偶发 I/O 错误（如网络盘 EIO）不是确定内容：报可重试的错误，不把 Image 改写成文本。"""
    import errno

    real_lstat = os.lstat

    def flaky(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).endswith("logo.png"):
            raise OSError(errno.EIO, "I/O error")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", flaky)
    serve = started(root)
    item = next(i for i in run(serve).items if i.get("file_uri") == uri("a.md"))
    assert (item["kind"], item["code"], item["retryable"]) == ("error", "source_unavailable", True)


@pytest.mark.skipif(sys.platform == "win32", reason="符号链接与管道")
def test_md_replaced_after_discovery_is_not_read(tmp_path: Path) -> None:
    """md 在发现之后、读取之前被换成指向外部的符号链接或管道：不读，报可重试的错误。"""
    from dpe_markdown_dir.source import SourceError, build_document

    base = tmp_path / "notes"
    base.mkdir()
    secret = tmp_path / "credentials"
    secret.write_text("aws_secret = x\n", encoding="utf-8")
    swapped = base / "a.md"
    swapped.symlink_to(secret)  # 模拟：发现时是普通文件，读取前被换成符号链接
    with pytest.raises(SourceError):
        build_document(str(base), str(swapped), lambda: "h")
    piped = base / "b.md"
    os.mkfifo(piped)
    with pytest.raises(SourceError):
        build_document(str(base), str(piped), lambda: "h")  # 不会阻塞
