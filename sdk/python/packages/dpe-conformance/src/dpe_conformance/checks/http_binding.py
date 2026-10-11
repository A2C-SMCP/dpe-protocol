"""章：HTTP 绑定细节、RFC 9457 与 code → 状态码一致（#55 全量 2/6）。

- HTTP §3.1/§3.2/§3.3：条件头只出现在文档资源上（move 的前置条件在请求体，冲突取 ``409``，
  由 ``write_ops`` 章断言）；``If-Match`` 按值比较——``W/`` 前缀永不匹配、非 hash 值（旧契约、
  任意串）只是比较不上（``412``），MUST NOT 做格式校验；客户端按响应自报的 ``DPE-Doc-Hash``
  构造 ``If-Match`` 必须成功；
- HTTP §3.3/§4.9：``delete`` 的 ``204`` 不带文档令牌头；move 成功响应带 ``DPE-Doc-Hash`` 与
  指向目标文档的 ``Content-Location``（绝对 URL、规范化 ``to_uri`` 的百分号编码只保留
  unreserved），不带 ``ETag``；
- HTTP §3.3/§5：HEAD 出错只有 ``DPE-Error-Code`` 头——缺契约头 → ``400`` +
  ``DPE_CONTRACT_UNSUPPORTED``；失效/不存在会话的断点查询（``HEAD staging/{sid}/…``）→ ``410``
  + ``DPE_SESSION_EXPIRED``（会话状态矩阵归 #54 章）；
- HTTP §5（RFC 9457）+ CORE §6：每个 code 的 problem 形态由 ``support.assert_problem``
  全形把关（媒体类型、``type``/``title``/``status``/``code``/``retryable``、``DPE-Error-Code``
  头、Vary 面）；本节按 §5 的状态码表**逐码**断言每个可构造 code 的状态映射。同一 code 在
  不同端点的差异（``DPE_NOT_FOUND`` 带 ``If-Match`` 为 ``412``、move 的请求体前置条件取
  ``409``）在对应端点的检查里断言（``write_ops`` 章与本节）。

  逐码表是**一处齐**的实现：个别行的最小构造与别章的重叠（``DPE_VALIDATION`` 的信封、
  ``DPE_NOT_FOUND`` 的 HEAD 等）是表自身的完整性所需，作为「表内引用」保留——它们在一个
  文件里给出状态映射的全貌，不新增语义覆盖。
"""

from __future__ import annotations

import re
import uuid
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote

import dpe_hash
import pytest

from dpe_conformance.config import Identity
from dpe_conformance.hooks import PROBE_HEADER, HookDriver, inject_error_payload
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WireDocument,
    assert_commit_success,
    assert_head_error,
    assert_problem,
    build_document,
    commit,
    delete,
    has_dpe_vary,
    move,
    optional_identity,
    text_element,
)

_WRONG_HASH = "dpe1:" + "0" * 64
#: 百分号编码：只保留 unreserved（RFC 3986 §2.3：ALPHA / DIGIT / ``-`` / ``.`` / ``_`` / ``~``）
#: 或合法的 %XX（HTTP §4.9）
_PERCENT_ENCODED = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+")
#: 越界载荷检查的预算：合法服务端的 max_payload_bytes 更大时本检查超预算 skip（不判红）
_PAYLOAD_BUDGET = 64 * 1024 * 1024


def _document() -> WireDocument:
    return build_document([[text_element("http-binding")]], title="HTTP 绑定样例")


def _seed(target: Target, identity: Identity, uri: str, document: WireDocument) -> WireDocument:
    response = commit(target, uri, document, identity=identity, if_none_match_star=True)
    assert response.status_code == 201, f"铺垫失败：{response.status_code} {response.text[:200]}"
    return document


def _invalid_element_body(element: dict[str, Any]) -> dict[str, Any]:
    """含非法对象的 commit 体：hash 用占位值——报文校验是 schema 层判定，先于任何 hash 重算；
    非法对象无法（也不该）经 dpe-hash 构造。"""
    return {
        "document": {"file_type": "md", "pages": [_WRONG_HASH]},
        "pages": [{"elements": [_WRONG_HASH]}],
        "objects": [element],
    }


def _percent_normalize(value: str) -> str:
    """把 %XX 的十六进制统一为大写后比较（十六进制大小写不在规范约束内）。"""
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda match: "%" + match.group(1).upper(), value)


# -------------------------------------------------------------------- 条件头的按值比较（HTTP §3.2）


def test_weak_if_match_never_matches(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.1/§3.2：``W/"…"`` 合法但永不匹配——弱校验器形式的当前 doc_hash 在内容变化时
    得到 ``412`` + ``DPE_PRECONDITION_FAILED``。"""
    uri = conformance_target.uri("http/weak-if-match")
    document = _seed(conformance_target, full_identity, uri, _document())
    changed = build_document([[text_element("changed")]])
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'W/"{document.doc_hash}"'},
        json=changed.commit_body(),
    )
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")


@pytest.mark.parametrize(
    ("case", "value"), [("plain", "abc"), ("unknown-prefix", "dpe9:" + "0" * 64)]
)
def test_non_hash_if_match_value_compares_by_value(
    conformance_target: Target, full_identity: Identity, case: str, value: str
) -> None:
    """HTTP §3.2 + CORE §5.2：``If-Match`` 的值 MUST NOT 做格式校验——非 hash 的值只是比较不上，
    得到 ``412`` + ``DPE_PRECONDITION_FAILED``（不是 ``DPE_VALIDATION``、也不是
    ``DPE_CONTRACT_UNSUPPORTED``）。"""
    uri = conformance_target.uri(f"http/value-compare/{case}")
    _seed(conformance_target, full_identity, uri, _document())
    changed = build_document([[text_element("changed")]])
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{value}"'},
        json=changed.commit_body(),
    )
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")


def test_if_match_from_dpe_doc_hash_succeeds(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.1：客户端取 ``DPE-Doc-Hash``（权威值）构造 ``If-Match`` 的写入必须成功。"""
    uri = conformance_target.uri("http/authority-header")
    _seed(conformance_target, full_identity, uri, _document())
    head = conformance_target.send("HEAD", "documents", identity=full_identity, params={"uri": uri})
    assert head.status_code == 200, f"HEAD 返回 {head.status_code}"
    current = head.headers.get("DPE-Doc-Hash")
    assert current, "HEAD 必须带 DPE-Doc-Hash（HTTP §3.3）"

    changed = build_document([[text_element("via-authority-header")]])
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{current}"'},
        json=changed.commit_body(),
    )
    assert_commit_success(response, status=200, status_text="updated", doc_hash=changed.doc_hash)


# ---------------------------------------------------------------------- 响应头（HTTP §3.3/§4.9）


def test_delete_204_has_no_document_token_headers(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.3：delete 的 ``204`` 不带 ``DPE-Doc-Hash`` 与 ``ETag``。"""
    uri = conformance_target.uri("http/delete-headers")
    document = _seed(conformance_target, full_identity, uri, _document())
    response = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert response.status_code == 204, f"delete 预期 204，实际 {response.status_code}"
    assert "DPE-Doc-Hash" not in response.headers, "delete 的 204 不得带 DPE-Doc-Hash"
    assert "ETag" not in response.headers, "delete 的 204 不得带 ETag"


def test_move_response_headers_and_content_location(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.9 + §3.3：move 成功响应体带目标文档的 ``doc_hash``；头带目标文档的
    ``DPE-Doc-Hash`` 与 ``Content-Location``（绝对 URL：remote + ``/documents?uri=`` +
    规范化 ``to_uri`` 的百分号编码，只保留 unreserved），不带 ``ETag``（``/move`` 不是文档
    资源）。

    响应信封按仓库口径宽容（同协议版本内新增的响应成员不应判失败，见 ``dpe_sdk.wire``
    与 ``assert_commit_success`` 的写法），只断言 ``doc_hash`` 的值。
    """
    source = conformance_target.uri("http/move-from")
    to_uri = conformance_target.uri("http/move-to")
    document = _seed(conformance_target, full_identity, source, _document())
    response = move(
        conformance_target, source, to_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert response.status_code == 200, f"move 返回 {response.status_code}（{response.text[:200]}）"
    body = response.json()
    assert body["doc_hash"] == document.doc_hash, f"move 体的 doc_hash：{body!r}"
    assert response.headers.get("DPE-Doc-Hash") == document.doc_hash
    assert "ETag" not in response.headers, "move 响应不带 ETag（HTTP §3.3）"

    location = response.headers.get("Content-Location")
    assert location is not None, "move 成功响应必须带 Content-Location（HTTP §4.9）"
    prefix = f"{conformance_target.base_url}/documents?uri="
    assert location.startswith(prefix), (
        f"Content-Location 必须以 remote 的对外 base URL 开头：{location!r}（期望前缀 {prefix!r}）"
    )
    encoded = location[len(prefix) :]
    assert _PERCENT_ENCODED.fullmatch(encoded), (
        f"Content-Location 的 uri 只能保留 unreserved 或合法 %XX：{encoded!r}"
    )
    normalized = dpe_hash.normalize_file_uri(to_uri)
    assert unquote(encoded) == normalized, (
        f"Content-Location 指向的 uri 解码后是 {unquote(encoded)!r}，期望规范化形式 {normalized!r}"
    )
    expected = quote(normalized, safe="")
    assert _percent_normalize(encoded) == _percent_normalize(expected), (
        f"Content-Location 的编码不符「只保留 unreserved」：{encoded!r}，期望 {expected!r}"
    )


# ------------------------------------------------------------------- HEAD 的错误面（HTTP §3.3、§5）


def test_head_without_contract_header_is_contract_unsupported(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.1/§3.3/§5 + CORE §3.1：HEAD 缺 ``DPE-Hash-Contract`` → ``400`` +
    ``DPE_CONTRACT_UNSUPPORTED``（HEAD 出错只有错误码头）。"""
    uri = conformance_target.uri("http/head-no-contract")
    _seed(conformance_target, full_identity, uri, _document())
    response = conformance_target.send(
        "HEAD", "documents", identity=full_identity, contract=None, params={"uri": uri}
    )
    assert_head_error(response, status=400, code="DPE_CONTRACT_UNSUPPORTED")


@pytest.mark.parametrize("target_kind", ["pages", "blobs"], ids=["page", "blob"])
def test_staging_head_on_unknown_session_is_expired(
    conformance_target: Target, full_identity: Identity, target_kind: str
) -> None:
    """HTTP §4.7 + §5 + CORE §3.4：对不存在（或不可用）的会话做断点查询 →
    ``410`` + ``DPE_SESSION_EXPIRED``——会话不存在的断点查询**不是** 404；staging 路径的响应
    不带 ``Vary``（HTTP §3.1）。"""
    document = _document()
    if target_kind == "pages":
        path = f"staging/st-conformance-absent/pages/{document.page_hashes[0]}"
    else:
        path = f"staging/st-conformance-absent/blobs/{dpe_hash.blob_ref(b'absent')}"
    response = conformance_target.send("HEAD", path, identity=full_identity)
    assert response.status_code == 410, f"失效会话的断点查询预期 410，实际 {response.status_code}"
    # httpx 不读取 HEAD 的响应体；此断言表达规范要求，不作为「无体」的可证伪证据（HTTP §3.3）
    assert response.content == b"", "HEAD 响应不得带内容"
    assert response.headers.get("DPE-Error-Code") == "DPE_SESSION_EXPIRED"
    assert not has_dpe_vary(response), "staging 路径的响应不带 Vary: DPE-Hash-Contract（HTTP §3.1）"


# --------------------------------------------------------------------- code → 状态码逐码（HTTP §5）


def test_code_status_validation(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5：``DPE_VALIDATION`` → ``400``（信封未定义成员触发）。"""
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("http/code-validation")},
        json={**_document().commit_body(), "undefined_member": 1},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_code_status_category_unknown(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5 + CORE §2.8：``DPE_CATEGORY_UNKNOWN`` → ``400``（未知 category 的元素对象）。"""
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("http/code-category")},
        json=_invalid_element_body({"category": "NoSuchCategory", "text": "x"}),
    )
    assert_problem(response, status=400, code="DPE_CATEGORY_UNKNOWN")


def test_code_status_hash_mismatch(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5 + CORE §3.4：``DPE_HASH_MISMATCH`` → ``400``（暂存上传的路径 hash 与到齐内容
    不符；上传路径的完整矩阵归 #54 章，这里只取状态映射的最小构造）。"""
    uri = conformance_target.uri("http/code-hash-mismatch")
    page_owner = _document()
    other = build_document([[text_element("different content")]])
    negotiated = conformance_target.send(
        "POST",
        "negotiate",
        identity=full_identity,
        json={"file_uri": uri, "document": page_owner.document},
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id = negotiated.json()["staging_session"]["id"]
    response = conformance_target.send(
        "PUT",
        f"staging/{session_id}/pages/{other.page_hashes[0]}",
        identity=full_identity,
        json=page_owner.pages[0],
    )
    assert_problem(response, status=400, code="DPE_HASH_MISMATCH", documents_path=False)


def test_code_status_missing_content(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5 + CORE §3.3 第 6 步：``DPE_MISSING_CONTENT`` → ``400``，problem 体按层给出
    缺失清单（这里缺一个页对象；清单内容与构造一致，非只看状态码）。"""
    uri = conformance_target.uri("http/code-missing")
    document = _document()
    body = {
        "document": document.document,
        "pages": [],
        "objects": [],
    }
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json=body,
    )
    problem = assert_problem(response, status=400, code="DPE_MISSING_CONTENT")
    assert problem["missing"]["pages"] == document.page_hashes, (
        f"缺失清单的 pages 应为 {document.page_hashes!r}，实际 {problem['missing']['pages']!r}"
    )
    assert problem["missing"]["content_hashes"] == [] and problem["missing"]["blobs"] == []
    assert problem["missing_truncated"] is False


def test_code_status_forbidden(conformance_target: Target, scoped_identity: Identity) -> None:
    """HTTP §5 + CORE §5 总则：``DPE_FORBIDDEN`` → ``403``（scoped 身份写可写前缀之外）。

    测试命名空间（``uri-prefix``）与 scoped 可写前缀都由操作者配置且无互斥约束：命名空间
    落在 scoped 前缀内时无法构造越权写，显式 skip 并计入计数（合规配置不得被判失败）。
    """
    assert scoped_identity.prefix is not None, "scoped 身份必须带可写前缀"
    if conformance_target.uri_prefix.startswith(scoped_identity.prefix):
        pytest.skip("测试命名空间落在 scoped 可写前缀内，无法构造越权写")
    uri = conformance_target.uri("http/code-forbidden")
    response = commit(
        conformance_target, uri, _document(), identity=scoped_identity, if_none_match_star=True
    )
    assert_problem(response, status=403, code="DPE_FORBIDDEN")


def test_code_status_not_found(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5：``DPE_NOT_FOUND`` → ``404``（HEAD 不存在的文档；带 ``If-Match`` 的写操作取
    ``412``，见 ``write_ops`` 章）。"""
    response = conformance_target.send(
        "HEAD",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("http/code-not-found")},
    )
    assert_head_error(response, status=404, code="DPE_NOT_FOUND")


def test_code_status_precondition_failed(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §5：``DPE_PRECONDITION_FAILED`` → ``412``（条件头；move 的请求体前置条件取
    ``409``，见 ``write_ops`` 章）。"""
    uri = conformance_target.uri("http/code-precondition")
    _seed(conformance_target, full_identity, uri, _document())
    changed = build_document([[text_element("changed")]])
    response = commit(
        conformance_target, uri, changed, identity=full_identity, if_match=_WRONG_HASH
    )
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")


def test_code_status_already_exists(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5：``DPE_ALREADY_EXISTS`` → ``412``（``If-None-Match: *`` 命中已存在且内容不同）。"""
    uri = conformance_target.uri("http/code-already-exists")
    _seed(conformance_target, full_identity, uri, _document())
    changed = build_document([[text_element("changed")]])
    response = commit(
        conformance_target, uri, changed, identity=full_identity, if_none_match_star=True
    )
    assert_problem(response, status=412, code="DPE_ALREADY_EXISTS")


def test_code_status_payload_too_large(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5 + §3.1 第 4 步：``DPE_PAYLOAD_TOO_LARGE`` → ``413``（请求体超过
    ``max_payload_bytes``，按解码后计算）。服务端声明的上限超过预算时显式 skip 并计入计数。"""
    limits = conformance_target.send(
        "GET", "capabilities", contract=None, identity=full_identity
    ).json()["limits"]
    limit = limits["max_payload_bytes"]
    if limit > _PAYLOAD_BUDGET:
        pytest.skip(f"max_payload_bytes={limit} 超过本检查预算 {_PAYLOAD_BUDGET}，不构造越界请求")
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": conformance_target.uri("http/code-payload")},
        content=b"x" * (limit + 1),
        headers={"Content-Type": "application/json"},
    )
    assert_problem(response, status=413, code="DPE_PAYLOAD_TOO_LARGE")


def test_code_status_precondition_required(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §5 + CORE §5.1：``DPE_PRECONDITION_REQUIRED`` → ``428``（写操作不带前置条件）。"""
    response = commit(
        conformance_target,
        conformance_target.uri("http/code-required"),
        _document(),
        identity=full_identity,
    )
    assert_problem(response, status=428, code="DPE_PRECONDITION_REQUIRED")


def _assert_retry_after_syntax(value: str) -> None:
    """``Retry-After`` 的语法（RFC 9110 §10.2.3，与 #109 对齐）：``delay-seconds``（1*DIGIT）
    或 HTTP-date；语义（等待时长、取整方式）不在规范内，不在这里断言。"""
    if value.isascii() and value.isdigit():
        return
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        when = None
    assert when is not None, (
        f"Retry-After 既不是 delay-seconds 也不是 HTTP-date（RFC 9110 §10.2.3）：{value!r}"
    )


@pytest.mark.parametrize(("code", "status"), [("DPE_RATE_LIMITED", 429), ("DPE_UNAVAILABLE", 503)])
def test_error_injection_status_problem_and_recovery(
    conformance_target: Target,
    inject_error_hook: HookDriver,
    code: str,
    status: int,
) -> None:
    """HTTP §5（#123）：经错误注入钩子构造 ``DPE_RATE_LIMITED`` → 429 与 ``DPE_UNAVAILABLE`` →
    503——problem 体全形、``Retry-After`` MUST 出现且语法符合 RFC 9110 §10.2.3、给定的提示值被
    如实反映（delay-seconds 形态）；不带探针头的请求不受影响（受鉴权保护的服务端也照常跑只读
    检查，故带可选身份）；注入计数用完后带探针头的请求恢复正常。

    命中探针的请求**不带凭证**：注入按裁定契约不依赖身份与凭证，这条请求同时证明该属性
    （拦截在认证之前，见包 README 的钩子契约）。
    """
    probe = uuid.uuid4().hex
    result = inject_error_hook.call(
        inject_error_payload(code=code, probe=probe, count=2, retry_after=7)
    )
    assert result.ok, f"inject-error 钩子失败：{result.detail}"

    untouched = conformance_target.send(
        "GET", "capabilities", contract=None, identity=optional_identity(conformance_target)
    )
    assert untouched.status_code == 200, "不带探针头的请求不得受注入影响"
    for _ in range(2):
        response = conformance_target.send(
            "GET", "capabilities", contract=None, headers={PROBE_HEADER: probe}
        )
        assert_problem(response, status=status, code=code, documents_path=False)
        header = response.headers.get("Retry-After")
        assert header is not None, "429 / 503 MUST 带 Retry-After（HTTP §5）"
        _assert_retry_after_syntax(header)
        if header.isascii() and header.isdigit():
            # delay-seconds 形态：给定的 retry_after 必须被如实反映（HTTP-date 形态豁免）
            assert int(header) == 7, f"注入的 retry_after=7 应被如实反映，实际为 {header!r}"
    recovered = conformance_target.send(
        "GET",
        "capabilities",
        contract=None,
        identity=optional_identity(conformance_target),
        headers={PROBE_HEADER: probe},
    )
    assert recovered.status_code == 200, "注入计数用完后探针请求应恢复走引擎"


def test_error_injection_default_retry_after_is_one(
    conformance_target: Target, inject_error_hook: HookDriver
) -> None:
    """HTTP §5（#123）：注入不給 ``retry_after`` 时，``Retry-After`` 恰好是服务端的缺省提示值
    ``1``（「实现没有可给的提示值时给 1」，不可缺省）。"""
    probe = uuid.uuid4().hex
    result = inject_error_hook.call(inject_error_payload(code="DPE_RATE_LIMITED", probe=probe))
    assert result.ok, f"inject-error 钩子失败：{result.detail}"
    response = conformance_target.send(
        "GET", "capabilities", contract=None, headers={PROBE_HEADER: probe}
    )
    assert_problem(response, status=429, code="DPE_RATE_LIMITED", documents_path=False)
    assert response.headers.get("Retry-After") == "1", (
        "缺省提示值必须是 1（HTTP §5：429 / 503 的 Retry-After 不可缺省）"
    )
