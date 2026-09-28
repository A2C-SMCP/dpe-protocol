"""DPE 三层结构：Document / DocPage / DocElement（源端视角）。

与内核模型的差异是刻意的：

- **不含任何 hash 字段**（``content_hash`` / ``page_hash`` / ``doc_hash`` / ``seq_in_page`` /
  ``hash_strategy_uri``）。hash 由 :mod:`dpe_protocol.hashing` 按需计算、只存在于 manifest，
  对应 dpe-push/1 §5「第 0 条校验规则」：客户端永远不以文档字段形式提交 hash。
- **不含数据库主键**（``doc_id`` / ``page_id`` / ``ele_id`` / ``entrance_*``）。
- ``DocElement`` 是单一类，``category`` 为普通字段；hash 多态由 hashing 层按 ``category``
  分派，避免内核「dict 构造丢失子类多态」一类问题。
"""

import uuid
from typing import Any, Self

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

from dpe_protocol.schema.metadata import DocMetadata, ElementMetadata
from dpe_protocol.schema.types import ElementCategory, FileType

#: 内核持久化 / 装配产生的字段。源端不得提交 hash 与主键，读取内核 dump 时剔除
KERNEL_ONLY_FIELDS: dict[str, frozenset[str]] = {
    "document": frozenset(
        {"doc_id", "entrance_page_id", "doc_hash", "hash_strategy_uri", "source_file_hash", "creator_id", "group_id"}
    ),
    "page": frozenset({"page_id", "doc_id", "entrance_ele_id", "page_hash"}),
    "element": frozenset({"ele_id", "page_id", "seq_in_page", "content_hash"}),
}


class DocElement(BaseModel):
    """最小内容单元。在页内的位置由数组顺序表达（数组顺序即阅读顺序）。"""

    model_config = ConfigDict(extra="forbid")

    category: ElementCategory = ElementCategory.UNCATEGORIZED_TEXT
    text: str = ""
    element_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    keywords: list[str] | None = None
    ele_metadata: ElementMetadata = Field(default_factory=ElementMetadata)
    payload: dict[str, Any] | None = None


class DocPage(BaseModel):
    """页。``number`` 是页身份：内核按 ``number`` 配对新旧页，数据源必须保证其稳定。"""

    model_config = ConfigDict(extra="forbid")

    number: int
    title: str | None = None
    keywords: list[str] | None = None
    elements: list[DocElement] = Field(default_factory=list)
    page_metadata: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    """文档。``file_uri`` 是 Robot Memory 中的唯一键（PG UNIQUE）。"""

    model_config = ConfigDict(extra="forbid")

    file_uri: AnyUrl
    file_type: FileType
    keywords: list[str] | None = None
    pages: list[DocPage] = Field(default_factory=list)
    doc_metadata: DocMetadata = Field(default_factory=DocMetadata)

    @model_validator(mode="after")
    def _check_unique_page_numbers(self) -> Self:
        numbers = [p.number for p in self.pages]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"page.number must be unique within a document, got {numbers}")
        return self

    @classmethod
    def from_kernel_dump(cls, data: dict[str, Any]) -> "Document":
        """从内核 ``Document`` 的 dump（或上游解析服务按内核格式的产出）构造，剔除内核独有字段。"""
        doc = {k: v for k, v in data.items() if k not in KERNEL_ONLY_FIELDS["document"]}
        doc["pages"] = [
            {
                **{k: v for k, v in page.items() if k not in KERNEL_ONLY_FIELDS["page"]},
                "elements": [
                    {k: v for k, v in ele.items() if k not in KERNEL_ONLY_FIELDS["element"]}
                    for ele in page.get("elements", [])
                ],
            }
            for page in data.get("pages", [])
        ]
        return cls.model_validate(doc)

    def iter_elements(self) -> "list[tuple[DocPage, DocElement]]":
        """按页、按阅读顺序展开全部 element。"""
        return [(page, ele) for page in self.pages for ele in page.elements]
