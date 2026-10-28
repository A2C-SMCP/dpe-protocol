"""httpx 传输适配（HTTP 绑定 §1、§3、§7）：把 sans-IO 核心的 ``Request`` 发出去、把响应还原为
``Response``；``DpeClient``（同步）与 ``AsyncDpeClient``（异步）两个客户端。

**职责分界**（见 ``dpe_sdk.protocol`` 模块说明）：本层只做 I/O——拼 URL（remote 之后接核心产出
的已编码 target，不再编码）、注入凭证、按策略 gzip、包装响应；协议判定与写操作的重试规则全部
在核心。传输层**可以**自行重发的情形只有三类：

1. ``Request.retry_safe`` 为真的请求（只读操作与断点查询）遇到连接错误、超时等网络失败；
2. 任何请求遇到确定未发出的失败（``httpx.ConnectError`` / ``httpx.ConnectTimeout``，以及经代理时
   隧道建立被拒的 ``httpx.ProxyError``）——
   放弃发送时在该异常上打底标（``protocol.mark_request_not_sent``），写操作据此把「确定未生效」
   与「结果未知」分开（异常类型不变，兼容新增）；
3. 响应是可重试的 DPE 错误——按 ``code`` 的 ``retryable`` 判定（不按状态码，HTTP 绑定 §3.2），
   即 ``DPE_RATE_LIMITED`` / ``DPE_UNAVAILABLE``——等待 ``Retry-After`` 后原样重发（等待不超过
   ``_MAX_RETRY_WAIT``；更长的等待不吸收，错误带 ``retry_after`` 抛出，何时重试由调用方决定）。

其余失败（``retry_safe`` 为假的请求可能已发出后的超时、读错误、响应丢失等）原样抛出，由
``drive`` / ``adrive`` 送回操作，按 core §5.2 处理。httpx 的异常不包装：调用方按异常类型区分
连接失败、超时与响应丢失。**例外（#207）**：URL 主机不是合法 A-label 或请求头字段值含 CTL 时，
build 阶段抛 ``LocalRequestError``（本次尝试确定未发出、写操作不重发；整体结果仍按核心累计的
写尝试状态判定）；send 阶段的 ``httpx.LocalProtocolError`` 判定不变，只把消息重抛为固定文本、
原错误留在 ``__cause__``。
写请求在连续建连失败时两级的预算会叠加（传输层每轮与核心的
``max_resends``，最坏 ``(1+max_retries)×(1+max_resends)`` 次尝试）——两者都只覆盖「请求确定
未生效」的情形，叠加是安全的：建连失败确定未发出；带码的 ``DPE_RATE_LIMITED`` /
``DPE_UNAVAILABLE`` 依 core §6 表示请求未产生任何效果。

**gzip**（HTTP 绑定 §7）：请求体在满足全部条件时压缩并带 ``Content-Encoding: gzip``——服务端
capabilities 的 ``content_encodings`` 含 ``gzip``、媒体类型为 ``application/json``、体不小于
1024 字节（更小的体压缩后反而更大）、且不是分块请求（带 ``Content-Range`` 的请求体按原始字节
处理、不走解码路径，MUST NOT 压缩）。

**remote**：一个客户端绑定一个 remote（HTTP 绑定 §1 的 base URL，如
``https://example.com/anything/r/42``）；只接受 http / https、无查询串与片段、不带凭证信息
（凭证走 ``CredentialProvider``）。capabilities 在客户端实例内快照一次，核心的契约与 limits
与它绑定；服务端变更能力后应新建客户端。

**代理只来自环境变量**（#132）：未传入 ``transport`` 时，按 ``dpe_sdk._proxy`` 的规则从
``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` 判定 remote 走哪个代理，
显式交给 httpx 的传输层——httpx 缺省在 macOS / Windows 上会回退读系统代理设置，这里不用它的
缺省（传入了传输层的 httpx 客户端不再自行探测代理）。连不上代理是建连失败；HTTPS 经代理时
隧道（``CONNECT``）被拒抛 ``httpx.ProxyError``，发生在请求到达 remote 之前，同样确定未发出；
明文 HTTP 经代理转发时，代理的应答（如 502）是普通响应。传入 ``transport`` 时原样使用。
"""

from __future__ import annotations

import gzip
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import TypeVar

import anyio
import anyio.to_thread
import dpe_hash
import httpx
import idna

from dpe_sdk._proxy import proxy_for
from dpe_sdk.credentials import CredentialProvider
from dpe_sdk.errors import DpeError, LocalRequestError
from dpe_sdk.models import Document, ElementObject, PageObject
from dpe_sdk.protocol import (
    BlobRange,
    BlobSource,
    CommitPrecondition,
    Negotiated,
    Operation,
    ProtocolCore,
    Request,
    Response,
    Session,
    adrive,
    drive,
    error_from_response,
    fetch_capabilities,
    mark_outcome_not_sent,
    mark_request_not_sent,
)
from dpe_sdk.remote_url import check_remote_url
from dpe_sdk.wire import Capabilities, CommitResult, ListPage, MoveResult, Skeleton

__all__ = ["AsyncDpeClient", "DpeClient", "not_sent"]

_JSON = "application/json"
_AUTHORIZATION = "Authorization"

#: 小于该字节数的请求体不压缩：JSON 头与 gzip 尾的开销会让小体反而变大
_MIN_COMPRESS_BYTES = 1024

#: 可重试错误没给 ``Retry-After`` 时的等待秒数：对齐规范给服务端的缺省提示值（HTTP 绑定 §5）
_DEFAULT_RETRY_AFTER = 1.0

#: 传输层愿在请求内等待的上限：更长的等待（含 ``math.inf``）不吸收，错误带 ``retry_after``
#: 如实抛出——库不在一次调用里替调用方长时间阻塞，何时重试由调用方决定
_MAX_RETRY_WAIT = 300.0

#: 确定未发出（请求一个字节都没到 remote）的失败：任何请求都可原样重发。``ProxyError`` 只在
#: 经代理建立隧道（``CONNECT`` 应答非 2xx）或 SOCKS 握手失败时抛出，都在请求发出之前
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

#: send 阶段本地协议错误的重抛消息：库的原文可能回显请求头（含凭证），不得外泄（#207）
_REDACTED_LOCAL_PROTOCOL = "本地协议错误（原始消息已隐去，见 __cause__）"


def _field_value_is_valid(value: str) -> bool:
    """RFC 9110 field-value：CTL（``%x00-08``、``%x0A-1F``、``%x7F``）不允许，HTAB 除外。

    在 build 阶段由 SDK 自己判定（不依赖 h11 的实现）：命中即确定请求还没发出，抛
    ``LocalRequestError``。非 ASCII 字符不在判定范围（#207：两侧对非 ASCII 凭证的判定不一致
    属于规范空白，另开 spec Issue）。
    """
    for char in value:
        code = ord(char)
        if code == 0x7F or (code < 0x20 and char != "\t"):
            return False
    return True


def not_sent(exc: BaseException) -> bool:
    """该异常是否表示请求**确定未发出**（如建连失败）。

    只覆盖传输层的建连失败类异常；``LocalRequestError``（本地请求构造失败）同样确定未发出，
    但它是独立的 SDK 错误类型，不走本谓词。**写操作结果的判定请用
    ``protocol.outcome_unknown(exc)``**——写操作是多请求、核心内部还会重发的复合操作，必须按
    单次尝试的判据整体判定，不能对最终异常套用本函数。
    """
    return isinstance(exc, _NOT_SENT)


#: 网络失败（对端或链路的问题，重发是恢复手段）；``LocalProtocolError``（本地请求构造问题）与
#: ``UnsupportedProtocol``（配置问题）不在此列，重发没有意义
_NETWORK_FAILURES = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)

_T = TypeVar("_T")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """头字段名不区分大小写（``Request`` 的 headers 由核心填写，大小写规格未作约定）。"""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _media_type(value: str | None) -> str:
    return value.split(";", 1)[0].strip().lower() if value else ""


def _remote_base(remote: str) -> str:
    """按 HTTP 绑定 §1 的文法校验 remote，原样返回（下文路径直接拼在它之后）。"""
    if not isinstance(remote, str):
        raise TypeError(f"remote 必须是字符串，实际为 {type(remote).__name__}")
    return check_remote_url(remote)


def _network_failure(exc: Exception) -> bool:
    return isinstance(exc, _NETWORK_FAILURES)


class _ClientBase:
    """两个客户端的共享配置与纯计算（拼 URL、注入凭证、gzip 策略、重发判定），不做 I/O。"""

    def __init__(
        self,
        remote: str,
        *,
        credential: CredentialProvider | None = None,
        contract: str = dpe_hash.CONTRACT,
        max_resends: int = 2,
        max_retries: int = 2,
    ) -> None:
        self.remote = _remote_base(remote)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError(f"max_retries 必须是非负整数，实际为 {max_retries!r}")
        if isinstance(max_resends, bool) or not isinstance(max_resends, int) or max_resends < 0:
            # 与 ProtocolCore 同一规则与措辞（那里是第二道防线）：配置错误在构造时即报，
            # 不是等首个操作先发一轮 capabilities 才抛
            raise ValueError(f"max_resends 必须是非负整数，实际为 {max_resends!r}")
        self._credential = credential
        self._contract = contract
        self._max_resends = max_resends
        self._max_retries = max_retries
        self._core: ProtocolCore | None = None

    def _new_core(self, capabilities: Capabilities) -> ProtocolCore:
        return ProtocolCore(capabilities, self._contract, max_resends=self._max_resends)

    def _encodings(self) -> frozenset[str]:
        """服务端声明的请求体编码（capabilities 快照）。
        核心尚未建立时无快照——此时还没有带体的请求（capabilities 是第一个）。"""
        core = self._core
        if core is None:
            return frozenset()
        return frozenset(core.capabilities.content_encodings)

    def _wire(self, request: Request, body: bytes | None) -> tuple[str, bytes | None, bool]:
        """(URL, 请求体, 是否已 gzip)：一次算好，重试各轮复用（大体重压一次即可）。

        压缩按本模块 docstring 的 gzip 策略；``gzipped`` 为真时每轮的头部须带
        ``Content-Encoding``（见 ``_headers``）。
        """
        url = self.remote + "/" + request.target
        if body is None or not self._compressible(request, body):
            return url, body, False
        return url, gzip.compress(body, mtime=0), True  # mtime=0：输出确定，不掺入压缩时刻

    def _compressible(self, request: Request, body: bytes) -> bool:
        return (
            len(body) >= _MIN_COMPRESS_BYTES
            and _header(request.headers, "Content-Range") is None
            and _media_type(_header(request.headers, "Content-Type")) == _JSON
            and "gzip" in self._encodings()
        )

    def _headers(self, request: Request, gzipped: bool) -> dict[str, str]:
        """本轮请求头：核心给的字段 + 凭证。

        凭证**每轮重试前重新取**（provider 的调用时机就是「请求发送前」）：网络失败后的重发、
        按 ``Retry-After`` 等待数百秒后的重试，都能拿到轮换后的凭证。
        """
        headers = dict(request.headers)
        if gzipped:
            headers["Content-Encoding"] = "gzip"
        if self._credential is not None:
            try:
                value = self._credential.authorization()
            except Exception as exc:
                # provider 自己的失败发生在请求发出之前：确定未发出；异常实例与类型原样保留，
                # 不包装成 LocalRequestError（#256 会把可能暂时性的失败误判为不可重试）
                mark_outcome_not_sent(exc)
                raise
            if value is not None:
                headers[_AUTHORIZATION] = value
        return self._checked_headers(headers)

    def _checked_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """请求头字段值必须符合 RFC 9110 field-value（#207）：非法时请求确定未发出。"""
        for value in headers.values():
            if not _field_value_is_valid(value):
                raise LocalRequestError()
        return headers

    def _delay(self, response: Response) -> float | None:
        """响应是可重试的 DPE 错误时给出等待秒数，否则 ``None``（不再重试）。

        按 ``code`` 的 ``retryable`` 判定（``errors`` 的通用重试只依据它），不按状态码
        （HTTP 绑定 §3.2）；等待值取 ``Retry-After``（缺省对齐规范给服务端的 1 秒），超过
        ``_MAX_RETRY_WAIT``（含 ``math.inf``）时不再重试——留给核心如实抛出，``retry_after`` 不丢。
        """
        if response.status < 400:
            return None
        error = error_from_response(response)
        if not isinstance(error, DpeError) or not error.retryable:
            return None
        if error.retry_after is None:
            return _DEFAULT_RETRY_AFTER
        if 0.0 <= error.retry_after <= _MAX_RETRY_WAIT:
            return error.retry_after
        return None

    def _resendable(self, request: Request, exc: Exception) -> bool:
        """该异常下原样重发是否安全：确定未发出的失败任何请求都可以；网络失败只在
        ``retry_safe`` 的请求上（写请求可能已发出，交核心按 core §5.2 判定）。"""
        if not_sent(exc):
            return True
        return request.retry_safe and _network_failure(exc)


class DpeClient(_ClientBase):
    """同步客户端：一个 httpx 客户端对应一个 remote，可跨线程使用（httpx 连接池线程安全）。

    使用 ``with DpeClient(...) as client:`` 或显式 ``close()`` 释放连接。操作方法与
    ``ProtocolCore`` 一一对应（``deliver`` 前的能力协商在首次操作时自动完成）。
    """

    def __init__(
        self,
        remote: str,
        *,
        credential: CredentialProvider | None = None,
        contract: str = dpe_hash.CONTRACT,
        max_resends: int = 2,
        max_retries: int = 2,
        timeout: httpx.Timeout | float | None = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            remote,
            credential=credential,
            contract=contract,
            max_resends=max_resends,
            max_retries=max_retries,
        )
        if transport is None:
            transport = httpx.HTTPTransport(proxy=proxy_for(remote, os.environ))
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        # 并发首次访问 core 时只协商一次能力
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DpeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(self, request: Request) -> Response:
        """发送一个请求（``drive`` 的驱动回调），返回 ``Response``。

        可以自行重发的只有本模块 docstring 列出的三类；其余失败按 httpx 的异常类型原样抛出。
        本地构造失败抛 ``LocalRequestError``；send 阶段的 ``LocalProtocolError`` 消息脱敏（#207）。
        """
        body = request.body_bytes()
        url, content, gzipped = self._wire(request, body)
        attempt = 0
        while True:
            headers = self._headers(request, gzipped)  # 每轮重试重新取凭证
            try:
                built = self._http.build_request(
                    request.method, url, headers=headers, content=content
                )
            except (idna.IDNAError, httpx.InvalidURL) as exc:
                # 非法 A-label 或 URL 本身不可构造：构造阶段失败，请求确定未发出（#207）
                raise LocalRequestError() from exc
            try:
                reply = self._http.send(built)
            except httpx.LocalProtocolError as exc:
                # 判定不变（仍按可能已发出处理），只隐去可能含凭证的库原文
                raise httpx.LocalProtocolError(_REDACTED_LOCAL_PROTOCOL) from exc
            except Exception as exc:
                if attempt >= self._max_retries or not self._resendable(request, exc):
                    if not_sent(exc):
                        mark_request_not_sent(exc)  # 底标：写操作据此判定「确定未生效」
                    raise
                attempt += 1
                continue
            response = Response(reply.status_code, dict(reply.headers), reply.content)
            delay = self._delay(response)
            if delay is None or attempt >= self._max_retries:
                return response
            _sleep(delay)
            attempt += 1

    def _run(self, op: Operation[_T]) -> _T:
        return drive(op, self.send)

    # ------------------------------------------------------------------
    # 与 ProtocolCore 一一对应的操作（薄包装，协议逻辑在核心）
    # ------------------------------------------------------------------

    def core(self) -> ProtocolCore:
        """与 remote 协商后的协议核心（首次访问取 capabilities 并构造，之后快照复用）。

        契约与 limits 与快照绑定：服务端变更能力后应新建客户端。
        """
        core = self._core
        if core is None:
            with self._lock:
                core = self._core  # 可能已被其他线程建好
                if core is None:
                    core = self._new_core(self._run(fetch_capabilities()))
                    self._core = core
        return core

    def capabilities(self) -> Capabilities:
        """服务端能力（``GET capabilities``，HTTP 绑定 §4.1）：客户端实例内快照一次。"""
        return self.core().capabilities

    def head(self, uri: str) -> str | None:
        """``ProtocolCore.head``（HTTP 绑定 §3.3）：文档的 doc_hash，不存在为 ``None``。"""
        return self._run(self.core().head(uri))

    def batch_head(self, uris: list[str]) -> list[str | None]:
        """``ProtocolCore.batch_head``：按 ``batch_head_max`` 分批，与入参一一对应。"""
        return self._run(self.core().batch_head(uris))

    def get_skeleton(self, uri: str) -> Skeleton | None:
        """``ProtocolCore.get_skeleton``（HTTP 绑定 §4.3）：文档对象与全部页对象。"""
        return self._run(self.core().get_skeleton(uri))

    def list_page(
        self, prefix: str = "", cursor: str | None = None, limit: int | None = None
    ) -> ListPage:
        """``ProtocolCore.list_page``（HTTP 绑定 §4.4）：``next_cursor`` 为 ``None`` 即结束。"""
        return self._run(self.core().list_page(prefix, cursor, limit))

    def negotiate(self, uri: str, document: Document) -> Negotiated:
        """``ProtocolCore.negotiate``（HTTP 绑定 §4.5）：开启暂存会话并返回逐层缺失清单。"""
        return self._run(self.core().negotiate(uri, document))

    def upload_page(self, session: Session, page: PageObject) -> list[str]:
        """``ProtocolCore.upload_page``（HTTP 绑定 §4.6）：上传页对象，返回缺失的元素 hash。"""
        return self._run(self.core().upload_page(session, page))

    def upload_element(self, session: Session, element: ElementObject) -> list[str]:
        """``ProtocolCore.upload_element``（HTTP 绑定 §4.6）：上传元素对象，返回缺失的 blob。"""
        return self._run(self.core().upload_element(session, element))

    def upload_blob(
        self,
        session: Session,
        source: BlobSource | bytes | bytearray | memoryview,
        *,
        media_type: str | None = None,
    ) -> None:
        """``ProtocolCore.upload_blob``（HTTP 绑定 §4.7）：整段或分块上传 blob。"""
        self._run(self.core().upload_blob(session, source, media_type=media_type))

    def commit(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        session: Session | None = None,
    ) -> CommitResult:
        """``ProtocolCore.commit``（core §3.2）：快路径或引用暂存会话提交，绝不自动 force。"""
        return self._run(self.core().commit(uri, document, precondition, session=session))

    def deliver(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        blobs: Callable[[str], BlobSource | bytes] | None = None,
        session: Session | None = None,
    ) -> CommitResult:
        """``ProtocolCore.deliver``（core §3.2）：增量投递，只传变更，绝不自动 force。"""
        return self._run(
            self.core().deliver(uri, document, precondition, blobs=blobs, session=session)
        )

    def delete(self, uri: str, base_hash: str) -> None:
        """``ProtocolCore.delete``（core §4）：按 CAS 删除文档。"""
        self._run(self.core().delete(uri, base_hash))

    def move(self, from_uri: str, to_uri: str, base_hash: str) -> MoveResult:
        """``ProtocolCore.move``（core §5.2）：按 CAS 原子改名，学习产物原样保留。"""
        return self._run(self.core().move(from_uri, to_uri, base_hash))


class AsyncDpeClient(_ClientBase):
    """``DpeClient`` 的异步版本（基于 httpx 的 ``AsyncClient``）。

    使用 ``async with AsyncDpeClient(...) as client:`` 或显式 ``await client.aclose()``。
    分块上传等大 blob 的字节在工作线程取出，不阻塞事件循环。
    """

    def __init__(
        self,
        remote: str,
        *,
        credential: CredentialProvider | None = None,
        contract: str = dpe_hash.CONTRACT,
        max_resends: int = 2,
        max_retries: int = 2,
        timeout: httpx.Timeout | float | None = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            remote,
            credential=credential,
            contract=contract,
            max_resends=max_resends,
            max_retries=max_retries,
        )
        if transport is None:
            transport = httpx.AsyncHTTPTransport(proxy=proxy_for(remote, os.environ))
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        # 首次访问 core 的并发保护；在事件循环内创建（anyio 的锁绑定创建时的运行时）
        self._lock: anyio.Lock | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncDpeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _body(self, request: Request) -> bytes | None:
        """请求体的字节：``BlobRange`` 可能是文件读取，放到工作线程；bytes 体不绕线程。"""
        if isinstance(request.body, BlobRange):
            return await anyio.to_thread.run_sync(request.body_bytes)
        return request.body

    async def send(self, request: Request) -> Response:
        """``DpeClient.send`` 的异步版本：自行重发的规则与异常语义完全相同。"""
        body = await self._body(request)
        url, content, gzipped = self._wire(request, body)
        attempt = 0
        while True:
            headers = self._headers(request, gzipped)  # 每轮重试重新取凭证
            try:
                built = self._http.build_request(
                    request.method, url, headers=headers, content=content
                )
            except (idna.IDNAError, httpx.InvalidURL) as exc:
                # 非法 A-label 或 URL 本身不可构造：构造阶段失败，请求确定未发出（#207）
                raise LocalRequestError() from exc
            try:
                reply = await self._http.send(built)
            except httpx.LocalProtocolError as exc:
                # 判定不变（仍按可能已发出处理），只隐去可能含凭证的库原文
                raise httpx.LocalProtocolError(_REDACTED_LOCAL_PROTOCOL) from exc
            except Exception as exc:
                if attempt >= self._max_retries or not self._resendable(request, exc):
                    if not_sent(exc):
                        mark_request_not_sent(exc)  # 底标：写操作据此判定「确定未生效」
                    raise
                attempt += 1
                continue
            response = Response(reply.status_code, dict(reply.headers), reply.content)
            delay = self._delay(response)
            if delay is None or attempt >= self._max_retries:
                return response
            await _asleep(delay)
            attempt += 1

    async def _run(self, op: Operation[_T]) -> _T:
        return await adrive(op, self.send)

    # ------------------------------------------------------------------
    # 与 ProtocolCore 一一对应的操作（薄包装，协议逻辑在核心；见同步版本的同名方法）
    # ------------------------------------------------------------------

    async def core(self) -> ProtocolCore:
        """``DpeClient.core`` 的异步版本（首次访问时协商能力并构造核心）。"""
        core = self._core
        if core is None:
            lock = self._lock
            if lock is None:
                # 并发任务同时首次访问时只协商一次（锁在事件循环内创建，绑定当前运行时）
                lock = self._lock = anyio.Lock()
            async with lock:
                core = self._core
                if core is None:
                    core = self._new_core(await self._run(fetch_capabilities()))
                    self._core = core
        return core

    async def capabilities(self) -> Capabilities:
        """服务端能力（``GET capabilities``，HTTP 绑定 §4.1）：客户端实例内快照一次。"""
        return (await self.core()).capabilities

    async def head(self, uri: str) -> str | None:
        """``ProtocolCore.head``（HTTP 绑定 §3.3）：文档的 doc_hash，不存在为 ``None``。"""
        return await self._run((await self.core()).head(uri))

    async def batch_head(self, uris: list[str]) -> list[str | None]:
        """``ProtocolCore.batch_head``：按 ``batch_head_max`` 分批，与入参一一对应。"""
        return await self._run((await self.core()).batch_head(uris))

    async def get_skeleton(self, uri: str) -> Skeleton | None:
        """``ProtocolCore.get_skeleton``（HTTP 绑定 §4.3）：文档对象与全部页对象。"""
        return await self._run((await self.core()).get_skeleton(uri))

    async def list_page(
        self, prefix: str = "", cursor: str | None = None, limit: int | None = None
    ) -> ListPage:
        """``ProtocolCore.list_page``（HTTP 绑定 §4.4）：``next_cursor`` 为 ``None`` 即结束。"""
        return await self._run((await self.core()).list_page(prefix, cursor, limit))

    async def negotiate(self, uri: str, document: Document) -> Negotiated:
        """``ProtocolCore.negotiate``（HTTP 绑定 §4.5）：开启暂存会话并返回逐层缺失清单。"""
        return await self._run((await self.core()).negotiate(uri, document))

    async def upload_page(self, session: Session, page: PageObject) -> list[str]:
        """``ProtocolCore.upload_page``（HTTP 绑定 §4.6）：上传页对象，返回缺失的元素 hash。"""
        return await self._run((await self.core()).upload_page(session, page))

    async def upload_element(self, session: Session, element: ElementObject) -> list[str]:
        """``ProtocolCore.upload_element``（HTTP 绑定 §4.6）：上传元素对象，返回缺失的 blob。"""
        return await self._run((await self.core()).upload_element(session, element))

    async def upload_blob(
        self,
        session: Session,
        source: BlobSource | bytes | bytearray | memoryview,
        *,
        media_type: str | None = None,
    ) -> None:
        """``ProtocolCore.upload_blob``（HTTP 绑定 §4.7）：整段或分块上传 blob。"""
        await self._run((await self.core()).upload_blob(session, source, media_type=media_type))

    async def commit(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        session: Session | None = None,
    ) -> CommitResult:
        """``ProtocolCore.commit``（core §3.2）：快路径或引用暂存会话提交，绝不自动 force。"""
        return await self._run(
            (await self.core()).commit(uri, document, precondition, session=session)
        )

    async def deliver(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        blobs: Callable[[str], BlobSource | bytes] | None = None,
        session: Session | None = None,
    ) -> CommitResult:
        """``ProtocolCore.deliver``（core §3.2）：增量投递，只传变更，绝不自动 force。"""
        return await self._run(
            (await self.core()).deliver(uri, document, precondition, blobs=blobs, session=session)
        )

    async def delete(self, uri: str, base_hash: str) -> None:
        """``ProtocolCore.delete``（core §4）：按 CAS 删除文档。"""
        await self._run((await self.core()).delete(uri, base_hash))

    async def move(self, from_uri: str, to_uri: str, base_hash: str) -> MoveResult:
        """``ProtocolCore.move``（core §5.2）：按 CAS 原子改名，学习产物原样保留。"""
        return await self._run((await self.core()).move(from_uri, to_uri, base_hash))


#: 等待用的薄间接层：测试替换它断言等待值而不真的睡（同步 / 异步各一）
_sleep = time.sleep


async def _asleep(seconds: float) -> None:
    await anyio.sleep(seconds)
