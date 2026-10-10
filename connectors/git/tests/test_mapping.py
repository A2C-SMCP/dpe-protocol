"""映射层单测：file_type 表、file_uri 构造、抽取结果与 doc_hash、超限守卫、条目构造。"""

from __future__ import annotations

from typing import Any, cast

from dpe_hash import ExpandedDocument, document_hashes, normalize_file_uri

from dpe_git_connector.extractors import ELEMENT_BUDGET_BYTES, element_bytes, extract
from dpe_git_connector.files import file_type_of
from dpe_git_connector.gitrepo import FileEntry
from dpe_git_connector.mapping import DocumentMapper, Limits, build_file_uri

PREFIX = "git://docs/"


class FakeRepo:
    """按对象 id 返回固定字节的假仓库（映射层单测用，不落盘）。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def read_blob(self, object_id: str, size: int) -> bytes:
        data = self.files[object_id]
        assert len(data) == size
        return data


def entry(path: str, data: bytes, object_id: str = "oid") -> FileEntry:
    return FileEntry(path=path.encode(), object_id=object_id, size=len(data))


def only_item(mapper: DocumentMapper, path: str, data: bytes) -> dict[str, Any]:
    file_type = file_type_of(path.encode())
    assert file_type is not None, path
    repo = FakeRepo({"oid": data})
    return mapper.document(repo, entry(path, data), file_type).payload()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- file_type 表


def test_file_type_mapping() -> None:
    assert file_type_of(b"a.md") == "md"
    assert file_type_of(b"a.MARKDOWN") == "md"
    assert file_type_of(b"dir/b.txt") == "txt"
    assert file_type_of(b"a.csv") == "csv"
    assert file_type_of(b"a.tsv") == "tsv"
    assert file_type_of(b"a.json") == "json"
    assert file_type_of(b"a.ndjson") == "ndjson"
    # 源代码标 txt：枚举里没有单个代码文件的类型，不发明取值
    for name in (b"m.py", b"M.JAVA", b"x.tsx", b"x.mjs"):
        assert file_type_of(name) == "txt"
    assert file_type_of(b"Makefile") == "txt"
    assert file_type_of(b"dir/Dockerfile") == "txt"
    # 未支持：不产出
    for name in (b"a.png", b"a.pdf", b"a.docx", b"a.html", b"a.xml", b"a.rst", b"a.org", b"a.zip"):
        assert file_type_of(name) is None, name
    assert file_type_of(b"noext") is None
    assert file_type_of(b"a.MD") == "md"
    # 路径按原始字节判定：非 UTF-8 与中文的名字照样命中扩展名（file_uri 用百分号编码表达）
    assert file_type_of(b"bad_\xff\xfe.md") == "md"
    assert file_type_of(b"docs/\xe7\xac\x94\xe8\xae\xb0.txt") == "txt"
    # 点文件：点在最前不算扩展名
    assert file_type_of(b".gitignore") is None


# --------------------------------------------------------------------------- file_uri


def test_build_file_uri_encodes_and_normalizes() -> None:
    uri = build_file_uri(PREFIX, "docs/说明.md".encode())
    assert uri == "git://docs/docs/%E8%AF%B4%E6%98%8E.md"
    assert normalize_file_uri(uri) == uri  # 规范化不动点


def test_build_file_uri_keeps_unreserved_and_encodes_specials() -> None:
    uri = build_file_uri(PREFIX, b"a b\tc.md")
    assert uri == "git://docs/a%20b%09c.md"
    # 非 UTF-8 路径字节同样可表达（百分号编码对字节透明）
    assert build_file_uri(PREFIX, b"bad_\xff.md") == "git://docs/bad_%FF.md"


def test_build_file_uri_rejects_empty_prefix() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_file_uri("", b"a.md")


# --------------------------------------------------------------------------- 文档条目


def test_document_item_maps_markdown() -> None:
    mapper = DocumentMapper(PREFIX)
    data = "# 标题\n\n正文。\n".encode()
    item = only_item(mapper, "a.md", data)
    assert item["kind"] == "document"
    assert item["file_uri"] == "git://docs/a.md"
    assert item["document"] == {"file_type": "md"}
    elements = item["pages"][0]["elements"]
    assert [e["category"] for e in elements] == ["Title", "NarrativeText"]
    # 产出的对象经 dpe-hash 校验并算出稳定的三层 hash（与运行器同口径）
    expanded = cast(ExpandedDocument, {"file_type": "md", "pages": item["pages"]})
    first = document_hashes(expanded)
    second = document_hashes(expanded)
    assert first == second
    assert first["doc_hash"].startswith("dpe1:")


def test_document_item_includes_front_matter_title() -> None:
    mapper = DocumentMapper(PREFIX)
    item = only_item(mapper, "a.md", "---\ntitle: 标题\n---\n\n正文\n".encode())
    assert item["document"]["title"] == "标题"


def test_document_item_skips_unsupported_file_type_before_mapping() -> None:
    # 调用方按 file_type_of 过滤；这里确认 html（映射表外）确实不命中
    assert file_type_of(b"a.html") is None


def test_content_invalid_on_bad_utf8() -> None:
    mapper = DocumentMapper(PREFIX)
    item = only_item(mapper, "a.md", b"\xff\xfe not utf8")
    assert item["kind"] == "error"
    assert item["code"] == "content_invalid"
    assert item["retryable"] is False
    assert item["file_uri"] == "git://docs/a.md"


def test_content_invalid_on_bad_json() -> None:
    mapper = DocumentMapper(PREFIX)
    item = only_item(mapper, "a.json", b"{not json")
    assert item["kind"] == "error"
    assert item["code"] == "content_invalid"


# --------------------------------------------------------------------------- 超限守卫


def _big_text(size: int) -> bytes:
    return ("x" * (size - 1) + "\n").encode()


def test_oversize_element_error_when_remote_limit_smaller_than_budget() -> None:
    mapper = DocumentMapper(PREFIX, Limits(max_payload_bytes=1024))
    item = only_item(mapper, "a.txt", _big_text(4096))
    # 固定预算切分后每块仍大于远端 max_payload_bytes：如实报错，不按远端值重新切分
    assert item["kind"] == "error"
    assert item["code"] == "content_invalid"
    assert "max_payload_bytes" in item["message"]


def test_no_oversize_error_without_remote_limits() -> None:
    mapper = DocumentMapper(PREFIX)
    item = only_item(mapper, "a.txt", _big_text(4096))
    assert item["kind"] == "document"


def test_split_elements_stay_within_budget() -> None:
    text = ("段落。" * 100 + "\n") * 9000  # 约 2.7 MiB 的 UTF-8 文本
    extracted = extract("txt", None, text.encode())
    elements = extracted.pages[0]["elements"]
    assert len(elements) > 1
    assert all(element_bytes(element) <= ELEMENT_BUDGET_BYTES for element in elements)
    # 行边界切分不丢字节：拼回来与原文一致
    assert "".join(element["text"] for element in elements) == text


def test_split_budget_accounts_for_json_escaping() -> None:
    """预算按 JCS 序列化后的字节实测量，引号等转义不会让片段越界（引号密集的文本）。"""
    text = '"a": "b",\n' * 120_000  # 约 1.2 MiB，引号占比高（JCS 下每个引号多 1 字节）
    extracted = extract("txt", None, text.encode())
    elements = extracted.pages[0]["elements"]
    assert len(elements) > 1
    assert all(element_bytes(element) <= ELEMENT_BUDGET_BYTES for element in elements)
    assert "".join(element["text"] for element in elements) == text


# --------------------------------------------------------------------------- markdown 块映射


def markdown_elements(source: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = extract("md", None, source.encode()).pages[0]["elements"]
    return elements


def test_markdown_list_items_are_mapped() -> None:
    """列表整段不得被跳过（回归：容器分支曾把整个列表跳过，ListItem 永不产出）。"""
    elements = markdown_elements("- 甲\n- 乙\n\n尾段。\n")
    assert [element["category"] for element in elements] == [
        "ListItem",
        "ListItem",
        "NarrativeText",
    ]
    assert [element["text"] for element in elements[:2]] == ["甲", "乙"]


def test_markdown_nested_list_folds_into_parent_item() -> None:
    elements = markdown_elements("- 外层\n\n  - 内层\n- 第二项\n")
    assert [element["category"] for element in elements] == ["ListItem", "ListItem"]
    assert elements[0]["text"] == "外层\n内层"
    assert elements[1]["text"] == "第二项"


def test_markdown_ordered_list_and_code_snippet() -> None:
    elements = markdown_elements("1. 一\n2. 二\n\n```py\nx = 1\n```\n")
    assert [element["category"] for element in elements] == [
        "ListItem",
        "ListItem",
        "CodeSnippet",
    ]


def test_markdown_heading_paragraph_table_and_hr() -> None:
    source = "# 标题\n\n段落。\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n---\n"
    elements = markdown_elements(source)
    assert [element["category"] for element in elements] == [
        "Title",
        "NarrativeText",
        "Table",
        "PageBreak",
    ]
    table = elements[2]
    assert table["text"] == "a\tb\n1\t2"
    assert "<table>" in table["text_as_html"]


def test_front_matter_requires_yaml_keys() -> None:
    """开头 ''---'' 块要真是键值形态才算 front matter；否则是水平线 + 正文，不得静默丢弃。"""
    titled = extract("md", None, b"---\ntitle: T\n---\n\n# H\n")
    assert titled.title == "T"
    assert [element["category"] for element in titled.pages[0]["elements"]] == ["Title"]

    # 不是 YAML 键值：整段按 CommonMark 解析（hr + setext 标题 + 段落），内容都在
    confusable = extract("md", None, b"---\nTitle\n---\nbody\n")
    assert confusable.title is None
    categories = [element["category"] for element in confusable.pages[0]["elements"]]
    assert categories == ["PageBreak", "Title", "NarrativeText"]
    assert [element["text"] for element in confusable.pages[0]["elements"][1:]] == ["Title", "body"]


def test_entry_level_guard_returns_internal_error() -> None:
    """条目级兜底：解析器抛出的任何未预期异常都记为 internal 错误条目，不带崩进程。"""
    mapper = DocumentMapper(PREFIX)
    huge = '{"n": ' + "9" * 5000 + "}"  # json 可解析，但重新序列化时超出 int→str 的位数上限
    item = only_item(mapper, "a.json", huge.encode())
    assert item["kind"] == "error"
    assert item["code"] == "internal"
    assert item["retryable"] is False
    assert item["file_uri"] == "git://docs/a.json"


def test_csv_field_limit_is_reported_as_error() -> None:
    """csv 的单字段上限（131072 字符）触发 csv.Error：条目级 internal，不带崩进程。"""
    mapper = DocumentMapper(PREFIX)
    item = only_item(mapper, "a.csv", b"a," + b"x" * 200_000 + b"\n")
    assert item["kind"] == "error"
    assert item["code"] == "internal"


def test_every_mapped_file_type_has_an_extractor() -> None:
    """映射表与抽取器不脱钩：``files`` 产出的每个 file_type 都必须有抽取器（反之亦然）。"""
    from dpe_git_connector.extractors import supported_file_types
    from dpe_git_connector.files import _EXTENSIONS, _FILENAMES

    mapped = set(_EXTENSIONS.values()) | set(_FILENAMES.values())
    assert mapped == set(supported_file_types())


def test_plain_block_text_preserves_bytes_exactly() -> None:
    """txt 的块内字节（含行尾）原样保留：LF 与 CRLF 两套都不得被改写、不得多出前导换行。"""
    lf = extract("txt", None, b"first\n\nsecond\n")
    assert [e["text"] for e in lf.pages[0]["elements"]] == ["first\n", "second\n"]

    crlf = extract("txt", None, b"a\r\n\r\nb\r\n")
    assert [e["text"] for e in crlf.pages[0]["elements"]] == ["a\r\n", "b\r\n"]

    # 多个连续空行与含空格的空行都只作分隔；块尾行尾保留
    many = extract("txt", None, b"x\n\n\n  \ny\n")
    assert [e["text"] for e in many.pages[0]["elements"]] == ["x\n", "y\n"]


def test_markdown_html_block_is_preserved() -> None:
    """块级原始 HTML 不得被静默丢弃（按源字面量、UncategorizedText 呈现）。"""
    elements = markdown_elements('<div class="x">hello</div>\n\npara\n')
    assert [e["category"] for e in elements] == ["UncategorizedText", "NarrativeText"]
    assert elements[0]["text"] == '<div class="x">hello</div>'


def test_large_csv_split_is_linear() -> None:
    """大 CSV 的切分必须是线性（回归：逐行重算整块字节曾让 2 MB csv 跑 268 秒）。"""
    import time

    rows = 60_000
    data = "".join(f"{i},value-{i},padding-{'x' * 20}\n" for i in range(rows)).encode()
    assert len(data) > ELEMENT_BUDGET_BYTES
    start = time.monotonic()
    extracted = extract("csv", None, data)
    elapsed = time.monotonic() - start
    elements = extracted.pages[0]["elements"]
    assert len(elements) > 1
    assert {e["category"] for e in elements} == {"TableChunk"}
    assert all(element_bytes(e) <= ELEMENT_BUDGET_BYTES for e in elements)
    assert elapsed < 10, f"切分耗时 {elapsed:.1f}s，疑似退化为二次方"
