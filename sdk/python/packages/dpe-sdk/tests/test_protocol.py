"""sans-IO 协议核心（#47）：能力协商、只读操作、错误分派与驱动约定。

响应按 HTTP 绑定 §4.1–§4.4、§5 的报文形状手工构造，用固定应答的 ``send`` 驱动操作。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.protocol import (
    ProtocolCore,
    Request,
    Response,
    adrive,
    drive,
    fetch_capabilities,
    retry_after_seconds,
)
from dpe_sdk.wire import Capabilities
from engine_helpers import inline, text_doc

H1 = "dpe1:" + "1" * 64
H2 = "dpe1:" + "2" * 64
H_DRILL = "dpe2:" + "3" * 64

CAPABILITIES: dict[str, Any] = {
    "protocol": "dpe/1",
    "hash_contracts": ["dpe1"],
    "limits": {
        "max_payload_bytes": 8388608,
        "page_max_bytes": 268435456,
        "staging_ttl_seconds": 86400,
        "blob_max_bytes": 104857600,
        "blob_chunk_bytes": 8388608,
        "batch_head_max": 2,
        "list_page_max": 1000,
    },
    "content_encodings": ["gzip"],
    "features": ["move"],
}


def caps(**overrides: Any) -> Capabilities:
    return Capabilities.model_validate({**CAPABILITIES, **overrides})


def core(contract: str = "dpe1", **overrides: Any) -> ProtocolCore:
    return ProtocolCore(caps(**overrides), contract)


def ok(body: Any = None, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    hdrs = dict(headers or {})
    if body is None:
        return Response(status, hdrs)
    return Response(status, {"Content-Type": "application/json", **hdrs}, json.dumps(body).encode())


def problem(code: str, status: int, **extra: Any) -> Response:
    body = {"type": "urn:dpe:error:x", "title": "t", "status": status, "code": code, **extra}
    return Response(
        status,
        {"Content-Type": "application/problem+json", "DPE-Error-Code": code},
        json.dumps(body).encode(),
    )


class Server:
    """按顺序回放响应，并记录收到的请求。"""

    def __init__(self, *responses: Response) -> None:
        self.responses: Iterator[Response] = iter(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        return next(self.responses)


def run(op: Any, *responses: Response) -> tuple[Any, Server]:
    server = Server(*responses)
    return drive(op, server), server


# ---------------------------------------------------------------------- 能力协商


def test_fetch_capabilities_sends_no_contract_header() -> None:
    result, server = run(fetch_capabilities(), ok(CAPABILITIES))
    assert result == caps()
    (request,) = server.requests
    assert (request.method, request.target, request.body) == ("GET", "capabilities", None)
    assert "DPE-Hash-Contract" not in request.headers


def test_fetch_capabilities_rejects_malformed_body() -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(fetch_capabilities(), ok({"protocol": "dpe/1"}))


@pytest.mark.parametrize(("name", "value"), [("batch_head_max", 0), ("batch_head_max", -1)])
def test_fetch_capabilities_rejects_non_positive_limits(name: str, value: int) -> None:
    body = {**CAPABILITIES, "limits": {**CAPABILITIES["limits"], name: value}}
    with pytest.raises(errors.UnexpectedResponseError, match="Capabilities"):
        run(fetch_capabilities(), ok(body))


def test_contract_must_be_accepted_by_server_no_downgrade() -> None:
    with pytest.raises(errors.ContractUnsupportedError):
        core(hash_contracts=["dpe2"])
    with pytest.raises(errors.ContractUnsupportedError):
        core(hash_contracts=[])
    # 演练契约只在显式指定且服务端接受时使用
    assert core("dpe2", hash_contracts=["dpe1", "dpe2"]).contract == "dpe2"
    assert core(hash_contracts=["dpe1", "dpe2"]).contract == dpe_hash.CONTRACT


def test_unknown_contract_and_protocol() -> None:
    with pytest.raises(ValueError, match="dpe9"):
        core("dpe9", hash_contracts=["dpe9"])
    with pytest.raises(errors.IncompatibleServerError):
        core(protocol="dpe/2")


def test_every_request_declares_the_contract() -> None:
    c = core("dpe2", hash_contracts=["dpe2"])
    ops = [c.head("a:1"), c.batch_head(["a:1"]), c.get_skeleton("a:1"), c.list_page()]
    for op in ops:
        request = next(op)
        assert request.headers["DPE-Hash-Contract"] == "dpe2"
        op.close()


# ---------------------------------------------------------------------- 编码


@pytest.mark.parametrize("uri", ["s3://b/k?x=1&y=2#f", "file:///a b/%41/中文", "feishu://doc/a+b"])
def test_query_values_are_percent_encoded(uri: str) -> None:
    c = core()
    for op in (c.head(uri), c.get_skeleton(uri)):
        target = next(op).target
        op.close()
        path, _, query = target.partition("?")
        assert path == "documents"
        key, _, value = query.partition("=")
        assert key == "uri"
        assert all(ch.isascii() and ch not in "?&#/ +" for ch in value)
        assert unquote(value) == uri


def test_list_page_query() -> None:
    c = core()
    op = c.list_page()
    assert next(op).target == "documents?prefix="
    op.close()
    op = c.list_page("s3://b/a&b/", cursor="c/1=", limit=10)
    assert next(op).target == "documents?prefix=s3%3A%2F%2Fb%2Fa%26b%2F&cursor=c%2F1%3D&limit=10"
    op.close()


# ---------------------------------------------------------------------- head


def test_head_found() -> None:
    result, server = run(core().head("a:1"), ok(headers={"DPE-Doc-Hash": H1, "ETag": f'"{H1}"'}))
    assert result == H1
    assert server.requests[0].method == "HEAD"
    assert server.requests[0].body is None


def test_head_not_found_by_error_code_header() -> None:
    result, _ = run(core().head("a:1"), Response(404, {"dpe-error-code": "DPE_NOT_FOUND"}))
    assert result is None


def test_head_404_without_error_code_is_not_a_dpe_error() -> None:
    with pytest.raises(errors.UnexpectedResponseError) as info:
        run(core().head("a:1"), Response(404))
    assert info.value.status == 404
    assert info.value.retryable is False


def test_head_errors_dispatch_by_header() -> None:
    with pytest.raises(errors.ForbiddenError):
        run(core().head("a:1"), Response(403, {"DPE-Error-Code": "DPE_FORBIDDEN"}))
    with pytest.raises(errors.RateLimitedError) as info:
        run(
            core().head("a:1"),
            Response(429, {"DPE-Error-Code": "DPE_RATE_LIMITED", "Retry-After": "7"}),
        )
    assert info.value.retryable is True
    assert info.value.retry_after == 7.0


def test_head_rejects_missing_or_foreign_doc_hash() -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().head("a:1"), ok())
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().head("a:1"), ok(headers={"DPE-Doc-Hash": H_DRILL}))
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().head("a:1"), ok(headers={"DPE-Doc-Hash": "dpe1:xyz"}))
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().head("a:1"), ok(status=204, headers={"DPE-Doc-Hash": H1}))


# ---------------------------------------------------------------------- batch_head


def test_batch_head_empty_makes_no_request() -> None:
    result, server = run(core().batch_head([]))
    assert result == []
    assert server.requests == []


def test_batch_head_single_batch_at_limit() -> None:
    result, server = run(core().batch_head(["a:1", "a:2"]), ok({"heads": [{"doc_hash": H1}, None]}))
    assert result == [H1, None]
    (request,) = server.requests
    assert (request.method, request.target) == ("POST", "heads")
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body or b"") == {"uris": ["a:1", "a:2"]}


def test_batch_head_splits_by_batch_head_max() -> None:
    result, server = run(
        core().batch_head(["a:1", "a:2", "a:3"]),
        ok({"heads": [None, {"doc_hash": H1}]}),
        ok({"heads": [{"doc_hash": H2}]}),
    )
    assert result == [None, H1, H2]
    assert [json.loads(r.body or b"")["uris"] for r in server.requests] == [["a:1", "a:2"], ["a:3"]]


def test_batch_head_rejects_a_single_string() -> None:
    with pytest.raises(TypeError, match="单个字符串"):
        run(core().batch_head("feishu://doc/a"))


def test_batch_head_checks_every_batch() -> None:
    with pytest.raises(errors.UnexpectedResponseError, match="2 项"):
        run(
            core().batch_head(["a:1", "a:2", "a:3"]),
            ok({"heads": [None, None]}),
            ok({"heads": [None, None]}),
        )
    with pytest.raises(errors.UnexpectedResponseError):
        run(
            core().batch_head(["a:1", "a:2", "a:3"]),
            ok({"heads": [None, None]}),
            ok({"heads": [{"doc_hash": H_DRILL}]}),
        )


def test_batch_head_rejects_count_mismatch_and_foreign_hash() -> None:
    with pytest.raises(errors.UnexpectedResponseError, match="1 项"):
        run(core().batch_head(["a:1", "a:2"]), ok({"heads": [None]}))
    with pytest.raises(errors.UnexpectedResponseError, match=r"heads\[1\]"):
        run(core().batch_head(["a:1", "a:2"]), ok({"heads": [None, {"doc_hash": H_DRILL}]}))


def test_batch_head_error_stops_and_raises() -> None:
    server = Server(ok({"heads": [None, None]}), problem("DPE_VALIDATION", 400))
    with pytest.raises(errors.ValidationError):
        drive(core().batch_head(["a:1", "a:2", "a:3"]), server)
    assert len(server.requests) == 2


# ---------------------------------------------------------------------- get_skeleton


def skeleton_body(contract: str = "dpe1") -> dict[str, Any]:
    doc = inline(text_doc(["x", "y"], ["z"], ["x", "y"], title="T"), contract)
    return {
        "file_uri": "a:1",
        "doc_hash": doc.doc_hash,
        "document": doc.document,
        "pages": doc.pages,
    }


def test_get_skeleton() -> None:
    # 第 1、3 页内容相同：document.pages 中同一页 hash 出现两次，页对象也重复给出（HTTP 绑定 §4.3）
    body = skeleton_body()
    assert body["document"]["pages"][0] == body["document"]["pages"][2]
    result, server = run(
        core().get_skeleton("a:1"), ok(body, headers={"DPE-Doc-Hash": body["doc_hash"]})
    )
    assert result.doc_hash == body["doc_hash"]
    assert [p.model_dump() for p in result.pages] == body["pages"]
    assert server.requests[0].method == "GET"


def test_get_skeleton_under_drill_contract() -> None:
    body = skeleton_body("dpe2")
    c = core("dpe2", hash_contracts=["dpe1", "dpe2"])
    result, _ = run(c.get_skeleton("a:1"), ok(body))
    assert result.doc_hash.startswith("dpe2:")
    # 同一份 dpe2 骨架交给 dpe1 核心：hash 不属于声明的契约
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().get_skeleton("a:1"), ok(body))


def test_get_skeleton_not_found() -> None:
    result, _ = run(core().get_skeleton("a:1"), problem("DPE_NOT_FOUND", 404))
    assert result is None


def _tamper(body: dict[str, Any], how: str) -> tuple[dict[str, Any], dict[str, str]]:
    body = json.loads(json.dumps(body))
    headers: dict[str, str] = {}
    if how == "page_content":
        body["pages"][1]["title"] = "changed"
    elif how == "page_order":
        body["pages"][0], body["pages"][1] = body["pages"][1], body["pages"][0]
    elif how == "page_count":
        body["pages"].pop()
    elif how == "doc_hash":
        body["doc_hash"] = H1
    elif how == "document":
        body["document"]["title"] = "changed"
    elif how == "header":
        headers["DPE-Doc-Hash"] = H1
    return body, headers


@pytest.mark.parametrize(
    "how", ["page_content", "page_order", "page_count", "doc_hash", "document", "header"]
)
def test_get_skeleton_integrity(how: str) -> None:
    body, headers = _tamper(skeleton_body(), how)
    with pytest.raises(errors.UnexpectedResponseError, match="骨架不自洽"):
        run(core().get_skeleton("a:1"), ok(body, headers=headers))


def test_get_skeleton_rejects_invalid_objects() -> None:
    body = skeleton_body()
    body["pages"][0]["extra"] = 1
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().get_skeleton("a:1"), ok(body))


# ---------------------------------------------------------------------- list


def test_list_page() -> None:
    body = {"documents": [{"file_uri": "a:1", "doc_hash": H1}], "next_cursor": "c1"}
    result, _ = run(core().list_page("a:"), ok(body))
    assert [(d.file_uri, d.doc_hash) for d in result.documents] == [("a:1", H1)]
    assert result.next_cursor == "c1"
    result, _ = run(core().list_page("a:", "c1"), ok({"documents": [], "next_cursor": None}))
    assert result.next_cursor is None


def test_list_page_rejects_foreign_hash() -> None:
    body = {"documents": [{"file_uri": "a:1", "doc_hash": H_DRILL}], "next_cursor": None}
    with pytest.raises(errors.UnexpectedResponseError, match=r"documents\[0\]"):
        run(core().list_page(), ok(body))


@pytest.mark.parametrize("limit", [True, "10", 1.0])
def test_list_page_limit_must_be_an_int(limit: Any) -> None:
    with pytest.raises(TypeError):
        run(core().list_page(limit=limit))


@pytest.mark.parametrize("limit", [0, -5, 1001])
def test_list_page_limit_range(limit: int) -> None:
    with pytest.raises(ValueError, match="1 到 1000"):
        run(core().list_page(limit=limit))
    assert next(core().list_page(limit=1000)).target.endswith("&limit=1000")


def test_list_page_invalid_cursor() -> None:
    with pytest.raises(errors.ValidationError):
        run(core().list_page(cursor="bogus"), problem("DPE_VALIDATION", 400))


# ---------------------------------------------------------------------- 错误分派


def _list_error(response: Response) -> Any:
    with pytest.raises((errors.DpeError, errors.ClientError)) as info:
        run(core().list_page(), response)
    return info.value


def test_problem_body_code_wins_over_header() -> None:
    body = json.dumps({"code": "DPE_FORBIDDEN", "detail": "no"}).encode()
    response = Response(
        403,
        {"Content-Type": "application/problem+json; charset=utf-8", "DPE-Error-Code": "DPE_X"},
        body,
    )
    exc = _list_error(response)
    assert isinstance(exc, errors.ForbiddenError)
    assert exc.message == "no"


def test_problem_body_without_header() -> None:
    body = json.dumps({"code": "DPE_SESSION_EXPIRED", "title": "gone"}).encode()
    exc = _list_error(Response(410, {"Content-Type": "application/problem+json"}, body))
    assert isinstance(exc, errors.SessionExpiredError)
    assert exc.message == "gone"


def test_header_used_when_body_is_not_a_problem() -> None:
    for body, ctype in [
        (b"", "application/problem+json"),
        (b"{not json", "application/problem+json"),
        (b'{"code": 1}', "application/problem+json"),
        (b'{"code": "DPE_VALIDATION"}', "application/json"),
        (b"<html>", "text/html"),
    ]:
        exc = _list_error(
            Response(403, {"Content-Type": ctype, "DPE-Error-Code": "DPE_FORBIDDEN"}, body)
        )
        assert isinstance(exc, errors.ForbiddenError), body


def test_no_problem_body_and_no_header_is_not_a_dpe_error() -> None:
    for response in [
        Response(500),
        Response(502, {"Content-Type": "text/html"}, b"<html>bad gateway</html>"),
        Response(400, {"Content-Type": "application/json"}, b'{"code": "DPE_VALIDATION"}'),
        Response(503, {"DPE-Error-Code": " ", "Retry-After": "3"}),
    ]:
        exc = _list_error(response)
        assert isinstance(exc, errors.UnexpectedResponseError)
        assert exc.status == response.status
        assert exc.retry_after == (3.0 if response.status == 503 else None)


def test_unknown_code_is_preserved() -> None:
    exc = _list_error(Response(418, {"DPE-Error-Code": "DPE_FUTURE"}))
    assert isinstance(exc, errors.UnknownCodeError)
    assert exc.code == "DPE_FUTURE"
    assert exc.retryable is False


def test_missing_content_header_only_has_untrusted_list() -> None:
    exc = _list_error(Response(400, {"DPE-Error-Code": "DPE_MISSING_CONTENT"}))
    assert isinstance(exc, errors.MissingContentError)
    assert exc.truncated is True


def test_redirect_and_other_statuses_are_unexpected() -> None:
    exc = _list_error(Response(302, {"Location": "elsewhere", "DPE-Error-Code": "DPE_NOT_FOUND"}))
    assert isinstance(exc, errors.UnexpectedResponseError)


def test_success_body_not_json() -> None:
    for body in [b"", b"{", b'{"documents": [], "next_cursor": null, "x": 1, "x": 2}']:
        exc = _list_error(Response(200, {"Content-Type": "application/json"}, body))
        assert isinstance(exc, errors.UnexpectedResponseError)
        assert not isinstance(exc, errors.DpeError)


# ---------------------------------------------------------------------- Retry-After


def test_retry_after_seconds() -> None:
    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds(None) is None
    assert retry_after_seconds(" 120 ") == 120.0
    assert retry_after_seconds("Thu, 08 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert retry_after_seconds("Thu, 08 Oct 2026 11:00:00 GMT", now=now) == 0.0
    for bad in ["", "-1", "1.5", "soon", "٣"]:
        assert retry_after_seconds(bad, now=now) is None
    # 合法但无法表示的秒数仍是「等待很久」，不是「没有提示」
    assert retry_after_seconds("9" * 400) == math.inf
    assert retry_after_seconds("9" * 5000) == math.inf
    # 不带时区的 now 按 UTC 理解
    naive = datetime(2026, 10, 8, 12, 0, 0)
    assert retry_after_seconds("Thu, 08 Oct 2026 12:00:30 GMT", now=naive) == 30.0


# ---------------------------------------------------------------------- 驱动约定


def test_transport_errors_are_thrown_into_the_operation() -> None:
    seen: list[BaseException] = []

    def op() -> Any:
        try:
            yield Request("GET", "x")
        except ConnectionError as exc:
            seen.append(exc)
        response = yield Request("GET", "y")
        return response.status

    calls: Iterator[Response | Exception] = iter([ConnectionError("down"), Response(204)])

    def send(request: Request) -> Response:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    assert drive(op(), send) == 204
    assert len(seen) == 1


def test_unhandled_transport_error_propagates_and_closes() -> None:
    op = core().batch_head(["a:1", "a:2", "a:3"])

    def send(request: Request) -> Response:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        drive(op, send)
    with pytest.raises(StopIteration):
        next(op)


def test_adrive() -> None:
    async def main() -> Any:
        responses = iter([ok({"heads": [None, None]}), ok({"heads": [{"doc_hash": H1}]})])

        async def send(request: Request) -> Response:
            await asyncio.sleep(0)
            return next(responses)

        return await adrive(core().batch_head(["a:1", "a:2", "a:3"]), send)

    assert asyncio.run(main()) == [None, None, H1]


def test_adrive_throws_transport_errors_into_the_operation() -> None:
    seen: list[BaseException] = []

    def op() -> Any:
        try:
            yield Request("GET", "x")
        except ConnectionError as exc:
            seen.append(exc)
        response = yield Request("GET", "y")
        return response.status

    calls: Iterator[Response | Exception] = iter([ConnectionError("down"), Response(204)])

    async def send(request: Request) -> Response:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    assert asyncio.run(adrive(op(), send)) == 204
    assert len(seen) == 1


def test_adrive_unhandled_transport_error_propagates_and_closes() -> None:
    op = core().batch_head(["a:1", "a:2", "a:3"])

    async def send(request: Request) -> Response:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        asyncio.run(adrive(op, send))
    with pytest.raises(StopIteration):
        next(op)


def test_adrive_zero_round_trips() -> None:
    async def send(request: Request) -> Response:
        raise AssertionError("不应发送请求")

    assert asyncio.run(adrive(core().batch_head([]), send)) == []


def test_response_headers_are_case_insensitive() -> None:
    response = Response(200, {"DPE-Doc-Hash": H1})
    assert response.header("dpe-doc-hash") == H1
    assert response.header("DPE-DOC-HASH") == H1
