"""响应报文（HTTP 绑定 §4、§5）：客户端解析与参考服务端构造共用这一份定义。

- 信封对未知成员**宽容**（``extra="ignore"``）：服务端在同一协议版本内增加的响应成员（如新的
  limits 键）不应让旧客户端失败；能力扩展点是 capabilities 的 ``features``。
- 信封内嵌的三层对象仍是封闭 schema（``dpe_sdk.models``），未定义字段照常拒绝。
- 请求体不在这里定义：服务端须按规范的求值顺序逐步校验（core §3.3），不能交给模型一次性解析。

``Skeleton`` 中对象的子 hash 按 validation context 的 ``contract`` 校验，同 ``dpe_sdk.models``，
如 ``Skeleton.model_validate(data, context={"contract": "dpe2"})``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from dpe_sdk.models import DocumentObject, PageObject

__all__ = [
    "BatchHeads",
    "Capabilities",
    "CommitResult",
    "CommitStatus",
    "Delta",
    "Head",
    "Limits",
    "ListEntry",
    "ListPage",
    "Missing",
    "MoveResult",
    "Skeleton",
]


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Limits(_Envelope):
    """capabilities 的 ``limits``（HTTP 绑定 §4.1）。"""

    max_payload_bytes: int
    page_max_bytes: int
    staging_ttl_seconds: int
    blob_max_bytes: int
    blob_chunk_bytes: int
    batch_head_max: int
    list_page_max: int


class Capabilities(_Envelope):
    """``GET capabilities``（HTTP 绑定 §4.1）。"""

    protocol: str
    hash_contracts: list[str]
    limits: Limits
    content_encodings: list[str]
    features: list[str]


class Head(_Envelope):
    """head / batch_head 的单项：文档存在时的 doc_hash（按请求声明的契约）。"""

    doc_hash: str


class BatchHeads(_Envelope):
    """``POST heads``：与请求的 ``uris`` 一一对应，不存在的文档为 ``None``。"""

    heads: list[Head | None]


class Skeleton(_Envelope):
    """``GET documents?uri=``：文档对象与按其顺序给出的全部页对象，不含元素对象。"""

    file_uri: str
    doc_hash: str
    document: DocumentObject
    pages: list[PageObject]


class ListEntry(_Envelope):
    file_uri: str
    doc_hash: str


class ListPage(_Envelope):
    """``GET documents?prefix=``：``next_cursor`` 为 ``None`` 表示结束。"""

    documents: list[ListEntry]
    next_cursor: str | None


class Delta(_Envelope):
    """新旧文档元素 content_hash 多重集之差（core §3.3）。"""

    added: int
    removed: int
    retained: int


CommitStatus = Literal["created", "updated", "unchanged"]


class CommitResult(_Envelope):
    """commit 响应（core §3.3）。"""

    status: CommitStatus
    doc_hash: str
    delta: Delta


class MoveResult(_Envelope):
    """move 响应：目标文档的 doc_hash（HTTP 绑定 §4.9）。"""

    doc_hash: str


class Missing(_Envelope):
    """``DPE_MISSING_CONTENT`` 的缺失清单，按层给出（core §3.3、HTTP 绑定 §5）。"""

    pages: list[str] = []
    content_hashes: list[str] = []
    blobs: list[str] = []
