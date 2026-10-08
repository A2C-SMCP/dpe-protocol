"""响应报文（HTTP 绑定 §4、§5）：客户端解析与参考服务端构造共用这一份定义。

- 信封对未知成员**宽容**（``extra="ignore"``）：服务端在同一协议版本内增加的响应成员（如新的
  limits 键）不应让旧客户端失败；能力扩展点是 capabilities 的 ``features``。
- 信封内嵌的三层对象仍是封闭 schema（``dpe_sdk.models``），未定义字段照常拒绝。
- 请求体不在这里定义：服务端须按规范的求值顺序逐步校验（core §3.3），不能交给模型一次性解析。

``Skeleton`` 中对象的子 hash 按 validation context 的 ``contract`` 校验，同 ``dpe_sdk.models``，
如 ``Skeleton.model_validate(data, context={"contract": "dpe2"})``。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from dpe_sdk.models import DocumentObject, PageObject

__all__ = [
    "BatchHeads",
    "Capabilities",
    "CommitResult",
    "CommitStatus",
    "Delta",
    "ElementUploadMissing",
    "Head",
    "Limits",
    "ListEntry",
    "ListPage",
    "Missing",
    "MoveResult",
    "NegotiateResult",
    "PageUploadMissing",
    "Skeleton",
    "StagingSession",
]


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


#: 限额必须是正整数（不接受 bool 与可转换的字符串）：客户端按它分批、分块，非正值会让循环静默空转
_Positive = Annotated[int, Field(strict=True, ge=1)]


class Limits(_Envelope):
    """capabilities 的 ``limits``（HTTP 绑定 §4.1）：各项为正整数，会话 TTL 不少于 3600 秒。"""

    max_payload_bytes: _Positive
    page_max_bytes: _Positive
    staging_ttl_seconds: Annotated[int, Field(strict=True, ge=3600)]
    blob_max_bytes: _Positive
    blob_chunk_bytes: _Positive
    batch_head_max: _Positive
    list_page_max: _Positive


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


class StagingSession(_Envelope):
    """negotiate 开启的暂存会话（HTTP 绑定 §4.5）。``expires_at`` 为 RFC 3339 UTC。"""

    id: str
    expires_at: str


class NegotiateResult(_Envelope):
    """``POST negotiate`` 的响应（HTTP 绑定 §4.5）：逐层缺失清单与会话。"""

    missing_pages: list[str] = []
    missing_content_hashes: list[str] = []
    staging_session: StagingSession


class PageUploadMissing(_Envelope):
    """页对象上传完成后的响应体：它引用的缺失元素对象（HTTP 绑定 §4.6）。"""

    missing_content_hashes: list[str] = []


class ElementUploadMissing(_Envelope):
    """元素对象上传完成后的响应体：它引用的缺失 blob（HTTP 绑定 §4.6）。"""

    missing_blobs: list[str] = []
