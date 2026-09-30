"""dpe-hash：DPE hash 契约 1 的独立 hash 核心（M1 原型）。

预演 #3 S1 的形态：零运行时依赖、纯 Python、导出契约常量，
内核以后直接 ``from dpe_hash import …``，不自行维护副本。
"""

from dpe_hash.contract1 import (
    CONTRACT,
    HTML_CATEGORIES,
    RESERVED_METADATA_KEYS,
    TEXT_ONLY_CATEGORIES,
    content_hash,
    document_hashes,
    page_hash,
)
from dpe_hash.jcs import jcs

__all__ = [
    "CONTRACT",
    "TEXT_ONLY_CATEGORIES",
    "HTML_CATEGORIES",
    "RESERVED_METADATA_KEYS",
    "content_hash",
    "page_hash",
    "document_hashes",
    "jcs",
]
