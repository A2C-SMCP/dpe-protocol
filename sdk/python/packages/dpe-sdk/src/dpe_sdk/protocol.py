"""sans-IO 协议核心（core §3、HTTP 绑定）：只产出请求描述、消费响应描述，不做任何 I/O。

**操作是生成器协程**（``Operation[T]``）：每产出一个 ``Request``，驱动方发送它并把 ``Response``
送回，操作最终 ``return`` 结果或抛出异常。多轮往返（batch_head 分批等）都在操作内部完成，
同步与异步传输只需各写一个驱动循环（``drive`` / ``adrive``）。操作也可能零往返直接返回。

**驱动约定**：发送请求时抛出的 ``Exception``（连接失败、超时等）由驱动方 ``throw`` 回操作，
操作不处理时原样传出；需要据此判定结果的操作（如 force 提交的响应丢失）在操作内部处理。

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
    ContractUnsupportedError,
    DpeError,
    IncompatibleServerError,
    NotFoundError,
    UnexpectedResponseError,
    from_problem,
)
from dpe_sdk.wire import BatchHeads, Capabilities, Limits, ListPage, Skeleton

__all__ = [
    "CONTRACT_HEADER",
    "DOC_HASH_HEADER",
    "ERROR_CODE_HEADER",
    "PROTOCOL",
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
    """

    def __init__(self, capabilities: Capabilities, contract: str = dpe_hash.CONTRACT) -> None:
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

    @property
    def limits(self) -> Limits:
        return self.capabilities.limits

    def _request(self, method: str, target: str, body: Mapping[str, Any] | None = None) -> Request:
        headers = {CONTRACT_HEADER: self.contract}
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
        暂不校验响应的 ``file_uri`` 与 ``uri`` 对应：服务端返回规范化后的 URI（core §1），比较须经
        唯一的规范化实现 ``normalize_file_uri``（#39），落地后补上。
        """
        response = yield self._request("GET", "documents?" + _query(uri=uri))
        try:
            _expect(response)
        except NotFoundError:
            return None
        skeleton = _parse(response, Skeleton, {"contract": self.contract})
        self._verify_skeleton(skeleton, response)
        return skeleton

    def _verify_skeleton(self, skeleton: Skeleton, response: Response) -> None:
        def broken(reason: str) -> UnexpectedResponseError:
            return UnexpectedResponseError(f"骨架不自洽：{reason}", status=response.status)

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
