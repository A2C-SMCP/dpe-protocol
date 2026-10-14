"""Markdown 文本 → 页与元素（纯函数，只用标准库）。

这是示例插件的「内容定义」：同一份文本总是得到同一组页与元素（确定性，契约 §8.3 判据 1）。
规则刻意简单，只覆盖常见的块级结构：

- **页**：代码块之外、单独成行的 ``---`` 是页边界（主题分隔线）；
- **元素**（按块，空行分隔）：
  - ATX 标题 ``#`` … ``######`` → ``Title``，``metadata {"level": n}``；
  - 围栏代码（``````` 或 ``~~~``）→ ``CodeSnippet``，有语言标注时 ``metadata {"language": …}``；
  - 列表项（``-`` / ``*`` / ``+`` / ``1.``）→ 每项一个 ``ListItem``（续行并入该项）；
  - 只含一张本地图片的段落 ``![alt](path)`` → ``Image``（字节由调用方读取，见 ``ImageRef``）；
  - 其余段落 → ``NarrativeText``（行内的图片、链接等原样保留为文本）。

文本原样进入元素（只去掉块首尾的空格与制表符），不渲染、不转义；不写易变字段与位置序号。

**规则与语言无关**（便于用别的语言照着实现、得到相同的内容与 hash）：只按 ``\n`` 切行（行尾
的 ``\r`` 去掉）；「空白」只指空格与制表符——不用宿主语言自带的 Unicode 行界与空白判定
（如 Python 的 ``splitlines`` / ``strip`` 会把 U+2028、全角空格等也算进去）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["ImageRef", "parse_markdown"]

#: 收尾的 ``#`` 串只在前面有空白时才算闭合序列（``# 讲 C#`` 的 ``#`` 是正文）
_HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
_FENCE = re.compile(r"(`{3,}|~{3,})[ \t]*([^ \t`]*)")
_LIST_ITEM = re.compile(r"(?:[-*+]|[0-9]{1,9}[.)])[ \t]+")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^) \t\n]+)\)")
_PAGE_BREAK = "---"
#: 本模块所说的「空白」：只有空格与制表符
_BLANK = " \t"
#: 不当作本地文件的图片地址（URL 形式按普通文本处理，不抓取）
_REMOTE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class ImageRef:
    """一个 Image 元素引用的本地图片：页、元素下标与 markdown 中写的相对路径。"""

    page: int
    element: int
    path: str
    #: 段落原文：图片不可读（越出 root 等）时按普通文本保留它
    source: str


def parse_markdown(text: str) -> tuple[list[list[dict[str, Any]]], list[ImageRef]]:
    """返回（每页的元素列表, 图片引用）。Image 元素此时还没有 ``blob`` 与 ``mime_type``：
    由调用方读取字节后补上（字节来源与类型取决于文件，不取决于文本）。"""
    pages: list[list[dict[str, Any]]] = [[]]
    images: list[ImageRef] = []
    paragraph: list[str] = []
    item: list[str] | None = None  # 进行中的列表项
    fence: tuple[str, str, list[str]] | None = None  # (围栏标记, 语言, 代码行)

    def flush() -> None:
        nonlocal paragraph, item
        if item is not None:
            pages[-1].append({"category": "ListItem", "text": "\n".join(item).strip(_BLANK)})
            item = None
        if paragraph:
            block = "\n".join(paragraph).strip(_BLANK)
            paragraph = []
            if block:
                pages[-1].append(_paragraph(block, len(pages) - 1, len(pages[-1]), images))

    for line in _lines(text):
        if fence is not None:
            marker, language, code = fence
            closing = line.strip(_BLANK)
            if closing.startswith(marker) and not closing[len(marker) :].strip(_BLANK):
                element: dict[str, Any] = {"category": "CodeSnippet", "text": "\n".join(code)}
                if language:
                    element["metadata"] = {"language": language}
                pages[-1].append(element)
                fence = None
            else:
                code.append(line)
            continue
        stripped = line.strip(_BLANK)
        opening = _FENCE.fullmatch(stripped)
        if opening:
            flush()
            fence = (opening.group(1), opening.group(2), [])
            continue
        if stripped == _PAGE_BREAK:
            flush()
            pages.append([])
            continue
        if not stripped:
            flush()
            continue
        heading = _HEADING.fullmatch(stripped)
        if heading:
            flush()
            pages[-1].append(
                {
                    "category": "Title",
                    "text": heading.group(2),
                    "metadata": {"level": len(heading.group(1))},
                }
            )
            continue
        bullet = _LIST_ITEM.match(stripped)
        if bullet:
            flush()
            item = [stripped[bullet.end() :]]
            continue
        if item is not None:
            item.append(stripped)  # 列表项的续行
        else:
            paragraph.append(line)
    if fence is not None:  # 未闭合的围栏：按到文末为止的代码处理（与常见渲染器一致）
        marker, language, code = fence
        element = {"category": "CodeSnippet", "text": "\n".join(code)}
        if language:
            element["metadata"] = {"language": language}
        pages[-1].append(element)
    flush()
    return pages, images


def _lines(text: str) -> list[str]:
    """只按 ``\n`` 切行，去掉行尾的 ``\r``（CRLF 文件与 LF 文件得到相同的行）；文末的
    ``\n`` 只是结束最后一行，不另起一个空行。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _paragraph(block: str, page: int, index: int, images: list[ImageRef]) -> dict[str, Any]:
    image = _IMAGE.fullmatch(block)
    if image and not _REMOTE.match(image.group(2)):
        images.append(ImageRef(page=page, element=index, path=image.group(2), source=block))
        return {"category": "Image", "text": image.group(1)}
    return {"category": "NarrativeText", "text": block}
