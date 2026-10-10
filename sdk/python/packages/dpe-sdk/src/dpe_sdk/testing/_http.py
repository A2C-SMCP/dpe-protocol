"""参考服务端的 HTTP 绑定（spec/bindings/http.md）：把 ``Engine`` 暴露为 ASGI 应用。

本层只做映射：请求 → 引擎调用，结果与 ``DpeError`` → 响应。**求值顺序在引擎里**（core §3.3、
§3.4、§5.2）；本层先于引擎完成的只有与文档状态无关的传输层判定，依次为：路由（未知路径
``404``、方法不符 ``405`` + ``Allow``，均非 DPE 错误）→ 认证（``401`` + ``WWW-Authenticate``，
非 DPE 错误）→ 查询参数的形态（重复、混用、缺 ``uri`` → ``DPE_VALIDATION``；查询串在请求行里，
不读请求体即可拒绝，故先于请求体）→ 请求体（超过 ``max_payload_bytes`` → ``413``，按解码后
计算；不支持的 ``Content-Encoding`` → ``415`` + ``Accept-Encoding``，非 DPE 错误；gzip 损坏 →
``DPE_VALIDATION``）。

条件头与 ``Content-Range`` 的语法错误不在本层拒绝：分别以 ``InvalidPrecondition`` /
``InvalidChunk`` 交给引擎，在规范规定的那一步判定（commit 报文校验末步；GET 的
``If-None-Match`` 在「契约 → uri」之后、文档状态之前；分块阶梯第 4 步，「本会话内已完成 →
200」先于它）。合法 entity-tag 中的值原样交给引擎按值比较（core §5.2）：``W/"…"`` 永不匹配，
格式不对的值同样比较不上。

引擎持线程锁、授权器可插拔（可能慢，也可能经 HTTP 回调本应用），引擎调用一律放到工作线程。
"""

from __future__ import annotations

import json
import math
import re
import zlib
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, TypeAlias
from urllib.parse import quote, unquote_to_bytes

import anyio.to_thread
import dpe_hash
from pydantic import BaseModel

from dpe_sdk.errors import (
    DpeError,
    MissingContentError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from dpe_sdk.protocol import (
    CONTRACT_HEADER,
    DOC_HASH_HEADER,
    ERROR_CODE_HEADER,
    SESSION_EXPIRES_HEADER,
    UPLOAD_OFFSET_HEADER,
)
from dpe_sdk.testing._engine import (
    BaseHash,
    Engine,
    IfAbsent,
    IfNoneMatch,
    InvalidPrecondition,
    Precondition,
)
from dpe_sdk.testing._staging import (
    InvalidChunk,
    UploadChunk,
    UploadOffsetError,
    UploadResult,
)

__all__ = ["ASGIApp", "Authenticator", "create_app", "open_access"]

Scope: TypeAlias = MutableMapping[str, Any]
Message: TypeAlias = MutableMapping[str, Any]
Receive: TypeAlias = Callable[[], Awaitable[Message]]
Send: TypeAlias = Callable[[Message], Awaitable[None]]
ASGIApp: TypeAlias = Callable[[Scope, Receive, Send], Awaitable[None]]

#: 认证：``Authorization`` 头（缺省为 ``None``）→ 调用者身份；返回 ``None`` 即未认证（401）。
#: 协议不定义鉴权方案（HTTP 绑定 §6），调用者身份对引擎是不透明字符串。
Authenticator: TypeAlias = Callable[[str | None], str | None]

_JSON = "application/json"
_PROBLEM_JSON = "application/problem+json"
_VARY = ("Vary", CONTRACT_HEADER)

#: 每个 code 的基础状态码（HTTP 绑定 §5）；端点相关的例外见 ``_status``
_STATUS = {
    "DPE_VALIDATION": 400,
    "DPE_CONTRACT_UNSUPPORTED": 400,
    "DPE_CATEGORY_UNKNOWN": 400,
    "DPE_HASH_MISMATCH": 400,
    "DPE_MISSING_CONTENT": 400,
    "DPE_FORBIDDEN": 403,
    "DPE_NOT_FOUND": 404,
    "DPE_PRECONDITION_FAILED": 412,
    "DPE_ALREADY_EXISTS": 412,
    "DPE_SESSION_EXPIRED": 410,
    "DPE_PAYLOAD_TOO_LARGE": 413,
    "DPE_PRECONDITION_REQUIRED": 428,
    "DPE_RATE_LIMITED": 429,
    "DPE_UNAVAILABLE": 503,
}

#: RFC 9110 §8.8.3 的 entity-tag：``[W/] DQUOTE *etagc DQUOTE``
_ENTITY_TAG = re.compile(r'(W/)?"([\x21\x23-\x7e\x80-\xff]*)"')
#: RFC 9110 §14.4 的 Content-Range（只接受 ``bytes first-last/complete``）
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)")
_DECIMAL = re.compile(r"[0-9]+")
#: 十进制数值的上限：字节数与条数都按有符号 64 位表示，超出即不是可表示的值（也避开 int()
#: 对超长十进制串的位数上限，它会抛 ValueError 而不是协议错误）
_DECIMAL_MAX = 2**63 - 1

#: path 中无需百分号编码的字符（RFC 3986 §3.3 的 pchar 去掉 pct-encoded，加上 "/"）
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_BASE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^/]")

_DOCUMENT_PARAMS = frozenset({"uri", "prefix", "cursor", "limit"})
_LIST_PARAMS = ("prefix", "cursor", "limit")


def open_access(authorization: str | None) -> str:
    """默认认证器：放行一切，调用者身份取 ``Authorization`` 头原值（缺省为空串）。

    不同凭证即不同身份，会话绑定 ``(file_uri, 调用者)`` 可复现；按身份授权由
    ``EngineConfig.authorizer`` 判定。
    """
    return "" if authorization is None else authorization


@dataclass
class _Response:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


class _NotDpe(Exception):
    """传输层的非 DPE 错误响应（401 / 404 / 405 / 415）：无 problem 体、无 ``DPE-Error-Code``。"""

    def __init__(self, status: int, headers: list[tuple[str, str]] | None = None) -> None:
        super().__init__(status)
        self.response = _Response(status, headers or [])


@dataclass(frozen=True)
class _Request:
    method: str
    #: 去掉挂载前缀后的路径段
    segments: tuple[str, ...]
    query: Mapping[str, str]
    headers: Mapping[str, str]
    scope: Scope
    receive: Receive

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def contract(self) -> str | None:
        return self.header(CONTRACT_HEADER)


#: 路由种类：处理函数的选择与错误状态码的端点相关例外（§5）
_Route = Literal[
    "capabilities", "heads", "documents", "negotiate", "move", "pages", "objects", "blobs"
]

_SINGLE: dict[str, _Route] = {
    "capabilities": "capabilities",
    "heads": "heads",
    "documents": "documents",
    "negotiate": "negotiate",
    "move": "move",
}
_STAGED: dict[str, _Route] = {"pages": "pages", "objects": "objects", "blobs": "blobs"}

_METHODS: dict[_Route, tuple[str, ...]] = {
    "capabilities": ("GET",),
    "heads": ("POST",),
    "documents": ("GET", "HEAD", "PUT", "DELETE"),
    "negotiate": ("POST",),
    "move": ("POST",),
    "pages": ("PUT", "HEAD"),
    "objects": ("PUT",),
    "blobs": ("PUT", "HEAD"),
}


def create_app(
    engine: Engine,
    *,
    prefix: str = "",
    authenticate: Authenticator = open_access,
    public_url: str | None = None,
    www_authenticate: str = 'Bearer realm="dpe"',
    max_threads: int = 64,
) -> ASGIApp:
    """把引擎按 HTTP 绑定暴露为 ASGI 应用。

    - ``prefix``：remote 在本应用内的路径（如 ``/r/42``，不带尾部 ``/``）；remote 的 base URL
      即「应用的对外地址 + prefix」（HTTP 绑定 §1）。
    - ``authenticate``：``Authorization`` → 调用者身份，返回 ``None`` 时 401（缺省放行一切）。
    - ``public_url``：remote 的对外 base URL（部署在反向代理之后时），用于 move 成功响应的
      ``Content-Location``；缺省由请求的 scheme、``Host`` 头、``root_path`` 与 ``prefix`` 组成，
      不信任 ``X-Forwarded-*``。
    - ``max_threads``：引擎与认证器调用所用工作线程的上限（本应用专用，不占 anyio 的全局默认
      池）。授权器 / 认证器经 HTTP 回调本应用时，每层嵌套各占一个线程：并发请求数 × 嵌套深度
      须小于该上限，否则回调拿不到线程而互相等待。线程上限在首次请求时绑定当时的异步后端：
      一个应用实例只用于一种后端（asyncio 或 trio）。
    """
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError(f"prefix 必须以 / 开头且不带尾部 /，实际为 {prefix!r}")
    if public_url is not None and public_url.endswith("/"):
        raise ValueError(f"public_url 不带尾部 /，实际为 {public_url!r}")
    # prefix 既参与路由（与已解码的 ASGI path 比较）又原样进入 Content-Location：只允许无需
    # 编码的 path 字符，两处才是同一个串
    if quote(prefix, safe=_PATH_SAFE) != prefix:
        raise ValueError(f"prefix 只能含无需百分号编码的 path 字符，实际为 {prefix!r}")
    # public_url 原样进入响应头：带 scheme 与 authority 的已编码 ASCII URL，百分号后跟两位
    # HEXDIG；remote 是 base URL，不带查询串与片段（§1，否则拼出的 Content-Location 错位）
    if public_url is not None and (
        not _BASE_URL.match(public_url)
        or quote(public_url, safe=_PATH_SAFE + "%[]") != public_url
        or _BAD_PERCENT.search(public_url)
    ):
        raise ValueError(f"public_url 必须是已编码的 ASCII URL，实际为 {public_url!r}")
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or max_threads < 1:
        raise ValueError(f"max_threads 必须是正整数，实际为 {max_threads!r}")
    return _App(engine, prefix, authenticate, public_url, www_authenticate, max_threads)


class _App:
    def __init__(
        self,
        engine: Engine,
        prefix: str,
        authenticate: Authenticator,
        public_url: str | None,
        www_authenticate: str,
        max_threads: int,
    ) -> None:
        self.engine = engine
        self._max_threads = max_threads
        #: 首次使用时在事件循环内创建（CapacityLimiter 绑定所在的事件循环）
        self._limiter: anyio.CapacityLimiter | None = None
        self.prefix = prefix
        self.authenticate = authenticate
        self.public_url = public_url
        self.www_authenticate = www_authenticate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"不支持的 ASGI scope：{scope['type']!r}")
        response = await self._respond(scope, receive)
        await _send(send, response, head=scope["method"] == "HEAD")

    async def _respond(self, scope: Scope, receive: Receive) -> _Response:
        route: _Route | None = None
        try:
            route, segments = self._route(scope)
            method = scope["method"]
            if method not in _METHODS[route]:
                raise _NotDpe(405, [("Allow", ", ".join(_METHODS[route]))])
            headers = _headers(scope)
            # 认证器可插拔（可能慢或回调本应用），与引擎调用一样不在事件循环上执行
            caller = await self._run(self.authenticate, headers.get("authorization"))
            if caller is None:
                raise _NotDpe(401, [("WWW-Authenticate", self.www_authenticate)])
            # 只有文档资源有查询参数（§4.3、§4.4）；其他端点的查询串一律忽略
            query = _query(scope) if route == "documents" else {}
            request = _Request(method, segments, query, headers, scope, receive)
            response = await self._dispatch(route, request, caller)
        except _NotDpe as exc:
            response = exc.response
        except DpeError as exc:
            # 带 If-Match 的 PUT / DELETE documents：文档不存在是条件头求值失败（§3.2）
            if_match = scope["method"] in ("PUT", "DELETE") and _has(scope, b"if-match")
            response = _problem(exc, route, if_match=if_match)
        if route == "documents":
            response.headers.append(_VARY)
        return response

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(self._max_threads)
        return await anyio.to_thread.run_sync(partial(func, *args), limiter=self._limiter)

    def _route(self, scope: Scope) -> tuple[_Route, tuple[str, ...]]:
        path: str = scope["path"]
        root_path: str = scope.get("root_path", "")
        if root_path:
            # 按段边界剥离挂载路径：/api 不得吃掉 /apix 的前缀
            if path != root_path and not path.startswith(root_path + "/"):
                raise _NotDpe(404)
            path = path[len(root_path) :]
        if self.prefix:
            if not path.startswith(self.prefix + "/"):
                raise _NotDpe(404)
            path = path[len(self.prefix) :]
        segments = tuple(path.split("/")[1:])
        if len(segments) == 1 and segments[0] in _SINGLE:
            return _SINGLE[segments[0]], segments
        if (
            len(segments) == 4
            and segments[0] == "staging"
            and segments[2] in _STAGED
            and segments[1]
            and segments[3]
        ):
            return _STAGED[segments[2]], segments
        raise _NotDpe(404)

    async def _dispatch(self, route: _Route, request: _Request, caller: str) -> _Response:
        engine = self.engine
        if route == "capabilities":
            return _json(200, engine.capabilities())
        if route == "heads":
            body = await self._body(request)
            return _json(200, await self._run(engine.batch_head, caller, body, request.contract))
        if route == "negotiate":
            body = await self._body(request)
            return _json(200, await self._run(engine.negotiate, caller, body, request.contract))
        if route == "move":
            return await self._move(request, caller)
        if route == "documents":
            return await self._documents(request, caller)
        return await self._staging(route, request, caller)

    # ------------------------------------------------------------------
    # 文档资源（HTTP 绑定 §3、§4.3、§4.4、§4.8、§4.9）
    # ------------------------------------------------------------------

    async def _documents(self, request: _Request, caller: str) -> _Response:
        engine = self.engine
        query = request.query
        contract = request.contract
        if request.method == "GET" and "uri" not in query:
            raw_limit = query.get("limit")
            limit = None if raw_limit is None else _decimal(raw_limit)
            if raw_limit is not None and limit is None:
                raise ValidationError(f"limit 必须是十进制整数：{raw_limit!r}")
            page = await self._run(
                engine.list_documents,
                caller,
                contract,
                query.get("prefix", ""),
                query.get("cursor"),
                limit,
            )
            return _json(200, page)
        uri = query.get("uri")
        if uri is None:
            raise ValidationError("文档资源的请求必须带查询参数 uri")
        mixed = [p for p in _LIST_PARAMS if p in query]
        if mixed:
            raise ValidationError(f"查询参数 uri 不得与 {mixed[0]} 并用")
        if request.method == "GET":
            condition = _if_none_match(request.header("if-none-match"))
            skeleton = await self._run(engine.get_skeleton, caller, uri, contract, condition)
            if skeleton is None:
                raise NotFoundError(f"{uri} 不存在")
            tags = _doc_hash_headers(skeleton.doc_hash)
            # InvalidPrecondition 已在引擎的「契约 → uri」之后抛出，这里只剩 IfNoneMatch / None
            if isinstance(condition, IfNoneMatch) and _matched_weakly(condition, skeleton.doc_hash):
                return _Response(304, tags)
            response = _json(200, skeleton)
            response.headers.extend(tags)
            return response
        if request.method == "HEAD":
            head = await self._run(engine.head, caller, uri, contract)
            if head is None:
                raise NotFoundError(f"{uri} 不存在")
            return _Response(200, _doc_hash_headers(head.doc_hash))
        if request.method == "PUT":
            precondition = _write_precondition(request, allow_if_absent=True)
            body = await self._body(request)
            result = await self._run(engine.commit, caller, uri, body, contract, precondition)
            response = _json(201 if result.status == "created" else 200, result)
            response.headers.extend(_doc_hash_headers(result.doc_hash))
            return response
        # DELETE
        precondition = _write_precondition(request, allow_if_absent=False)
        base_hash = precondition.value if isinstance(precondition, BaseHash) else precondition
        assert not isinstance(base_hash, IfAbsent)
        await self._run(engine.delete, caller, uri, contract, base_hash)
        return _Response(204)

    async def _move(self, request: _Request, caller: str) -> _Response:
        body = await self._body(request)
        outcome = await self._run(self.engine.move, caller, body, request.contract)
        # 目标 URI 由引擎规范化后随结果返回，本层不再解析请求体
        location = f"{self._remote(request)}/documents?uri={quote(outcome.to_uri, safe='')}"
        response = _json(200, outcome.result)
        response.headers.append((DOC_HASH_HEADER, outcome.result.doc_hash))
        response.headers.append(("Content-Location", location))
        return response

    def _remote(self, request: _Request) -> str:
        """remote 的对外 base URL（HTTP 绑定 §1，不带尾部 /）。"""
        if self.public_url is not None:
            return self.public_url
        scope = request.scope
        host = request.header("host")
        if host is None:
            server = scope.get("server") or ("localhost", None)
            host = server[0] if server[1] is None else f"{server[0]}:{server[1]}"
        # root_path 是已解码的挂载路径：只编码 path 中不能直接出现的字符，保留 sub-delims 等
        root_path = quote(scope.get("root_path", ""), safe=_PATH_SAFE)
        return f"{scope.get('scheme', 'http')}://{host}{root_path}{self.prefix}"

    # ------------------------------------------------------------------
    # 暂存上传（HTTP 绑定 §4.6、§4.7）
    # ------------------------------------------------------------------

    async def _staging(self, route: _Route, request: _Request, caller: str) -> _Response:
        engine = self.engine
        _, session_id, _, target = request.segments
        contract = request.contract
        if request.method == "HEAD":
            kind: Literal["page", "blob"] = "page" if route == "pages" else "blob"
            snapshot = await self._run(
                partial(engine.upload_offset, kind=kind), caller, session_id, target, contract
            )
            return _Response(
                200,
                [
                    (UPLOAD_OFFSET_HEADER, str(snapshot.offset)),
                    (SESSION_EXPIRES_HEADER, snapshot.expires_at),
                ],
            )
        content_range = request.header("content-range")
        if route == "objects":
            # 元素对象不分块：带 Content-Range 时请求体按原始字节交给引擎，由它按 #83 的顺序判定
            invalid = None
            if content_range is not None:
                invalid = InvalidChunk("元素对象不分块，请求不得带 Content-Range")
            body = await self._body(request, decode=invalid is None)
            result = await self._run(
                partial(engine.upload_element, chunk=invalid),
                caller,
                session_id,
                target,
                body,
                contract,
            )
            return _upload(result)
        chunk: UploadChunk | InvalidChunk | None = None
        if content_range is not None:
            chunk = _chunk(content_range)
            if _coding(request):
                # 分块的偏移按原始字节计（§4.7），与传输编码不相容
                chunk = InvalidChunk("分块上传不得带 Content-Encoding")
        body = await self._body(request, decode=chunk is None)
        upload = engine.upload_page if route == "pages" else engine.upload_blob
        result = await self._run(
            partial(upload, chunk=chunk), caller, session_id, target, body, contract
        )
        return _upload(result)

    # ------------------------------------------------------------------
    # 请求体（传输层，core §3.3 第 0 步）
    # ------------------------------------------------------------------

    async def _body(self, request: _Request, *, decode: bool = True) -> bytes:
        """读请求体并按 ``Content-Encoding`` 解码；解码后超过 ``max_payload_bytes`` 即 413。"""
        config = self.engine.config
        limit = config.max_payload_bytes
        coding = _coding(request)
        if not decode or not coding:
            return await _read(request.receive, limit, None)
        if coding not in ("gzip", "x-gzip") or "gzip" not in config.content_encodings:
            raise _NotDpe(415, [("Accept-Encoding", ", ".join(config.content_encodings))])
        return await _read(request.receive, limit, zlib.decompressobj(16 + zlib.MAX_WBITS))


def _coding(request: _Request) -> str:
    """请求体的内容编码（小写）；未编码（缺省或 ``identity``）为空串。"""
    coding = (request.header("content-encoding") or "").strip().lower()
    return "" if coding == "identity" else coding


def _feed(decoder: Any, data: bytes, limit: int, out: bytearray) -> Any:
    """把一块压缩数据喂给 ``decoder``，返回可继续加料的 decoder（跨 member 时换成新的）。

    一个 member 结束后剩下的字节属于下一个 member——RFC 1952 允许拼接多个 member（``gzip``
    命令的输出就是这样），因此换一个新的 decoder 继续解，而不是把尾随数据判成违例。
    **总解压上限跨 member 累计**（由 ``out`` 与 ``limit`` 共同约束），换 member 不重置：
    每轮先判产出是否已超限（``decompress`` 的 ``max_length`` 为 0 表示**不设上限**，为负会抛
    ``ValueError``——两者都必须挡在这里），因此 ``max_length`` 恒为正。
    """
    while True:
        if len(out) > limit:
            raise PayloadTooLargeError(f"请求体解码后超过 max_payload_bytes={limit}")
        try:
            out.extend(decoder.decompress(data, limit + 1 - len(out)))
        except zlib.error as exc:
            raise ValidationError(f"gzip 请求体损坏：{exc}") from None
        if decoder.unconsumed_tail:
            raise PayloadTooLargeError(f"请求体解码后超过 max_payload_bytes={limit}")
        rest = decoder.unused_data if decoder.eof else b""
        if not rest:
            return decoder
        # 上一 member 已结束且还有剩余字节：作为下一个 member 继续
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = rest


async def _read(receive: Receive, limit: int, decoder: Any) -> bytes:
    """流式读取（并解压）请求体，产出超过 ``limit`` 即停止并报 413（防解压炸弹）。

    压缩体按 RFC 1952 接受多个 member 拼接；原始字节的上限（压缩炸弹的另一半）同样跨 member
    累计（``raw``）。
    """
    out = bytearray()
    raw = 0
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ValidationError("请求体不完整：客户端已断开")
        data: bytes = message.get("body", b"")
        more = bool(message.get("more_body", False))
        raw += len(data)
        if decoder is None:
            out.extend(data)
        elif data:
            if decoder.eof and not decoder.unused_data:
                # 上一 member 已完整结束、本块以新 member 开头：换 decoder（不把字节判成尾随垃圾）
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoder = _feed(decoder, data, limit, out)
        if len(out) > limit or (decoder is not None and raw > 2 * limit + 1024):
            raise PayloadTooLargeError(f"请求体超过 max_payload_bytes={limit}")
    if decoder is not None and not decoder.eof:
        raise ValidationError("gzip 请求体不完整")
    return bytes(out)


# ----------------------------------------------------------------------
# 请求解析
# ----------------------------------------------------------------------


def _headers(scope: Scope) -> dict[str, str]:
    """头字段名小写；同名多行按 RFC 9110 §5.3 以逗号合并。"""
    merged: dict[str, str] = {}
    for raw_name, raw_value in scope["headers"]:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()
        merged[name] = f"{merged[name]}, {value}" if name in merged else value
    return merged


def _has(scope: Scope, name: bytes) -> bool:
    return any(k.lower() == name for k, _ in scope["headers"])


def _query(scope: Scope) -> dict[str, str]:
    """文档资源的查询参数（RFC 3986：``&`` 分隔、首个 ``=`` 分名值、百分号解码为 UTF-8，``+``
    不是空格）。

    只看 ``uri`` / ``prefix`` / ``cursor`` / ``limit``，其余参数忽略；这四个参数重复、值解码后
    不是 UTF-8 → ``DPE_VALIDATION``。
    """
    params: dict[str, str] = {}
    raw: bytes = scope.get("query_string", b"")
    for part in raw.split(b"&"):
        if not part:
            continue
        name_raw, _, value_raw = part.partition(b"=")
        name = unquote_to_bytes(name_raw).decode("utf-8", errors="replace")
        if name not in _DOCUMENT_PARAMS:
            continue  # 先按名过滤：未定义的参数（含无法解码的）不参与任何校验
        try:
            value = unquote_to_bytes(value_raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationError(f"查询参数 {name} 解码后不是合法的 UTF-8") from None
        if name in params:
            raise ValidationError(f"查询参数 {name} 重复")
        params[name] = value
    return params


def _write_precondition(
    request: _Request, *, allow_if_absent: bool
) -> Precondition | InvalidPrecondition | None:
    """写操作的条件头（HTTP 绑定 §3.2）：单个 entity-tag 的 ``If-Match``，或
    ``If-None-Match: *``。"""
    if_match = request.header("if-match")
    if_none_match = request.header("if-none-match")
    if if_match is not None and if_none_match is not None:
        return InvalidPrecondition("If-Match 与 If-None-Match 不得并存")
    if if_none_match is not None:
        if not allow_if_absent:
            return InvalidPrecondition("delete 不接受 If-None-Match")
        if if_none_match != "*":
            return InvalidPrecondition("If-None-Match 只接受 *（仅新建）")
        return IfAbsent()
    if if_match is None:
        return None
    if if_match == "*":
        return InvalidPrecondition("If-Match 不得为 *：前置条件必须是一个 doc_hash")
    match = _ENTITY_TAG.fullmatch(if_match)
    if match is None:
        return InvalidPrecondition('If-Match 必须是单个 entity-tag（"<doc_hash>"）')
    if match.group(1):
        return BaseHash(if_match)  # W/"…"：弱校验器永不匹配（§3.1），整串比较必然不等
    return BaseHash(match.group(2))


def _if_none_match(value: str | None) -> IfNoneMatch | InvalidPrecondition | None:
    """GET 的 ``If-None-Match``（HTTP 绑定 §3.3）：``*``，或逗号分隔的 entity-tag 列表。

    语法非法（既不是单独的 ``*``、也不是合法的 tag 列表，如 ``*, "x"``、空值与尾随逗号）时返回
    ``InvalidPrecondition``，由引擎在「契约 → uri」之后抛 ``DPE_VALIDATION``——与写操作的条件头
    同样只看请求本身，非法输入 MUST NOT 被静默当作「不匹配」。
    """
    if value is None:
        return None
    if value.strip() == "*":
        return IfNoneMatch(())
    tags: list[str] = []
    for item in _split_entity_tags(value):
        match = _ENTITY_TAG.fullmatch(item.strip())
        if match is None:  # 含空项（§3.3：列表不含空项）
            return InvalidPrecondition(f"If-None-Match 必须是 * 或 entity-tag 列表：{value!r}")
        tags.append(match.group(2))  # 弱比较：W/ 前缀不影响取值
    return IfNoneMatch(tuple(tags))


def _split_entity_tags(value: str) -> list[str]:
    """按逗号切分 entity-tag 列表，**引号内的逗号不是分隔符**（``etagc`` 含逗号，RFC 9110 §8.8.3）。

    写侧的 ``If-Match`` 是整串 ``fullmatch``，因此 ``"a,b"`` 在那条路径上已被视为单个 tag；这里
    必须同样把它当成一项，否则读写两条路径对同一输入给出互斥的结论。
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':  # etagc 不含 "，无需处理转义
            quoted = not quoted
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _matched_weakly(condition: IfNoneMatch, doc_hash: str) -> bool:
    """弱比较（RFC 9110 §13.1.2）：``*`` 命中任何存在的文档，否则逐项与 doc_hash 比。"""
    return not condition.tags or doc_hash in condition.tags


def _chunk(value: str) -> UploadChunk | InvalidChunk:
    match = _CONTENT_RANGE.fullmatch(value)
    if match is None:
        return InvalidChunk(f"Content-Range 必须是 bytes first-last/total：{value!r}")
    first, last, total = (_decimal(g) for g in match.groups())
    if first is None or last is None or total is None:
        return InvalidChunk(f"Content-Range 的数值超出可表示范围：{value!r}")
    return UploadChunk(first, last, total)


def _decimal(text: str) -> int | None:
    """十进制非负整数（只含 ASCII 数字）；不是这样的串、或超出 ``_DECIMAL_MAX`` 时为 ``None``。"""
    if not _DECIMAL.fullmatch(text):
        return None
    digits = text.lstrip("0") or "0"
    if len(digits) > len(str(_DECIMAL_MAX)):
        return None
    value = int(digits)
    return value if value <= _DECIMAL_MAX else None


# ----------------------------------------------------------------------
# 响应
# ----------------------------------------------------------------------


def _json(status: int, model: BaseModel | Mapping[str, Any]) -> _Response:
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    return _Response(status, [("Content-Type", _JSON)], _dumps(data))


def _dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _doc_hash_headers(doc_hash: str) -> list[tuple[str, str]]:
    return [(DOC_HASH_HEADER, doc_hash), ("ETag", f'"{doc_hash}"')]


def _upload(result: UploadResult) -> _Response:
    """upload 结果（§4.6、§4.7）：201 新写入、200 重复、202 中间块（无体）。"""
    expires = (SESSION_EXPIRES_HEADER, result.expires_at)
    if result.outcome == "partial":
        return _Response(202, [expires, (UPLOAD_OFFSET_HEADER, str(result.offset))])
    status = 201 if result.outcome == "created" else 200
    response = _json(status, result.missing if result.missing is not None else {})
    response.headers.append(expires)
    return response


def _status(code: str, route: _Route | None, *, if_match: bool) -> int:
    """code → 状态码（§5）：每个 code 在给定端点上只有一个状态码。"""
    if route == "move" and code in ("DPE_PRECONDITION_FAILED", "DPE_ALREADY_EXISTS"):
        return 409  # 请求体中的前置条件（§3.2）
    if route == "documents" and code == "DPE_NOT_FOUND" and if_match:
        return 412  # 条件头求值失败（RFC 9110 §13.1.1）
    return _STATUS.get(code, 500)


def _problem(exc: DpeError, route: _Route | None, *, if_match: bool) -> _Response:
    """RFC 9457 problem 体 + ``DPE-Error-Code``（§5）。HEAD 的体由 ``_send`` 丢弃。"""
    status = _status(exc.code, route, if_match=if_match)
    slug = exc.code.removeprefix("DPE_").lower().replace("_", "-")
    title = slug.replace("-", " ")
    problem: dict[str, Any] = {
        "type": f"urn:dpe:error:{slug}",
        "title": title,
        "status": status,
        "detail": str(exc) or title,
        "code": exc.code,
        "retryable": exc.retryable,
    }
    if isinstance(exc, MissingContentError):
        problem["missing"] = exc.missing.model_dump(mode="json")
        problem["missing_truncated"] = exc.truncated
    headers = [("Content-Type", _PROBLEM_JSON), (ERROR_CODE_HEADER, exc.code)]
    if isinstance(exc, UploadOffsetError):
        headers.append((UPLOAD_OFFSET_HEADER, str(exc.offset)))
    if status in (429, 503):
        delay = exc.retry_after
        seconds = 1 if delay is None or not math.isfinite(delay) else max(0, math.ceil(delay))
        headers.append(("Retry-After", str(seconds)))
    return _Response(status, headers, _dumps(problem))


async def _send(send: Send, response: _Response, *, head: bool) -> None:
    body = b"" if head or response.status in (204, 304) else response.body
    headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
    if not head and response.status not in (204, 304):
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _lifespan(receive: Receive, send: Send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
