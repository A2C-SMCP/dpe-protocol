"""dpe-push/1 异步客户端：能力发现 → 协商 → 投递。

典型用法::

    async with DPEPushClient("https://robot.example.com", token="...") as client:
        result = await client.push(doc, base_doc_hash=last_doc_hash)
        last_doc_hash = result.doc_hash
"""

import asyncio
import gzip
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from dpe_protocol.hashing import SUPPORTED_HASH_STRATEGIES, DocumentHashes, HashStrategyRef, compute_hashes
from dpe_protocol.push.errors import (
    DPEError,
    DPEErrorCode,
    DPEPushError,
    DPEValidationError,
    NoCompatibleHashStrategyError,
)
from dpe_protocol.push.models import (
    MEDIA_TYPE,
    PROTOCOL_VERSION,
    WELL_KNOWN_PATH,
    Capabilities,
    CommitCounts,
    CommitRequest,
    CommitResponse,
    CommitStatus,
    ContentItem,
    ErrorEnvelope,
    Manifest,
    NegotiateResponse,
)
from dpe_protocol.schema import DocElement, Document

TokenProvider = str | Callable[[], Awaitable[str]]

_RETRYABLE_HTTP_STATUS = frozenset({429, 503})

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class PushResult:
    file_uri: str
    #: ``unchanged`` 可能来自协商（未进入 Phase 2）或 commit
    status: CommitStatus
    #: 服务端权威 doc_hash，应持久化为下次投递的 ``base_doc_hash``
    doc_hash: str
    hash_strategy_uri: str
    counts: CommitCounts | None
    negotiate: NegotiateResponse | None
    contents_sent: int
    #: 提交次数；大于 1 表示触发了文档层分片
    shards: int = 1


def check_pushable(doc: Document) -> list[str]:
    """dpe-push/1 v1 不支持的内容（push-protocol-v1 §10.4），在本地提前拦截。"""
    violations: list[str] = []
    for page, ele in doc.iter_elements():
        if ele.ele_metadata.image_path:
            violations.append(
                f"page {page.number} element {ele.element_id}: image_path is not allowed in dpe-push/1, "
                "use image_base64 or image_url"
            )
    return violations


class DPEPushClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: TokenProvider | None = None,
        robot_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_retry_after: float = 60.0,
        capabilities_ttl: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._robot_id = robot_id
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after
        self._capabilities_ttl = capabilities_ttl
        self._capabilities: tuple[float, Capabilities] | None = None
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------ Phase 0

    async def discover(self, *, refresh: bool = False) -> Capabilities:
        """读取能力文档（带 TTL 缓存）。它只是协商提示，服务端写入路径会重新校验。"""
        now = time.monotonic()
        if not refresh and self._capabilities and now - self._capabilities[0] < self._capabilities_ttl:
            return self._capabilities[1]
        resp = await self._send("GET", WELL_KNOWN_PATH, authenticated=False)
        caps = self._parse(resp, Capabilities)
        self._capabilities = (now, caps)
        return caps

    async def select_strategy(self) -> HashStrategyRef:
        """在 SDK 已实现策略与服务端 allowlist 的交集中选择，优先服务端默认、其次未废弃者。"""
        caps = await self.discover()
        accepted = {HashStrategyRef.parse(s.uri): s for s in caps.accepted_hash_strategies}
        candidates = [ref for ref in accepted if ref in SUPPORTED_HASH_STRATEGIES]
        if not candidates:
            raise NoCompatibleHashStrategyError(
                f"server accepts {[s.uri for s in caps.accepted_hash_strategies]}, "
                f"SDK supports {sorted(str(s) for s in SUPPORTED_HASH_STRATEGIES)}"
            )
        if caps.default_hash_strategy_uri:
            default = HashStrategyRef.parse(caps.default_hash_strategy_uri)
            if default in candidates:
                return default
        return min(candidates, key=lambda ref: (accepted[ref].deprecated_at is not None, str(ref)))

    # ------------------------------------------------------------ Phase 1 / 2

    async def negotiate(self, manifest: Manifest) -> NegotiateResponse:
        caps = await self.discover()
        resp = await self._send("POST", caps.endpoints.negotiate, body=manifest, caps=caps)
        return self._parse(resp, NegotiateResponse)

    async def commit(self, request: CommitRequest, *, idempotency_key: str) -> CommitResponse:
        caps = await self.discover()
        resp = await self._send(
            "POST", caps.endpoints.commit, body=request, caps=caps, extra_headers={"Idempotency-Key": idempotency_key}
        )
        return self._parse(resp, CommitResponse)

    async def push(
        self, doc: Document, *, base_doc_hash: str | None = None, skip_negotiate: bool = False
    ) -> PushResult:
        """完整投递一个文档。

        Args:
            doc: 完整的新文档（永远是全貌，不支持局部投递）。
            base_doc_hash: 上次投递成功时服务端返回的 ``doc_hash``，仅用于服务端诊断。
            skip_negotiate: 快速路径，确信服务端已持有全部内容时跳过协商；
                若服务端回 ``DPE_INSUFFICIENT_CONTENT``，自动补传一次。
        """
        caps = await self.discover()
        if PROTOCOL_VERSION not in caps.protocol_versions:
            raise DPEError(f"server supports dpe-push {caps.protocol_versions}, SDK speaks {PROTOCOL_VERSION}")
        if violations := check_pushable(doc):
            raise DPEValidationError(violations)

        hashes = compute_hashes(doc, await self.select_strategy())
        manifest = Manifest.build(doc, hashes, base_doc_hash=base_doc_hash)
        elements = _index_elements(doc, hashes)

        negotiated: NegotiateResponse | None = None
        missing: set[str] = set()
        if not skip_negotiate:
            negotiated = await self.negotiate(manifest)
            if negotiated.status == "unchanged":
                return PushResult(
                    file_uri=manifest.file_uri,
                    status="unchanged",
                    doc_hash=negotiated.server_doc_hash or manifest.doc_hash,
                    hash_strategy_uri=manifest.hash_strategy_uri,
                    counts=None,
                    negotiate=negotiated,
                    contents_sent=0,
                    shards=0,
                )
            missing = set(elements) if negotiated.status == "new" else set(negotiated.missing_content_hashes)

        # 至多两轮：首轮按协商结果补传；若服务端判定内容不足（快速路径或并发变更），按其清单再补一轮。
        for attempt in range(2):
            try:
                result, contents_sent, shards = await self._commit_missing(doc, hashes, manifest, elements, missing)
            except DPEPushError as err:
                if err.code != DPEErrorCode.INSUFFICIENT_CONTENT or attempt > 0:
                    raise
                missing |= set((err.data or {}).get("missing_content_hashes", []))
                continue
            return PushResult(
                file_uri=manifest.file_uri,
                status=result.status,
                doc_hash=result.doc_hash,
                hash_strategy_uri=result.hash_strategy_uri,
                counts=result.counts,
                negotiate=negotiated,
                contents_sent=contents_sent,
                shards=shards,
            )
        raise AssertionError("unreachable")

    async def _commit_missing(
        self,
        doc: Document,
        hashes: DocumentHashes,
        manifest: Manifest,
        elements: dict[str, DocElement],
        missing: set[str],
    ) -> tuple[CommitResponse, int, int]:
        """提交缺口内容；请求体超过 ``max_payload_bytes`` 时自动做文档层分片。返回 (结果, 发送内容数, 分片数)。

        分片按「缺失内容」切批，而不是按页前缀（push-protocol-v1 §10.3 的前缀方案）：
        第 i 片提交的是**完整文档结构**，但只保留「服务端已有 + 前 i 批」的 element。
        这样每一片都是合法的完整 manifest，且从不删除最终文档中仍存在的 element——
        对更新已有文档同样适用，不会因前缀截断导致尾部页面先删后建、重复传输与重复学习。
        """
        caps = await self.discover()
        contents = _build_contents(elements, missing)
        request = CommitRequest(manifest=manifest, contents=contents)
        limit = caps.max_payload_bytes
        if limit is None or _json_size(request) <= limit:
            # 请求体不同必须换新的幂等键，否则服务端会回放首次响应
            return await self.commit(request, idempotency_key=uuid.uuid4().hex), len(contents), 1

        batches = _plan_batches(contents, budget=limit - _json_size(manifest) - _ENVELOPE_OVERHEAD)
        available = set(elements) - missing
        base_doc_hash = manifest.base_doc_hash
        result: CommitResponse | None = None
        for index, batch in enumerate(batches):
            available |= {item.content_hash for item in batch}
            if index == len(batches) - 1:
                shard_manifest = manifest.model_copy(update={"base_doc_hash": base_doc_hash})
            else:
                shard_doc = _filter_elements(doc, hashes, available)
                shard_manifest = Manifest.build(
                    shard_doc, compute_hashes(shard_doc, hashes.strategy), base_doc_hash=base_doc_hash
                )
            result = await self.commit(
                CommitRequest(manifest=shard_manifest, contents=batch), idempotency_key=uuid.uuid4().hex
            )
            base_doc_hash = result.doc_hash
        assert result is not None
        return result, len(contents), len(batches)

    # ------------------------------------------------------------ transport

    async def _headers(self, *, authenticated: bool) -> dict[str, str]:
        headers = {"Accept": MEDIA_TYPE}
        if authenticated:
            if self._token is not None:
                token = self._token if isinstance(self._token, str) else await self._token()
                headers["Authorization"] = f"Bearer {token}"
            if self._robot_id:
                headers["X-TFRS-Robot-Id"] = self._robot_id
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        caps: Capabilities | None = None,
        authenticated: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = await self._headers(authenticated=authenticated) | (extra_headers or {})
        content: bytes | None = None
        if body is not None:
            content = body.model_dump_json().encode("utf-8")
            # 按解压后大小比较：服务端需按解压后体积设限才能防御压缩炸弹
            if caps and caps.max_payload_bytes and len(content) > caps.max_payload_bytes:
                raise DPEPushError(
                    DPEErrorCode.PAYLOAD_TOO_LARGE,
                    f"request body {len(content)} bytes exceeds max_payload_bytes {caps.max_payload_bytes}; "
                    "split the document into growing prefixes (push-protocol-v1 §10.3)",
                    http_status=413,
                    data={"max_payload_bytes": caps.max_payload_bytes},
                )
            headers["Content-Type"] = MEDIA_TYPE
            if caps and "gzip" in caps.content_encodings:
                content = gzip.compress(content)
                headers["Content-Encoding"] = "gzip"

        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt == self._max_retries
            try:
                resp = await self._http.request(method, url, content=content, headers=headers)
            except httpx.TransportError:
                # negotiate 只读、commit 有幂等键，网络层失败均可安全重试
                if last_attempt:
                    raise
                await self._sleep(self._backoff(attempt, None))
                continue
            if resp.status_code < 400:
                return resp
            err = _parse_error(resp)
            if resp.status_code in _RETRYABLE_HTTP_STATUS and not last_attempt:
                await self._sleep(self._backoff(attempt, err.retry_after))
                continue
            raise err
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        delay = retry_after if retry_after is not None else 0.5 * 2**attempt
        return min(delay, self._max_retry_after)

    @staticmethod
    def _parse(resp: httpx.Response, model: type[M]) -> M:
        try:
            return model.model_validate_json(resp.content)
        except ValidationError as exc:
            raise DPEError(f"malformed {model.__name__} from server: {exc}") from exc


def _index_elements(doc: Document, hashes: DocumentHashes) -> dict[str, DocElement]:
    """content_hash → element，保持文档顺序。相同内容的 element 共享同一 hash，传一次即可。"""
    index: dict[str, DocElement] = {}
    for page, page_hashes in zip(doc.pages, hashes.pages, strict=True):
        for ele, content_hash in zip(page.elements, page_hashes.content_hashes, strict=True):
            index.setdefault(content_hash, ele)
    return index


def _build_contents(elements: dict[str, DocElement], missing: set[str]) -> list[ContentItem]:
    if unknown := missing - elements.keys():
        raise DPEError(f"server requested content hashes not present in this document: {sorted(unknown)}")
    return [ContentItem.build(h, ele) for h, ele in elements.items() if h in missing]


def _parse_error(resp: httpx.Response) -> DPEPushError:
    retry_after: float | None = None
    if raw := resp.headers.get("Retry-After"):
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = None
    try:
        envelope = ErrorEnvelope.model_validate_json(resp.content)
        code, message, data = str(envelope.code), envelope.message, envelope.data
    except ValidationError:
        code, message, data = f"HTTP_{resp.status_code}", resp.text[:500], None
    return DPEPushError(code, message, http_status=resp.status_code, data=data, retry_after=retry_after)


#: CommitRequest 外层 JSON 结构与分隔符的预留字节
_ENVELOPE_OVERHEAD = 64


def _json_size(model: BaseModel) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def _plan_batches(contents: list[ContentItem], *, budget: int) -> list[list[ContentItem]]:
    """按文档顺序贪心切批，每批序列化后不超过 ``budget``。"""
    batches: list[list[ContentItem]] = [[]]
    used = 0
    for item in contents:
        size = _json_size(item) + 1  # 逗号
        if size > budget:
            raise DPEPushError(
                DPEErrorCode.PAYLOAD_TOO_LARGE,
                f"element {item.content_hash} alone ({size} bytes) exceeds the per-request budget ({budget} bytes); "
                "it cannot be sharded at document level (needs content staging, push-protocol-v1 §10.3 v2)",
                http_status=413,
            )
        if used + size > budget and batches[-1]:
            batches.append([])
            used = 0
        batches[-1].append(item)
        used += size
    return batches


def _filter_elements(doc: Document, hashes: DocumentHashes, keep: set[str]) -> Document:
    """保留全部页，但每页只保留 content_hash 在 ``keep`` 中的 element（顺序不变）。"""
    pages = [
        page.model_copy(
            update={"elements": [e for e, h in zip(page.elements, ph.content_hashes, strict=True) if h in keep]}
        )
        for page, ph in zip(doc.pages, hashes.pages, strict=True)
    ]
    return doc.model_copy(update={"pages": pages})
