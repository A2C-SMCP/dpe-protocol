"""三层对象的类型（TypedDict，标准库实现）：供 mypy strict 下的调用方在编译期发现字段拼错。

运行时校验（封闭 schema、各 category 允许的字段）由 hash 函数完成，与类型无关。
TypedDict 在类型系统中是开放的：带多余键的字典（如带 ``elements`` 的页）仍可通过
``PageFields`` 的类型检查。封闭 TypedDict（PEP 728）需要第三方的 ``typing_extensions``，与本包
零依赖冲突，因此这类误用只在运行时拒绝（``page_hash`` / ``doc_hash`` 抛 ``UndefinedFieldError``）。

- 线上原像（契约 1 §4–§5）：``ElementObject``、``PageObject``（带 ``elements``）、
  ``DocumentObject``（带 ``pages``）；
- 不含子对象列表的自身字段：``PageFields``、``DocumentFields``，配合 ``page_hash`` /
  ``doc_hash`` 用已存的子 hash 上溯；
- 展开视图（vectors/README.md）：``ExpandedDocument`` / ``ExpandedPage``，配合
  ``document_hashes``；其输出为 ``DocumentHashes`` / ``PageHashes``。
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict

__all__ = [
    "DocumentFields",
    "DocumentHashes",
    "DocumentObject",
    "ElementObject",
    "ExpandedDocument",
    "ExpandedPage",
    "ObjectKind",
    "PageFields",
    "PageHashes",
    "PageObject",
]

#: ``object_hash`` / ``children`` 的对象层级
ObjectKind = Literal["element", "page", "document"]


class ElementObject(TypedDict):
    """元素对象（core.md §2.3）。内容字段是否允许取决于 ``category``（契约 1 §4.1）。"""

    category: str
    text: NotRequired[str | None]
    text_as_html: NotRequired[str | None]
    image_blob: NotRequired[str | None]
    image_mime_type: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any] | None]


class PageFields(TypedDict, total=False):
    """页对象除 ``elements`` 之外的字段。"""

    title: str | None
    page_metadata: dict[str, Any] | None


class PageObject(PageFields):
    """页对象（core.md §2.2）：``elements`` 为 content_hash 列表。"""

    elements: Required[list[str]]


class DocumentFields(TypedDict):
    """文档对象除 ``pages`` 之外的字段。"""

    file_type: str
    title: NotRequired[str | None]
    doc_metadata: NotRequired[dict[str, Any] | None]


class DocumentObject(DocumentFields):
    """文档对象（core.md §2.1）：``pages`` 为 page_hash 列表。"""

    pages: list[str]


class ExpandedPage(PageFields):
    """展开视图中的页：``elements`` 为元素对象本身。"""

    elements: Required[list[ElementObject]]


class ExpandedDocument(DocumentFields):
    """展开视图中的文档：``pages`` 为展开的页。"""

    pages: list[ExpandedPage]


class PageHashes(TypedDict):
    page_hash: str
    elements: list[str]


class DocumentHashes(TypedDict):
    """``document_hashes`` 的结果，形状同向量的 ``expected``。"""

    doc_hash: str
    pages: list[PageHashes]
