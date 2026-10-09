"""sans-IO 协议核心（#48）：快路径 commit、CAS、delete / move 与重试规则（core §5.2）。

响应按 HTTP 绑定 §3.2、§4.8、§4.9、§5 的报文形状手工构造，用固定应答的 ``send`` 驱动操作；
末尾一组把操作产出的请求交给参考服务端引擎（``dpe_sdk.testing.Engine``）执行，确认请求体与
前置条件的形状被参考实现接受。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs

import dpe_hash
import pytest
from dpe_sdk import Document, errors
from dpe_sdk.protocol import (
    CONTRACT_HEADER,
    BaseHash,
    Force,
    IfAbsent,
    ProtocolCore,
    Request,
    Response,
    adrive,
    drive,
)
from dpe_sdk.testing import Engine
from dpe_sdk.wire import CommitResult, MoveResult
from engine_helpers import inline, make_engine, text_doc
from protocol_helpers import H1, H2, H_DRILL, caps, core, ok, problem, run

URI = "s3://bucket/a.md"
TARGET = "documents?uri=s3%3A%2F%2Fbucket%2Fa.md"


def lost(what: str = "响应丢失") -> ConnectionError:
    """每次新建异常实例：断言 ``__cause__`` 时能区分它来自哪一次发送。"""
    return ConnectionError(what)


def doc(*pages: list[str]) -> Document:
    return Document.model_validate(text_doc(*pages))


DOC = doc(["x", "y"])
DOC_HASH = DOC.doc_hash()


def committed(status: str, added: int, removed: int, retained: int, **headers: str) -> Response:
    """commit 的成功响应：created 为 201，其余为 200；默认带 ``DPE-Doc-Hash``。"""
    body = {
        "status": status,
        "doc_hash": DOC_HASH,
        "delta": {"added": added, "removed": removed, "retained": retained},
    }
    hdrs = {"DPE-Doc-Hash": DOC_HASH, **headers}
    return ok(body, 201 if status == "created" else 200, hdrs)


def unchanged() -> CommitResult:
    return CommitResult.model_validate(
        {
            "status": "unchanged",
            "doc_hash": DOC_HASH,
            "delta": {"added": 0, "removed": 0, "retained": 2},
        }
    )


def head_found(value: str) -> Response:
    return Response(200, {"DPE-Doc-Hash": value, "ETag": f'"{value}"'})


def head_missing() -> Response:
    return Response(404, {"DPE-Error-Code": "DPE_NOT_FOUND"})


def bare_412() -> Response:
    """网关自行求值条件头得到的 412：既无 problem 体、也无 ``DPE-Error-Code`` 头。"""
    return Response(412, {"Content-Type": "text/html"}, b"<h1>412</h1>")


class Flaky:
    """前 ``failures`` 次发送抛出连接错误（请求可能已到达服务端），之后按顺序回放响应。"""

    def __init__(self, failures: int, *responses: Response) -> None:
        self.failures = failures
        self.responses = iter(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if self.failures:
            self.failures -= 1
            raise lost()
        return next(self.responses)


class Script:
    """按脚本逐次给出响应或抛出异常。"""

    def __init__(self, *steps: Response | Exception) -> None:
        self.steps = iter(steps)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


def methods(requests: list[Request]) -> list[str]:
    return [r.method for r in requests]


# ---------------------------------------------------------------------- 构造


def test_max_resends_must_be_a_non_negative_int() -> None:
    assert core().max_resends == 2
    assert ProtocolCore(caps(), max_resends=0).max_resends == 0
    for bad in (-1, True, 1.5, "2"):
        with pytest.raises(ValueError, match="max_resends"):
            ProtocolCore(caps(), max_resends=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- commit：请求形状


def test_commit_if_absent_request() -> None:
    result, server = run(core().commit(URI, DOC, IfAbsent()), committed("created", 2, 0, 0))
    assert result.status == "created"
    (request,) = server.requests
    assert (request.method, request.target) == ("PUT", TARGET)
    assert request.headers[CONTRACT_HEADER] == "dpe1"
    assert request.headers["If-None-Match"] == "*"
    assert "If-Match" not in request.headers
    assert request.headers["Content-Type"] == "application/json"
    req = inline(text_doc(["x", "y"]))
    assert json.loads(request.body or b"") == {
        "document": req.document,
        "pages": req.pages,
        "objects": req.objects,
    }


def test_commit_base_hash_builds_if_match_itself() -> None:
    """条件头由客户端用 doc_hash 自行构造（HTTP 绑定 §3.1），不回显 ETag。"""
    _, server = run(core().commit(URI, DOC, BaseHash(H1)), committed("updated", 1, 1, 1))
    (request,) = server.requests
    assert request.headers["If-Match"] == f'"{H1}"'
    assert "If-None-Match" not in request.headers
    assert "force" not in json.loads(request.body or b"")


def test_commit_force_has_no_condition_header() -> None:
    _, server = run(core().commit(URI, DOC, Force()), committed("updated", 1, 1, 1))
    (request,) = server.requests
    assert "If-Match" not in request.headers and "If-None-Match" not in request.headers
    assert json.loads(request.body or b"")["force"] is True


def test_commit_dedups_pages_and_objects() -> None:
    """重复的页对象与元素对象各内联一份；文档对象与 delta 仍按完整序列计。"""
    document = doc(["x", "y"], ["x", "y"], ["y", "z"])
    body = {
        "status": "created",
        "doc_hash": document.doc_hash(),
        "delta": {"added": 6, "removed": 0, "retained": 0},
    }
    _, server = run(core().commit(URI, document, IfAbsent()), ok(body, 201))
    sent = json.loads(server.requests[0].body or b"")
    assert len(sent["document"]["pages"]) == 3
    assert len(sent["pages"]) == 2
    assert [o["text"] for o in sent["objects"]] == ["x", "y", "z"]


def test_commit_requires_a_precondition() -> None:
    """写操作没有「不带前置条件」的形式（core §5.1），本地即拒绝，不发请求。"""
    op = core().commit(URI, DOC, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="前置条件"):
        next(op)


def test_commit_requires_a_document_model() -> None:
    with pytest.raises(TypeError, match="Document"):
        next(core().commit(URI, text_doc(["x"]), IfAbsent()))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("1" * 64, dpe_hash.ContractUnsupportedError),  # 裸 hash
        (H_DRILL, dpe_hash.ContractUnsupportedError),  # 不是声明的契约
        ("dpe1:XYZ", dpe_hash.ValidationError),
    ],
)
def test_base_hash_must_belong_to_the_declared_contract(value: str, error: type[Exception]) -> None:
    c = core()
    for op in (
        c.commit(URI, DOC, BaseHash(value)),
        c.delete(URI, value),
        c.move(URI, "s3://b/c", value),
    ):
        with pytest.raises(error):
            next(op)


def test_write_operations_validate_uris_locally() -> None:
    c = core()
    for op in (
        c.commit("no scheme", DOC, IfAbsent()),
        c.delete("no scheme", H1),
        c.move("no scheme", URI, H1),
        c.move(URI, "no scheme", H1),
    ):
        with pytest.raises(dpe_hash.ValidationError):
            next(op)


def test_commit_under_drill_contract() -> None:
    c = core("dpe2", hash_contracts=["dpe1", "dpe2"])
    expected = DOC.doc_hash("dpe2")
    body = {
        "status": "created",
        "doc_hash": expected,
        "delta": {"added": 2, "removed": 0, "retained": 0},
    }
    result, server = run(c.commit(URI, DOC, IfAbsent()), ok(body, 201))
    assert result.doc_hash == expected
    assert server.requests[0].headers[CONTRACT_HEADER] == "dpe2"


# ---------------------------------------------------------------------- commit：结果校验


def test_commit_results() -> None:
    for response in (
        committed("created", 2, 0, 0),
        committed("updated", 1, 3, 1),
        committed("unchanged", 0, 0, 2),
    ):
        result, _ = run(core().commit(URI, DOC, BaseHash(H1)), response)
        assert result.doc_hash == DOC_HASH


@pytest.mark.parametrize(
    "response",
    [
        ok(
            {
                "status": "created",
                "doc_hash": DOC_HASH,
                "delta": {"added": 2, "removed": 0, "retained": 0},
            }
        ),
        ok(
            {
                "status": "updated",
                "doc_hash": DOC_HASH,
                "delta": {"added": 1, "removed": 0, "retained": 1},
            },
            201,
        ),
        ok(
            {
                "status": "updated",
                "doc_hash": H1,
                "delta": {"added": 1, "removed": 0, "retained": 1},
            }
        ),
        committed("updated", 1, 0, 0),  # added + retained ≠ 元素个数
        committed("created", 1, 0, 1),  # created 时 retained 应为 0
        committed("created", 2, 1, 0),  # created 时 removed 应为 0
        committed("unchanged", 1, 0, 1),  # unchanged 时 added 应为 0
        committed("updated", -1, 0, 3),
        committed("updated", 1, 0, 1, **{"DPE-Doc-Hash": H1}),
        ok({"status": "updated"}),
        Response(204),
    ],
)
def test_commit_rejects_inconsistent_results(response: Response) -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().commit(URI, DOC, BaseHash(H1)), response)


@pytest.mark.parametrize(
    ("code", "status", "error"),
    [
        ("DPE_PRECONDITION_FAILED", 412, errors.PreconditionFailedError),
        ("DPE_ALREADY_EXISTS", 412, errors.AlreadyExistsError),
        ("DPE_NOT_FOUND", 412, errors.NotFoundError),
        ("DPE_MISSING_CONTENT", 400, errors.MissingContentError),
        ("DPE_FORBIDDEN", 403, errors.ForbiddenError),
        ("DPE_PAYLOAD_TOO_LARGE", 413, errors.PayloadTooLargeError),
    ],
)
def test_commit_errors_are_raised_as_is(code: str, status: int, error: type[Exception]) -> None:
    """按 code 分派、如实上报：不重发、不 head，绝不改用 force（core §5.2）。"""
    server = Script(problem(code, status))
    with pytest.raises(error):
        drive(core().commit(URI, DOC, BaseHash(H1)), server)
    assert methods(server.requests) == ["PUT"]


# ---------------------------------------------------------------------- commit：响应丢失


def test_commit_lost_response_is_resent_and_recovers_as_unchanged() -> None:
    server = Flaky(1, committed("unchanged", 0, 0, 2))
    result = drive(core().commit(URI, DOC, BaseHash(H1)), server)
    assert result == unchanged()
    first, second = server.requests
    assert first == second


def test_commit_resend_then_precondition_failed_is_reported() -> None:
    server = Flaky(1, problem("DPE_PRECONDITION_FAILED", 412))
    with pytest.raises(errors.PreconditionFailedError):
        drive(core().commit(URI, DOC, BaseHash(H1)), server)
    assert methods(server.requests) == ["PUT", "PUT"]


@pytest.mark.parametrize("max_resends", [0, 1, 3])
def test_commit_resends_are_bounded(max_resends: int) -> None:
    server = Flaky(max_resends + 1)
    with pytest.raises(ConnectionError):
        drive(ProtocolCore(caps(), max_resends=max_resends).commit(URI, DOC, IfAbsent()), server)
    assert methods(server.requests) == ["PUT"] * (max_resends + 1)


def test_force_lost_response_is_not_replayed_head_confirms() -> None:
    server = Script(lost(), head_found(DOC_HASH))
    result = drive(core().commit(URI, DOC, Force()), server)
    assert result == unchanged()
    put, head = server.requests
    assert put.method == "PUT" and head.method == "HEAD"
    assert head.target == TARGET


@pytest.mark.parametrize("current", [H1, None])
def test_force_lost_response_not_confirmed_is_reported(current: str | None) -> None:
    put_lost = lost("put")
    server = Script(put_lost, head_found(current) if current else head_missing())
    with pytest.raises(errors.ForceNotConfirmedError) as info:
        drive(core().commit(URI, DOC, Force()), server)
    assert (info.value.doc_hash, info.value.current, info.value.checked) == (
        DOC_HASH,
        current,
        True,
    )
    assert info.value.__cause__ is put_lost
    assert info.value.retryable is False
    assert methods(server.requests) == ["PUT", "HEAD"]


def test_force_lost_response_and_head_failure() -> None:
    """head 本身的响应丢失按只读重发；仍失败则无法确认，同样不重放 force。"""
    head_lost = lost("head 3")
    server = Script(lost("put"), lost("head 1"), lost("head 2"), head_lost)
    with pytest.raises(errors.ForceNotConfirmedError) as info:
        drive(core().commit(URI, DOC, Force()), server)
    assert info.value.checked is False
    assert info.value.__cause__ is head_lost
    assert methods(server.requests) == ["PUT", "HEAD", "HEAD", "HEAD"]


def test_force_head_resend_then_confirms() -> None:
    server = Script(lost(), lost(), head_found(DOC_HASH))
    assert drive(core().commit(URI, DOC, Force()), server) == unchanged()
    assert methods(server.requests) == ["PUT", "HEAD", "HEAD"]


def test_force_head_error_is_not_confirmed() -> None:
    server = Script(lost(), problem("DPE_UNAVAILABLE", 503))
    with pytest.raises(errors.ForceNotConfirmedError) as info:
        drive(core().commit(URI, DOC, Force()), server)
    assert info.value.checked is False
    assert isinstance(info.value.__cause__, errors.UnavailableError)


# ---------------------------------------------------------------------- commit：无 DPE 错误码的 412


def test_commit_bare_412_head_equal_is_success() -> None:
    server = Script(bare_412(), head_found(DOC_HASH))
    assert drive(core().commit(URI, DOC, BaseHash(H1)), server) == unchanged()
    assert methods(server.requests) == ["PUT", "HEAD"]


@pytest.mark.parametrize(
    ("precondition", "head", "error"),
    [
        (BaseHash(H1), head_missing(), errors.NotFoundError),
        (BaseHash(H1), head_found(H2), errors.PreconditionFailedError),
        (IfAbsent(), head_found(H2), errors.AlreadyExistsError),
        # 按 HTTP 绑定 §3.2 字面；If-None-Match 下这是中间层误判，规范待补正（#81）
        (IfAbsent(), head_missing(), errors.NotFoundError),
    ],
)
def test_commit_bare_412_is_judged_by_head(
    precondition: BaseHash | IfAbsent, head: Response, error: type[Exception]
) -> None:
    server = Script(bare_412(), head)
    with pytest.raises(error):
        drive(core().commit(URI, DOC, precondition), server)
    assert methods(server.requests) == ["PUT", "HEAD"]


def test_commit_lost_then_bare_412_is_judged_by_head() -> None:
    server = Script(lost(), bare_412(), head_found(DOC_HASH))
    assert drive(core().commit(URI, DOC, BaseHash(H1)), server) == unchanged()
    assert methods(server.requests) == ["PUT", "PUT", "HEAD"]


def test_commit_412_with_error_code_header_is_not_bare() -> None:
    server = Script(Response(412, {"DPE-Error-Code": "DPE_PRECONDITION_FAILED"}))
    with pytest.raises(errors.PreconditionFailedError):
        drive(core().commit(URI, DOC, BaseHash(H1)), server)
    assert methods(server.requests) == ["PUT"]


def test_force_bare_412_is_unexpected() -> None:
    """force 不带条件头，412 无从谈起：不回退 head，按非协议错误上报。"""
    server = Script(bare_412())
    with pytest.raises(errors.UnexpectedResponseError):
        drive(core().commit(URI, DOC, Force()), server)
    assert methods(server.requests) == ["PUT"]


# ---------------------------------------------------------------------- delete


def test_delete_request() -> None:
    result, server = run(core().delete(URI, H1), Response(204))
    assert result is None
    (request,) = server.requests
    assert (request.method, request.target, request.body) == ("DELETE", TARGET, None)
    assert request.headers["If-Match"] == f'"{H1}"'
    assert request.headers[CONTRACT_HEADER] == "dpe1"


def test_delete_not_found_on_first_attempt_is_reported() -> None:
    with pytest.raises(errors.NotFoundError):
        run(core().delete(URI, H1), problem("DPE_NOT_FOUND", 412))


def test_delete_resend_not_found_is_success() -> None:
    """响应丢失后重发得到 DPE_NOT_FOUND：目标状态「不存在」已达成（core §5.2）。"""
    server = Flaky(1, problem("DPE_NOT_FOUND", 412))
    assert drive(core().delete(URI, H1), server) is None
    assert methods(server.requests) == ["DELETE", "DELETE"]


def test_delete_resend_succeeds() -> None:
    server = Flaky(1, Response(204))
    assert drive(core().delete(URI, H1), server) is None
    assert methods(server.requests) == ["DELETE", "DELETE"]


def test_delete_resend_precondition_failed_is_reported() -> None:
    server = Flaky(1, problem("DPE_PRECONDITION_FAILED", 412))
    with pytest.raises(errors.PreconditionFailedError):
        drive(core().delete(URI, H1), server)


def test_delete_resends_are_bounded() -> None:
    server = Flaky(3)
    with pytest.raises(ConnectionError):
        drive(core().delete(URI, H1), server)
    assert methods(server.requests) == ["DELETE"] * 3


def test_delete_bare_412_head_missing_is_success() -> None:
    server = Script(bare_412(), head_missing())
    assert drive(core().delete(URI, H1), server) is None
    assert methods(server.requests) == ["DELETE", "HEAD"]


def test_delete_bare_412_head_differs_is_precondition_failed() -> None:
    server = Script(bare_412(), head_found(H2))
    with pytest.raises(errors.PreconditionFailedError):
        drive(core().delete(URI, H1), server)
    assert methods(server.requests) == ["DELETE", "HEAD"]


@pytest.mark.parametrize("response", [Response(204), problem("DPE_NOT_FOUND", 412)])
def test_delete_bare_412_head_equal_resends_once(response: Response) -> None:
    """doc_hash 仍等于 If-Match：中间层误判，原样重发一次（HTTP 绑定 §3.2）。"""
    server = Script(bare_412(), head_found(H1), response)
    assert drive(core().delete(URI, H1), server) is None
    first, head, again = server.requests
    assert (head.method, again) == ("HEAD", first)


def test_delete_bare_412_twice_with_head_still_equal_is_reported() -> None:
    server = Script(bare_412(), head_found(H1), bare_412(), head_found(H1))
    with pytest.raises(errors.UnexpectedResponseError, match="412"):
        drive(core().delete(URI, H1), server)
    assert methods(server.requests) == ["DELETE", "HEAD", "DELETE", "HEAD"]


def test_delete_bare_412_twice_is_judged_by_head_again() -> None:
    """重发前他人改写或删除了文档：第二次 412 再经 head 判定，不误报为不合规响应。"""
    server = Script(bare_412(), head_found(H1), bare_412(), head_found(H2))
    with pytest.raises(errors.PreconditionFailedError):
        drive(core().delete(URI, H1), server)
    server = Script(bare_412(), head_found(H1), bare_412(), head_missing())
    assert drive(core().delete(URI, H1), server) is None
    assert methods(server.requests) == ["DELETE", "HEAD", "DELETE", "HEAD"]


def test_delete_lost_then_bare_412_head_missing_is_success() -> None:
    server = Script(lost(), bare_412(), head_missing())
    assert drive(core().delete(URI, H1), server) is None
    assert methods(server.requests) == ["DELETE", "DELETE", "HEAD"]


# ---------------------------------------------------------------------- 无 DPE 错误码的 502 / 504


def gateway(status: int) -> Response:
    return Response(status, {"Content-Type": "text/html"}, b"<h1>gateway</h1>")


@pytest.mark.parametrize("status", [502, 504])
def test_force_gateway_timeout_is_confirmed_by_head(status: int) -> None:
    """网关已转发、源站结果未知：force 不重放，经 head 确认（core §5.2）。"""
    server = Script(gateway(status), head_found(DOC_HASH))
    assert drive(core().commit(URI, DOC, Force()), server) == unchanged()
    assert methods(server.requests) == ["PUT", "HEAD"]


def test_force_gateway_timeout_not_confirmed() -> None:
    server = Script(gateway(504), head_found(H1))
    with pytest.raises(errors.ForceNotConfirmedError) as info:
        drive(core().commit(URI, DOC, Force()), server)
    assert info.value.checked is True
    cause = info.value.__cause__
    assert isinstance(cause, errors.UnexpectedResponseError) and cause.status == 504


def test_gateway_timeout_is_resent_for_cas_writes() -> None:
    server = Script(gateway(504), committed("unchanged", 0, 0, 2))
    assert drive(core().commit(URI, DOC, BaseHash(H1)), server) == unchanged()
    server = Script(gateway(502), problem("DPE_NOT_FOUND", 412))
    assert drive(core().delete(URI, H1), server) is None
    server = Script(gateway(504), ok({"doc_hash": H1}, 200, {"DPE-Doc-Hash": H1}))
    assert drive(core().move(URI, "s3://bucket/b.md", H1), server).doc_hash == H1


def test_gateway_timeout_resends_are_bounded() -> None:
    server = Script(gateway(504), gateway(504), gateway(504))
    with pytest.raises(errors.UnexpectedResponseError) as info:
        drive(core().commit(URI, DOC, IfAbsent()), server)
    assert info.value.status == 504
    assert methods(server.requests) == ["PUT"] * 3


def test_gateway_status_with_error_code_is_a_dpe_error() -> None:
    """带 DPE 错误码的 502 / 504 是服务端给出的结果，按 code 分派，不当作响应丢失。"""
    server = Script(problem("DPE_UNAVAILABLE", 504))
    with pytest.raises(errors.UnavailableError):
        drive(core().commit(URI, DOC, Force()), server)
    assert methods(server.requests) == ["PUT"]


def test_delete_unexpected_status() -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().delete(URI, H1), ok({}))


# ---------------------------------------------------------------------- move


def moved(value: str = H1, **headers: str) -> Response:
    return ok({"doc_hash": value}, 200, {"DPE-Doc-Hash": value, **headers})


def test_move_request() -> None:
    result, server = run(core().move(URI, "s3://bucket/b.md", H1), moved())
    assert result == MoveResult(doc_hash=H1)
    (request,) = server.requests
    assert (request.method, request.target) == ("POST", "move")
    assert "If-Match" not in request.headers
    assert json.loads(request.body or b"") == {
        "from_uri": URI,
        "to_uri": "s3://bucket/b.md",
        "base_hash": H1,
    }


@pytest.mark.parametrize(
    "response",
    [moved(H2), moved(H1, **{"DPE-Doc-Hash": H2}), ok({}), Response(204)],
)
def test_move_rejects_inconsistent_results(response: Response) -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().move(URI, "s3://bucket/b.md", H1), response)


def test_move_lost_response_is_resent() -> None:
    """服务端对重发按「目标状态已达成」返回成功（core §5.2 第 4 步）。"""
    server = Flaky(2, moved())
    assert drive(core().move(URI, "s3://bucket/b.md", H1), server) == MoveResult(doc_hash=H1)
    assert len(server.requests) == 3 and len({r.body for r in server.requests}) == 1


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("DPE_NOT_FOUND", errors.NotFoundError),
        ("DPE_PRECONDITION_FAILED", errors.PreconditionFailedError),
        ("DPE_ALREADY_EXISTS", errors.AlreadyExistsError),
    ],
)
def test_move_resend_errors_are_reported(code: str, error: type[Exception]) -> None:
    """首次 move 之后目标又被改写或删除，重发得到的错误如实上报（core §5.2）。"""
    server = Flaky(1, problem(code, 404 if code == "DPE_NOT_FOUND" else 409))
    with pytest.raises(error):
        drive(core().move(URI, "s3://bucket/b.md", H1), server)


# ---------------------------------------------------------------------- 异步驱动


def test_adrive_write_operation_with_lost_response() -> None:
    server = Script(lost(), head_found(DOC_HASH))

    async def send(request: Request) -> Response:
        return server(request)

    assert asyncio.run(adrive(core().commit(URI, DOC, Force()), send)) == unchanged()


# ---------------------------------------------------------------------- 对参考服务端引擎


def engine_send(engine: Engine, caller: str = "u") -> Any:
    """把操作产出的请求交给引擎：条件头还原为前置条件，结果与 ``DpeError`` 映射为响应。"""

    def send(request: Request) -> Response:
        path, _, query = request.target.partition("?")
        uri = parse_qs(query).get("uri", [""])[0]
        contract = request.headers.get(CONTRACT_HEADER)
        try:
            if request.method == "PUT":
                if_match = request.headers.get("If-Match")
                precondition = (
                    BaseHash(if_match.strip('"'))
                    if if_match
                    else IfAbsent()
                    if request.headers.get("If-None-Match") == "*"
                    else None
                )
                result = engine.commit(caller, uri, request.body or b"", contract, precondition)
                status = 201 if result.status == "created" else 200
                return ok(result.model_dump(), status, {"DPE-Doc-Hash": result.doc_hash})
            if request.method == "DELETE":
                base = (request.headers.get("If-Match") or "").strip('"') or None
                engine.delete(caller, uri, contract, base)
                return Response(204)
            if request.method == "HEAD":
                head = engine.head(caller, uri, contract)
                return head_found(head.doc_hash) if head else head_missing()
            assert (request.method, path) == ("POST", "move")
            moved_to = engine.move(caller, request.body or b"", contract)
            return ok(moved_to.model_dump(), 200, {"DPE-Doc-Hash": moved_to.doc_hash})
        except errors.DpeError as exc:
            return problem(exc.code, 400)

    return send


def test_engine_accepts_commit_delete_and_move_requests() -> None:
    engine = make_engine()
    send = engine_send(engine)
    c = core(batch_head_max=500)
    created = drive(c.commit(URI, doc(["x", "y"], ["x", "y"]), IfAbsent()), send)
    assert (created.status, created.delta.added) == ("created", 4)
    updated = drive(c.commit(URI, DOC, BaseHash(created.doc_hash)), send)
    assert (updated.status, updated.delta.removed, updated.delta.retained) == ("updated", 2, 2)
    assert drive(c.commit(URI, DOC, Force()), send).status == "unchanged"
    with pytest.raises(errors.PreconditionFailedError):
        drive(c.commit(URI, doc(["z"]), BaseHash(created.doc_hash)), send)

    target = "s3://bucket/b.md"
    assert drive(c.move(URI, target, DOC_HASH), send) == MoveResult(doc_hash=DOC_HASH)
    assert drive(c.head(URI), send) is None
    drive(c.delete(target, DOC_HASH), send)
    assert drive(c.head(target), send) is None
    with pytest.raises(errors.NotFoundError):
        drive(c.delete(target, DOC_HASH), send)


class LoseFirstResponse:
    """引擎执行每种写方法的首个请求后丢弃其响应（服务端已生效，客户端收不到结果）。"""

    def __init__(self, send: Any) -> None:
        self.send = send
        self.lost: set[str] = set()
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        response: Response = self.send(request)
        if request.method != "HEAD" and request.method not in self.lost:
            self.lost.add(request.method)
            raise lost()
        return response


def test_engine_round_trip_of_retry_rules() -> None:
    """首次请求已生效、响应丢失：commit 重发得 unchanged，move 重发按「目标状态已达成」成功，
    delete 重发得 NOT_FOUND 即成功。"""
    send = engine_send(make_engine())
    lossy = LoseFirstResponse(send)
    c = core()
    target = "s3://bucket/b.md"
    assert drive(c.commit(URI, DOC, IfAbsent()), lossy) == unchanged()
    assert drive(c.move(URI, target, DOC_HASH), lossy) == MoveResult(doc_hash=DOC_HASH)
    assert drive(c.delete(target, DOC_HASH), lossy) is None
    assert methods(lossy.requests) == ["PUT", "PUT", "POST", "POST", "DELETE", "DELETE"]
    assert drive(c.head(URI), send) is None
    assert drive(c.head(target), send) is None


def test_engine_round_trip_of_lost_force() -> None:
    """force 已生效、响应丢失：不重放，经 head 确认。"""
    lossy = LoseFirstResponse(engine_send(make_engine()))
    assert drive(core().commit(URI, DOC, Force()), lossy) == unchanged()
    assert methods(lossy.requests) == ["PUT", "HEAD"]
