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
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeAlias, TypeVar, cast
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
    NotFoundError,
    PreconditionFailedError,
    UnexpectedResponseError,
    from_problem,
)
from dpe_sdk.models import Document
from dpe_sdk.wire import (
    BatchHeads,
    Capabilities,
    CommitResult,
    Delta,
    Limits,
    ListPage,
    MoveResult,
    Skeleton,
)

__all__ = [
    "CONTRACT_HEADER",
    "DOC_HASH_HEADER",
    "ERROR_CODE_HEADER",
    "PROTOCOL",
    "BaseHash",
    "CommitPrecondition",
    "Force",
    "IfAbsent",
    "Operation",
    "ProtocolCore",
    "Request",
    "Response",
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

_JSON = "application/json"
_PROBLEM_JSON = "application/problem+json"

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True)
class Request:
    """请求描述。``target`` 是相对 remote 的路径与已编码的查询串（HTTP 绑定 §1），传输层把它
    原样拼在 remote 之后（remote 不带尾部 ``/``），MUST NOT 再次编码。"""

    method: str
    target: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


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
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Request:
        headers = {CONTRACT_HEADER: self.contract, **(headers or {})}
        payload = None
        if body is not None:
            headers["Content-Type"] = _JSON
            payload = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        return Request(method, target, headers, payload)

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
    # 写操作（core §3.3、§4、§5）
    # ------------------------------------------------------------------

    def commit(
        self, uri: str, document: Document, precondition: CommitPrecondition
    ) -> Operation[CommitResult]:
        """``PUT documents?uri=`` 快路径（core §3.2）：内联文档对象、全部页对象与元素对象。

        页对象与元素对象按 hash 去重后各发一份。前置条件三选一（``CommitPrecondition``），
        ``BaseHash`` 的值必须是本次声明契约的 hash；``uri`` 或 ``base_hash`` 不合法时本地即抛
        dpe_hash 的错误，不发请求。

        结果的 ``doc_hash`` 必须等于本地算出的提交内容的 doc_hash，``delta`` 必须与提交的元素
        个数和 ``status`` 自洽，否则抛 ``UnexpectedResponseError``。重试规则（core §5.2、HTTP
        绑定 §3.2）：

        - 非 force 的响应丢失时原样重发；首次已生效则得到 ``unchanged``（首次的 delta 不可得）；
        - force 的响应丢失（含无 DPE 错误码的 502 / 504）时不重放：先 ``head``，等于提交内容即
          成功，否则抛 ``ForceNotConfirmedError``；
        - 无 DPE 错误码的 412 先 ``head`` 判定：等于提交内容即成功，不存在抛 ``NotFoundError``，
          否则抛 ``PreconditionFailedError``（``IfAbsent`` 时为 ``AlreadyExistsError``）。
          （``IfAbsent`` 且文档不存在时按规范字面抛 ``NotFoundError``，规范待补正：#81。）

        经 ``head`` 确认的成功返回 ``unchanged``，与原样重发得到的结果相同。
        ``DPE_PRECONDITION_FAILED`` 等错误如实抛出，绝不自动改用 force。
        """
        if not isinstance(document, Document):
            raise TypeError(f"document 必须是 dpe_sdk.Document，实际为 {type(document).__name__}")
        dpe_hash.normalize_file_uri(uri)
        headers = self._condition(precondition)
        body, expected, total = self._commit_body(document, force=isinstance(precondition, Force))
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
            current = yield from self._confirm_head(uri)
            if current == expected:
                return _recovered(expected, total)
            if current is None:
                raise NotFoundError(f"{uri} 不存在（412 无 DPE 错误码，经 head 判定）")
            if isinstance(precondition, IfAbsent):
                raise AlreadyExistsError(f"{uri} 已存在（412 无 DPE 错误码，经 head 判定）")
            raise PreconditionFailedError(
                f"expected {precondition.value}, current {current}"
                "（412 无 DPE 错误码，经 head 判定）"
            )
        return self._commit_result(response, expected, total)

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
        retried = False
        while _bare_412(response):
            current = yield from self._confirm_head(uri)
            if current is None:
                return None
            if current != base_hash:
                raise PreconditionFailedError(
                    f"expected {base_hash}, current {current}（412 无 DPE 错误码，经 head 判定）"
                )
            if retried:
                raise UnexpectedResponseError(
                    "doc_hash 等于 If-Match，重发后仍是无 DPE 错误码的 412", status=412
                )
            retried = resent = True
            response, _ = yield from self._send(request)
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

    def _commit_body(self, document: Document, *, force: bool) -> tuple[dict[str, Any], str, int]:
        """快路径请求体、提交内容的 doc_hash 与元素总数（含重复）。"""
        hashes = document.hashes(self.contract)
        data = document.model_dump()
        pages: list[dict[str, Any]] = []
        objects: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        seen_objects: set[str] = set()
        total = 0
        for page, page_hashes in zip(data["pages"], hashes["pages"], strict=True):
            total += len(page_hashes["elements"])
            if page_hashes["page_hash"] in seen_pages:
                continue  # 同一页对象的元素也已全部收录
            seen_pages.add(page_hashes["page_hash"])
            fields = {k: v for k, v in page.items() if k != "elements"}
            pages.append({**fields, "elements": page_hashes["elements"]})
            for element, content_hash in zip(
                page["elements"], page_hashes["elements"], strict=True
            ):
                if content_hash not in seen_objects:
                    seen_objects.add(content_hash)
                    objects.append(element)
        document_object = {k: v for k, v in data.items() if k != "pages"}
        document_object["pages"] = [p["page_hash"] for p in hashes["pages"]]
        body: dict[str, Any] = {"document": document_object, "pages": pages, "objects": objects}
        if force:
            body["force"] = True
        return body, hashes["doc_hash"], total

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


def _recovered(doc_hash: str, total: int) -> CommitResult:
    """经 ``head`` 确认的成功：与原样重发得到的 ``unchanged`` 相同，首次写入的 delta 不可得
    （core §5.2）。"""
    return CommitResult(
        status="unchanged", doc_hash=doc_hash, delta=Delta(added=0, removed=0, retained=total)
    )
