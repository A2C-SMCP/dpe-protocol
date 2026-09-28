"""DPE 数据模型。"""

from dpe_protocol.schema.document import DocElement, DocPage, Document
from dpe_protocol.schema.metadata import DocMetadata, ElementMetadata
from dpe_protocol.schema.types import ElementCategory, FileType

__all__ = ["DocElement", "DocMetadata", "DocPage", "Document", "ElementCategory", "ElementMetadata", "FileType"]
