"""示例：本地 Markdown 目录 → DPE → Robot。

这是**上层调用方**的示例，演示如何使用本包：自行遍历源数据、构造 Document、交给
:class:`~dpe_protocol.StatefulPusher` 投递，并用 :func:`~dpe_protocol.testing.check_documents` 自检。
遍历、解析、调度都属于上层逻辑，本包不提供也不规定其形态。

每个 ``.md`` 文件对应一个 Document、一页（``number=0``），按块切分为 element：
标题 → Title、列表项 → ListItem、围栏代码 → CodeSnippet、表格 → Table、其余段落 → NarrativeText。

运行::

    uv run python examples/push_markdown_dir.py <markdown-dir> <tenant> [--prefix P] [--push URL --token TOKEN]

不带 ``--push`` 时只做一致性自检；带上后增量投递到 Robot。
"""

import argparse
import asyncio
import hashlib
import html
import re
from pathlib import Path

from dpe_protocol import (
    DocElement,
    DocMetadata,
    DocPage,
    Document,
    DPEPushClient,
    ElementCategory,
    ElementMetadata,
    FileType,
    JsonFileStateStore,
    PushResult,
    StatefulPusher,
    make_dpe_uri,
)
from dpe_protocol.testing import ConformanceReport, check_documents

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def markdown_to_elements(text: str) -> list[DocElement]:
    elements: list[DocElement] = []
    paragraph: list[str] = []
    table: list[str] = []
    lines = text.splitlines()

    def flush_paragraph() -> None:
        if paragraph:
            elements.append(DocElement(category=ElementCategory.NARRATIVE_TEXT, text="\n".join(paragraph).strip()))
            paragraph.clear()

    def flush_table() -> None:
        if table:
            elements.append(_table_element(table))
            table.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if fence := _FENCE.match(line):
            flush_paragraph()
            flush_table()
            marker, body = fence.group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            elements.append(DocElement(category=ElementCategory.CODE_SNIPPET, text="\n".join(body)))
        elif line.lstrip().startswith("|"):
            flush_paragraph()
            table.append(line)
        elif heading := _HEADING.match(line):
            flush_paragraph()
            flush_table()
            elements.append(
                DocElement(
                    category=ElementCategory.TITLE,
                    text=heading.group(2),
                    ele_metadata=ElementMetadata(category_depth=len(heading.group(1)) - 1),
                )
            )
        elif item := _LIST_ITEM.match(line):
            flush_paragraph()
            flush_table()
            elements.append(DocElement(category=ElementCategory.LIST_ITEM, text=item.group(1).strip()))
        elif not line.strip():
            flush_paragraph()
            flush_table()
        else:
            flush_table()
            paragraph.append(line)
        i += 1
    flush_paragraph()
    flush_table()
    return elements


def _table_element(lines: list[str]) -> DocElement:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in lines
        if not _TABLE_SEPARATOR.match(line)
    ]
    body = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in rows)
    return DocElement(
        category=ElementCategory.TABLE,
        text="\n".join(" ".join(row) for row in rows),
        ele_metadata=ElementMetadata(text_as_html=f"<table>{body}</table>"),
    )


def file_uri_for(rel: str, *, tenant: str, prefix: str = "") -> str:
    return make_dpe_uri(tenant, f"{prefix.strip('/')}/{rel}" if prefix.strip("/") else rel)


def build_document(root: Path, rel: str, *, tenant: str, prefix: str = "") -> Document:
    path = root / rel
    elements = markdown_to_elements(path.read_text(encoding="utf-8"))
    title = next((e.text for e in elements if e.category == ElementCategory.TITLE), None)
    return Document.model_validate(
        {
            "file_uri": file_uri_for(rel, tenant=tenant, prefix=prefix),
            "file_type": FileType.MD,
            # 只放由内容决定的字段；mtime 之类会让未改动的文件也产生新 doc_hash
            "doc_metadata": DocMetadata(filename=path.name),
            "pages": [DocPage(number=0, title=title, elements=elements)],
        }
    )


def list_markdown(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in sorted(root.glob("**/*.md"))]


async def check_directory(root: Path, *, tenant: str, prefix: str = "") -> ConformanceReport:
    """对每个文件构造两次文档，运行一致性检查。"""
    report = ConformanceReport()
    for rel in list_markdown(root):
        first = build_document(root, rel, tenant=tenant, prefix=prefix)
        second = build_document(root, rel, tenant=tenant, prefix=prefix)
        expected = file_uri_for(rel, tenant=tenant, prefix=prefix)
        report.issues += (await check_documents(first, second, expected_file_uri=expected)).issues
    return report


async def push_directory(
    root: Path, pusher: StatefulPusher, *, tenant: str, prefix: str = ""
) -> dict[str, PushResult | None]:
    """增量投递目录：文件内容未变（fingerprint 相同）时跳过，结果为 ``None``。"""
    results: dict[str, PushResult | None] = {}
    for rel in list_markdown(root):
        uri = file_uri_for(rel, tenant=tenant, prefix=prefix)
        fingerprint = hashlib.sha256((root / rel).read_bytes()).hexdigest()[:32]
        if await pusher.needs_push(uri, fingerprint):
            results[uri] = await pusher.push(
                build_document(root, rel, tenant=tenant, prefix=prefix), fingerprint=fingerprint
            )
        else:
            results[uri] = None
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path)
    parser.add_argument("tenant")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--push", metavar="ROBOT_URL")
    parser.add_argument("--token")
    parser.add_argument("--state", default=".dpe-state/markdown.json")
    args = parser.parse_args()

    report = await check_directory(args.root, tenant=args.tenant, prefix=args.prefix)
    print(f"conformance: {len(report.issues)} issues")
    report.raise_for_issues()

    if args.push:
        async with DPEPushClient(args.push, token=args.token) as client:
            pusher = StatefulPusher(client, JsonFileStateStore(args.state))
            results = await push_directory(args.root, pusher, tenant=args.tenant, prefix=args.prefix)
        for uri, result in results.items():
            print(uri, "skipped" if result is None else f"{result.status} {result.counts}")


if __name__ == "__main__":
    asyncio.run(main())
