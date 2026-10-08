"""参考服务端引擎：core 语义的内存实现，不含传输（HTTP 绑定由 #44 映射到本引擎）。

**求值顺序全部在引擎里**：带 JSON 请求体的操作（batch_head、commit、move）接收原始请求体字节，
按规范的顺序依次做传输层上限、I-JSON、契约声明、请求信封、对象校验，再授权，再做依赖文档状态的
判定（core §3.3、§5.2，HTTP 绑定 §3.1）。传输层只负责把请求映射为调用、把结果与 ``DpeError``
映射为响应。

调用者身份 ``caller`` 是不透明字符串，由传输层认证后给出；授权由可插拔的 ``Authorizer`` 判定。
"""

from __future__ import annotations

import base64
import binascii
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import Any, NoReturn, Protocol, TypeVar

import dpe_hash
from dpe_hash import DpeHashError, has_invalid_unicode

# RFC 6901 与 UTF-16 码元序工具复用 dpe_hash 的同一份实现（dpe-sdk 精确依赖同版本 dpe-hash）
from dpe_hash.jcs import pointer, utf16_key

from dpe_sdk import _ijson
from dpe_sdk.errors import (
    AlreadyExistsError,
    ContractUnsupportedError,
    ForbiddenError,
    MissingContentError,
    NotFoundError,
    PayloadTooLargeError,
    PreconditionFailedError,
    PreconditionRequiredError,
    SessionExpiredError,
    ValidationError,
    from_hash_error,
)
from dpe_sdk.testing._store import DocState, ObjectRecord, Store
from dpe_sdk.wire import (
    BatchHeads,
    Capabilities,
    CommitResult,
    Delta,
    Head,
    Limits,
    ListEntry,
    ListPage,
    Missing,
    MoveResult,
    Skeleton,
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
    """限额默认值同 HTTP 绑定 §4.1 的示例。"""

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
    dedup_scope: DedupScope = DedupScope.DOCUMENT
    authorizer: Authorizer = field(default_factory=AllowAll)

    def __post_init__(self) -> None:
        if not self.contracts:
            raise ValueError("contracts 至少要有一个契约")
        known = {*dpe_hash.SUPPORTED_CONTRACTS, dpe_hash.DRILL_CONTRACT}
        unknown = [c for c in self.contracts if c not in known]
        if unknown:
            raise ValueError(f"dpe_hash 不支持的契约：{unknown}")
        if self.staging_ttl_seconds < 3600:
            raise ValueError("staging_ttl_seconds 不得少于 3600（core §3.4）")


_COMMIT_MEMBERS = frozenset({"document", "pages", "objects", "staging_session", "force"})
_MOVE_MEMBERS = frozenset({"from_uri", "to_uri", "base_hash"})
_HEADS_MEMBERS = frozenset({"uris"})
_CURSOR_PREFIX = "c1."

_T = TypeVar("_T")


def _normalize_uri(uri: str) -> str:
    """file_uri 比较前的语法规范化（core §1）。

    接缝：#39 合入后改为 ``dpe_hash.normalize_file_uri``；在此之前按原样比较。
    """
    return uri


def _validated(compute: Callable[[], _T], at: str) -> _T:
    """调用 dpe_hash 校验对象；错误转换为协议错误，位置前缀为该对象在请求体中的位置。"""
    try:
        return compute()
    except DpeHashError as exc:
        raise from_hash_error(exc, at) from None


def _parse_body(body: bytes, limit: int) -> Any:
    """传输层上限（第 0 步）与 I-JSON（第 1 步的第 1 子步）。"""
    if len(body) > limit:
        raise PayloadTooLargeError(f"请求体 {len(body)} 字节，超过 max_payload_bytes={limit}")
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
    pages: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    staging_session: str | None
    force: bool


class Engine:
    """内存版 DPE 服务端引擎。线程安全：写操作与读操作在同一把锁内完成。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._store = Store(self.config.contracts)
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
        """按规范化 URI 的码点前缀分页（core §3、HTTP 绑定 §4.4）。不识别 path 段边界。"""
        contract = self._contract(contract)
        max_limit = self.config.list_page_max
        if limit is None:
            limit = max_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
            raise ValidationError(f"limit 必须是 1 到 {max_limit} 之间的整数")
        after = None if cursor is None else _decode_cursor(cursor)
        prefix = _normalize_uri(prefix) if prefix else ""
        with self._lock:
            matched = sorted(
                (uri, state.doc_hashes[contract])
                for uri, state in self._store.docs.items()
                if uri.startswith(prefix) and (after is None or uri > after)
            )
        page = matched[:limit]
        more = len(matched) > limit
        return ListPage(
            documents=[ListEntry(file_uri=u, doc_hash=h) for u, h in page],
            next_cursor=_encode_cursor(page[-1][0]) if more else None,
        )

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
            if req.staging_session is not None:
                self._resolve_session(caller, uri, req.staging_session)
            state = self._materialize(caller, uri, contract, req)
            self._store.replace(uri, state)
        return CommitResult(
            status="created" if current is None else "updated",
            doc_hash=state.doc_hashes[contract],
            delta=_delta(current, state),
        )

    def _parse_commit(self, data: Any, contract: str) -> _Commit:
        env = _envelope(data, _COMMIT_MEMBERS)
        if "document" not in env:
            raise ValidationError("请求体缺少 document")
        pages = _optional(env, "pages", list, "页对象数组") or []
        objects = _optional(env, "objects", list, "元素对象数组") or []
        session = _optional(env, "staging_session", str, "字符串")
        force = _optional(env, "force", bool, "布尔值") or False
        document = env["document"]
        doc_hash = _validated(
            lambda: dpe_hash.object_hash(document, "document", contract), "/document"
        )
        for i, page in enumerate(pages):
            _validated(partial(dpe_hash.object_hash, page, "page", contract), pointer("pages", i))
        for i, element in enumerate(objects):
            _validated(
                partial(dpe_hash.object_hash, element, "element", contract), pointer("objects", i)
            )
        return _Commit(document, doc_hash, pages, objects, session, force)

    def _resolve_session(self, caller: str, uri: str, session_id: str) -> NoReturn:
        """会话接缝（core §3.4）：#42 阶段没有暂存会话，任何引用一律不可用；#43 接入真实会话。"""
        raise SessionExpiredError("暂存会话不可用")

    def _scope(self, caller: str, uri: str) -> list[DocState]:
        docs = self._store.docs
        if self.config.dedup_scope is DedupScope.WRITABLE:
            can_write = self.config.authorizer.can_write
            return [s for u, s in docs.items() if can_write(caller, u)]
        current = docs.get(uri)
        return [] if current is None else [current]

    def _materialize(self, caller: str, uri: str, contract: str, req: _Commit) -> DocState:
        """可得性检查（core §3.3 第 6 步），通过后把内联对象登记进对象表并构造新状态。

        hash 一律由服务端算出（Rule 0）。缺失时本次 commit 无任何效果。
        """
        store = self._store
        scope = self._scope(caller, uri)
        scope_pages = frozenset().union(*(s.pages for s in scope))
        scope_contents = frozenset().union(*(s.contents for s in scope))
        scope_blobs = frozenset().union(*(s.blobs for s in scope))

        def in_scope(value: str, pool: frozenset[str]) -> str | None:
            primary = store.to_primary(contract, value)
            return primary if primary is not None and primary in pool else None

        inline_pages = {dpe_hash.object_hash(p, "page", contract): p for p in req.pages}
        inline_elements = {dpe_hash.object_hash(e, "element", contract): e for e in req.objects}

        missing: dict[str, list[str]] = {"pages": [], "content_hashes": [], "blobs": []}
        seen: set[str] = set()

        def report(layer: str, value: str) -> None:
            if value not in seen:
                seen.add(value)
                missing[layer].append(value)

        # 页以内联版本优先：即使范围内已有同 hash 的页，本次写入的原样表示也要生效（core §2.7）。
        # 元素没有读接口，范围内已有即可复用。
        used_pages: dict[str, dict[str, Any]] = {}
        used_elements: dict[str, dict[str, Any]] = {}
        for ph in req.document["pages"]:
            if ph in used_pages:
                continue
            page = inline_pages.get(ph)
            if page is None:
                if in_scope(ph, scope_pages) is None:
                    report("pages", ph)
                continue
            used_pages[ph] = page
            for eh in page["elements"]:
                if eh in used_elements or in_scope(eh, scope_contents) is not None:
                    continue
                element = inline_elements.get(eh)
                if element is None:
                    report("content_hashes", eh)
                    continue
                used_elements[eh] = element
                blob = element.get("blob")
                if blob is not None and blob not in scope_blobs:
                    report("blobs", blob)
        if any(missing.values()):
            raise _missing_error(missing, self.config.missing_max)

        # 登记内联对象（自底向上），子 hash 换成主契约
        def primary_of(value: str) -> str:
            primary = store.to_primary(contract, value)
            assert primary is not None
            return primary

        for element in used_elements.values():
            hashes = store.element_hashes(element)
            store.put(hashes[store.primary], ObjectRecord("element", dict(element), hashes))
        written: dict[str, dict[str, Any]] = {}
        for page in used_pages.values():
            children = [primary_of(h) for h in page["elements"]]
            body = {**page, "elements": children}
            hashes = store.tree_hashes("page", body, children)
            store.put(hashes[store.primary], ObjectRecord("page", body, hashes))
            written[hashes[store.primary]] = body
        page_seq = [primary_of(h) for h in req.document["pages"]]
        document = {**req.document, "pages": page_seq}
        doc_hashes = store.tree_hashes("document", document, page_seq)

        # 每页的原样表示归属本文档（不写进共享的对象记录，避免一次写入改变别的文档的读回）：
        # 本次内联 → 本文档当前状态中的表示 → 对象记录首次登记的表示（来自范围内的别的文档，
        # 与之内容等价，core §2.7 允许返回等价类中的任一表示）
        current = store.docs.get(uri)
        previous: dict[str, dict[str, Any]] = {}
        if current is not None:
            previous = dict(zip(current.document["pages"], current.page_bodies, strict=True))

        def representation(ph: str) -> dict[str, Any]:
            if ph in written:
                return written[ph]
            if ph in previous:
                return previous[ph]
            return store.objects[ph].body

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
            docs[to_uri] = docs.pop(from_uri)
            return MoveResult(doc_hash=source.doc_hashes[contract])


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
