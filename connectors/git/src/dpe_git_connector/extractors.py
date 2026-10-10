"""各 file_type 的源内容 → DPE 页与元素（连接器自己的映射策略，见 README「映射规则」）。

每个抽取器只做一件事：把一份文件的字节忠实映射成页对象与内联元素对象（connector 契约 §6.5
的 ``pages``）。**每个字段都来自源**（契约 §2）：不写位置序号、不写扩展名、不写大小、不写
commit 与 mtime（core §2.4）。元素与页的定序即阅读顺序。

大元素的确定性切分
------------------
元素对象不分块（core §3.2），整文件映射因此必须有切分规则。切分预算是一个**固定常量**
（``ELEMENT_BUDGET_BYTES``，按元素对象 JCS 序列化后的 UTF-8 字节数计），而不是运行器协商出的
限额：远端调大或调小限额都不改变本连接器的切分，同一份源在任何远端都得到相同的 hash
（契约 §2 确定性、§8.3 判据 1；远端调大限额也不会让已投递的大文件整体重传）。

``remote_limits`` 的角色是**守卫**而不是重新切分（契约 §6.3 把它交给插件做切分决策）：按固定
预算切分后元素仍超过远端 ``max_payload_bytes`` 的，属部署配置问题，如实上报为条目级失败——
静默按远端值再切一遍会得到同一源的第二套映射。守卫在 ``server`` 层实现（那里才有 remote_limits）。
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from dpe_hash import ElementObject, jcs
from markdown_it import MarkdownIt

__all__ = [
    "ELEMENT_BUDGET_BYTES",
    "ExtractError",
    "ExtractedDocument",
    "element_bytes",
    "extract",
]

#: 单个元素对象（JCS 序列化后）的 UTF-8 字节预算。超出的元素按行边界切成多个连续元素
#: （类别不变），行尾字节留在前一块里。取 1 MiB 的原因：远小于各实现常见的
#: ``max_payload_bytes``（如 8 MiB）与 ``max_message_bytes``，同时大到不会让正常文档碎片化。
ELEMENT_BUDGET_BYTES = 1024 * 1024


#: markdown 解析器：CommonMark（``commonmark`` 预设）+ 表格。版本在本包装配时钉死；
#: 解析器升级可能改变块结构，进而改变全部 markdown 文档的 hash——升级须在 release notes 标注。
_MD = MarkdownIt("commonmark").enable("table")

#: markdown 文件开头的 YAML front matter 块（取 title 并在正文解析前剥去）
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)

#: YAML 顶层键值行（front matter 的认定条件：块内每行都是它，或为空行）
_YAML_KEY = re.compile(r"^[A-Za-z0-9_.-]+[ \t]*:")

#: 块级 token 的类型 → 元素类别由 ``_markdown`` 逐个判定（见那里的 if/elif）


class ExtractError(Exception):
    """源内容无法映射为合法 DPE 内容（条目级 ``content_invalid``，§6.5）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


@dataclass(frozen=True)
class ExtractedDocument:
    """一份文件映射出的文档：文档对象（不含 ``pages``）与全部页对象。"""

    file_type: str
    title: str | None
    pages: list[dict[str, Any]]

    def document(self) -> dict[str, Any]:
        """文档对象（core §2.1）；``title`` 缺省时不出现该键。"""
        obj: dict[str, Any] = {"file_type": self.file_type}
        if self.title is not None:
            obj["title"] = self.title
        return obj


def extract(file_type: str, title: str | None, data: bytes) -> ExtractedDocument:
    """按 ``file_type`` 抽取；内容无法映射时抛 ``ExtractError``。

    ``title`` 由调用方从源里显式声明的字段给出（如 markdown front matter 的 ``title``）；
    本模块不从文件名或正文推导标题。
    """
    extractor = _EXTRACTORS.get(file_type)
    if extractor is None:  # 调用方只对 files.file_type_of 命中的类型调用
        raise ValueError(f"没有 file_type {file_type!r} 的抽取器")
    return extractor(title, data)


# ---------------------------------------------------------------------------
# 通用构造
# ---------------------------------------------------------------------------


def _page(*elements: ElementObject) -> dict[str, Any]:
    """一页（core §2.2）：本连接器的文件映射一律单页（流式格式，core §3.2）。"""
    return {"page_metadata": {}, "elements": [dict(element) for element in elements]}


def element_bytes(element: ElementObject) -> int:
    """元素对象 JCS 序列化后的 UTF-8 字节数（预算按这个量，与传输时的实际大小同口径）。

    ``jcs`` 是 dpe-hash 的公开入口（纯规范化，不做 DPE 的 null 键删除）——本模块构造的元素
    不带 null 键，两者一致。``server`` 的 ``remote_limits`` 守卫也用这个量。
    """
    return len(jcs(dict(element)).encode("utf-8"))


def _split_element(
    element: ElementObject, budget: int = ELEMENT_BUDGET_BYTES
) -> list[ElementObject]:
    """把超预算的文本元素切成连续片段（带同一组非文本字段），每个片段都不超过 ``budget`` 字节。

    切分在行边界进行，行尾字节留在前一块里；单个片段至少含一行——某行自身就超预算时该行独占
    一块（不再细分，留给 ``server`` 的 ``remote_limits`` 守卫上报）。非文本字段（如
    ``text_as_html``）原样复制到每个片段。

    大小按**元素级增量计费**：常数开销（键名、标点与固定字段的 JCS 字节）加上各行的
    ``jcs(line)``。每行的 JCS 转义只会让长度不小于原字节数（转义是变长的），所以这个估计
    是上界，切出的片段必定 ≤ ``budget``；同时每行只做一次序列化，整体 O(n)。
    """
    raw = element.get("text")
    if not isinstance(raw, str):
        return [element]
    text: str = raw
    if element_bytes(element) <= budget:
        return [element]

    def build(piece: str) -> ElementObject:
        clone: dict[str, Any] = {key: value for key, value in element.items() if key != "text"}
        clone["text"] = piece
        return cast(ElementObject, clone)

    # 元素骨架的开销：把 text 换成空串后 JCS 的长度（键名、标点与固定字段都在其中）
    overhead = element_bytes(build(""))
    pieces: list[ElementObject] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_size = len(jcs(line).encode("utf-8"))
        if current and overhead + size + line_size > budget:
            pieces.append(build("".join(current)))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        pieces.append(build("".join(current)))
    return pieces


def _text_elements(category: str, text: str) -> list[ElementObject]:
    """一个文本块 → 一个或多个元素（超出预算的块切分，类别不变）。"""
    return _split_element({"category": category, "text": text})


def _lines_with_ends(text: str) -> list[str]:
    """按 LF 切分并保留行尾（``\\r`` 留在行内，CRLF 因此原样保留）。

    不用 ``str.splitlines(keepends=True)``：它还按 ``\\v``、``\\f``、U+0085、U+2028/U+2029
    等 Unicode 行边界切分，会把源里不成行的字节当成行尾，块内容不再原样。
    """
    lines = text.split("\n")
    out = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def _split_blank_lines(text: str) -> list[str]:
    """按「空行」切成块：块内字节（含行尾）原样保留，分隔用的空行本身不属于任何块。

    逐行扫描而不是用正则：正则的 ``split`` 只能吃掉分隔点的一个字符，会把上一行的行尾
    拆走（LF 时下一块多出前导 ``\\n``、CRLF 时块尾留下裸 ``\\r``）。这里以整行为单位判定，
    一次把连续的空行连同它们的换行全部消费掉。
    """
    blocks: list[str] = []
    current: list[str] = []
    for chunk in _lines_with_ends(text):
        if not chunk.strip():
            # 空行是分隔：它之前的行构成一个块，空行自身（连同全部换行）不属于任何块
            if current:
                blocks.append("".join(current))
                current = []
            continue
        current.append(chunk)
    if current:
        blocks.append("".join(current))
    return [block for block in blocks if block.strip()]


def _decode(data: bytes) -> str:
    """严格 UTF-8 解码；不是合法 UTF-8 即 ``content_invalid``（不猜测其他编码）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractError(f"不是合法 UTF-8：{exc.reason}（偏移 {exc.start}）") from None


# ---------------------------------------------------------------------------
# markdown：CommonMark 块 → 元素
# ---------------------------------------------------------------------------


def _inline_text(tokens: Sequence[Any], index: int) -> str:
    """取 ``index`` 处开标签之后、与之配对的闭标签之前的 inline 文本（源字面量）。"""
    for token in tokens[index + 1 :]:
        if token.type == "inline":
            content: str = token.content
            return content
        if token.type.endswith("_close"):
            break
    return ""


def _closing_index(tokens: Sequence[Any], index: int, kind: str) -> int:
    """``index`` 处容器开标签对应的闭标签下标（同类型嵌套配对）；找不到则在末尾。"""
    depth = 0
    for cursor in range(index, len(tokens)):
        if tokens[cursor].type == kind + "_open":
            depth += 1
        elif tokens[cursor].type == kind + "_close":
            depth -= 1
            if depth == 0:
                return cursor
    return len(tokens) - 1


def _list_item(tokens: Sequence[Any], index: int) -> ElementObject | None:
    """列表项（``list_item_open`` 起，到配对的闭标签）→ ``ListItem`` 元素。

    文本按**本项为根的子树**内的段落/标题 inline 与代码块收集、块之间用换行连接：嵌套列表项
    因此并入本项（各层不另立条目），与 README「列表项 → ListItem」一致。
    """
    close = _closing_index(tokens, index, "list_item")
    parts: list[str] = []
    for cursor in range(index + 1, close):
        token = tokens[cursor]
        if token.type == "inline":
            content: str = token.content
            if content:
                parts.append(content)
        elif token.type in ("fence", "code_block", "html_block"):
            block: str = token.content.rstrip("\n")
            if block:
                parts.append(block)
    text = "\n".join(parts)
    if not text:
        return None
    return {"category": "ListItem", "text": text}


def _table_rows(tokens: Sequence[Any], index: int) -> list[list[str]]:
    """``table_open`` 起的表格 token 片段 → 行 × 单元格的文本矩阵（表头行也在内）。"""
    rows: list[list[str]] = []
    row: list[str] = []
    for token in tokens[index:]:
        if token.type in ("td_open", "th_open"):
            row.append("")
        elif token.type == "inline" and row:
            row[-1] = token.content
        elif token.type == "tr_open":
            row = []
            rows.append(row)
        elif token.type == "table_close":
            break
    return rows


def _html_table(rows: Sequence[Sequence[str]]) -> str:
    """文本矩阵 → HTML 表格（单元格内容按 HTML 转义，保留源字面量）。"""
    lines = ["<table>"]
    for row in rows:
        lines.append("<tr>")
        lines += [f"<td>{html.escape(cell)}</td>" for cell in row]
        lines.append("</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _strip_front_matter(source: str) -> tuple[str, str | None]:
    """剥去文件开头的 YAML front matter 块，返回（正文, 其中的 title 或 ``None``）。

    front matter 是源的元数据头，不是正文：它在 DPE 里由文档 ``title`` 表达，因此不进元素。
    只做最小解析——文件开头 ``---`` 块内**每一行都是 YAML 顶层键值**（``key:`` 后跟值或为空）
    时才认定为 front matter；否则整段按 CommonMark 解析（开头两行 ``---`` 是水平线，不是元数据
    头），避免把正文当元数据静默丢掉。``title:`` 的值两端成对的引号去掉。
    """
    match = _FRONT_MATTER.match(source)
    if match is None:
        return source, None
    body = match.group(1)
    lines = body.split("\n")
    if not all(not line.strip() or _YAML_KEY.match(line) for line in lines):
        return source, None
    title: str | None = None
    for line in lines:
        if line.startswith("title:"):
            value = line.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            title = value or None
            break
    return source[match.end() :], title


def _markdown(title: str | None, data: bytes) -> ExtractedDocument:
    source, front_matter_title = _strip_front_matter(_decode(data))
    if title is None:
        title = front_matter_title
    tokens = _MD.parse(source)
    elements: list[ElementObject] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "heading_open":
            elements += _text_elements("Title", _inline_text(tokens, index))
        elif kind == "paragraph_open":
            elements += _text_elements("NarrativeText", _inline_text(tokens, index))
        elif kind == "blockquote_open":
            # 引用的内容由块内的段落等自行产出，这里只跳过容器
            pass
        elif kind == "list_item_open":
            element = _list_item(tokens, index)
            if element is not None:
                elements.append(element)
            index = _closing_index(tokens, index, "list_item")
        elif kind in ("bullet_list_open", "ordered_list_open"):
            # 列表容器本身不是内容；条目由 list_item_open 分支逐个产出（嵌套项并入所属项）
            pass
        elif kind in ("fence", "code_block"):
            elements += _text_elements("CodeSnippet", token.content)
        elif kind == "html_block":
            # 块级原始 HTML：按源字面量保留（不解析 DOM，也不改写标记）。类别取
            # UncategorizedText——它是原样的标记文本，不是叙述文本（不说假话）。
            elements += _text_elements("UncategorizedText", token.content.rstrip("\n"))
        elif kind == "hr":
            # 水平线是源写下的分块标记（CommonMark thematic break），映射为 PageBreak
            elements.append({"category": "PageBreak"})
        elif kind == "table_open":
            rows = _table_rows(tokens, index)
            if rows:
                text = _table_text(rows, "\t")
                full: ElementObject = {
                    "category": "Table",
                    "text": text,
                    "text_as_html": _html_table(rows),
                }
                if element_bytes(full) <= ELEMENT_BUDGET_BYTES:
                    # 完整表格：文本与 HTML 是同一内容的两份表达，都进 hash
                    elements.append(full)
                else:
                    # 超出预算：按行切分，各块不再是完整表格，类别取 TableChunk（只允许 text）
                    elements += _split_element({"category": "TableChunk", "text": text})
            index = _closing_index(tokens, index, "table")
        index += 1
    return ExtractedDocument(file_type="md", title=title, pages=[_page(*elements)])


# ---------------------------------------------------------------------------
# 纯文本与源代码
# ---------------------------------------------------------------------------


def _plain(title: str | None, data: bytes) -> ExtractedDocument:
    """txt / 源代码：按空行切成块，每块一个 ``UncategorizedText`` 元素。

    纯文本没有语义标记，类别一律取 ``UncategorizedText``（不冒充叙述文本）。行尾字节是内容，
    原样保留在 ``text`` 里；切分只按「连续换行」判定，LF 与 CRLF 都识别为空行。
    """
    source = _decode(data)
    blocks = [block for block in _split_blank_lines(source) if block.strip()]
    elements: list[ElementObject] = []
    for block in blocks:
        elements += _text_elements("UncategorizedText", block)
    return ExtractedDocument(file_type="txt", title=title, pages=[_page(*elements)])


# ---------------------------------------------------------------------------
# 分隔文本（csv / tsv）：整表一个 Table 元素，超预算按行切成 TableChunk
# ---------------------------------------------------------------------------


def _table_text(rows: Sequence[Sequence[str]], delimiter: str) -> str:
    return "\n".join(delimiter.join(row) for row in rows)


def _delimited(file_type: str, title: str | None, data: bytes, delimiter: str) -> ExtractedDocument:
    source = _decode(data)
    # 用 csv 模块而不是手写拆分：引号包裹、字段内换行与分隔符都由它按 RFC 4180 处理
    rows = [row for row in csv.reader(io.StringIO(source), delimiter=delimiter)]
    text = _table_text(rows, delimiter)
    elements: list[ElementObject] = []
    if not text:
        return ExtractedDocument(file_type=file_type, title=title, pages=[_page()])
    if element_bytes({"category": "Table", "text": text}) <= ELEMENT_BUDGET_BYTES:
        elements.append({"category": "Table", "text": text})
    else:
        # 超出预算：按行切成连续的 TableChunk（TableChunk 只允许 text，不再带 text_as_html）。
        # 复用 _split_element 的增量计费：逐行重算整个块的字节是二次方复杂度（曾在 2 MB csv 上
        # 实测 268 秒，见 test_large_csv_split_is_linear）。
        elements += _split_element({"category": "TableChunk", "text": text})
    return ExtractedDocument(file_type=file_type, title=title, pages=[_page(*elements)])


# ---------------------------------------------------------------------------
# json / ndjson：规范化文本（确定性），超预算按行切成 NarrativeText
# ---------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    """确定性序列化：键排序、固定缩进、``ensure_ascii=False``（结果只由值决定）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _json(file_type: str, title: str | None, data: bytes) -> ExtractedDocument:
    source = _decode(data)
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"不是合法 JSON：{exc.msg}（行 {exc.lineno} 列 {exc.colno}）") from None
    elements = _text_elements("NarrativeText", _json_text(value))
    return ExtractedDocument(file_type=file_type, title=title, pages=[_page(*elements)])


def _ndjson(file_type: str, title: str | None, data: bytes) -> ExtractedDocument:
    source = _decode(data)
    values: list[str] = []
    # 只按 LF 断行（ndjson 的行分隔符），不按 U+2028/U+2029/U+0085 等 Unicode 行边界
    for lineno, line in enumerate(source.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            values.append(_json_text(json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ExtractError(
                f"第 {lineno} 行不是合法 JSON：{exc.msg}（列 {exc.colno}）"
            ) from None
    text = "\n".join(values)
    return ExtractedDocument(
        file_type=file_type, title=title, pages=[_page(*_text_elements("NarrativeText", text))]
    )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_Extractor = Callable[[str | None, bytes], ExtractedDocument]

_EXTRACTORS: dict[str, _Extractor] = {
    "md": _markdown,
    "txt": _plain,
    "csv": lambda title, data: _delimited("csv", title, data, ","),
    "tsv": lambda title, data: _delimited("tsv", title, data, "\t"),
    "json": lambda title, data: _json("json", title, data),
    "ndjson": lambda title, data: _ndjson("ndjson", title, data),
}


def supported_file_types() -> frozenset[str]:
    """本连接器能映射的 file_type 集合（``files`` 的映射表必须只产出这里的取值，见测试）。"""
    return frozenset(_EXTRACTORS)
