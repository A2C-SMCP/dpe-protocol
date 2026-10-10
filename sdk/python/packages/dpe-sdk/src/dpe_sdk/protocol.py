"""sans-IO 协议核心（core §3、HTTP 绑定）：只产出请求描述、消费响应描述，不做任何 I/O。

**操作是生成器协程**（``Operation[T]``）：每产出一个 ``Request``，驱动方发送它并把 ``Response``
送回，操作最终 ``return`` 结果或抛出异常。多轮往返（batch_head 分批等）都在操作内部完成，
同步与异步传输只需各写一个驱动循环（``drive`` / ``adrive``）。操作也可能零往返直接返回。

**驱动约定**：发送请求时抛出的 ``Exception``（连接失败、超时等）由驱动方 ``throw`` 回操作，
操作不处理时原样传出；需要据此判定结果的操作（如 force 提交的响应丢失）在操作内部处理。

**写操作的重试**（core §5.2）：写操作把抛回的任何异常，以及无 DPE 错误码的 502 / 504（网关
已转发、源站结果未知），都视为「请求可能已到达服务端、响应丢失」，由操作自己判定——commit
（非 force）、delete、move 原样重发（幂等由内容保证，至多 ``max_resends`` 次），delete 在重发
后得到 ``DPE_NOT_FOUND`` 视为成功；force 提交不重放，先 ``head`` 判定。只有操作知道自己在重发，
所以写请求可能已发出之后，传输层 MUST NOT 自行重发；确定未发出的失败（如建连失败）与
``DPE_RATE_LIMITED`` / ``DPE_UNAVAILABLE`` 的原样重试（``retryable``）仍可由传输层处理，它们
不改变请求是否生效的判定。只读请求原样重发总是安全的，传输层可以自行重发。

**契约**：``ProtocolCore`` 只使用调用方显式指定的一个 hash 契约（缺省 ``dpe_hash.CONTRACT``），
服务端不接受时失败，不换契约、不降级（core §3.1）；之后每个请求都带 ``DPE-Hash-Contract``，
响应中的每个 hash 都必须属于该契约。

**错误**（HTTP 绑定 §3.2、§5）：按 code 分派，不按状态码。有 problem 体时取体中的 ``code``，
否则取 ``DPE-Error-Code`` 头（HEAD 不能带体）；两者都没有就不是 DPE 错误，抛
``UnexpectedResponseError``，不猜测 code。成功响应的报文不合规同样抛 ``UnexpectedResponseError``，
不冒充请求侧的 ``DPE_VALIDATION``。

**暂存路径与大文档投递**（core §3.2–§3.4）：``negotiate`` / ``upload_page`` / ``upload_element`` /
``upload_blob`` 是逐层补传的原语，``commit`` 可引用暂存会话；``deliver`` 是组合操作——自动选择
快路径（一次内联 commit）或暂存路径（只传缺失的页、元素与 blob），并只处理两类恢复：
``DPE_MISSING_CONTENT`` 按分层清单补传同一会话后重提、``DPE_SESSION_EXPIRED`` 重开 negotiate；
其余错误（含 CAS 三类）原样抛出，绝不自动 force。分块上传的任何一块响应丢失或不确定时，
MUST NOT 盲目重发：先断点查询（HEAD 同一 URL，只读、不续期）再按已收偏移续传（HTTP 绑定 §4.7）。

**核心不做 I/O**：blob 字节不进核心。``upload_blob`` 与 ``deliver`` 只把「哪个区间」写进
``Request.body``（``BlobRange``），字节由传输适配层在 ``Request.body_bytes()`` 里取出（同步
适配层直接读，异步适配层自行把该调用放到工作线程）。``BlobSource`` 的 ``ref`` 与 ``size`` 由
调用方算好传入（如 #15 媒体辅助、运行器），核心不读源、不算摘要。
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO, TypeAlias, TypeVar, cast
from urllib.parse import quote

import dpe_hash
from dpe_hash import DpeHashError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from dpe_sdk import _ijson
from dpe_sdk.errors import (
    AlreadyExistsError,
    ContractUnsupportedError,
    DpeError,
    ForceNotConfirmedError,
    IncompatibleServerError,
    MissingContentError,
    NotFoundError,
    PayloadTooLargeError,
    PreconditionFailedError,
    SessionExpiredError,
    UnexpectedResponseError,
    ValidationError,
    from_problem,
)
from dpe_sdk.models import Document, ElementObject, PageObject
from dpe_sdk.wire import (
    BatchHeads,
    Capabilities,
    CommitResult,
    Delta,
    ElementUploadMissing,
    Limits,
    ListPage,
    Missing,
    MoveResult,
    NegotiateResult,
    PageUploadMissing,
    Skeleton,
    StagingSession,
)

__all__ = [
    "CONTRACT_HEADER",
    "DOC_HASH_HEADER",
    "ERROR_CODE_HEADER",
    "PROTOCOL",
    "SESSION_EXPIRES_HEADER",
    "UPLOAD_OFFSET_HEADER",
    "BaseHash",
    "BlobRange",
    "BlobSource",
    "CommitPrecondition",
    "Force",
    "IfAbsent",
    "Negotiated",
    "Operation",
    "ProtocolCore",
    "Request",
    "Response",
    "Session",
    "adrive",
    "drive",
    "fetch_capabilities",
    "retry_after_seconds",
]

#: 本 SDK 实现的协议版本（capabilities 的 ``protocol``）
PROTOCOL = "dpe/1"
CONTRACT_HEADER = "DPE-Hash-Contract"
DOC_HASH_HEADER = "DPE-Doc-Hash"
ERROR_CODE_HEADER = "DPE-Error-Code"
#: 会话续期后的过期时间（negotiate / upload / 断点查询的响应头；core §3.4）
SESSION_EXPIRES_HEADER = "DPE-Session-Expires"
#: 分块上传与断点查询的本会话已收字节数（HTTP 绑定 §4.7）
UPLOAD_OFFSET_HEADER = "DPE-Upload-Offset"

_JSON = "application/json"
_PROBLEM_JSON = "application/problem+json"

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True)
class BlobSource:
    """blob 字节来源：``ref``（``sha256:…``）与总长 ``size`` 在上传前确定，``read(offset, length)``
    按任意偏移给出字节（可 seek 的二进制流或内存字节都能适配）。

    **核心不读源**（sans-IO）：上传时核心只写 ``BlobRange`` 描述区间，``read`` 由传输适配层
    在 ``Request.body_bytes()`` 里调用；异步适配层自行把这次调用放到工作线程。摘要由调用方
    算好传入（如 ``dpe_hash.blob_ref``），核心不读源、不算摘要，也不做本地校验——服务端在
    字节到齐后校验，可能返回 ``DPE_HASH_MISMATCH``（core §3.4）。
    """

    ref: str
    size: int
    read: Callable[[int, int], bytes]

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> BlobSource:
        """由内存字节构造：ref 与 size 本地算出（纯计算，不涉及 I/O）。"""
        raw = bytes(data)
        return cls(
            dpe_hash.blob_ref(raw),
            len(raw),
            lambda offset, length: raw[offset : offset + length],
        )

    @classmethod
    def from_seekable(cls, stream: BinaryIO, ref: str, size: int) -> BlobSource:
        """把可 seek 的二进制流适配为来源（#15 媒体辅助、运行器用）。

        ``ref`` 与 ``size`` 由调用方算好传入（读取流算 sha256 是调用方的 I/O，不在核心）；
        读取发生在 ``read`` 被适配层调用时。
        """

        def read(offset: int, length: int) -> bytes:
            stream.seek(offset)
            block = bytearray()
            while len(block) < length:
                piece = stream.read(length - len(block))
                if not piece:
                    break
                block.extend(piece)
            return bytes(block)

        return cls(ref, size, read)


@dataclass(frozen=True)
class BlobRange:
    """延迟的字节区间（请求体）：核心只描述「谁的 ``[offset, offset+length)``」，字节由传输
    适配层经 ``Request.body_bytes()`` 取出（同步适配层直接读；异步适配层放进工作线程）。"""

    source: BlobSource
    offset: int
    length: int


@dataclass(frozen=True)
class Request:
    """请求描述。``target`` 是相对 remote 的路径与已编码的查询串（HTTP 绑定 §1），传输层把它
    原样拼在 remote 之后（remote 不带尾部 ``/``），MUST NOT 再次编码。

    ``body`` 为 ``BlobRange`` 时是延迟的字节区间（大 blob 的分块上传），用 ``body_bytes()``
    取出字节；适配层 MUST NOT 把 ``BlobRange`` 本身交给 HTTP 客户端。
    """

    method: str
    target: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | BlobRange | None = None

    def body_bytes(self) -> bytes | None:
        """取出请求体的字节（``BlobRange`` 在这里解出）。源给的字节数与声明的区间不符时抛
        ``ValueError``（调用方的源有问题），不发请求。"""
        body = self.body
        if isinstance(body, BlobRange):
            data = body.source.read(body.offset, body.length)
            if len(data) != body.length:
                raise ValueError(
                    f"blob 源 {body.source.ref} 在 [{body.offset}, {body.offset + body.length}) "
                    f"上给了 {len(data)} 字节（应为 {body.length}）"
                )
            return data
        return body


@dataclass(frozen=True)
class Response:
    """响应描述。头字段名不区分大小写（构造时统一为小写）；HEAD 响应的 ``body`` 为空。"""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", {k.lower(): v for k, v in self.headers.items()})

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


#: 一个协议操作：产出 ``Request``、接收 ``Response``，返回 ``_T``
Operation: TypeAlias = Generator[Request, Response, _T]


@dataclass
class Session:
    """客户端持有的暂存会话（core §3.4）：``id`` 与规范化 ``uri`` 供 upload / commit 引用。

    ``expires_at`` 随每个响应续期，只作诊断（日志、展示）——是否过期只信服务端信号：410
    （``DPE_SESSION_EXPIRED``）与断点查询响应里的 ``DPE-Session-Expires``。本地时钟偏差会
    让仍有效的会话被提前放弃（新会话从空开始、已暂存内容全部重传），因此不用本地时钟判定。
    会话只作缓存：失效时重开 negotiate，结果与不复用时一致。
    """

    id: str
    uri: str
    expires_at: datetime


@dataclass(frozen=True)
class Negotiated:
    """``negotiate`` 的结果（HTTP 绑定 §4.5）：会话与逐层缺失清单。

    ``missing_content_hashes`` 只在附带页对象时非空——本 SDK 不附带页对象，页引用的缺失元素
    由各页上传的响应给出（``upload_page`` 的返回值）。
    """

    session: Session
    missing_pages: list[str]
    missing_content_hashes: list[str]


@dataclass(frozen=True)
class BaseHash:
    """前置条件 ``base_hash = H``（core §5.1）：H 是读到的 doc_hash（HTTP：``If-Match: "H"``）。

    ``ProtocolCore`` 发出前校验 H 是本次声明契约的 hash（不接受裸 hash）；服务端按值比较，
    不做格式校验（core §5.2）。
    """

    value: str


@dataclass(frozen=True)
class IfAbsent:
    """前置条件 ``if_absent``：仅新建（HTTP：``If-None-Match: *``）。"""


@dataclass(frozen=True)
class Force:
    """无条件覆盖（仅 commit，core §5.1）：请求体 ``"force": true``，不带条件头。

    只在调用方显式传入时使用；SDK 绝不自动改用 force（core §5.2）。
    """


#: commit 的前置条件：必须三选一，写操作没有「不带前置条件」的形式（core §5.1）
CommitPrecondition: TypeAlias = BaseHash | IfAbsent | Force


def drive(op: Operation[_T], send: Callable[[Request], Response]) -> _T:
    """同步驱动一个操作直到完成；``send`` 抛出的 ``Exception`` 送回操作（见模块说明）。"""
    try:
        request = next(op)
        while True:
            try:
                response = send(request)
            except Exception as exc:
                request = op.throw(exc)
            else:
                request = op.send(response)
    except StopIteration as stop:
        return cast(_T, stop.value)
    finally:
        op.close()


async def adrive(op: Operation[_T], send: Callable[[Request], Awaitable[Response]]) -> _T:
    """``drive`` 的异步版本。"""
    try:
        request = next(op)
        while True:
            try:
                response = await send(request)
            except Exception as exc:
                request = op.throw(exc)
            else:
                request = op.send(response)
    except StopIteration as stop:
        return cast(_T, stop.value)
    finally:
        op.close()


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` 头（RFC 9110 §10.2.3）换算成秒：delta-seconds 或 HTTP-date（相对 ``now``，
    缺省为当前 UTC 时间，已过去的时刻为 0）；缺省或无法解析时为 ``None``。合法但大到无法表示的
    秒数为 ``math.inf``：它仍是「等待很久」的提示，不能退化成「没有提示」。"""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        # 超长数字串：int() 超出位数上限抛 ValueError，float() 溢出抛 OverflowError
        try:
            return float(int(text))
        except (ValueError, OverflowError):
            return math.inf
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(UTC)
    # 不带时区的时刻一律按 UTC 理解（HTTP-date 本身就是 GMT）
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def _query(**params: str | None) -> str:
    """按 RFC 3986 百分号编码的查询串；值为 ``None`` 的参数省略。"""
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items() if v is not None)


def _media_type(response: Response) -> str:
    return (response.header("content-type") or "").split(";", 1)[0].strip().lower()


def _problem(response: Response) -> Mapping[str, Any] | None:
    """problem 体（含非空字符串 ``code``）；响应体不是 problem+json 或不可解析时为 ``None``。"""
    if _media_type(response) != _PROBLEM_JSON or not response.body:
        return None
    try:
        problem = _ijson.loads(response.body)
    except DpeHashError:
        return None
    if isinstance(problem, Mapping):
        code = problem.get("code")
        if isinstance(code, str) and code:
            return problem
    return None


def _error(response: Response) -> DpeError | UnexpectedResponseError:
    """错误响应对应的异常（HTTP 绑定 §3.2、§5）：problem 体的 ``code`` 优先，其次
    ``DPE-Error-Code`` 头，都没有则不是 DPE 错误。"""
    retry_after = retry_after_seconds(response.header("retry-after"))
    problem = _problem(response)
    if problem is not None:
        return from_problem(problem, retry_after)
    code = (response.header(ERROR_CODE_HEADER) or "").strip()
    if code:
        return from_problem({"code": code}, retry_after)
    return UnexpectedResponseError(
        "错误响应既无 problem 体、也无 DPE-Error-Code 头，不是 DPE 错误",
        status=response.status,
        retry_after=retry_after,
    )


def _is_dpe_error(response: Response) -> bool:
    """响应带 DPE 错误码：problem 体的 ``code`` 或 ``DPE-Error-Code`` 头（HTTP 绑定 §5）。"""
    header = (response.header(ERROR_CODE_HEADER) or "").strip()
    return _problem(response) is not None or bool(header)


def _bare_412(response: Response) -> bool:
    """无 DPE 错误码的 412，如网关自行求值条件头的结果（HTTP 绑定 §3.2）。"""
    return response.status == 412 and not _is_dpe_error(response)


#: 网关类状态码：请求可能已转发到源站，结果未知（RFC 9110 §15.6.3 Bad Gateway、§15.6.5 Gateway
#: Timeout）。服务端自己的暂时不可用是带错误码的 ``DPE_UNAVAILABLE``（503），不在此列。
_GATEWAY_STATUSES = frozenset({502, 504})


def _gateway_lost(response: Response) -> bool:
    """无 DPE 错误码的 502 / 504：写操作按响应丢失处理（见模块说明）。"""
    return response.status in _GATEWAY_STATUSES and not _is_dpe_error(response)


def _expect(response: Response, status: int = 200) -> None:
    """响应状态码必须是 ``status``；错误状态码按 ``_error`` 抛出，其他状态码视为不合规。"""
    if response.status == status:
        return
    if response.status >= 400:
        raise _error(response)
    raise UnexpectedResponseError(f"期望 HTTP {status}", status=response.status)


def _parse(response: Response, model: type[_M], context: Mapping[str, Any] | None = None) -> _M:
    try:
        return model.model_validate(_ijson.loads(response.body), context=context)
    except (DpeHashError, PydanticValidationError) as exc:
        raise UnexpectedResponseError(
            f"响应体不是合法的 {model.__name__}：{exc}", status=response.status
        ) from None


def fetch_capabilities() -> Operation[Capabilities]:
    """``GET capabilities``（HTTP 绑定 §4.1）：唯一不带契约头的请求。"""
    response = yield Request("GET", "capabilities")
    _expect(response)
    return _parse(response, Capabilities)


@dataclass(frozen=True)
class _Parts:
    """展开文档的线上部件（快路径请求体与暂存上传共用同一份推导）。"""

    document: dict[str, Any]
    pages: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    page_by_hash: dict[str, dict[str, Any]]
    object_by_hash: dict[str, dict[str, Any]]
    #: blob 引用 → 媒体类型（元素未给 ``mime_type`` 时为 ``None``）；键集合即本文档引用的全部 blob
    blob_mime: dict[str, str | None]
    doc_hash: str
    total: int


class ProtocolCore:
    """与一个 remote 协商后的协议核心：持有选定的契约与 limits，产出各操作。

    ``capabilities`` 来自 ``fetch_capabilities``。``contract`` 必须是服务端
    ``hash_contracts`` 之一，否则抛 ``ContractUnsupportedError``（本地判定，不发请求）；
    不是 dpe_hash 认识的契约抛 ``ValueError``；服务端协议版本不是 ``dpe/1`` 抛
    ``IncompatibleServerError``。

    ``max_resends`` 是写操作在响应丢失后原样重发的次数上限（见模块说明），须为非负整数。
    """

    def __init__(
        self,
        capabilities: Capabilities,
        contract: str = dpe_hash.CONTRACT,
        *,
        max_resends: int = 2,
    ) -> None:
        if isinstance(max_resends, bool) or not isinstance(max_resends, int) or max_resends < 0:
            raise ValueError(f"max_resends 必须是非负整数，实际为 {max_resends!r}")
        if capabilities.protocol != PROTOCOL:
            raise IncompatibleServerError(
                f"服务端协议版本 {capabilities.protocol!r} 不是本 SDK 实现的 {PROTOCOL!r}"
            )
        if contract not in dpe_hash.KNOWN_CONTRACTS:
            raise ValueError(f"dpe_hash 不认识的 hash 契约：{contract!r}")
        if contract not in capabilities.hash_contracts:
            raise ContractUnsupportedError(
                f"服务端不接受 hash 契约 {contract!r}（接受 {capabilities.hash_contracts!r}）"
            )
        self.capabilities = capabilities
        self.contract = contract
        self.max_resends = max_resends

    @property
    def limits(self) -> Limits:
        return self.capabilities.limits

    def _request(
        self,
        method: str,
        target: str,
        body: Mapping[str, Any] | bytes | BlobRange | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        content_type: str = _JSON,
    ) -> Request:
        headers = {CONTRACT_HEADER: self.contract, **(headers or {})}
        payload: bytes | BlobRange | None = None
        if body is not None:
            headers["Content-Type"] = content_type
            payload = body if isinstance(body, (bytes, BlobRange)) else self._json_body(body)
        return Request(method, target, headers, payload)

    def _json_body(self, body: Mapping[str, Any]) -> bytes:
        """JSON 请求体的字节；尺寸预检与实际发送共用它（量到的字节就是要发出去的字节）。"""
        return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )

    def _wire_parts(self, document: Document) -> _Parts:
        """把展开文档拆成线上部件：文档对象、按 hash 去重的页对象与元素对象，以及 doc_hash
        与元素总数（含重复，供 delta 校验）。"""
        hashes = document.hashes(self.contract)
        data = document.model_dump()
        pages: list[dict[str, Any]] = []
        objects: list[dict[str, Any]] = []
        page_by_hash: dict[str, dict[str, Any]] = {}
        object_by_hash: dict[str, dict[str, Any]] = {}
        blob_mime: dict[str, str | None] = {}
        total = 0
        for page, page_hashes in zip(data["pages"], hashes["pages"], strict=True):
            total += len(page_hashes["elements"])
            if page_hashes["page_hash"] in page_by_hash:
                continue  # 同一页对象的元素也已全部收录
            fields = {k: v for k, v in page.items() if k != "elements"}
            wire_page = {**fields, "elements": page_hashes["elements"]}
            page_by_hash[page_hashes["page_hash"]] = wire_page
            pages.append(wire_page)
            for element, content_hash in zip(
                page["elements"], page_hashes["elements"], strict=True
            ):
                if content_hash in object_by_hash:
                    continue
                object_by_hash[content_hash] = element
                objects.append(element)
                blob = element.get("blob")
                if isinstance(blob, str):
                    mime = element.get("mime_type")
                    blob_mime.setdefault(blob, mime if isinstance(mime, str) else None)
        document_object = {k: v for k, v in data.items() if k != "pages"}
        document_object["pages"] = [p["page_hash"] for p in hashes["pages"]]
        return _Parts(
            document=document_object,
            pages=pages,
            objects=objects,
            page_by_hash=page_by_hash,
            object_by_hash=object_by_hash,
            blob_mime=blob_mime,
            doc_hash=hashes["doc_hash"],
            total=total,
        )

    def _hash(self, value: str, response: Response, what: str) -> str:
        """响应中的 hash 必须属于本次声明的契约（core §3.1）。"""
        try:
            dpe_hash.parse_hash(value, (self.contract,))
        except DpeHashError:
            raise UnexpectedResponseError(
                f"{what} {value!r} 不是契约 {self.contract} 的 hash", status=response.status
            ) from None
        return value

    def head(self, uri: str) -> Operation[str | None]:
        """``HEAD documents?uri=``：文档的 doc_hash，不存在为 ``None``（HTTP 绑定 §3.3）。"""
        response = yield self._request("HEAD", "documents?" + _query(uri=uri))
        return self._head_result(response)

    def _head_result(self, response: Response) -> str | None:
        try:
            _expect(response)
        except NotFoundError:
            return None
        value = response.header(DOC_HASH_HEADER)
        if value is None:
            raise UnexpectedResponseError(f"head 响应缺少 {DOC_HASH_HEADER}", status=200)
        return self._hash(value, response, DOC_HASH_HEADER)

    def batch_head(self, uris: Sequence[str]) -> Operation[list[str | None]]:
        """``POST heads``：按 ``batch_head_max`` 分批；结果与 ``uris`` 一一对应，不存在为 ``None``。

        ``uris`` 为空时不发请求；传入单个字符串抛 ``TypeError``（它也是 ``Sequence[str]``）。
        """
        if isinstance(uris, str):
            raise TypeError('uris 必须是 URI 的序列（如 ["s3://b/k"]），不能是单个字符串')
        size = self.limits.batch_head_max
        result: list[str | None] = []
        for start in range(0, len(uris), size):
            chunk = list(uris[start : start + size])
            response = yield self._request("POST", "heads", {"uris": chunk})
            _expect(response)
            heads = _parse(response, BatchHeads).heads
            if len(heads) != len(chunk):
                raise UnexpectedResponseError(
                    f"heads 有 {len(heads)} 项，请求了 {len(chunk)} 项", status=response.status
                )
            for i, head in enumerate(heads):
                if head is None:
                    result.append(None)
                else:
                    result.append(self._hash(head.doc_hash, response, f"heads[{i}].doc_hash"))
        return result

    def get_skeleton(self, uri: str) -> Operation[Skeleton | None]:
        """``GET documents?uri=``：文档对象与全部页对象，不存在为 ``None``。

        按声明的契约重算每个页对象的 page_hash 与文档对象的 doc_hash，必须与返回值一致（core §3）。
        响应的 ``file_uri`` MUST 等于请求 ``uri`` 的规范化形式（core §1.1，服务端以规范化形式
        返回），否则抛 ``UnexpectedResponseError``——不能把别的文档的骨架当成目标文档接受。
        ``uri`` 不合法（core §1.1 文法）时本地即抛 ``dpe_hash.ValidationError``，不发请求。
        """
        expected_uri = dpe_hash.normalize_file_uri(uri)
        response = yield self._request("GET", "documents?" + _query(uri=uri))
        try:
            _expect(response)
        except NotFoundError:
            return None
        skeleton = _parse(response, Skeleton, {"contract": self.contract})
        self._verify_skeleton(skeleton, expected_uri, response)
        return skeleton

    def _verify_skeleton(self, skeleton: Skeleton, expected_uri: str, response: Response) -> None:
        def broken(reason: str) -> UnexpectedResponseError:
            return UnexpectedResponseError(f"骨架不自洽：{reason}", status=response.status)

        if skeleton.file_uri != expected_uri:
            raise broken(
                f"file_uri {skeleton.file_uri!r} 与请求 uri 的规范化形式 {expected_uri!r} 不符"
            )
        refs = skeleton.document.pages
        if len(skeleton.pages) != len(refs):
            raise broken(f"文档对象引用 {len(refs)} 页，返回了 {len(skeleton.pages)} 个页对象")
        for i, (page, ref) in enumerate(zip(skeleton.pages, refs, strict=True)):
            if page.page_hash(self.contract) != ref:
                raise broken(f"pages[{i}] 的 page_hash 与 document.pages[{i}] 不符")
        doc_hash = self._hash(skeleton.doc_hash, response, "doc_hash")
        if skeleton.document.doc_hash(self.contract) != doc_hash:
            raise broken("文档对象重算的 doc_hash 与 doc_hash 不符")
        header = response.header(DOC_HASH_HEADER)
        if header is not None and header != doc_hash:
            raise broken(f"{DOC_HASH_HEADER} 与响应体的 doc_hash 不符")

    def list_page(
        self, prefix: str = "", cursor: str | None = None, limit: int | None = None
    ) -> Operation[ListPage]:
        """``GET documents?prefix=&cursor=&limit=`` 的一页；``next_cursor`` 为 ``None`` 表示结束。

        ``prefix`` 总是发送（空串即列出全部）：查询串中有 ``prefix`` 才是 list，有 ``uri`` 是
        get_skeleton。``limit`` 缺省取服务端的 ``list_page_max``；给出时必须是 1 到
        ``list_page_max`` 之间的 int（HTTP 绑定 §4.4），类型不对抛 ``TypeError``，越界抛
        ``ValueError``。
        """
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError(f"limit 必须是整数，实际为 {limit!r}")
            if not 1 <= limit <= self.limits.list_page_max:
                raise ValueError(
                    f"limit 必须在 1 到 {self.limits.list_page_max} 之间，实际为 {limit}"
                )
        target = "documents?" + _query(
            prefix=prefix, cursor=cursor, limit=None if limit is None else str(limit)
        )
        response = yield self._request("GET", target)
        _expect(response)
        page = _parse(response, ListPage)
        for i, entry in enumerate(page.documents):
            self._hash(entry.doc_hash, response, f"documents[{i}].doc_hash")
        return page

    # ------------------------------------------------------------------
    # 暂存路径（core §3.4、HTTP 绑定 §4.5–§4.7）
    # ------------------------------------------------------------------

    def negotiate(self, uri: str, document: Document) -> Operation[Negotiated]:
        """``POST negotiate``（HTTP 绑定 §4.5）：开启暂存会话并返回逐层缺失清单。

        只提交文档对象——规范允许附带部分页对象（MAY），本 SDK 暂不提供该开关：增量场景下
        附带会把服务端已有的页也当请求体重传，收益方向相反。``uri`` 不合法（core §1.1）时
        本地即抛 dpe_hash 的错误，不发请求。
        """
        if not isinstance(document, Document):
            raise TypeError(f"document 必须是 dpe_sdk.Document，实际为 {type(document).__name__}")
        return (yield from self._negotiate(uri, self._wire_parts(document)))

    def _negotiate(self, uri: str, parts: _Parts) -> Generator[Request, Response, Negotiated]:
        expected_uri = dpe_hash.normalize_file_uri(uri)
        body = {"file_uri": uri, "document": parts.document}
        response = yield self._request("POST", "negotiate", body)
        _expect(response)
        result = _parse(response, NegotiateResult)
        for i, page_hash in enumerate(result.missing_pages):
            self._hash(page_hash, response, f"missing_pages[{i}]")
        for i, content_hash in enumerate(result.missing_content_hashes):
            self._hash(content_hash, response, f"missing_content_hashes[{i}]")
        return Negotiated(
            session=self._session(result.staging_session, expected_uri, response),
            missing_pages=result.missing_pages,
            missing_content_hashes=result.missing_content_hashes,
        )

    def _session(self, raw: StagingSession, uri: str, response: Response) -> Session:
        """由 negotiate 响应对构造客户端会话；``expires_at`` 是 RFC 3339 UTC。"""
        try:
            expires = datetime.fromisoformat(raw.expires_at)
        except ValueError:
            raise UnexpectedResponseError(
                f"会话过期时间不是 RFC 3339：{raw.expires_at!r}", status=response.status
            ) from None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return Session(raw.id, uri, expires)

    def _renew(self, session: Session, response: Response) -> None:
        """按 ``DPE-Session-Expires`` 更新会话的过期时间（诊断用）：缺失或无法解析时沿用旧值——
        它只是副本，是否过期以服务端判定为准（core §3.4）。"""
        value = response.header(SESSION_EXPIRES_HEADER)
        if value is None:
            return
        try:
            expires = datetime.fromisoformat(value)
        except ValueError:
            return
        session.expires_at = expires if expires.tzinfo else expires.replace(tzinfo=UTC)

    def _upload_ok(self, session: Session, response: Response) -> None:
        """upload 的成功响应（201 本会话新写入 / 200 重复，HTTP 绑定 §4.6）：续期并校验，
        失败（含 410 会话不可用）按 problem 体的 code 抛出。"""
        if response.status >= 400:
            raise _error(response)
        if response.status not in (200, 201):
            raise UnexpectedResponseError("upload 期望 HTTP 200 或 201", status=response.status)
        self._renew(session, response)

    def _offset_hint(self, response: Response) -> int | None:
        """响应里可用的 ``DPE-Upload-Offset``（本会话已收字节数）；不可用时为 ``None``。"""
        value = response.header(UPLOAD_OFFSET_HEADER)
        if value is None or not (value.isascii() and value.isdigit()):
            return None
        try:
            return int(value)
        except ValueError:  # 超长数字串超出 int() 的位数上限（同 retry_after_seconds）
            return None

    def _offset(self, response: Response, what: str) -> int:
        offset = self._offset_hint(response)
        if offset is None:
            raise UnexpectedResponseError(
                f"{what} 的响应缺少合法的 {UPLOAD_OFFSET_HEADER}", status=response.status
            )
        return offset

    def _blob_ref(self, value: str, *, status: int, what: str) -> str:
        """报文中的 blob 引用必须是合法的 ``sha256:<64hex>``（契约 1 §1）。"""
        try:
            dpe_hash.parse_blob_ref(value)
        except DpeHashError:
            raise UnexpectedResponseError(
                f"{what} {value!r} 不是合法的 blob 引用", status=status
            ) from None
        return value

    def upload_page(self, session: Session, page: PageObject) -> Operation[list[str]]:
        """``PUT staging/{sid}/pages/{page_hash}``（HTTP 绑定 §4.6）：上传一个页对象（对象本身
        即请求体），返回它引用、而在去重范围与会话中都不存在的元素 content_hash。

        整段超过 ``max_payload_bytes`` 时按 §4.7 分块（块上限 ``blob_chunk_bytes``，总量上限
        ``page_max_bytes``，超限本地即拒；上游应把文档拆成更多页）。重复上传幂等（200）。
        """
        if not isinstance(page, PageObject):
            raise TypeError(f"page 必须是 dpe_sdk.PageObject，实际为 {type(page).__name__}")
        page_hash = page.page_hash(self.contract)
        payload = self._json_body(page.model_dump())
        if len(payload) > self.limits.page_max_bytes:
            raise PayloadTooLargeError(
                f"页对象 {len(payload)} 字节，超过 page_max_bytes={self.limits.page_max_bytes}；"
                "上游应把文档拆成更多页"
            )
        target = f"staging/{session.id}/pages/{page_hash}"
        if len(payload) <= self.limits.max_payload_bytes:
            response, _ = yield from self._send(self._request("PUT", target, payload))
        else:
            response = yield from self._chunked(
                session,
                target,
                len(payload),
                lambda offset, length: payload[offset : offset + length],
                content_type=_JSON,
                what=f"页对象 {page_hash}",
            )
        self._upload_ok(session, response)
        result = _parse(response, PageUploadMissing)
        for i, content_hash in enumerate(result.missing_content_hashes):
            self._hash(content_hash, response, f"missing_content_hashes[{i}]")
        return result.missing_content_hashes

    def upload_element(self, session: Session, element: ElementObject) -> Operation[list[str]]:
        """``PUT staging/{sid}/objects/{content_hash}``（HTTP 绑定 §4.6）：上传一个元素对象，
        返回它引用的缺失 blob。

        元素对象不分块（core §3.2）：超过 ``max_payload_bytes``（只可能是超大文本）本地即拒，
        切分是上游的内容决策。重复上传幂等（200）。
        """
        if not isinstance(element, ElementObject):
            raise TypeError(
                f"element 必须是 dpe_sdk.ElementObject，实际为 {type(element).__name__}"
            )
        content_hash = element.content_hash(self.contract)
        payload = self._json_body(element.model_dump())
        if len(payload) > self.limits.max_payload_bytes:
            limit = self.limits.max_payload_bytes
            raise PayloadTooLargeError(
                f"元素对象 {len(payload)} 字节，超过 max_payload_bytes={limit}；"
                "元素对象不分块，上游应切分文本"
            )
        target = f"staging/{session.id}/objects/{content_hash}"
        response, _ = yield from self._send(self._request("PUT", target, payload))
        self._upload_ok(session, response)
        result = _parse(response, ElementUploadMissing)
        for i, blob in enumerate(result.missing_blobs):
            self._blob_ref(blob, status=response.status, what=f"missing_blobs[{i}]")
        return result.missing_blobs

    def upload_blob(
        self,
        session: Session,
        source: BlobSource | bytes | bytearray | memoryview,
        *,
        media_type: str | None = None,
    ) -> Operation[None]:
        """``PUT staging/{sid}/blobs/{sha256}``（HTTP 绑定 §4.7）：整段或分块上传一个 blob。

        字节来源是 ``BlobSource``（大文件：字节不进核心，分块时只描述区间）或内存 bytes
        （ref 与 size 本地算出）。总量超过 ``blob_max_bytes`` 本地即拒；不超过
        ``max_payload_bytes`` 时一次 PUT（幂等，响应丢失原样重发），否则按 §4.7 分块并遵守
        「响应不确定先断点查询」的客户端义务。``media_type`` 缺省 ``application/octet-stream``。
        """
        blob = self._blob_source(source)
        if blob.size > self.limits.blob_max_bytes:
            raise PayloadTooLargeError(
                f"blob {blob.size} 字节，超过 blob_max_bytes={self.limits.blob_max_bytes}"
            )
        content_type = media_type or "application/octet-stream"
        target = f"staging/{session.id}/blobs/{blob.ref}"
        if blob.size <= self.limits.max_payload_bytes:
            response, _ = yield from self._send(
                self._request(
                    "PUT", target, BlobRange(blob, 0, blob.size), content_type=content_type
                )
            )
        else:
            response = yield from self._chunked(
                session,
                target,
                blob.size,
                lambda offset, length: BlobRange(blob, offset, length),
                content_type=content_type,
                what=f"blob {blob.ref}",
            )
        self._upload_ok(session, response)
        return None

    def _blob_source(
        self, source: BlobSource | bytes | bytearray | memoryview, expected: str | None = None
    ) -> BlobSource:
        """归一化 blob 来源并做本地校验：``ref`` 必须是 ``sha256:…``；``expected`` 给出时
        （补传场景）必须与它相符，否则是调用方接错了字节来源，抛 ``ValueError``。"""
        if isinstance(source, (bytes, bytearray, memoryview)):
            source = BlobSource.from_bytes(source)
        if not isinstance(source, BlobSource):
            raise TypeError(f"blob 来源必须是 BlobSource 或 bytes，实际为 {type(source).__name__}")
        try:
            dpe_hash.parse_blob_ref(source.ref)
        except DpeHashError:
            raise ValueError(f"blob 来源的 ref 不是 sha256 引用：{source.ref!r}") from None
        if expected is not None and source.ref != expected:
            raise ValueError(f"blob 来源的 ref {source.ref} 与需要的 {expected} 不符")
        return source

    def _resume(
        self, session: Session, target: str, total: int
    ) -> Generator[Request, Response, int]:
        """断点查询（HTTP 绑定 §4.7）：HEAD 同一 URL，返回本会话已收字节数（无进度为 0）。

        只读、不续期；会话不可用（410）原样抛 ``SessionExpiredError``，由调用方重开 negotiate。
        """
        response, _ = yield from self._send(self._request("HEAD", target))
        if response.status >= 400:
            raise _error(response)
        if response.status != 200:
            raise UnexpectedResponseError("断点查询期望 HTTP 200", status=response.status)
        self._renew(session, response)
        offset = self._offset(response, target)
        if offset > total:
            raise UnexpectedResponseError(
                f"{target} 的已收偏移 {offset} 超过总量 {total}", status=response.status
            )
        return offset

    def _chunked(
        self,
        session: Session,
        target: str,
        total: int,
        at: Callable[[int, int], bytes | BlobRange],
        *,
        content_type: str,
        what: str,
    ) -> Generator[Request, Response, Response]:
        """分块上传与恢复（HTTP 绑定 §4.7，页对象与 blob 共用），返回完成的响应（200/201）。

        ``at(offset, length)`` 给出该区间的请求体（blob 是延迟的 ``BlobRange``，页对象是切好的
        内存字节）。任何一块的响应丢失或不确定（驱动抛回的异常、无错误码的 502/504）时 MUST NOT
        盲目重发：先断点查询；查到的偏移等于声明的总量即上传已完成，重发末块（非空区间）取
        缺失清单；偏移不连续（``DPE_VALIDATION`` 且响应带 ``DPE-Upload-Offset``）直接按该头
        重同步。每一步恢复计入 ``max_resends`` 预算，用尽后如实抛出。

        ``202`` 表示「已追加本块、未到齐」（HTTP 绑定 §4.7 第 7 步）：偏移必须恰好前进本块的
        长度且严格小于总量，否则服务端在重复旧状态——继续重发同一个块会陷入无进展的循环，
        因此判不合规（每轮 202 严格前进一块，循环由结构保证有界，不占用恢复预算）。
        """
        chunk_size = min(self.limits.blob_chunk_bytes, self.limits.max_payload_bytes)
        offset = 0
        last = 0
        recoveries = self.max_resends
        while True:
            if offset < total:
                last = offset  # 末块的起点（偏移等于总量时重发它取缺失清单，不发空区间）
            from_byte = offset if offset < total else last
            length = min(chunk_size, total - from_byte)
            request = self._request(
                "PUT",
                target,
                at(from_byte, length),
                {"Content-Range": f"bytes {from_byte}-{from_byte + length - 1}/{total}"},
                content_type=content_type,
            )
            try:
                response = yield request
            except Exception:
                if recoveries <= 0:
                    raise
                recoveries -= 1
                offset = yield from self._resume(session, target, total)
                continue
            if _gateway_lost(response):
                if recoveries <= 0:
                    raise _error(response)
                recoveries -= 1
                offset = yield from self._resume(session, target, total)
                continue
            if response.status == 202:
                self._renew(session, response)
                offset = self._offset(response, what)
                appended = from_byte + length
                if offset != appended or offset >= total:
                    raise UnexpectedResponseError(
                        f"{what} 的 202 已收偏移 {offset}：本块追加后应为 {appended} 且未到齐"
                        f"（总量 {total}）",
                        status=response.status,
                    )
                continue
            if response.status < 400:
                return response
            error = _error(response)
            hint = self._offset_hint(response)
            if isinstance(error, ValidationError) and hint is not None:
                # 偏移不连续的错误响应带本会话已收字节数：直接据它重同步（免一次断点查询）
                if hint > total:
                    raise UnexpectedResponseError(
                        f"{what} 的偏移重同步值 {hint} 超过总量 {total}", status=response.status
                    )
                if recoveries > 0:
                    recoveries -= 1
                    offset = hint
                    continue
            raise error

    # ------------------------------------------------------------------
    # 写操作（core §3.3、§4、§5）
    # ------------------------------------------------------------------

    def commit(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        session: Session | None = None,
    ) -> Operation[CommitResult]:
        """``PUT documents?uri=``（core §3.2）：内联快路径，或引用暂存会话提交。

        - 不给 ``session``：内联文档对象、全部页对象与元素对象，一次往返完成（页与元素按
          hash 去重后各发一份）；
        - 给 ``session``：请求体只含文档对象与 ``staging_session``——页与元素由暂存会话或
          服务端的去重范围提供（大文档的暂存路径：``negotiate`` → ``upload_*`` → 这里）。

        前置条件三选一（``CommitPrecondition``），
        ``BaseHash`` 的值必须是本次声明契约的 hash；``uri`` 或 ``base_hash`` 不合法时本地即抛
        dpe_hash 的错误，不发请求。

        结果的 ``doc_hash`` 必须等于本地算出的提交内容的 doc_hash，``delta`` 必须与提交的元素
        个数和 ``status`` 自洽，否则抛 ``UnexpectedResponseError``。重试规则（core §5.2、HTTP
        绑定 §3.2）：

        - 非 force 的响应丢失时原样重发；首次已生效则得到 ``unchanged``（首次的 delta 不可得）；
        - force 的响应丢失（含无 DPE 错误码的 502 / 504）时不重放：先 ``head``，等于提交内容即
          成功，否则抛 ``ForceNotConfirmedError``；
        - 无 DPE 错误码的 412 先 ``head`` 判定（只对带条件头的提交；``Force`` 不带条件头，412
          无从谈起，直接按非协议错误上报）：等于提交内容即成功；``IfAbsent`` 时文档存在即抛
          ``AlreadyExistsError``，不存在则前置条件实际成立（中间层误判），原样重发一次，重发后
          仍是这样的 412 时再 ``head`` 判定一次、文档仍不存在才抛 ``UnexpectedResponseError``
          （按非协议错误上报，绝不误报 ``NotFoundError``）；``BaseHash`` 时不存在抛
          ``NotFoundError``，否则抛 ``PreconditionFailedError``。

        经 ``head`` 确认的成功返回 ``unchanged``，与原样重发得到的结果相同。
        ``DPE_PRECONDITION_FAILED`` 等错误如实抛出，绝不自动改用 force。
        """
        if not isinstance(document, Document):
            raise TypeError(f"document 必须是 dpe_sdk.Document，实际为 {type(document).__name__}")
        dpe_hash.normalize_file_uri(uri)
        self._condition(precondition)
        parts = self._wire_parts(document)
        body: dict[str, Any] = {"document": parts.document}
        if session is not None:
            body["staging_session"] = session.id
        else:
            body["pages"] = parts.pages
            body["objects"] = parts.objects
        if isinstance(precondition, Force):
            body["force"] = True
        return (yield from self._commit(uri, body, precondition, parts.doc_hash, parts.total))

    def _commit(
        self,
        uri: str,
        body: Mapping[str, Any],
        precondition: CommitPrecondition,
        expected: str,
        total: int,
    ) -> Operation[CommitResult]:
        """commit 的请求与结果处理（请求体已构造）：force、裸 412 与响应丢失的规则见 ``commit``。"""
        headers = self._condition(precondition)
        request = self._request("PUT", "documents?" + _query(uri=uri), body, headers)
        if isinstance(precondition, Force):
            try:
                response = yield request
            except Exception as exc:
                return (yield from self._confirm_force(uri, expected, total, exc))
            if _gateway_lost(response):
                lost = _error(response)
                return (yield from self._confirm_force(uri, expected, total, lost))
            return self._commit_result(response, expected, total)
        response, _ = yield from self._send(request)
        if _bare_412(response):
            if isinstance(precondition, IfAbsent):
                # 文档不存在时 if_absent 的前置条件实际成立：412 只能出自中间层误判，
                # 原样重发一次；重发后仍如此则按非协议错误上报，绝不报 DPE_NOT_FOUND
                response, current, _ = yield from self._retry_misjudged_412(
                    request,
                    uri,
                    response,
                    holds=lambda head: head is None,
                    held="文档仍不存在（if_absent 的前置条件成立）",
                )
                if _bare_412(response):
                    if current == expected:
                        return _recovered(expected, total)
                    raise AlreadyExistsError(f"{uri} 已存在（412 无 DPE 错误码，经 head 判定）")
            else:
                current = yield from self._confirm_head(uri)
                if current == expected:
                    return _recovered(expected, total)
                if current is None:
                    raise NotFoundError(f"{uri} 不存在（412 无 DPE 错误码，经 head 判定）")
                raise PreconditionFailedError(
                    f"expected {precondition.value}, current {current}"
                    "（412 无 DPE 错误码，经 head 判定）"
                )
        return self._commit_result(response, expected, total)

    def deliver(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        blobs: Callable[[str], BlobSource | bytes] | None = None,
        session: Session | None = None,
    ) -> Operation[CommitResult]:
        """投递一篇文档（core §3.2）：自动选择快路径或暂存路径，只传变更。

        - 内联请求体不超过 ``max_payload_bytes`` 时走快路径（一次 ``commit``）；否则逐层协商
          （negotiate → 缺失页 → 缺失元素 → 缺失 blob → commit 引用会话），只传缺失的部分；
        - ``DPE_MISSING_CONTENT``：按分层清单把缺失对象补传进**同一会话**后重新 commit；快路径
          下用「内联 + staging_session」的请求体，其余对象不必重传（blob 永远不能内联，是快路径
          唯一可能缺的一层）；
        - ``DPE_SESSION_EXPIRED``：重开 negotiate，新会话从空开始、按重算的缺失清单重走；
        - ``blobs``：缺失 blob 的字节来源（``sha256:…`` → ``BlobSource`` 或 bytes）；缺省时
          若确需上传 blob，如实抛 ``DPE_MISSING_CONTENT``；
        - ``session``：复用的暂存会话（core §3.4：CAS 失败后重新读取、以新前置条件引用同一
          会话重提，免重传）。会话只作缓存：失效时自动重开，结果与不复用时一致；
        - 恢复轮预算（``max_resends``）用尽时抛出当时的 ``MissingContentError`` /
          ``SessionExpiredError``；异常带 ``session``，上层可重新读取后以
          ``deliver(..., session=exc.session)`` 继续，结果与不限预算一致。

        **绝不自动 force**：``DPE_PRECONDITION_FAILED`` / ``DPE_ALREADY_EXISTS`` /
        ``DPE_NOT_FOUND`` 等原样抛出，不做恢复；只处理上述两类。抛出的异常带 ``session``
        属性（当前会话，可能仍可复用；见 ``dpe_sdk.errors.DpeError.session``）。
        """
        if not isinstance(document, Document):
            raise TypeError(f"document 必须是 dpe_sdk.Document，实际为 {type(document).__name__}")
        dpe_hash.normalize_file_uri(uri)
        self._condition(precondition)
        parts = self._wire_parts(document)
        if session is not None:
            return (yield from self._staged(uri, parts, precondition, session, blobs, None))
        inline = self._inline_body(parts, force=isinstance(precondition, Force))
        if len(self._json_body(inline)) <= self.limits.max_payload_bytes:
            return (yield from self._inline(uri, parts, inline, precondition, blobs))
        negotiated = yield from self._negotiate(uri, parts)
        return (
            yield from self._staged(uri, parts, precondition, negotiated.session, blobs, negotiated)
        )

    def _inline_body(self, parts: _Parts, *, force: bool) -> dict[str, Any]:
        """快路径请求体（内联文档对象、全部页对象与元素对象）。"""
        body: dict[str, Any] = {
            "document": parts.document,
            "pages": parts.pages,
            "objects": parts.objects,
        }
        if force:
            body["force"] = True
        return body

    def _inline(
        self,
        uri: str,
        parts: _Parts,
        inline: dict[str, Any],
        precondition: CommitPrecondition,
        blobs: Callable[[str], BlobSource | bytes] | None,
    ) -> Operation[CommitResult]:
        """快路径，含缺内容的恢复：开会话把缺失对象补进去，再以「内联 + staging_session」重提。"""
        body = dict(inline)
        session: Session | None = None
        pending: MissingContentError | None = None
        budget = self.max_resends
        try:
            while True:
                try:
                    if pending is not None:
                        assert session is not None  # pending 只在会话开出后设置
                        yield from self._upload_manifest(session, pending.missing, parts, blobs)
                        pending = None
                    return (
                        yield from self._commit(
                            uri, body, precondition, parts.doc_hash, parts.total
                        )
                    )
                except MissingContentError as exc:
                    if isinstance(exc, _BlobsUnavailable):
                        raise  # 本地判定：直接逃出恢复循环，不消耗预算
                    if budget <= 0:
                        raise
                    budget -= 1
                    if session is None:
                        session = (yield from self._negotiate(uri, parts)).session
                        recovery = self._recovery_body(inline, session)
                        if recovery is None:
                            return (
                                yield from self._staged(
                                    uri, parts, precondition, session, blobs, None
                                )
                            )
                        body = recovery
                    pending = exc
                except SessionExpiredError:
                    if budget <= 0:
                        raise
                    budget -= 1
                    session = (yield from self._negotiate(uri, parts)).session
                    recovery = self._recovery_body(inline, session)
                    if recovery is None:
                        return (
                            yield from self._staged(uri, parts, precondition, session, blobs, None)
                        )
                    body = recovery
        except Exception as exc:
            _attach_session(exc, session)
            raise

    def _recovery_body(self, inline: dict[str, Any], session: Session) -> dict[str, Any] | None:
        """快路径恢复的请求体（内联 + ``staging_session``）；加上会话 id 会超过
        ``max_payload_bytes`` 时为 ``None``——会话已开出，改由暂存路径继续（它的请求体只含文档
        对象。传输层按整段请求体判 413，见 core §3.3 第 0 步）。"""
        body = {**inline, "staging_session": session.id}
        if len(self._json_body(body)) > self.limits.max_payload_bytes:
            return None
        return body

    def _staged(
        self,
        uri: str,
        parts: _Parts,
        precondition: CommitPrecondition,
        session: Session,
        blobs: Callable[[str], BlobSource | bytes] | None,
        negotiated: Negotiated | None,
    ) -> Operation[CommitResult]:
        """暂存路径：逐层补传后 commit 引用会话；两类恢复见 ``deliver``。

        ``negotiated`` 为 ``None`` 表示会话由调用方提供（内容应已在其中）：直接 commit，
        缺什么由缺失清单驱动补传；否则先按 negotiate 的清单补传再 commit。
        """
        body: dict[str, Any] = {"document": parts.document, "staging_session": session.id}
        if isinstance(precondition, Force):
            body["force"] = True
        pending_negotiated = negotiated
        pending_missing: MissingContentError | None = None
        budget = self.max_resends
        try:
            while True:
                try:
                    if pending_negotiated is not None:
                        manifest = Missing(
                            pages=pending_negotiated.missing_pages,
                            content_hashes=pending_negotiated.missing_content_hashes,
                        )
                        yield from self._upload_manifest(session, manifest, parts, blobs)
                        pending_negotiated = None
                    if pending_missing is not None:
                        yield from self._upload_manifest(
                            session, pending_missing.missing, parts, blobs
                        )
                        pending_missing = None
                    return (
                        yield from self._commit(
                            uri, body, precondition, parts.doc_hash, parts.total
                        )
                    )
                except MissingContentError as exc:
                    if isinstance(exc, _BlobsUnavailable):
                        raise  # 本地判定：直接逃出恢复循环，不消耗预算
                    if budget <= 0:
                        raise
                    budget -= 1
                    pending_missing = exc
                except SessionExpiredError:
                    if budget <= 0:
                        raise
                    budget -= 1
                    negotiated = yield from self._negotiate(uri, parts)
                    session = negotiated.session
                    body = {"document": parts.document, "staging_session": session.id}
                    if isinstance(precondition, Force):
                        body["force"] = True
                    pending_negotiated = negotiated
                    pending_missing = None
        except Exception as exc:
            _attach_session(exc, session)
            raise

    def _upload_manifest(
        self,
        session: Session,
        missing: Missing,
        parts: _Parts,
        blobs: Callable[[str], BlobSource | bytes] | None,
    ) -> Operation[None]:
        """按分层清单补传：页 →（各页响应给出的）元素 →（各元素响应给出的）blob。

        清单里的 hash 必须属于本文档引用的对象，否则是服务端问题，抛 ``UnexpectedResponseError``；
        重复的 hash 只上传一次。清单被服务端截断时先补传已列出的部分——剩下的会在重新 commit
        后的下一份清单里给出。
        """
        seen_pages: set[str] = set()
        seen_objects: set[str] = set()
        objects: list[str] = list(missing.content_hashes)
        blobs_needed: list[str] = list(missing.blobs)
        for page_hash in missing.pages:
            if page_hash in seen_pages:
                continue
            seen_pages.add(page_hash)
            page = parts.page_by_hash.get(page_hash)
            if page is None:
                raise UnexpectedResponseError(
                    f"缺失清单里的页 {page_hash} 不是本次提交引用的对象", status=400
                )
            missing_elements = yield from self.upload_page(
                session, PageObject.model_validate(page, context={"contract": self.contract})
            )
            objects.extend(missing_elements)
        for content_hash in objects:
            if content_hash in seen_objects:
                continue
            seen_objects.add(content_hash)
            element = parts.object_by_hash.get(content_hash)
            if element is None:
                raise UnexpectedResponseError(
                    f"缺失清单里的元素 {content_hash} 不是本次提交引用的对象", status=400
                )
            missing_blobs = yield from self.upload_element(
                session,
                ElementObject.model_validate(element, context={"contract": self.contract}),
            )
            blobs_needed.extend(missing_blobs)
        if not blobs_needed:
            return
        pending = list(dict.fromkeys(blobs_needed))
        for blob in pending:
            # 清单来自 400 的 DPE_MISSING_CONTENT：格式与成员资格先于取字节校验，口径同页与元素
            self._blob_ref(blob, status=400, what="缺失清单里的 blob")
            if blob not in parts.blob_mime:
                raise UnexpectedResponseError(
                    f"缺失清单里的 blob {blob} 不是本次提交引用的对象", status=400
                )
        if blobs is None:
            raise _BlobsUnavailable(
                "缺少 blob 的字节来源（deliver 的 blobs 参数或直接 upload_blob），无法补传",
                missing=Missing(blobs=pending),
            )
        for blob in pending:
            yield from self.upload_blob(
                session,
                self._blob_source(blobs(blob), expected=blob),
                media_type=parts.blob_mime[blob],
            )

    def delete(self, uri: str, base_hash: str) -> Operation[None]:
        """``DELETE documents?uri=``，CAS 走 ``If-Match``（core §4、§5.1）。

        ``base_hash`` 必须是本次声明契约的 hash。重试规则（core §5.2、HTTP 绑定 §3.2）：响应
        丢失时原样重发，重发后得到 ``DPE_NOT_FOUND`` 视为成功（首次请求得到它则如实抛出）；
        无 DPE 错误码的 412 先 ``head``：不存在即成功，doc_hash 不等于 ``base_hash`` 抛
        ``PreconditionFailedError``，仍等于则是中间层误判，原样重发一次；重发后仍是这样的 412
        时再 ``head`` 判定一次，doc_hash 仍等于 ``base_hash`` 才抛 ``UnexpectedResponseError``。
        """
        dpe_hash.normalize_file_uri(uri)
        self._check_base_hash(base_hash)
        request = self._request(
            "DELETE", "documents?" + _query(uri=uri), headers={"If-Match": f'"{base_hash}"'}
        )
        response, resent = yield from self._send(request)
        response, current, retried = yield from self._retry_misjudged_412(
            request,
            uri,
            response,
            holds=lambda head: head == base_hash,
            held="doc_hash 等于 If-Match",
        )
        resent = resent or retried
        if _bare_412(response):
            if current is None:
                return None
            raise PreconditionFailedError(
                f"expected {base_hash}, current {current}（412 无 DPE 错误码，经 head 判定）"
            )
        try:
            _expect(response, 204)
        except NotFoundError:
            if resent:
                return None  # 目标状态「不存在」已达成（core §5.2）
            raise
        return None

    def move(self, from_uri: str, to_uri: str, base_hash: str) -> Operation[MoveResult]:
        """``POST move``：``from_uri → to_uri`` 原子改名，前置条件在请求体（core §5.2）。

        ``base_hash`` 必须是本次声明契约的 hash；move 不支持 force。响应丢失时原样重发：
        服务端对「源已不存在、目标内容等于 ``base_hash``」返回成功（core §5.2 第 4 步），其余
        错误如实抛出。结果的 ``doc_hash`` 必须等于 ``base_hash``（file_uri 不进 hash）。
        """
        dpe_hash.normalize_file_uri(from_uri)
        dpe_hash.normalize_file_uri(to_uri)
        self._check_base_hash(base_hash)
        body = {"from_uri": from_uri, "to_uri": to_uri, "base_hash": base_hash}
        response, _ = yield from self._send(self._request("POST", "move", body))
        _expect(response)
        result = _parse(response, MoveResult)
        if result.doc_hash != base_hash:
            raise UnexpectedResponseError(
                f"move 结果的 doc_hash {result.doc_hash!r} 不等于 base_hash {base_hash!r}",
                status=response.status,
            )
        self._check_doc_hash_header(response, base_hash)
        return result

    def _send(self, request: Request) -> Generator[Request, Response, tuple[Response, bool]]:
        """发送 ``request``，驱动方抛回的异常与无 DPE 错误码的 502 / 504 视为响应丢失，原样重发
        至多 ``max_resends`` 次；仍失败则抛出最后一次的异常，或返回最后一次的网关响应（由调用方
        按非协议错误上报）。返回响应与它是否来自重发。"""
        resends = 0
        while True:
            try:
                response = yield request
            except Exception:
                if resends >= self.max_resends:
                    raise
            else:
                if not _gateway_lost(response) or resends >= self.max_resends:
                    return response, resends > 0
            resends += 1

    def _confirm_head(self, uri: str) -> Generator[Request, Response, str | None]:
        """写操作结果不确定时的 ``head``（只读，响应丢失时同样原样重发）。"""
        response, _ = yield from self._send(self._request("HEAD", "documents?" + _query(uri=uri)))
        return self._head_result(response)

    def _confirm_force(
        self, uri: str, expected: str, total: int, lost: Exception
    ) -> Generator[Request, Response, CommitResult]:
        """force 的响应丢失（core §5.2）：不重放，``head`` 等于提交内容即成功，否则上报。"""
        try:
            current = yield from self._confirm_head(uri)
        except Exception as exc:
            raise ForceNotConfirmedError(
                "force 提交的响应丢失，head 也失败，无法确认是否生效",
                doc_hash=expected,
                current=None,
                checked=False,
            ) from exc
        if current == expected:
            return _recovered(expected, total)
        raise ForceNotConfirmedError(
            f"force 提交的响应丢失，当前 doc_hash 为 {current!r}，不等于提交内容 {expected!r}",
            doc_hash=expected,
            current=current,
            checked=True,
        ) from lost

    def _retry_misjudged_412(
        self,
        request: Request,
        uri: str,
        response: Response,
        *,
        holds: Callable[[str | None], bool],
        held: str,
    ) -> Generator[Request, Response, tuple[Response, str | None, bool]]:
        """裸 412 的中间层误判重试（HTTP 绑定 §3.2）：``head`` 判定后前置条件实际成立
        （``holds`` 为真）而目标状态未达成时，原样重发一次；重发后仍是裸 412 时再 ``head`` 判定
        一次，条件仍成立才按非协议错误上报——MUST NOT 猜成任何 DPE code。

        返回 ``(response, current, retried)``：``response`` 仍是裸 412 时 ``current`` 是最后一次
        ``head`` 的读数（文档不存在为 ``None``），条件不成立的情形交调用方按各自的 code 判定；
        ``response`` 已不是裸 412（重发成功或得到带码错误）时 ``current`` 无意义。``retried``
        只计本循环的重发，``_send`` 因响应丢失的重发由调用方自行合并。
        """
        retried = False
        while _bare_412(response):
            current = yield from self._confirm_head(uri)
            if not holds(current):
                return response, current, retried
            if retried:
                raise UnexpectedResponseError(
                    f"{uri}：{held}，重发后仍是无 DPE 错误码的 412", status=412
                )
            retried = True
            response, _ = yield from self._send(request)
        return response, None, retried

    def _check_base_hash(self, value: str) -> None:
        """``base_hash`` 必须是本次声明契约的 hash：不接受裸 hash，也不接受别的契约的值。"""
        if not isinstance(value, str):
            raise TypeError(f"base_hash 必须是字符串，实际为 {type(value).__name__}")
        dpe_hash.parse_hash(value, (self.contract,))

    def _condition(self, precondition: CommitPrecondition) -> dict[str, str]:
        """前置条件对应的条件头（HTTP 绑定 §3.2）；force 不带条件头。"""
        if isinstance(precondition, BaseHash):
            self._check_base_hash(precondition.value)
            return {"If-Match": f'"{precondition.value}"'}
        if isinstance(precondition, IfAbsent):
            return {"If-None-Match": "*"}
        if isinstance(precondition, Force):
            return {}
        raise TypeError(
            "写操作必须带前置条件：BaseHash、IfAbsent 或 Force 之一，"
            f"实际为 {type(precondition).__name__}"
        )

    def _commit_result(self, response: Response, expected: str, total: int) -> CommitResult:
        """commit 响应（HTTP 绑定 §4.8）：201 即 created，200 即 updated / unchanged。"""
        if response.status >= 400:
            raise _error(response)
        if response.status not in (200, 201):
            raise UnexpectedResponseError("commit 期望 HTTP 200 或 201", status=response.status)
        result = _parse(response, CommitResult)

        def broken(reason: str) -> UnexpectedResponseError:
            return UnexpectedResponseError(f"commit 结果不自洽：{reason}", status=response.status)

        if (response.status == 201) != (result.status == "created"):
            raise broken(f"status {result.status!r} 与 HTTP {response.status} 不符")
        if result.doc_hash != expected:
            raise broken(f"doc_hash {result.doc_hash!r} 不等于提交内容的 {expected!r}")
        delta = result.delta
        if delta.added + delta.retained != total:
            raise broken(f"added + retained 应为提交的元素个数 {total}")
        if result.status == "created" and (delta.removed or delta.retained):
            raise broken("created 时旧文档为空，removed 与 retained 应为 0")
        if result.status == "unchanged" and (delta.added or delta.removed):
            raise broken("unchanged 时 added 与 removed 应为 0")
        self._check_doc_hash_header(response, expected)
        return result

    def _check_doc_hash_header(self, response: Response, expected: str) -> None:
        """``DPE-Doc-Hash`` 出现时必须等于结果的 doc_hash。缺失时宽容（同 ``get_skeleton``）：
        权威值在响应体中，头只是副本；服务端漏写这个 MUST 头由一致性跑分器拦截。"""
        header = response.header(DOC_HASH_HEADER)
        if header is not None and header != expected:
            raise UnexpectedResponseError(
                f"{DOC_HASH_HEADER} {header!r} 与结果的 doc_hash 不符", status=response.status
            )


class _BlobsUnavailable(MissingContentError):
    """本地判定：缺失的 blob 没有字节来源（``deliver`` 未给 ``blobs``）。

    与「服务端要求补传」（``DPE_MISSING_CONTENT`` 响应）区分：它直接逃出 ``deliver`` 的恢复
    循环，不消耗恢复预算、不空转；对外仍是带 ``missing`` 的 ``MissingContentError``。
    """


def _attach_session(exc: BaseException, session: Session | None) -> None:
    """把当前暂存会话附到异常上（``deliver`` 的失败契约）：本库异常直接设置属性，
    外部异常尽力而为（不允许设置属性时放弃）。"""
    with contextlib.suppress(AttributeError, TypeError):
        cast(Any, exc).session = session


def _recovered(doc_hash: str, total: int) -> CommitResult:
    """经 ``head`` 确认的成功：与原样重发得到的 ``unchanged`` 相同，首次写入的 delta 不可得
    （core §5.2）。"""
    return CommitResult(
        status="unchanged", doc_hash=doc_hash, delta=Delta(added=0, removed=0, retained=total)
    )
