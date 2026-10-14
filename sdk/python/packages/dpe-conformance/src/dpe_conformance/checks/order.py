"""章：求值顺序与校验顺序（#56 全量 3/6）。

错误码跨实现唯一：多处同时违例时，每个实现都返回规范中**先判**的那一步的码。本章的组合
用例一律「同时违反两处，断言先判的那个」：

- HTTP §3.1 传输层四步：路由（404 / 405 + ``Allow``）→ 认证（401 + ``WWW-Authenticate``）→
  查询参数的形态 → 请求体（core §3.3 第 0 步，413）；前三步都不读请求体。限流与不可用
  （429 / 503）**不在**这个顺序内（带码的须早于请求产生任何效果，core §6），本章不对它们下结论；
- HTTP §3.1 + core §1.1：``uri`` 的文法校验在契约声明之后（有请求体的端点与请求信封同一步，
  无体端点「契约 → uri」），先于授权与一切依赖文档状态的判定；
- HTTP §3.2 / §3.3：写操作条件头的语法在 core §3.3 第 1.5 步（授权之前，unchanged 也不放行）；
  GET 的 ``If-None-Match`` 语法在「契约 → uri」之后、文档状态之前；
- HTTP §4.6 / §4.7：元素对象带 ``Content-Range`` 按原始字节分流（契约在前、先于 I-JSON 与会话）；
  分块阶梯第 4 步的 ``Content-Encoding`` 违例在会话判定与「已完成 → 200」之后；断点查询
  「契约 → 路径 hash 语法 → 会话」；blob 整体上传「契约 → 路径引用语法 → sha256 → 会话」；
- core §3.3 / §5.2（#63）：报文校验先于授权；commit 的每个错误码只出自唯一一步。

「无写授权」用 scoped 身份写其前缀之外的 URI；依赖「未认证即 401」的检查以探针为前提——
服务端接受匿名请求时如实 skip（认证方案由服务端决定，HTTP §6）。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import dpe_hash
import httpx
import pytest
from dpe_hash import ElementObject

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WRONG_HASH,
    WireDocument,
    assert_commit_success,
    assert_problem,
    assert_staging_head_error,
    build_document,
    commit,
    delete,
    inside_scoped,
    move,
    negotiate,
    optional_identity,
    outside_scoped,
    payload_limit_within_budget,
    seed,
    session_of,
    text_element,
    upload_blob,
)

#: 不符合 core §1.1 文法的 URI（无 scheme）
_BAD_URI = "zzz-no-scheme"
#: 不是任何合法 hash / 引用的路径段（HTTP §4.7 路径 hash 的语法）
_BAD_REF = "not-a-hash"


def _document(text: str = "order") -> WireDocument:
    return build_document([[text_element(text)]])


def _open_session(target: Target, identity: Identity, name: str) -> str:
    response = negotiate(target, target.uri(f"order/{name}"), _document(name), identity=identity)
    assert response.status_code == 200, f"negotiate 失败：{response.status_code}"
    session_id, _ = session_of(response)
    return session_id


def _forged_session() -> str:
    return "st-" + uuid.uuid4().hex


def _assert_not_dpe(response: httpx.Response, status: int) -> None:
    """HTTP §5：非协议错误——既无 problem 体，也无 ``DPE-Error-Code`` 头。"""
    where = f"{response.request.method} {response.request.url}"
    assert response.status_code == status, f"{where}：预期 {status}，实际 {response.status_code}"
    assert "DPE-Error-Code" not in response.headers, f"{where}：非 DPE 错误不得带 DPE-Error-Code"
    content_type = response.headers.get("content-type", "")
    assert not content_type.startswith("application/problem+json"), (
        f"{where}：非 DPE 错误不得带 problem 体"
    )


# ---------------------------------------------------------------------- 传输层（HTTP §3.1、§6）


@pytest.fixture(scope="module")
def requires_auth(conformance_target: Target) -> None:
    """探针：不带凭证的合法 ``HEAD documents`` 得到 401 才说明服务端要求认证；
    服务端接受匿名请求时，「认证先于 X」无从观测，如实 skip。"""
    probe = conformance_target.send(
        "HEAD", "documents", params={"uri": conformance_target.uri("order/auth-probe")}
    )
    if probe.status_code != 401:
        pytest.skip(f"服务端接受匿名请求（探针得到 {probe.status_code}），认证顺序无从观测")


def test_unknown_path_is_404_not_dpe(conformance_target: Target) -> None:
    """HTTP §3.1 第 1 步 + §5：未知路径 → 404，不是 DPE 错误。"""
    response = conformance_target.send(
        "GET", "no-such-endpoint", identity=optional_identity(conformance_target)
    )
    _assert_not_dpe(response, 404)


def test_method_mismatch_is_405_with_allow(conformance_target: Target) -> None:
    """HTTP §3.1 第 1 步 + §5：路径存在但方法不符 → 405 + ``Allow``（列出允许的方法），
    不是 DPE 错误。"""
    response = conformance_target.send(
        "DELETE", "capabilities", contract=None, identity=optional_identity(conformance_target)
    )
    _assert_not_dpe(response, 405)
    allowed = {part.strip().upper() for part in response.headers.get("Allow", "").split(",")}
    assert "GET" in allowed, f"Allow 必须列出 GET：{response.headers.get('Allow')!r}"


@pytest.mark.usefixtures("requires_auth")
def test_authentication_precedes_all_validation(conformance_target: Target) -> None:
    """HTTP §3.1 第 2 步 + §6：未认证在一切报文校验之前——重复 ``uri``、缺契约头、非 JSON
    请求体同时出现，仍是 401 + ``WWW-Authenticate``（无 problem 体、无 DPE-Error-Code）。"""
    response = conformance_target.send(
        "PUT",
        "documents",
        contract=None,
        params=[("uri", conformance_target.uri("order/a")), ("uri", conformance_target.uri("b"))],
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    _assert_not_dpe(response, 401)
    assert "WWW-Authenticate" in response.headers, "401 必须带 WWW-Authenticate（HTTP §6）"


@pytest.mark.usefixtures("requires_auth")
def test_route_precedes_authentication(conformance_target: Target) -> None:
    """HTTP §3.1：路由先于认证——未知路径不带凭证也是 404，不是 401。"""
    _assert_not_dpe(conformance_target.send("GET", "no-such-endpoint"), 404)


def _send_oversized(target: Target, **kwargs: Any) -> httpx.Response:
    """发送超限请求体。规范要求服务端在读完请求体之前就作出判定，合规服务端因此可能提前回应
    并关闭连接；客户端仍在写时会读不到那个响应——此时无从判定，如实 skip，不判错误。"""
    try:
        return target.send("PUT", "documents", **kwargs)
    except (httpx.WriteError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
        pytest.skip(f"服务端在请求体发完前关闭了连接，客户端读不到响应，无从判定：{exc!r}")


def test_query_form_precedes_body_size(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.1 第 3 步先于第 4 步：查询参数的形态（重复 ``uri``）在读取请求体之前判定——
    坏查询 + 超大请求体 → DPE_VALIDATION，不是 413。"""
    limit = payload_limit_within_budget(conformance_target, full_identity)
    uri = conformance_target.uri("order/query-before-body")
    response = _send_oversized(
        conformance_target,
        identity=full_identity,
        params=[("uri", uri), ("uri", uri + "-2")],
        content=b"x" * (limit + 1),
        headers={"Content-Type": "application/json"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_payload_size_precedes_contract_and_authorization(
    conformance_target: Target, scoped_identity: Identity
) -> None:
    """HTTP §3.1 第 4 步 + core §3.3 第 0 步：超过 ``max_payload_bytes`` 的请求体先于 I-JSON、
    契约与授权——非 JSON、缺契约头、无写授权同时出现，仍是 413。"""
    limit = payload_limit_within_budget(conformance_target, scoped_identity)
    response = _send_oversized(
        conformance_target,
        identity=scoped_identity,
        contract=None,
        params={"uri": outside_scoped(conformance_target, scoped_identity, "too-large")},
        content=b"x" * (limit + 1),
        headers={"Content-Type": "application/json"},
    )
    assert_problem(response, status=413, code="DPE_PAYLOAD_TOO_LARGE")


# ---------------------------------------------------------------------- uri 的位置（HTTP §3.1）


def _send_with_uri(
    target: Target, endpoint: str, uri: str, *, identity: Identity, contract: str | None
) -> tuple[httpx.Response, bool]:
    """以 ``uri`` 调用端点；返回 (响应, 是否 documents 路径)。"""
    document = _document()
    if endpoint == "commit":
        response = target.send(
            "PUT",
            "documents",
            identity=identity,
            contract=contract,
            params={"uri": uri},
            headers={"If-None-Match": "*"},
            json=document.commit_body(),
        )
        return response, True
    if endpoint == "negotiate":
        body: dict[str, Any] = {"file_uri": uri, "document": document.document}
        return target.send(
            "POST", "negotiate", identity=identity, contract=contract, json=body
        ), False
    if endpoint == "delete":
        response = target.send(
            "DELETE",
            "documents",
            identity=identity,
            contract=contract,
            params={"uri": uri},
            headers={"If-Match": f'"{WRONG_HASH}"'},
        )
        return response, True
    assert endpoint == "get_skeleton"
    return target.send(
        "GET", "documents", identity=identity, contract=contract, params={"uri": uri}
    ), True


@pytest.mark.parametrize("endpoint", ["commit", "negotiate", "delete", "get_skeleton"])
def test_contract_precedes_uri_grammar(
    conformance_target: Target, full_identity: Identity, endpoint: str
) -> None:
    """HTTP §3.1：uri 文法校验在契约声明之后——缺契约头 + 非法 uri → DPE_CONTRACT_UNSUPPORTED
    （HEAD 的同一断言在 validation 章）。"""
    response, documents_path = _send_with_uri(
        conformance_target, endpoint, _BAD_URI, identity=full_identity, contract=None
    )
    assert_problem(
        response, status=400, code="DPE_CONTRACT_UNSUPPORTED", documents_path=documents_path
    )


@pytest.mark.parametrize("endpoint", ["commit", "negotiate", "delete"])
def test_uri_grammar_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity, endpoint: str
) -> None:
    """HTTP §3.1：uri 文法只看请求本身，先于授权——无写授权的身份提交非法 uri →
    DPE_VALIDATION，不是 403。"""
    response, documents_path = _send_with_uri(
        conformance_target,
        endpoint,
        _BAD_URI,
        identity=scoped_identity,
        contract=dpe_hash.CONTRACT,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=documents_path)


def test_move_uri_grammar_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity
) -> None:
    """HTTP §3.1 + core §5.2：move 的 ``from_uri`` 文法与请求信封同一步，先于两侧授权。"""
    response = move(
        conformance_target,
        _BAD_URI,
        outside_scoped(conformance_target, scoped_identity, "move-to"),
        identity=scoped_identity,
        base_hash=WRONG_HASH,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


# ---------------------------------------------------------------------- 条件头（HTTP §3.2、§3.3）


def test_commit_condition_syntax_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity
) -> None:
    """HTTP §3.2：条件头语法在 core §3.3 第 1.5 步，先于授权——无写授权 + ``If-Match: *`` →
    DPE_VALIDATION，不是 403。"""
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=scoped_identity,
        params={"uri": outside_scoped(conformance_target, scoped_identity, "cond")},
        headers={"If-Match": "*"},
        json=_document().commit_body(),
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_condition_syntax_is_not_bypassed_by_unchanged(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2 + core §3.3：第 1.5 步先于第 4 步——内容未变但 ``If-Match`` 语法非法 →
    DPE_VALIDATION，unchanged 不放行它。"""
    uri = conformance_target.uri("order/unchanged-syntax")
    document = _document("same")
    seed(conformance_target, uri, document, identity=full_identity)
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": '"a", "b"'},
        json=document.commit_body(),
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


@pytest.mark.parametrize(
    "headers", [{"If-Match": "*"}, {"If-None-Match": "*"}], ids=["if-match-star", "if-none-match"]
)
def test_delete_condition_syntax_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity, headers: dict[str, str]
) -> None:
    """HTTP §3.2（#63）：delete 的条件头语法同样排在授权之前——无权 + 非法条件头的 DELETE →
    400 DPE_VALIDATION，不是 403。"""
    response = delete(
        conformance_target,
        outside_scoped(conformance_target, scoped_identity, "delete-cond"),
        identity=scoped_identity,
        headers=headers,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_get_if_none_match_syntax_precedes_document_state(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.3：GET 的 ``If-None-Match`` 语法先于文档状态——文档不存在 + 非法取值 →
    DPE_VALIDATION，不是 404。"""
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("order/absent-inm")},
        headers={"If-None-Match": "x"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_contract_precedes_get_if_none_match_syntax(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.3：「契约 → uri → 条件头语法」——缺契约头 + 非法 ``If-None-Match`` →
    DPE_CONTRACT_UNSUPPORTED。"""
    response = conformance_target.send(
        "GET",
        "documents",
        identity=full_identity,
        contract=None,
        params={"uri": conformance_target.uri("order/absent-inm")},
        headers={"If-None-Match": "x"},
    )
    assert_problem(response, status=400, code="DPE_CONTRACT_UNSUPPORTED")


# ---------------------------------------------------------------------- 暂存上传（HTTP §4.6、§4.7）


def _element_path(session_id: str) -> str:
    content_hash = dpe_hash.content_hash(cast(ElementObject, text_element("element")))
    return f"staging/{session_id}/objects/{content_hash}"


@pytest.mark.parametrize("ranged", [True, False], ids=["content-range", "plain"])
def test_element_upload_splits_on_content_range(
    conformance_target: Target, full_identity: Identity, ranged: bool
) -> None:
    """HTTP §4.6：元素对象的 PUT 按有无 ``Content-Range`` 分流——非 JSON 体 + 缺契约头时，
    带它按原始字节处理、契约在前（DPE_CONTRACT_UNSUPPORTED）；不带它仍是 I-JSON 在前
    （DPE_VALIDATION）。"""
    session_id = _open_session(conformance_target, full_identity, "element-split")
    headers = {"Content-Type": "application/json"}
    if ranged:
        headers["Content-Range"] = "bytes 0-0/1"
    response = conformance_target.send(
        "PUT",
        _element_path(session_id),
        identity=full_identity,
        contract=None,
        content=b"{",
        headers=headers,
    )
    code = "DPE_CONTRACT_UNSUPPORTED" if ranged else "DPE_VALIDATION"
    assert_problem(response, status=400, code=code, documents_path=False)


def test_element_content_range_precedes_session(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.6：元素对象不分块，``Content-Range`` 只看头（契约之后），先于会话判定——
    伪造会话 + ``Content-Range`` → DPE_VALIDATION，不是 410。"""
    response = conformance_target.send(
        "PUT",
        _element_path(_forged_session()),
        identity=full_identity,
        content=b"{",
        headers={"Content-Range": "bytes 0-0/1"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


def _chunk(
    target: Target,
    session_id: str,
    identity: Identity,
    *,
    data: bytes = b"chunk-bytes",
    encoding: str | None = None,
) -> httpx.Response:
    headers = {"Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"}
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    return target.send(
        "PUT",
        f"staging/{session_id}/blobs/{dpe_hash.blob_ref(data)}",
        identity=identity,
        content=data,
        headers=headers,
    )


@pytest.mark.parametrize("encoding", ["gzip", "br"])
def test_chunk_with_content_encoding_is_validation(
    conformance_target: Target, full_identity: Identity, encoding: str
) -> None:
    """HTTP §4.7 第 4 步：分块请求同时带 ``Content-Encoding`` → DPE_VALIDATION；带不支持的编码
    也不走 415（带 ``Content-Range`` 的请求不适用 §3.1 第 4 步的 415，§7）。"""
    session_id = _open_session(conformance_target, full_identity, f"encoding-{encoding}")
    response = _chunk(conformance_target, session_id, full_identity, encoding=encoding)
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


def test_session_precedes_chunk_encoding_check(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：会话判定（第 2 步）先于分块一致性（第 4 步）——伪造会话 + 带
    ``Content-Encoding`` 的分块 → 410。"""
    response = _chunk(conformance_target, _forged_session(), full_identity, encoding="gzip")
    assert_problem(response, status=410, code="DPE_SESSION_EXPIRED", documents_path=False)


def test_completed_resend_with_content_encoding_is_200(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：「本会话内已完成 → 200」（第 3 步）先于第 4 步——已完成后带
    ``Content-Encoding`` 重发任意块仍得 200，幂等不受影响。"""
    session_id = _open_session(conformance_target, full_identity, "encoding-resend")
    data = b"chunk-bytes"
    first = upload_blob(conformance_target, session_id, data, identity=full_identity)
    assert first.status_code == 201, f"整体上传失败：{first.status_code}"
    resend = _chunk(conformance_target, session_id, full_identity, data=data, encoding="br")
    assert resend.status_code == 200, f"已完成后重发必须 200，实际 {resend.status_code}"


@pytest.mark.parametrize("kind", ["pages", "blobs"])
@pytest.mark.parametrize("session", ["live", "forged"])
def test_breakpoint_path_syntax_precedes_session(
    conformance_target: Target, full_identity: Identity, kind: str, session: str
) -> None:
    """HTTP §4.7：断点查询「契约 → 路径 hash 语法 → 会话」——非法路径 hash 在任何会话下都是
    DPE_VALIDATION：有效会话下不得得到「合法但无进度」的 200 + 0，伪造会话下不是 410。"""
    session_id = (
        _open_session(conformance_target, full_identity, f"bp-{kind}")
        if session == "live"
        else _forged_session()
    )
    response = conformance_target.send(
        "HEAD", f"staging/{session_id}/{kind}/{_BAD_REF}", identity=full_identity
    )
    assert_staging_head_error(response, status=400, code="DPE_VALIDATION")


@pytest.mark.parametrize("kind", ["pages", "blobs"])
def test_contract_precedes_breakpoint_path_syntax(
    conformance_target: Target, full_identity: Identity, kind: str
) -> None:
    """HTTP §4.7：断点查询同样 MUST 带契约头，且排在路径 hash 语法之前。"""
    session_id = _open_session(conformance_target, full_identity, f"bp-contract-{kind}")
    response = conformance_target.send(
        "HEAD", f"staging/{session_id}/{kind}/{_BAD_REF}", identity=full_identity, contract=None
    )
    assert_staging_head_error(response, status=400, code="DPE_CONTRACT_UNSUPPORTED")


def _put_blob(
    target: Target,
    session_id: str,
    ref: str,
    data: bytes,
    identity: Identity,
    *,
    contract: str | None = dpe_hash.CONTRACT,
) -> httpx.Response:
    return target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=identity,
        contract=contract,
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_contract_precedes_blob_ref_syntax(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7 整体上传：契约头先于路径引用的语法。"""
    session_id = _open_session(conformance_target, full_identity, "blob-contract")
    response = _put_blob(
        conformance_target, session_id, _BAD_REF, b"blob", full_identity, contract=None
    )
    assert_problem(response, status=400, code="DPE_CONTRACT_UNSUPPORTED", documents_path=False)


def test_blob_ref_syntax_precedes_session(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7 整体上传：路径引用的语法先于会话——伪造会话 + 非法引用 → DPE_VALIDATION。"""
    response = _put_blob(conformance_target, _forged_session(), _BAD_REF, b"blob", full_identity)
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


def test_blob_hash_mismatch_precedes_session(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7 整体上传：sha256 比较先于会话——伪造会话 + 语法合法但与字节不符的引用 →
    DPE_HASH_MISMATCH，不是 410。"""
    response = _put_blob(
        conformance_target,
        _forged_session(),
        dpe_hash.blob_ref(b"other"),
        b"blob",
        full_identity,
    )
    assert_problem(response, status=400, code="DPE_HASH_MISMATCH", documents_path=False)


def test_chunked_page_path_syntax_precedes_session(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：分块上传的 ``pages/{h}`` 路径语法同样先于会话——伪造会话 + 非法 hash →
    DPE_VALIDATION。"""
    response = conformance_target.send(
        "PUT",
        f"staging/{_forged_session()}/pages/{_BAD_REF}",
        identity=full_identity,
        content=b"{}",
        headers={"Content-Range": "bytes 0-1/2"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


# ---------------------------------------------------------------------- 报文校验先于授权（#63）


def _invalid_message(case: str, document: WireDocument) -> dict[str, Any]:
    """无写授权身份发出的非法报文：(content | json, headers)。"""
    body = document.commit_body()
    if case == "non-ijson":
        return {"content": b"{", "headers": {"Content-Type": "application/json"}}
    if case == "envelope":
        return {"json": {**body, "extra": 1}, "headers": {"If-None-Match": "*"}}
    if case == "object":
        bad = {"category": "NarrativeText", "text": "x", "text_as_html": "<p>x</p>"}
        return {"json": {**body, "objects": [bad]}, "headers": {"If-None-Match": "*"}}
    assert case == "force-with-condition"
    return {"json": {**body, "force": True}, "headers": {"If-Match": f'"{WRONG_HASH}"'}}


@pytest.mark.parametrize("case", ["non-ijson", "envelope", "object", "force-with-condition"])
def test_message_validation_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity, case: str
) -> None:
    """core §3.3（#63）：报文校验（I-JSON、请求信封、对象、force 与条件头并存）先于授权——
    无写授权的身份提交非法报文得到 DPE_VALIDATION，不是 403。"""
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=scoped_identity,
        params={"uri": outside_scoped(conformance_target, scoped_identity, case)},
        **_invalid_message(case, _document()),
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


@pytest.mark.parametrize("state", ["absent", "existing"])
def test_valid_message_without_write_access_is_forbidden(
    conformance_target: Target,
    full_identity: Identity,
    scoped_identity: Identity,
    state: str,
) -> None:
    """core §3.3 第 2 步 + §8：报文合法时无写授权一律 403，与文档状态无关（授权先于一切依赖
    文档状态的判定）。"""
    uri = outside_scoped(conformance_target, scoped_identity, f"forbidden-{state}")
    if state == "existing":
        seed(conformance_target, uri, _document("seeded"), identity=full_identity)
    response = commit(
        conformance_target, uri, _document("other"), identity=scoped_identity, if_match=WRONG_HASH
    )
    assert_problem(response, status=403, code="DPE_FORBIDDEN")


def test_move_envelope_precedes_authorization(
    conformance_target: Target, scoped_identity: Identity
) -> None:
    """core §5.2（#63）：move 的两侧授权排在请求信封校验之后——无写授权 + 未定义成员 →
    DPE_VALIDATION。"""
    response = move(
        conformance_target,
        outside_scoped(conformance_target, scoped_identity, "move-from"),
        outside_scoped(conformance_target, scoped_identity, "move-to"),
        identity=scoped_identity,
        base_hash=WRONG_HASH,
        extra_body={"extra": 1},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


@pytest.mark.parametrize("unauthorized", ["source", "target", "both"])
def test_move_without_write_access_is_forbidden(
    conformance_target: Target,
    full_identity: Identity,
    scoped_identity: Identity,
    unauthorized: str,
) -> None:
    """core §5.2：报文合法时，源与目标**任一侧**无写授权都是 403——先于源是否存在、base_hash
    是否匹配等依赖文档状态的判定（这里 base_hash 故意不匹配：授权若被跳过会得到 409）。"""

    def side(name: str, denied: bool) -> str:
        if denied:
            return outside_scoped(conformance_target, scoped_identity, name)
        return inside_scoped(scoped_identity, name)

    source = side("move-src", unauthorized in ("source", "both"))
    target = side("move-dst", unauthorized in ("target", "both"))
    seed(conformance_target, source, _document("move"), identity=full_identity)
    response = move(
        conformance_target, source, target, identity=scoped_identity, base_hash=WRONG_HASH
    )
    assert_problem(response, status=403, code="DPE_FORBIDDEN", documents_path=False)


# ------------------------------------------------------------------ commit 的唯一步（core §3.3）


def _invalid_inline() -> dict[str, Any]:
    return {"category": "NarrativeText", "text": "x", "text_as_html": "<p>x</p>"}


def test_inline_validation_precedes_precondition(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3：内联对象非法（第 1 步）+ 前置条件不满足（第 5 步）→ DPE_VALIDATION。"""
    uri = conformance_target.uri("order/inline-vs-precondition")
    seed(conformance_target, uri, _document("v1"), identity=full_identity)
    body = _document("v2").commit_body()
    body["objects"] = [*body["objects"], _invalid_inline()]
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{WRONG_HASH}"'},
        json=body,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_precondition_precedes_missing_content(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3：前置条件不满足（第 5 步）+ 内容缺失（第 6 步）→ DPE_PRECONDITION_FAILED。"""
    uri = conformance_target.uri("order/precondition-vs-missing")
    seed(conformance_target, uri, _document("v1"), identity=full_identity)
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{WRONG_HASH}"'},
        json={"document": _document("v2").document},
    )
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")


def test_unchanged_does_not_bypass_inline_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3：第 1 步先于第 4 步——内容未变但带非法内联对象 → DPE_VALIDATION。"""
    uri = conformance_target.uri("order/unchanged-invalid-inline")
    document = _document("same")
    seed(conformance_target, uri, document, identity=full_identity)
    body = document.commit_body()
    body["objects"] = [_invalid_inline()]
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{document.doc_hash}"'},
        json=body,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_unchanged_ignores_valid_redundant_inline(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3 第 4 步：内容未变时带合法但多余的内联对象 → 200 unchanged（不处理这些对象）。"""
    uri = conformance_target.uri("order/unchanged-redundant")
    document = _document("same")
    seed(conformance_target, uri, document, identity=full_identity)
    response = commit(
        conformance_target,
        uri,
        document,
        identity=full_identity,
        if_match=document.doc_hash,
        extra_body={"objects": [text_element("redundant")]},
    )
    assert_commit_success(response, status=200, status_text="unchanged", doc_hash=document.doc_hash)


def test_inline_objects_never_hash_mismatch(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3 第 6 步：内联对象没有声明的 hash，由服务端算出——内联页对象与文档引用的
    page_hash 不符时，结论是该页缺失（DPE_MISSING_CONTENT），永远不是 DPE_HASH_MISMATCH。"""
    document = _document("declared")
    other = _document("inlined")
    body = {"document": document.document, "pages": other.pages, "objects": other.objects}
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("order/inline-mismatch")},
        headers={"If-None-Match": "*"},
        json=body,
    )
    problem = assert_problem(response, status=400, code="DPE_MISSING_CONTENT")
    assert document.page_hashes[0] in problem["missing"]["pages"], (
        f"缺失清单应列出文档引用的页：{problem['missing']!r}"
    )
