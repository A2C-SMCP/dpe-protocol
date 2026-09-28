"""dpe-push/1 报文模型（TFRobotServer ``docs/protocol/dpe/push-protocol-v1.md``）。

- 请求模型（Manifest / CommitRequest）由本端构造，``extra="forbid"``。
- 响应模型对未知字段 must-ignore（``extra="ignore"``），保证服务端加可选字段时不破坏客户端。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dpe_protocol.hashing import DocumentHashes
from dpe_protocol.schema import DocElement, Document

PROTOCOL_VERSION = "1"
MEDIA_TYPE = "application/vnd.tfrs.dpe.v1+json"
WELL_KNOWN_PATH = "/.well-known/dpe-push"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- Phase 0


class AcceptedHashStrategy(_Response):
    uri: str
    status: str = "stable"
    deprecated_at: str | None = None


class Endpoints(_Response):
    negotiate: str = "/v1/memory/dpe:negotiate"
    commit: str = "/v1/memory/dpe:commit"


class Capabilities(_Response):
    protocol_versions: list[str]
    accepted_hash_strategies: list[AcceptedHashStrategy]
    default_hash_strategy_uri: str | None = None
    max_payload_bytes: int | None = None
    content_encodings: list[str] = Field(default_factory=lambda: ["identity"])
    delta_supported: bool = True
    endpoints: Endpoints = Field(default_factory=Endpoints)


# ---------------------------------------------------------------- Phase 1


class ManifestElement(_Request):
    content_hash: str


class ManifestPage(_Request):
    number: int
    title: str | None = None
    page_hash: str
    elements: list[ManifestElement]


class Manifest(_Request):
    """完整文档的 hash 骨架。永远覆盖整个文档（禁止局部 manifest）。"""

    hash_strategy_uri: str
    file_uri: str
    file_type: str
    #: 全量声明字段（含 null）。服务端据此重建 ``TFDocMetadata``；若省略 ``created_at``，
    #: 内核会用 ``datetime.now()`` 填充，导致服务端 ``doc_hash`` 每次都不同。
    doc_metadata: dict[str, Any]
    doc_hash: str
    base_doc_hash: str | None = None
    pages: list[ManifestPage]

    @classmethod
    def build(cls, doc: Document, hashes: DocumentHashes, *, base_doc_hash: str | None = None) -> "Manifest":
        return cls(
            hash_strategy_uri=hashes.strategy.to_uri(),
            file_uri=str(doc.file_uri),
            file_type=doc.file_type.value,
            doc_metadata=doc.doc_metadata.model_dump(mode="json"),
            doc_hash=hashes.doc_hash,
            base_doc_hash=base_doc_hash,
            pages=[
                ManifestPage(
                    number=p.number,
                    title=p.title,
                    page_hash=p.page_hash,
                    elements=[ManifestElement(content_hash=h) for h in p.content_hashes],
                )
                for p in hashes.pages
            ],
        )


NegotiateStatus = Literal["unchanged", "gap", "new"]


class NegotiateResponse(_Response):
    status: NegotiateStatus
    missing_content_hashes: list[str] = Field(default_factory=list)
    server_doc_exists: bool = False
    server_doc_hash: str | None = None
    base_doc_hash_matched: bool | None = None


# ---------------------------------------------------------------- Phase 2


class ContentItem(_Request):
    content_hash: str
    #: 完整 element payload，不含任何 hash / seq 字段
    element: dict[str, Any]

    @classmethod
    def build(cls, content_hash: str, ele: DocElement) -> "ContentItem":
        return cls(content_hash=content_hash, element=ele.model_dump(mode="json", exclude_none=True))


class CommitRequest(_Request):
    manifest: Manifest
    contents: list[ContentItem]


CommitStatus = Literal["created", "unchanged", "updated", "rebuilt", "applied"]


class CommitCounts(_Response):
    elements_added: int = 0
    elements_updated: int = 0
    elements_removed: int = 0
    elements_synced: int = 0
    elements_unchanged: int = 0
    llm_calls_saved: int = 0


class CommitResponse(_Response):
    status: CommitStatus
    #: 服务端权威 doc_hash，下次投递作为 ``base_doc_hash``
    doc_hash: str
    hash_strategy_uri: str
    counts: CommitCounts = Field(default_factory=CommitCounts)


# ---------------------------------------------------------------- Errors


class ErrorEnvelope(_Response):
    """TFRS 既有错误信封 ``{code, message, data}``。"""

    code: str | int
    message: str = ""
    data: Any = None
