"""契约常量：消费方（内核、服务端、dpe-sdk）直接 import，不自行维护副本。

测试断言它们与 ``vectors/manifest.json`` 一致（``file_types`` / ``category_content_fields``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "CATEGORY_CONTENT_FIELDS",
    "CONTRACT",
    "DRILL_CONTRACT",
    "KNOWN_CONTRACTS",
    "MAX_NESTING_DEPTH",
    "RECOMMENDED_FILE_TYPES",
    "SUPPORTED_CONTRACTS",
]

#: 当前契约版本（hash 值前缀），各 hash 函数的默认契约。
CONTRACT = "dpe1"

#: 本包支持的真实契约，供服务端 capabilities 的 ``hash_contracts`` 使用。
SUPPORTED_CONTRACTS: tuple[str, ...] = ("dpe1",)

#: 假想契约 ``dpe2``，**仅用于契约升级演练**（vectors/README.md）：摘要输入为 ASCII ``dpe2``
#: 后接 JCS 原像字节。不在 ``SUPPORTED_CONTRACTS`` 中：只有调用方显式传 ``contract="dpe2"``
#: 时 hash 函数才接受它，``parse_hash`` 默认拒绝。
DRILL_CONTRACT = "dpe2"

#: 本包认识的全部契约：真实契约加演练契约。消费方判断「契约能否由 dpe_hash 计算」时用它，
#: 不自行拼集合；它不是服务端应声明的契约（那是 ``SUPPORTED_CONTRACTS``）。
KNOWN_CONTRACTS: frozenset[str] = frozenset({*SUPPORTED_CONTRACTS, DRILL_CONTRACT})

#: 对象的嵌套深度上界（core.md §2.8 第 0 步第三项）：每个被校验的对象自身 ≤ 此值，
#: 计数口径见 core.md §2.8（页的 ``elements`` 与文档的 ``pages`` 按子 hash 列表计、不向内展开）。
MAX_NESTING_DEPTH = 64

#: category 封闭枚举 → 允许的内容字段（契约 1 §4.1，字段按表中顺序）。
#: 元素对象另有必有的 ``category`` 与可选的 ``metadata``，不在此列。
CATEGORY_CONTENT_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        **{
            c: ("text",)
            for c in (
                "UncategorizedText",
                "CheckBox",
                "CompositeElement",
                "FigureCaption",
                "NarrativeText",
                "ListItem",
                "Title",
                "Address",
                "EmailAddress",
                "PageBreak",
                "TableChunk",
                "Header",
                "Footer",
                "CodeSnippet",
                "PageNumber",
                "FormKeysValues",
                "tfchat",
            )
        },
        "Table": ("text", "text_as_html"),
        "Formula": ("text", "text_as_html"),
        "Image": ("text", "blob", "mime_type"),
    }
)

#: file_type 推荐登记表（core.md §2.5，按规范表中顺序）。取值只是推荐：语法合法的未登记取值
#: 同样被接受（``is_valid_file_type``），接收方 MUST NOT 因未登记而拒收。
RECOMMENDED_FILE_TYPES: tuple[str, ...] = (
    "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json", "md", "msg",
    "ndjson", "odt", "org", "pdf", "png", "ppt", "pptx", "rst", "rtf", "tiff", "tsv", "txt",
    "wav", "xls", "xlsx", "xml", "zip", "git_repo", "java_repo", "python_repo", "javascript_repo",
    "typescript_repo", "unk", "empty", "jira_project", "jira_issue",
)  # fmt: skip
