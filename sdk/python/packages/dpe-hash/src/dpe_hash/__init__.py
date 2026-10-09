"""dpe-hash：DPE hash 契约的独立 hash 核心。

零运行时依赖、纯 Python；内核与 dpe-sdk 都直接依赖本包，不另行维护 hash 实现。
实现 hash 契约 1（spec/hash-contract-1.md）与 file_uri 语法规范化（spec/core.md §1.1），
并导出契约常量、三层对象类型与带规范错误码的异常。
"""

from importlib.metadata import version

from dpe_hash.constants import (
    CATEGORY_CONTENT_FIELDS,
    CONTRACT,
    DRILL_CONTRACT,
    FILE_TYPES,
    KNOWN_CONTRACTS,
    SUPPORTED_CONTRACTS,
)
from dpe_hash.contract1 import (
    blob_ref,
    children,
    content_hash,
    doc_hash,
    document_hashes,
    object_hash,
    page_hash,
    parse_blob_ref,
    parse_hash,
)
from dpe_hash.errors import (
    CategoryUnknownError,
    ContractUnsupportedError,
    DpeHashError,
    FileTypeUnknownError,
    IntegerOutOfRangeError,
    InvalidUnicodeError,
    UndefinedFieldError,
    ValidationError,
)
from dpe_hash.jcs import has_invalid_unicode, jcs
from dpe_hash.models import (
    DocumentFields,
    DocumentHashes,
    DocumentObject,
    ElementObject,
    ExpandedDocument,
    ExpandedPage,
    ObjectKind,
    PageFields,
    PageHashes,
    PageObject,
)
from dpe_hash.uri import normalize_file_uri

__version__ = version("dpe-hash")

__all__ = [
    "CATEGORY_CONTENT_FIELDS",
    "CONTRACT",
    "DRILL_CONTRACT",
    "FILE_TYPES",
    "KNOWN_CONTRACTS",
    "SUPPORTED_CONTRACTS",
    "CategoryUnknownError",
    "ContractUnsupportedError",
    "DocumentFields",
    "DocumentHashes",
    "DocumentObject",
    "DpeHashError",
    "ElementObject",
    "ExpandedDocument",
    "ExpandedPage",
    "FileTypeUnknownError",
    "IntegerOutOfRangeError",
    "InvalidUnicodeError",
    "ObjectKind",
    "PageFields",
    "PageHashes",
    "PageObject",
    "UndefinedFieldError",
    "ValidationError",
    "__version__",
    "blob_ref",
    "children",
    "content_hash",
    "doc_hash",
    "document_hashes",
    "has_invalid_unicode",
    "jcs",
    "normalize_file_uri",
    "object_hash",
    "page_hash",
    "parse_blob_ref",
    "parse_hash",
]
