"""ADF（Atlassian Document Format）→ DPE 元素的确定性转换（#223 §4.1）。

只按 ADF 的节点结构转换，不依赖任何渲染库：换一种语言照着下面的规则实现，能得到逐字节相同的
元素序列（同 markdown-dir 的要求）。

**块级节点**（文档的顶层 ``content``，以及容器内的块）：

- ``paragraph`` → ``NarrativeText``；渲染后为空串的段落不产出（空段落不承载内容）；
- ``heading`` → ``Title``，``metadata.level`` = ``attrs.level``；
- ``bulletList`` / ``orderedList`` → 每个 ``listItem`` 一个 ``ListItem``：``text`` 为该项里段落
  的渲染文本（以换行连接），``metadata.depth`` 为嵌套层级（从 0 起），``metadata.ordered`` 表示
  是否有序；项内嵌套的列表按 ``depth + 1`` 递归，项内其他块按块级规则产出；
- ``codeBlock`` → ``CodeSnippet``，``text`` 为原样文本，``metadata.language`` = ``attrs.language``
  （有才记）；
- ``blockquote`` → 一个 ``NarrativeText``，``text`` 为其中各块的渲染文本以换行连接，
  ``metadata.quote`` = ``true``；
- ``table`` → ``Table``：``text`` 为逐行文本（单元格以制表符、行以换行连接），``text_as_html``
  为固定格式的 ``<table><tr><th|td>…</th|td></tr></table>``（单元格内容做 HTML 转义，换行写作
  ``<br>``）；
- ``mediaSingle`` / ``mediaGroup`` → 其中每个 ``media`` 一个 ``Image``：``text`` = ``attrs.alt``
  （没有则为空串），``metadata.media_id`` = ``attrs.id``；字节在附件一期接入（本期不带 blob，
  规范允许 blob 缺省）；
- ``rule`` 不产出；
- 其他（不认识的）节点：取其中全部文本，非空时按 ``UncategorizedText`` 产出——不丢内容、不报错。

**行内节点**渲染为纯文本：``text`` 原样（带 ``link`` 标记且链接与文字不同时写作
「文字 (URL)」），``mention`` 写作「@显示名」，``emoji`` 取 ``shortName``，``inlineCard`` 取 URL，
``hardBreak`` 为换行，``date`` 为 ``YYYY-MM-DD``（UTC），``status`` 取其文字；不认识的行内节点取
其中的文本。
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

__all__ = ["to_elements"]

Element = dict[str, Any]


def to_elements(doc: Any, metadata: dict[str, Any] | None = None) -> list[Element]:
    """把一份 ADF 文档转成元素列表；``metadata`` 并入每个元素的 metadata（评论用）。"""
    if not isinstance(doc, dict):
        return []
    elements: list[Element] = []
    for block in _children(doc):
        _block(block, elements)
    if metadata:
        for element in elements:
            element["metadata"] = {**element["metadata"], **metadata}
    return elements


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    content = node.get("content")
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, dict)]


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _element(category: str, text: str, metadata: dict[str, Any] | None = None) -> Element:
    return {"category": category, "text": text, "metadata": dict(metadata or {})}


def _block(node: dict[str, Any], out: list[Element], depth: int = 0) -> None:
    kind = node.get("type")
    meta: dict[str, Any]
    if kind == "paragraph":
        text = _inline(node)
        if text:
            out.append(_element("NarrativeText", text))
    elif kind == "heading":
        level = _attrs(node).get("level")
        meta = {"level": level} if isinstance(level, int) and not isinstance(level, bool) else {}
        out.append(_element("Title", _inline(node), meta))
    elif kind in ("bulletList", "orderedList"):
        _list(node, out, depth, ordered=kind == "orderedList")
    elif kind == "codeBlock":
        language = _attrs(node).get("language")
        meta = {"language": language} if isinstance(language, str) and language else {}
        out.append(_element("CodeSnippet", _raw_text(node), meta))
    elif kind == "blockquote":
        out.append(_element("NarrativeText", _block_text(node), {"quote": True}))
    elif kind == "table":
        out.append(_table(node))
    elif kind in ("mediaSingle", "mediaGroup"):
        for media in _children(node):
            if media.get("type") == "media":
                attrs = _attrs(media)
                alt = attrs.get("alt")
                meta = {"media_id": attrs["id"]} if isinstance(attrs.get("id"), str) else {}
                out.append(_element("Image", alt if isinstance(alt, str) else "", meta))
    elif kind == "rule":
        return
    else:
        text = _block_text(node)
        if text:
            out.append(_element("UncategorizedText", text))


def _list(node: dict[str, Any], out: list[Element], depth: int, *, ordered: bool) -> None:
    for item in _children(node):
        if item.get("type") != "listItem":
            _block(item, out, depth)
            continue
        lines: list[str] = []
        nested: list[dict[str, Any]] = []
        for child in _children(item):
            if child.get("type") == "paragraph":
                lines.append(_inline(child))
            else:
                nested.append(child)
        out.append(_element("ListItem", "\n".join(lines), {"depth": depth, "ordered": ordered}))
        for child in nested:
            if child.get("type") in ("bulletList", "orderedList"):
                _list(child, out, depth + 1, ordered=child.get("type") == "orderedList")
            else:
                _block(child, out, depth)


def _table(node: dict[str, Any]) -> Element:
    rows: list[list[tuple[str, str]]] = []
    for row in _children(node):
        if row.get("type") != "tableRow":
            continue
        cells: list[tuple[str, str]] = []
        for cell in _children(row):
            tag = "th" if cell.get("type") == "tableHeader" else "td"
            cells.append((tag, _block_text(cell)))
        rows.append(cells)
    text = "\n".join("\t".join(value for _, value in cells) for cells in rows)
    markup = "".join(
        "<tr>"
        + "".join(
            f"<{tag}>{html.escape(value, quote=True).replace(chr(10), '<br>')}</{tag}>"
            for tag, value in cells
        )
        + "</tr>"
        for cells in rows
    )
    return {
        "category": "Table",
        "text": text,
        "text_as_html": f"<table>{markup}</table>",
        "metadata": {},
    }


def _block_text(node: dict[str, Any]) -> str:
    """容器的文本：子块各自渲染（段落按行内规则，其余递归），非空的以换行连接。"""
    parts: list[str] = []
    for child in _children(node):
        if child.get("type") == "paragraph" or _is_inline(child):
            text = _inline(child) if child.get("type") == "paragraph" else _inline_node(child)
        elif child.get("type") == "codeBlock":
            text = _raw_text(child)
        else:
            text = _block_text(child)
        if text:
            parts.append(text)
    return "\n".join(parts)


_INLINE = frozenset(
    {"text", "mention", "emoji", "inlineCard", "hardBreak", "date", "status", "mediaInline"}
)


def _is_inline(node: dict[str, Any]) -> bool:
    return node.get("type") in _INLINE


def _inline(node: dict[str, Any]) -> str:
    return "".join(_inline_node(child) for child in _children(node))


def _inline_node(node: dict[str, Any]) -> str:
    kind = node.get("type")
    attrs = _attrs(node)
    if kind == "text":
        text = node.get("text")
        text = text if isinstance(text, str) else ""
        for mark in node.get("marks") or []:
            if isinstance(mark, dict) and mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href")
                if isinstance(href, str) and href and href != text:
                    text = f"{text} ({href})"
        return text
    if kind == "mention":
        name = attrs.get("text")
        name = name if isinstance(name, str) else ""
        return name if name.startswith("@") else f"@{name}"
    if kind == "emoji":
        short = attrs.get("shortName")
        return short if isinstance(short, str) else ""
    if kind == "inlineCard":
        url = attrs.get("url")
        return url if isinstance(url, str) else ""
    if kind == "hardBreak":
        return "\n"
    if kind == "date":
        stamp: Any = attrs.get("timestamp")
        try:
            return datetime.fromtimestamp(int(stamp) / 1000, UTC).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError, OSError):
            return ""
    if kind == "status":
        text = attrs.get("text")
        return text if isinstance(text, str) else ""
    # 不认识的行内节点：取其中的文本
    return _inline(node)


def _raw_text(node: dict[str, Any]) -> str:
    """代码块的原样文本：只拼接 text 节点，不渲染标记。"""
    return "".join(
        child.get("text", "")
        for child in _children(node)
        if child.get("type") == "text" and isinstance(child.get("text"), str)
    )
