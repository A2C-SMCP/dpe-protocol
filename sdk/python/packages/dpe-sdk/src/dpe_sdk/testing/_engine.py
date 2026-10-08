"""参考服务端引擎：core 语义的内存实现，不含传输（HTTP 绑定由 #44 映射到本引擎）。

**求值顺序全部在引擎里**：带 JSON 请求体的操作（batch_head、negotiate、commit、move）接收原始
请求体字节，按规范的顺序依次做传输层上限、I-JSON、契约声明、请求信封、对象校验，再授权，再做
依赖文档状态的判定（core §3.3、§3.4、§5.2，HTTP 绑定 §3.1）；上传操作按 HTTP 绑定 §4.6、§4.7
的两套阶梯（非分块「只看请求本身」优先、分块会话先于分块参数）把校验好的字节交给暂存会话。
传输层只负责把请求映射为调用、把结果与 ``DpeError`` 映射为响应。全部状态（对象存储、文档表与
暂存会话）在同一把锁内。

调用者身份 ``caller`` 是不透明字符串，由传输层认证后给出；授权由可插拔的 ``Authorizer`` 判定。
"""

from __future__ import annotations

import base64
import binascii
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import dpe_hash
from dpe_hash import DpeHashError, has_invalid_unicode

# RFC 6901、JCS 与 UTF-16 码元序工具复用 dpe_hash 的同一份实现（dpe-sdk 精确依赖同版本 dpe-hash）
from dpe_hash.jcs import jcs, pointer, utf16_key

from dpe_sdk import _ijson
from dpe_sdk.errors import (
    AlreadyExistsError,
    ContractUnsupportedError,
    ForbiddenError,
    HashMismatchError,
    MissingContentError,
    NotFoundError,
    PayloadTooLargeError,
    PreconditionFailedError,
    PreconditionRequiredError,
    ValidationError,
    from_hash_error,
)
from dpe_sdk.testing._staging import (
    PartialUpload,
    Session,
    SessionStore,
    StagedElement,
    StagedPage,
    UploadChunk,
    UploadOffsetError,
    UploadOutcome,
    UploadResult,
    UploadSnapshot,
    default_clock,
    default_session_id,
    rfc3339_utc,
)
from dpe_sdk.testing._store import DocState, ObjectRecord, Store
from dpe_sdk.wire import (
    BatchHeads,
    Capabilities,
    CommitResult,
    Delta,
    ElementUploadMissing,
    Head,
    Limits,
    ListEntry,
    ListPage,
    Missing,
    MoveResult,
    NegotiateResult,
    PageUploadMissing,
    Skeleton,
    StagingSession,
)

__all__ = [
    "AllowAll",
    "Authorizer",
    "BaseHash",
    "DedupScope",
    "Engine",
    "EngineConfig",
    "IfAbsent",
    "Precondition",
]

PROTOCOL = "dpe/1"


class Authorizer(Protocol):
    """写授权判定（core §5 总则）。读接口不做授权。"""

    def can_write(self, caller: str, uri: str) -> bool: ...

    def can_force(self, caller: str, uri: str) -> bool: ...


class AllowAll:
    """默认授权器：任何调用者可写任何 URI，且有 force 权限。"""

    def can_write(self, caller: str, uri: str) -> bool:
        return True

    def can_force(self, caller: str, uri: str) -> bool:
        return True


class DedupScope(Enum):
    """去重范围（core §3.3）：缺失清单与 commit 可得性判定使用同一范围。"""

    #: 下界：只含该文档当前状态引用的对象
    DOCUMENT = "document"
    #: 上界：调用者有写授权的全部文档引用的对象
    WRITABLE = "writable"


@dataclass(frozen=True)
class BaseHash:
    """前置条件 ``base_hash``（HTTP：``If-Match``）。按值比较，不做格式校验（core §5.2，#63）。"""

    value: str


@dataclass(frozen=True)
class IfAbsent:
    """前置条件 ``if_absent``（HTTP：``If-None-Match: *``）。"""


Precondition = BaseHash | IfAbsent


@dataclass(frozen=True)
class EngineConfig:
    """限额默认值同 HTTP 绑定 §4.1 的示例；时钟与会话 id 生成器可注入（会话过期与确定性测试）。"""

    #: 受支持的 hash 契约，第一个为主契约（对象按它存储）
    contracts: tuple[str, ...] = (dpe_hash.CONTRACT,)
    max_payload_bytes: int = 8 * 1024 * 1024
    page_max_bytes: int = 256 * 1024 * 1024
    staging_ttl_seconds: int = 86400
    blob_max_bytes: int = 100 * 1024 * 1024
    blob_chunk_bytes: int = 8 * 1024 * 1024
    batch_head_max: int = 500
    list_page_max: int = 1000
    content_encodings: tuple[str, ...] = ("gzip",)
    #: 缺失清单最多列出的 hash 数，超出时截断并注明
    missing_max: int = 1000
    #: 可注入时钟（会话语义用它判定过期与续期）；必须返回带时区的 datetime
    clock: Callable[[], datetime] = default_clock
    #: 可注入的会话 id 生成器；须避免与在用会话重复
    session_id_factory: Callable[[], str] = default_session_id
    dedup_scope: DedupScope = DedupScope.DOCUMENT
    authorizer: Authorizer = field(default_factory=AllowAll)

    def __post_init__(self) -> None:
        if not self.contracts:
            raise ValueError("contracts 至少要有一个契约")
        unknown = [c for c in self.contracts if c not in dpe_hash.KNOWN_CONTRACTS]
        if unknown:
            raise ValueError(f"dpe_hash 不支持的契约：{unknown}")
        if len(set(self.contracts)) != len(self.contracts):
            raise ValueError(f"contracts 不得重复：{self.contracts}")
        positive = (
            "max_payload_bytes",
            "page_max_bytes",
            "blob_max_bytes",
            "blob_chunk_bytes",
            "batch_head_max",
            "list_page_max",
            "missing_max",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数，实际为 {value!r}")
        ttl = self.staging_ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 3600:
            raise ValueError(
                f"staging_ttl_seconds 必须是不少于 3600 的整数（core §3.4），实际为 {ttl!r}"
            )
        if not callable(self.clock) or not callable(self.session_id_factory):
            raise ValueError("clock 与 session_id_factory 必须可调用")


_COMMIT_MEMBERS = frozenset({"document", "pages", "objects", "staging_session", "force"})
_NEGOTIATE_MEMBERS = frozenset({"file_uri", "document", "pages"})
_MOVE_MEMBERS = frozenset({"from_uri", "to_uri", "base_hash"})
_HEADS_MEMBERS = frozenset({"uris"})
_CURSOR_PREFIX = "c1."

_T = TypeVar("_T")


def _normalize_uri(uri: str) -> str:
    """file_uri 比较前的语法规范化（core §1）。

    接缝：#39 合入后改为 ``dpe_hash.normalize_file_uri``；在此之前按原样比较。
    """
    return uri


def _require_absolute_uri(uri: str) -> None:
    """file_uri 必须是绝对 URI（core §1）：带 scheme（RFC 3986）。"""
    try:
        scheme = urlsplit(uri).scheme
    except ValueError:
        scheme = ""
    if not scheme:
        raise ValidationError(f"file_uri 必须是绝对 URI：{uri!r}", pointer("file_uri"))


def _validated(compute: Callable[[], _T], at: str = "") -> _T:
    """调用 dpe_hash 校验对象；错误转换为协议错误，位置前缀为该对象在请求体中的位置（缺省为根）。"""
    try:
        return compute()
    except DpeHashError as exc:
        raise from_hash_error(exc, at) from None


def _parse_body(body: bytes, limit: int) -> Any:
    """传输层上限（第 0 步）与 I-JSON（第 1 步的第 1 子步）。"""
    if len(body) > limit:
        raise PayloadTooLargeError(f"请求体 {len(body)} 字节，超过 max_payload_bytes={limit}")
    return _parse_json(body)


def _parse_json(body: bytes) -> Any:
    """I-JSON 解析（core §2.8 第 0 步）；分块到齐后的字节同样经这里解析。"""
    try:
        data = _ijson.loads(body)
    except DpeHashError as exc:
        raise from_hash_error(exc) from None
    if has_invalid_unicode(data):
        raise ValidationError("字符串或对象键含孤立代理项，报文不是 I-JSON")
    return data


def _envelope(data: Any, allowed: frozenset[str]) -> dict[str, Any]:
    """请求信封：JSON 对象、顶层成员封闭；null 视同缺省（HTTP 绑定 §3.1）。"""
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    # 违例位置的口径同 core §2.8 第 3 步：取 UTF-16 码元序最小的未定义成员
    extra = sorted((k for k in data if k not in allowed), key=utf16_key)
    if extra:
        raise ValidationError(f"请求体不允许成员 {extra[0]!r}", pointer(extra[0]))
    return {k: v for k, v in data.items() if v is not None}


def _require(env: Mapping[str, Any], member: str, kind: type, what: str) -> Any:
    if member not in env:
        raise ValidationError(f"请求体缺少 {member}")
    return _optional(env, member, kind, what)


def _optional(env: Mapping[str, Any], member: str, kind: type, what: str) -> Any:
    value = env.get(member)
    if value is not None and not isinstance(value, kind):
        raise ValidationError(f"{member} 必须是{what}", pointer(member))
    return value


@dataclass
class _Commit:
    """通过报文校验的 commit 请求。"""

    document: dict[str, Any]
    doc_hash: str
    #: 内联对象按请求声明的契约下的 hash 索引（校验时算出，后续复用，不重算）
    pages: dict[str, dict[str, Any]]
    objects: dict[str, dict[str, Any]]
    staging_session: str | None
    force: bool


class _Gap(Exception):
    """可得性检查发现缺失（引擎内部）：完整的缺失清单与每个缺失对象的持有文档。"""

    def __init__(self, missing: dict[str, list[str]], holders: list[frozenset[str]]) -> None:
        super().__init__("missing content")
        self.missing = missing
        self.holders = holders

    def error(self, limit: int) -> MissingContentError:
        return _missing_error(self.missing, limit)


@dataclass(frozen=True)
class _Manifest:
    """negotiate / 上传响应的缺失清单与每个缺失对象的持有文档（去重后，按出现顺序）。

    持有文档用于去重范围上界的两阶段授权（core §3.3、§8）：只对实际缺失的对象询问授权器。
    """

    pages: list[str]
    content_hashes: list[str]
    blobs: list[str]
    holders: list[frozenset[str]]

    @property
    def empty(self) -> bool:
        return not (self.pages or self.content_hashes or self.blobs)


class Engine:
    """内存版 DPE 服务端引擎。线程安全：写操作与读操作在同一把锁内完成。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._store = Store(self.config.contracts)
        self._sessions = SessionStore(
            clock=self.config.clock,
            id_factory=self.config.session_id_factory,
            ttl_seconds=self.config.staging_ttl_seconds,
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公共检查
    # ------------------------------------------------------------------

    def _contract(self, contract: str | None) -> str:
        if contract is None or contract not in self.config.contracts:
            raise ContractUnsupportedError(f"不支持的 hash 契约：{contract!r}")
        return contract

    def _forbid_unless(self, allowed: bool, uri: str) -> None:
        if not allowed:
            raise ForbiddenError(f"无权写入 {uri}")

    # ------------------------------------------------------------------
    # 只读操作
    # ------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        c = self.config
        return Capabilities(
            protocol=PROTOCOL,
            hash_contracts=list(c.contracts),
            limits=Limits(
                max_payload_bytes=c.max_payload_bytes,
                page_max_bytes=c.page_max_bytes,
                staging_ttl_seconds=c.staging_ttl_seconds,
                blob_max_bytes=c.blob_max_bytes,
                blob_chunk_bytes=c.blob_chunk_bytes,
                batch_head_max=c.batch_head_max,
                list_page_max=c.list_page_max,
            ),
            content_encodings=list(c.content_encodings),
            features=["move"],
        )

    def head(self, caller: str, uri: str, contract: str | None) -> Head | None:
        contract = self._contract(contract)
        with self._lock:
            state = self._store.docs.get(_normalize_uri(uri))
            return None if state is None else Head(doc_hash=state.doc_hashes[contract])

    def batch_head(self, caller: str, body: bytes, contract: str | None) -> BatchHeads:
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        env = _envelope(data, _HEADS_MEMBERS)
        uris = _require(env, "uris", list, "字符串数组")
        for i, uri in enumerate(uris):
            if not isinstance(uri, str):
                raise ValidationError("uris 的每一项必须是字符串", pointer("uris", i))
        if len(uris) > self.config.batch_head_max:
            raise ValidationError(f"uris 超过 batch_head_max={self.config.batch_head_max}")
        with self._lock:
            docs = self._store.docs
            states = [docs.get(_normalize_uri(u)) for u in uris]
            return BatchHeads(
                heads=[None if s is None else Head(doc_hash=s.doc_hashes[contract]) for s in states]
            )

    def get_skeleton(self, caller: str, uri: str, contract: str | None) -> Skeleton | None:
        contract = self._contract(contract)
        uri = _normalize_uri(uri)
        with self._lock:
            state = self._store.docs.get(uri)
            if state is None:
                return None
            store = self._store
            data = {
                "file_uri": uri,
                "doc_hash": state.doc_hashes[contract],
                "document": store.in_contract(state.document, "document", contract),
                "pages": [store.in_contract(b, "page", contract) for b in state.page_bodies],
            }
        return Skeleton.model_validate(data, context={"contract": contract})

    def list_documents(
        self,
        caller: str,
        contract: str | None,
        prefix: str = "",
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ListPage:
        """码点前缀分页（core §3、HTTP 绑定 §4.4）。不识别 path 段边界。

        前缀按原样与规范化后的 file_uri 匹配：前缀不是完整的 URI，不经 ``_normalize_uri``。
        """
        contract = self._contract(contract)
        max_limit = self.config.list_page_max
        if limit is None:
            limit = max_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
            raise ValidationError(f"limit 必须是 1 到 {max_limit} 之间的整数")
        after = None if cursor is None else _decode_cursor(cursor)
        with self._lock:
            store = self._store
            # 多取一个判断是否还有下一页
            uris = store.page_after(prefix, after, limit + 1)
            page = [(u, store.docs[u].doc_hashes[contract]) for u in uris[:limit]]
        more = len(uris) > limit
        return ListPage(
            documents=[ListEntry(file_uri=u, doc_hash=h) for u, h in page],
            next_cursor=_encode_cursor(page[-1][0]) if more else None,
        )

    # ------------------------------------------------------------------
    # 暂存会话（core §3.4，HTTP 绑定 §4.5–§4.7）
    # ------------------------------------------------------------------

    def negotiate(self, caller: str, body: bytes, contract: str | None) -> NegotiateResult:
        """``POST negotiate``：开会话、存附带页、算逐层缺失清单（HTTP 绑定 §4.5）。

        授权（锁外调用授权器）排在只看请求本身的报文校验之后、开会话与一切依赖文档状态的
        判定之前（core §3.4、§5 总则）；未通过者 MUST NOT 获得会话与缺失清单。
        """
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        env = _envelope(data, _NEGOTIATE_MEMBERS)
        file_uri = _require(env, "file_uri", str, "字符串")
        _require_absolute_uri(file_uri)
        uri = _normalize_uri(file_uri)
        document = _require(env, "document", dict, "文档对象")
        pages = _optional(env, "pages", list, "页对象数组") or []
        _validated(
            partial(dpe_hash.object_hash, document, "document", contract), pointer("document")
        )
        attached: dict[str, dict[str, Any]] = {}
        for i, page in enumerate(pages):
            h = _validated(
                partial(dpe_hash.object_hash, page, "page", contract), pointer("pages", i)
            )
            attached[h] = page  # 同 hash 的等价页取后出现者（core §2.7 允许任一表示）
        self._forbid_unless(self.config.authorizer.can_write(caller, uri), uri)
        with self._lock:
            session = self._sessions.open(uri, caller)
            for h, page in attached.items():
                session.pages[h] = StagedPage(dict(page), contract, len(jcs(page).encode("utf-8")))
            manifest = self._negotiate_missing(session, uri, contract, document, frozenset())
        if not manifest.empty and self.config.dedup_scope is DedupScope.WRITABLE:
            writable = self._authorize_holders(caller, manifest.holders)
            if writable:
                with self._lock:
                    manifest = self._negotiate_missing(session, uri, contract, document, writable)
        return NegotiateResult(
            missing_pages=manifest.pages,
            missing_content_hashes=manifest.content_hashes,
            staging_session=StagingSession(
                id=session.id, expires_at=rfc3339_utc(session.expires_at)
            ),
        )

    def upload_page(
        self,
        caller: str,
        session_id: str,
        page_hash: str,
        body: bytes,
        contract: str | None,
        *,
        chunk: UploadChunk | None = None,
    ) -> UploadResult:
        """``PUT staging/{sid}/pages/{page_hash}``（HTTP 绑定 §4.6、§4.7）。

        非分块走「只看请求本身」优先的阶梯（§4.6）；分块请求的字节到齐前不可校验，会话与
        「已完成」判定先于分块参数与请求体（§4.7，core §3.4）。
        """
        if chunk is not None:
            return self._upload_page_chunked(caller, session_id, page_hash, body, contract, chunk)
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        if len(body) > self.config.page_max_bytes:
            raise PayloadTooLargeError(
                f"页对象 {len(body)} 字节，超过 page_max_bytes={self.config.page_max_bytes}"
            )
        h = _validated(partial(dpe_hash.object_hash, data, "page", contract))
        if h != page_hash:
            raise HashMismatchError(f"上传对象的 hash {h} 与路径 {page_hash} 不符")
        with self._lock:
            session = self._sessions.get(caller, session_id)
            outcome: UploadOutcome
            if page_hash in session.pages:
                outcome = "duplicate"
            else:
                session.pages[page_hash] = StagedPage(dict(data), contract, len(body))
                session.partials.pop(page_hash, None)  # 整段上传作废该对象的分块部分进度
                outcome = "created"
            self._sessions.renew(session)
            elements = session.pages[page_hash].body["elements"]
            uri = session.uri
        missing = self._manifest(
            caller,
            lambda writable: self._content_missing(session, uri, contract, elements, writable),
        )
        return UploadResult(
            outcome=outcome,
            expires_at=rfc3339_utc(session.expires_at),
            missing=PageUploadMissing(missing_content_hashes=missing),
        )

    def _upload_page_chunked(
        self,
        caller: str,
        session_id: str,
        page_hash: str,
        body: bytes,
        contract: str | None,
        chunk: UploadChunk,
    ) -> UploadResult:
        if len(body) > self.config.max_payload_bytes:
            raise PayloadTooLargeError(
                f"分块 {len(body)} 字节，超过 max_payload_bytes={self.config.max_payload_bytes}"
            )
        contract = self._contract(contract)
        with self._lock:
            session = self._sessions.get(caller, session_id)
            if page_hash in session.pages:
                # 已完成：不校验 Content-Range 与请求体（幂等；重发最后一块同样命中）
                outcome: UploadOutcome = "duplicate"
            else:
                received, complete = self._advance_chunk(
                    session, page_hash, body, chunk, self.config.page_max_bytes
                )
                if complete is None:
                    self._sessions.renew(session)
                    return UploadResult(
                        outcome="partial",
                        expires_at=rfc3339_utc(session.expires_at),
                        offset=received,
                    )
                # 到齐：解析、按 §2.8 校验、重算 hash 与路径比较；失败已丢弃已收内容
                data = _parse_json(complete)
                h = _validated(partial(dpe_hash.object_hash, data, "page", contract))
                if h != page_hash:
                    raise HashMismatchError(f"上传对象的 hash {h} 与路径 {page_hash} 不符")
                session.pages[page_hash] = StagedPage(dict(data), contract, len(complete))
                outcome = "created"
            self._sessions.renew(session)
            elements = session.pages[page_hash].body["elements"]
            uri = session.uri
        missing = self._manifest(
            caller,
            lambda writable: self._content_missing(session, uri, contract, elements, writable),
        )
        return UploadResult(
            outcome=outcome,
            expires_at=rfc3339_utc(session.expires_at),
            missing=PageUploadMissing(missing_content_hashes=missing),
        )

    def upload_element(
        self, caller: str, session_id: str, content_hash: str, body: bytes, contract: str | None
    ) -> UploadResult:
        """``PUT staging/{sid}/objects/{content_hash}``（HTTP 绑定 §4.6）。元素对象不分块。"""
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        h = _validated(partial(dpe_hash.object_hash, data, "element", contract))
        if h != content_hash:
            raise HashMismatchError(f"上传对象的 hash {h} 与路径 {content_hash} 不符")
        with self._lock:
            session = self._sessions.get(caller, session_id)
            outcome: UploadOutcome
            if content_hash in session.elements:
                outcome = "duplicate"
            else:
                session.elements[content_hash] = StagedElement(
                    body=dict(data),
                    hashes=self._store.element_hashes(data, {contract: h}),
                )
                outcome = "created"
            self._sessions.renew(session)
            element_body = session.elements[content_hash].body
            uri = session.uri
        blob = element_body.get("blob")
        missing: list[str] = []
        if blob is not None:
            missing = self._manifest(
                caller, lambda writable: self._blobs_missing(session, uri, blob, writable)
            )
        return UploadResult(
            outcome=outcome,
            expires_at=rfc3339_utc(session.expires_at),
            missing=ElementUploadMissing(missing_blobs=missing),
        )

    def upload_blob(
        self,
        caller: str,
        session_id: str,
        blob: str,
        data: bytes,
        contract: str | None,
        *,
        chunk: UploadChunk | None = None,
    ) -> UploadResult:
        """``PUT staging/{sid}/blobs/{sha256}``（HTTP 绑定 §4.7）：整体与分块两条路径。

        整体上传先校验原始字节的 sha256（与整段页 / 元素同一口径：不看会话）；分块则
        会话与「已完成」先于分块参数与请求体。
        """
        if chunk is not None:
            return self._upload_blob_chunked(caller, session_id, blob, data, contract, chunk)
        if len(data) > self.config.max_payload_bytes:
            raise PayloadTooLargeError(
                f"blob {len(data)} 字节，超过 max_payload_bytes={self.config.max_payload_bytes}"
            )
        if len(data) > self.config.blob_max_bytes:
            raise PayloadTooLargeError(
                f"blob {len(data)} 字节，超过 blob_max_bytes={self.config.blob_max_bytes}"
            )
        self._contract(contract)
        _validated(partial(dpe_hash.parse_blob_ref, blob))
        if dpe_hash.blob_ref(data) != blob:
            raise HashMismatchError(f"上传字节的 sha256 与路径 {blob} 不符")
        with self._lock:
            session = self._sessions.get(caller, session_id)
            outcome: UploadOutcome
            if blob in session.blobs:
                outcome = "duplicate"
            else:
                session.blobs[blob] = bytes(data)
                session.partials.pop(blob, None)  # 整段上传作废该对象的分块部分进度
                outcome = "created"
            self._sessions.renew(session)
        return UploadResult(outcome=outcome, expires_at=rfc3339_utc(session.expires_at))

    def _upload_blob_chunked(
        self,
        caller: str,
        session_id: str,
        blob: str,
        body: bytes,
        contract: str | None,
        chunk: UploadChunk,
    ) -> UploadResult:
        if len(body) > self.config.max_payload_bytes:
            raise PayloadTooLargeError(
                f"分块 {len(body)} 字节，超过 max_payload_bytes={self.config.max_payload_bytes}"
            )
        self._contract(contract)
        with self._lock:
            session = self._sessions.get(caller, session_id)
            # 路径 ref 的格式校验排在会话判定之后（§4.7 分块阶梯：契约头 → 会话 410）
            _validated(partial(dpe_hash.parse_blob_ref, blob))
            if blob in session.blobs:
                # 已完成：不校验 Content-Range 与请求体（幂等；重发最后一块同样命中）
                self._sessions.renew(session)
                return UploadResult("duplicate", rfc3339_utc(session.expires_at))
            received, complete = self._advance_chunk(
                session, blob, body, chunk, self.config.blob_max_bytes
            )
            if complete is None:
                self._sessions.renew(session)
                return UploadResult(
                    outcome="partial",
                    expires_at=rfc3339_utc(session.expires_at),
                    offset=received,
                )
            if dpe_hash.blob_ref(complete) != blob:
                raise HashMismatchError(f"上传字节的 sha256 与路径 {blob} 不符")
            session.blobs[blob] = complete
            self._sessions.renew(session)
        return UploadResult("created", rfc3339_utc(session.expires_at))

    def upload_offset(self, caller: str, session_id: str, target: str) -> UploadSnapshot:
        """断点查询（HTTP 绑定 §4.7）：只读、**不续期**；会话不可用一律 410。"""
        with self._lock:
            session = self._sessions.get(caller, session_id)
            return UploadSnapshot(
                offset=_staged_size(session, target),
                expires_at=rfc3339_utc(session.expires_at),
            )

    def _advance_chunk(
        self, session: Session, target: str, body: bytes, chunk: UploadChunk, total_limit: int
    ) -> tuple[int, bytes | None]:
        """分块的一致性与追加（HTTP 绑定 §4.7 第 4–7 步）。须在锁内调用。

        返回 (已收字节数, 完整字节或 None)；到齐时把内容移出中间态（调用方的到齐校验失败
        即丢弃该对象已收内容，重传从零）。违例：格式 / 块长 / total 不一致 →
        ``DPE_VALIDATION``；尺寸 → ``DPE_PAYLOAD_TOO_LARGE``；偏移不连续 →
        ``UploadOffsetError``（带本会话已收偏移，供客户端重新同步）。
        """
        if (
            chunk.from_byte < 0
            or chunk.to_byte < chunk.from_byte
            or chunk.to_byte >= chunk.total
            or chunk.to_byte - chunk.from_byte + 1 != len(body)
        ):
            raise ValidationError(
                f"Content-Range {chunk.from_byte}-{chunk.to_byte}/{chunk.total} 非法或与块长不符"
            )
        partial = session.partials.get(target)
        if partial is not None and partial.total != chunk.total:
            raise ValidationError(
                f"声明的总量 {chunk.total} 与本会话先前声明的 {partial.total} 不一致"
            )
        if len(body) > self.config.blob_chunk_bytes:
            raise PayloadTooLargeError(
                f"分块 {len(body)} 字节，超过 blob_chunk_bytes={self.config.blob_chunk_bytes}"
            )
        if chunk.total > total_limit:
            raise PayloadTooLargeError(f"声明的总量 {chunk.total} 字节，超过上限 {total_limit}")
        received = 0 if partial is None else len(partial.data)
        if chunk.from_byte != received:
            raise UploadOffsetError(
                f"分块起点 {chunk.from_byte} 与本会话已收字节数 {received} 不符", offset=received
            )
        if partial is None:
            partial = PartialUpload(bytearray(), chunk.total)
            session.partials[target] = partial
        partial.data.extend(body)
        if len(partial.data) < chunk.total:
            return len(partial.data), None
        complete = bytes(partial.data)
        del session.partials[target]
        return chunk.total, complete

    def _object_holders(self, contract: str, value: str) -> frozenset[str]:
        """某契约下的页 / 元素 hash 的持有文档；不在对象表返回空集。须在锁内调用。"""
        primary = self._store.to_primary(contract, value)
        return frozenset() if primary is None else frozenset(self._store.referrers_of(primary))

    def _in_scope(self, uri: str, holders: frozenset[str], writable: frozenset[str]) -> bool:
        """去重范围判定（core §3.3、§8）：本文档或调用者可写的文档持有该对象。"""
        return uri in holders or not holders.isdisjoint(writable)

    def _negotiate_missing(
        self,
        session: Session,
        uri: str,
        contract: str,
        document: Mapping[str, Any],
        writable: frozenset[str],
    ) -> _Manifest:
        """negotiate 的缺失清单（HTTP 绑定 §4.5）：页面层与附带页的元素层。须在锁内调用。"""
        pages: list[str] = []
        content: list[str] = []
        holders: list[frozenset[str]] = []
        seen_pages: set[str] = set()
        for ph in document["pages"]:
            if ph in seen_pages:
                continue
            seen_pages.add(ph)
            if ph in session.pages:  # 已附带（存入会话）
                continue
            hs = self._object_holders(contract, ph)
            if self._in_scope(uri, hs, writable):
                continue
            pages.append(ph)
            holders.append(hs - {uri})
        seen_content: set[str] = set()
        for staged in session.pages.values():  # 附带页引用的元素（negotiate 时会话中只有页）
            for eh in staged.body["elements"]:
                if eh in seen_content:
                    continue
                seen_content.add(eh)
                hs = self._object_holders(staged.contract, eh)
                if self._in_scope(uri, hs, writable):
                    continue
                content.append(eh)
                holders.append(hs - {uri})
        return _Manifest(pages, content, [], holders)

    def _content_missing(
        self,
        session: Session,
        uri: str,
        contract: str,
        elements: Sequence[str],
        writable: frozenset[str],
    ) -> tuple[list[str], list[frozenset[str]]]:
        """页对象引用的缺失元素清单（去重范围 ∪ 本会话）。须在锁内调用。"""
        missing: list[str] = []
        holders: list[frozenset[str]] = []
        seen: set[str] = set()
        for eh in elements:
            if eh in seen:
                continue
            seen.add(eh)
            if eh in session.elements:  # 本会话已收到（字节到齐前不可校验，只判在不在）
                continue
            hs = self._object_holders(contract, eh)
            if self._in_scope(uri, hs, writable):
                continue
            missing.append(eh)
            holders.append(hs - {uri})
        return missing, holders

    def _blobs_missing(
        self, session: Session, uri: str, blob: str, writable: frozenset[str]
    ) -> tuple[list[str], list[frozenset[str]]]:
        """元素对象引用的缺失 blob 清单（去重范围 ∪ 本会话）。须在锁内调用。"""
        if blob in session.blobs:
            return [], []
        hs = self._store.referrers_of(blob)
        if self._in_scope(uri, frozenset(hs), writable):
            return [], []
        return [blob], [frozenset(hs) - {uri}]

    def _manifest(
        self,
        caller: str,
        compute: Callable[[frozenset[str]], tuple[list[_T], list[frozenset[str]]]],
    ) -> list[_T]:
        """上传响应清单的两阶段计算（与 commit §3.3 同一去重范围、同一授权口径）。

        锁内乐观计算（writable=∅）→ 有缺失且范围为 WRITABLE 时在锁外为持有文档调用授权器
        （授权器可插拔、可能慢或回调引擎，从不在锁内调用）→ 锁内带可写集合重算；第二次
        出现的缺失对象保守处理——范围只会变小，不会超出写授权。
        """
        with self._lock:
            missing, holders = compute(frozenset())
        if missing and self.config.dedup_scope is DedupScope.WRITABLE:
            writable = self._authorize_holders(caller, holders)
            if writable:
                with self._lock:
                    missing, _ = compute(writable)
        return missing

    # ------------------------------------------------------------------
    # commit（core §3.3）
    # ------------------------------------------------------------------

    def commit(
        self,
        caller: str,
        uri: str,
        body: bytes,
        contract: str | None,
        precondition: Precondition | None = None,
    ) -> CommitResult:
        uri = _normalize_uri(uri)
        # 第 0–1 步：传输层上限与报文校验，只看请求本身
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        req = self._parse_commit(data, contract)
        if req.force and precondition is not None:
            raise ValidationError("force 不得与 base_hash / if_absent 并存")
        # 第 2 步：授权
        auth = self.config.authorizer
        self._forbid_unless(auth.can_write(caller, uri), uri)
        if req.force:
            self._forbid_unless(auth.can_force(caller, uri), uri)
        # 第 3 步：前置条件存在性
        if precondition is None and not req.force:
            raise PreconditionRequiredError("写操作必须带 base_hash、if_absent 或 force")
        # 乐观求值：先只用去重范围下界（本文档当前状态）在锁内完成第 4–6 步；只有缺对象、
        # 且范围是写授权级时，才在锁外为持有这些对象的文档调用授权器，再进锁重做第 4–6 步。
        # 授权器可插拔，可能很慢，也可能回调本引擎，因此从不在锁内调用。
        try:
            return self._apply_commit(caller, uri, contract, req, precondition, frozenset())
        except _Gap as gap:
            if self.config.dedup_scope is not DedupScope.WRITABLE:
                raise gap.error(self.config.missing_max) from None
            writable = self._authorize_holders(caller, gap.holders)
            if not writable:
                raise gap.error(self.config.missing_max) from None
        try:
            return self._apply_commit(caller, uri, contract, req, precondition, writable)
        except _Gap as gap:
            raise gap.error(self.config.missing_max) from None

    def _apply_commit(
        self,
        caller: str,
        uri: str,
        contract: str,
        req: _Commit,
        precondition: Precondition | None,
        writable: frozenset[str],
    ) -> CommitResult:
        """在一把锁内完成第 4–6 步与原子切换（CAS 只在这里裁决）。缺对象时抛 ``_Gap``。"""
        with self._lock:
            current = self._store.docs.get(uri)
            # 第 4 步：unchanged，不论前置条件是否满足，不处理内联对象、不检查会话
            if current is not None and current.doc_hashes[contract] == req.doc_hash:
                total = len(current.element_seq)
                return CommitResult(
                    status="unchanged",
                    doc_hash=req.doc_hash,
                    delta=Delta(added=0, removed=0, retained=total),
                )
            # 第 5 步：前置条件求值
            if isinstance(precondition, IfAbsent) and current is not None:
                raise AlreadyExistsError(f"{uri} 已存在")
            if isinstance(precondition, BaseHash):
                if current is None:
                    raise NotFoundError(f"{uri} 不存在")
                if not current.matches(precondition.value):
                    raise PreconditionFailedError(
                        f"expected {precondition.value}, current {current.doc_hashes[contract]}"
                    )
            # 第 6 步：会话与可得性
            session: Session | None = None
            if req.staging_session is not None:
                session = self._resolve_session(caller, uri, req.staging_session)
            state = self._materialize(uri, contract, req, writable, session)
            self._store.replace(uri, state)
            if session is not None:
                self._sessions.consume(session)  # created / updated 成功后消费；失败不消费
        return CommitResult(
            status="created" if current is None else "updated",
            doc_hash=state.doc_hashes[contract],
            delta=_delta(current, state),
        )

    def _authorize_holders(self, caller: str, holders: Sequence[frozenset[str]]) -> frozenset[str]:
        """为每个缺失对象找一个调用者可写的持有文档（去重范围上界，core §3.3）。锁外调用。

        每个对象找到第一个可写的持有者即停；同一 URI 只判定一次。授权在锁外判定、锁内使用，
        与第 2 步的授权一样存在判定与使用之间的窗口：之后才出现的持有者按范围外处理，
        范围只会变小，不会超出写授权，也不低于下界。
        """
        can_write = self.config.authorizer.can_write
        decided: dict[str, bool] = {}
        writable: set[str] = set()
        for candidates in holders:
            if not candidates.isdisjoint(writable):
                continue  # 已有已知可写的持有者，本对象可得
            for holder in sorted(candidates):
                if holder not in decided:
                    decided[holder] = can_write(caller, holder)
                if decided[holder]:
                    writable.add(holder)
                    break
        return frozenset(writable)

    def _parse_commit(self, data: Any, contract: str) -> _Commit:
        env = _envelope(data, _COMMIT_MEMBERS)
        document = _require(env, "document", dict, "文档对象")
        pages = _optional(env, "pages", list, "页对象数组") or []
        objects = _optional(env, "objects", list, "元素对象数组") or []
        session = _optional(env, "staging_session", str, "字符串")
        force = _optional(env, "force", bool, "布尔值") or False
        doc_hash = _validated(
            partial(dpe_hash.object_hash, document, "document", contract), pointer("document")
        )
        inline_pages: dict[str, dict[str, Any]] = {}
        for i, page in enumerate(pages):
            h = _validated(
                partial(dpe_hash.object_hash, page, "page", contract), pointer("pages", i)
            )
            inline_pages[h] = page  # 同 hash 的等价页取后出现者（core §2.7 允许任一表示）
        inline_elements: dict[str, dict[str, Any]] = {}
        for i, element in enumerate(objects):
            h = _validated(
                partial(dpe_hash.object_hash, element, "element", contract), pointer("objects", i)
            )
            inline_elements[h] = element
        return _Commit(document, doc_hash, inline_pages, inline_elements, session, force)

    def _resolve_session(self, caller: str, uri: str, session_id: str) -> Session:
        """会话校验（core §3.4 的封闭清单）：不可用一律 ``DPE_SESSION_EXPIRED``，不区分原因。"""
        return self._sessions.get(caller, session_id, uri=uri)

    def _materialize(
        self,
        uri: str,
        contract: str,
        req: _Commit,
        writable: frozenset[str],
        session: Session | None,
    ) -> DocState:
        """可得性检查（core §3.3 第 6 步），通过后登记内容（内联与会话中的）并构造新状态。

        可得 = 去重范围（本文档当前状态 ∪ ``writable`` 中文档的当前状态）内已存内容 ∪ 暂存
        会话 ∪ 本次内联（core §3.3、§8）。**范围内已存的页走闭包捷径**（其整棵子树必在范围
        内）；**会话中页的子树不保证闭合**（逐层上传），必须逐层遍历到 blob。
        hash 一律由服务端算出（Rule 0）。缺失时抛 ``_Gap``，本次 commit 无任何效果。须在锁内调用。
        """
        store = self._store
        gap_holders: list[frozenset[str]] = []

        def holders_of_object(value: str) -> frozenset[str]:
            """页 / 元素对象（请求声明契约下的 hash）的持有文档。"""
            return self._object_holders(contract, value)

        def in_scope(holders: frozenset[str]) -> bool:
            return uri in holders or not holders.isdisjoint(writable)

        inline_pages = req.pages
        inline_elements = req.objects

        missing: dict[str, list[str]] = {"pages": [], "content_hashes": [], "blobs": []}
        seen: set[str] = set()

        def report(layer: str, value: str, holders: frozenset[str]) -> None:
            if value not in seen:
                seen.add(value)
                missing[layer].append(value)
                gap_holders.append(holders - {uri})

        # 页以内联版本优先：即使范围内已有同 hash 的页，本次写入的原样表示也要生效（core §2.7）；
        # 元素没有读接口，范围内已有即可复用。会话中的内容到齐后随本次写入提升进对象表。
        used_pages: dict[str, dict[str, Any]] = {}
        staged_pages: dict[str, StagedPage] = {}
        used_elements: dict[str, dict[str, Any]] = {}
        staged_elements: dict[str, StagedElement] = {}
        staged_blobs: dict[str, bytes] = {}
        seen_elements: set[str] = set()

        def check_blob(blob: str) -> None:
            if blob in staged_blobs:
                return
            holders = frozenset(store.referrers_of(blob))
            if in_scope(holders):
                return
            if session is not None and blob in session.blobs:
                staged_blobs[blob] = session.blobs[blob]
                return
            report("blobs", blob, holders)

        def check_element(eh: str) -> None:
            """元素层：范围 ∪ 会话 ∪ 内联；范围命中的整棵子树（含 blob）必在范围内。"""
            if eh in seen_elements:
                return
            seen_elements.add(eh)
            holders = holders_of_object(eh)
            if in_scope(holders):
                return
            if session is not None and eh in session.elements:
                staged = session.elements[eh]
                staged_elements[eh] = staged
                blob = staged.body.get("blob")
                if blob is not None:
                    check_blob(blob)
                return
            element = inline_elements.get(eh)
            if element is None:
                report("content_hashes", eh, holders)
                return
            used_elements[eh] = element
            blob = element.get("blob")
            if blob is not None:
                check_blob(blob)

        for ph in req.document["pages"]:
            if ph in used_pages or ph in staged_pages:
                continue
            page = inline_pages.get(ph)
            if page is not None:
                used_pages[ph] = page
                for eh in page["elements"]:
                    check_element(eh)
                continue
            staged = None if session is None else session.pages.get(ph)
            if staged is not None:
                # 会话页优先于范围内已存页：这是本次写入上传的原样表示（core §2.7 SHOULD）；
                # 其子树不保证闭合，须逐层遍历
                staged_pages[ph] = staged
                for eh in staged.body["elements"]:
                    check_element(eh)
                continue
            holders = holders_of_object(ph)
            if in_scope(holders):
                continue  # 范围内已存页：闭包保证整棵子树可得
            report("pages", ph, holders)
        if any(missing.values()):
            raise _Gap(missing, gap_holders)

        # 登记内容（自底向上）：会话元素 → 内联元素 → 内联页 → 会话页 → 会话 blob，
        # 子 hash 换成主契约
        for staged_element in staged_elements.values():
            store.put(
                staged_element.hashes[store.primary],
                ObjectRecord("element", dict(staged_element.body), dict(staged_element.hashes)),
            )

        def primary_of(value: str) -> str:
            primary = store.to_primary(contract, value)
            assert primary is not None
            return primary

        # 请求声明契约下的 hash 已在校验时算出，逐契约换算时复用
        for eh, element in used_elements.items():
            hashes = store.element_hashes(element, {contract: eh})
            store.put(hashes[store.primary], ObjectRecord("element", dict(element), hashes))
        written: dict[str, dict[str, Any]] = {}
        for ph, page in used_pages.items():
            children = [primary_of(h) for h in page["elements"]]
            body = {**page, "elements": children}
            hashes = store.tree_hashes("page", body, children, {contract: ph})
            store.put(hashes[store.primary], ObjectRecord("page", body, hashes))
            written[hashes[store.primary]] = body
        for ph, staged in staged_pages.items():
            # 会话页的子引用按上传声明的契约给出；到齐的页此时子对象必已兑现
            # （范围内已存、会话中提升或本次内联）
            children = [primary_of(h) for h in staged.body["elements"]]
            body = {**staged.body, "elements": children}
            hashes = store.tree_hashes("page", body, children, {contract: ph})
            store.put(hashes[store.primary], ObjectRecord("page", body, hashes))
            written[hashes[store.primary]] = body
        for ref, data in staged_blobs.items():
            store.put_blob(ref, data)

        page_seq = [primary_of(h) for h in req.document["pages"]]
        document = {**req.document, "pages": page_seq}
        doc_hashes = store.tree_hashes("document", document, page_seq, {contract: req.doc_hash})

        # 每页的原样表示归属本文档（不写进共享的对象记录，避免一次写入改变别的文档的读回）：
        # 本次内联或会话上传 → 本文档当前状态中的表示 → 范围内某篇持有它的文档中的表示（与之
        # 内容等价，core §2.7 允许返回等价类中的任一表示；只取范围内的文档，不带出范围外文档的写法）
        current = store.docs.get(uri)
        previous: dict[str, dict[str, Any]] = {}
        if current is not None:
            previous = dict(zip(current.document["pages"], current.page_bodies, strict=True))

        def representation(ph: str) -> dict[str, Any]:
            if ph in written:
                return written[ph]
            if ph in previous:
                return previous[ph]
            # 走到这里的页既未内联、也不在本文档当前状态中，它之所以可得，只能是因为 writable 中
            # 有持有者（_materialize 的可得性判定），因此候选非空
            holder = min((u for u in store.referrers_of(ph) if u in writable), default=None)
            assert holder is not None, "范围内的页必有 writable 中的持有者"
            state = store.docs[holder]
            position: int = state.document["pages"].index(ph)
            return state.page_bodies[position]

        page_bodies = tuple(representation(ph) for ph in page_seq)

        element_seq: list[str] = []
        for ph in page_seq:
            element_seq.extend(store.objects[ph].body["elements"])
        contents = frozenset(element_seq)
        blobs = frozenset(
            b for h in contents if (b := store.objects[h].body.get("blob")) is not None
        )
        return DocState(
            document=document,
            page_bodies=page_bodies,
            doc_hashes=doc_hashes,
            element_seq=tuple(element_seq),
            pages=frozenset(page_seq),
            contents=contents,
            blobs=blobs,
        )

    # ------------------------------------------------------------------
    # delete / move（core §4、§5）
    # ------------------------------------------------------------------

    def delete(self, caller: str, uri: str, contract: str | None, base_hash: str | None) -> None:
        """契约声明 → 授权 → 前置条件存在性 → 文档存在性 → base_hash 比较（core §5.1）。"""
        uri = _normalize_uri(uri)
        self._contract(contract)
        self._forbid_unless(self.config.authorizer.can_write(caller, uri), uri)
        if base_hash is None:
            raise PreconditionRequiredError("delete 必须带 base_hash")
        with self._lock:
            current = self._store.docs.get(uri)
            if current is None:
                raise NotFoundError(f"{uri} 不存在")
            if not current.matches(base_hash):
                raise PreconditionFailedError(f"expected {base_hash}")
            self._store.replace(uri, None)

    def move(self, caller: str, body: bytes, contract: str | None) -> MoveResult:
        """求值顺序见 core §5.2（#63）。"""
        data = _parse_body(body, self.config.max_payload_bytes)
        contract = self._contract(contract)
        env = _envelope(data, _MOVE_MEMBERS)
        from_uri = _normalize_uri(_require(env, "from_uri", str, "字符串"))
        to_uri = _normalize_uri(_require(env, "to_uri", str, "字符串"))
        base_hash = _optional(env, "base_hash", str, "字符串")
        can_write = self.config.authorizer.can_write
        self._forbid_unless(can_write(caller, from_uri), from_uri)
        self._forbid_unless(can_write(caller, to_uri), to_uri)
        if base_hash is None:
            raise PreconditionRequiredError("move 必须带 base_hash")
        with self._lock:
            docs = self._store.docs
            source = docs.get(from_uri)
            target = docs.get(to_uri)
            if source is None:
                # 目标状态已达成即成功（core §5.2 第 4 步）
                if target is not None and target.matches(base_hash):
                    return MoveResult(doc_hash=target.doc_hashes[contract])
                raise NotFoundError(f"{from_uri} 不存在")
            if not source.matches(base_hash):
                raise PreconditionFailedError(f"expected {base_hash}")
            if target is not None:
                raise AlreadyExistsError(f"{to_uri} 已存在")
            self._store.rename(from_uri, to_uri)
            return MoveResult(doc_hash=source.doc_hashes[contract])


def _staged_size(session: Session, target: str) -> int:
    """目标（页或 blob）在本会话的已收字节数：无进度 0、进行中已收、完成总量（HTTP 绑定 §4.7）。"""
    completed = session.completed_size(target)
    if completed is not None:
        return completed
    partial = session.partials.get(target)
    return 0 if partial is None else len(partial.data)


def _delta(old: DocState | None, new: DocState) -> Delta:
    """元素 content_hash 多重集之差（core §3.3）。"""
    before = Counter(old.element_seq if old is not None else ())
    after = Counter(new.element_seq)
    return Delta(
        added=sum((after - before).values()),
        removed=sum((before - after).values()),
        retained=sum((after & before).values()),
    )


def _missing_error(missing: Mapping[str, Sequence[str]], limit: int) -> MissingContentError:
    """按层给出缺失清单；总数超过 ``limit`` 时按 pages → content_hashes → blobs 的顺序截断。"""
    budget = limit
    kept: dict[str, list[str]] = {}
    for layer in ("pages", "content_hashes", "blobs"):
        kept[layer] = list(missing[layer][: max(budget, 0)])
        budget -= len(kept[layer])
    truncated = sum(len(v) for v in missing.values()) > limit
    return MissingContentError(missing=Missing(**kept), truncated=truncated)


def _encode_cursor(uri: str) -> str:
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(uri.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    """只接受本引擎签发的 cursor：严格 base64url（不丢弃字母表外字符），且重新编码后不变。"""
    if not cursor.startswith(_CURSOR_PREFIX):
        raise ValidationError("无法识别的 cursor")
    try:
        raw = base64.b64decode(cursor[len(_CURSOR_PREFIX) :], altchars=b"-_", validate=True)
        uri = raw.decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        raise ValidationError("无法识别的 cursor") from None
    if _encode_cursor(uri) != cursor:
        raise ValidationError("无法识别的 cursor")
    return uri
