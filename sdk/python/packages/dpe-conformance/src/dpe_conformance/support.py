"""各章共用的构造与断言素材：文档生成（期望 hash 只经 dpe-hash）、problem 断言、身份助手。

放在包根而不是 ``checks/``：那是章节目录，``--section`` 的粒度就是它下面的一级模块/包。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import dpe_hash
import httpx
import pytest
from dpe_hash import DocumentFields, ElementObject, PageFields

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target

#: ``retryable`` 为真的错误码（core.md §6 的逐码表）
_RETRYABLE_CODES = frozenset({"DPE_RATE_LIMITED", "DPE_UNAVAILABLE"})


@dataclass(frozen=True)
class WireDocument:
    """一份线上形态的三层对象 + 各层期望 hash（只经 dpe-hash 计算，不 import SDK）。"""

    document: dict[str, Any]
    pages: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    doc_hash: str
    page_hashes: list[str]

    def commit_body(self) -> dict[str, Any]:
        """commit 请求体（core.md §3.3：document 必填，pages/objects 内联）。"""
        return {"document": self.document, "pages": self.pages, "objects": self.objects}

    def content_multiset(self) -> Counter[str]:
        """全部页展开后的元素 content_hash 多重集（delta 的计数单位，core.md §3.3）。"""
        return Counter(h for page in self.pages for h in page["elements"])


def build_document(
    pages: Sequence[Sequence[Mapping[str, Any]]],
    *,
    file_type: str = "md",
    title: str | None = None,
    doc_metadata: Mapping[str, Any] | None = None,
    contract: str = dpe_hash.CONTRACT,
) -> WireDocument:
    """由「每页一组元素对象（线上形态）」构造完整文档与全部期望 hash。

    ``contract`` 指定子 hash 与期望 hash 所按的契约（契约升级期一章以第二契约构造请求体，#58）；
    请求须以同一契约声明（``DPE-Hash-Contract``）。

    元素对象按原样参与 hash；``pages`` 的顺序即文档顺序（core.md §2.1：位置只由数组顺序
    表达）。页/文档的字段表见 hash-contract-1 §5：``title`` 缺省不进原像；两个 metadata
    字段缺省取 ``{}``，在原像中总是出现（§3.2）。
    """
    objects: list[dict[str, Any]] = []
    page_objects: list[dict[str, Any]] = []
    page_hashes: list[str] = []
    for page_elements in pages:
        elements: list[dict[str, Any]] = [dict(element) for element in page_elements]
        content_hashes = [
            dpe_hash.content_hash(cast(ElementObject, element), contract) for element in elements
        ]
        page_hashes.append(dpe_hash.page_hash(PageFields(), content_hashes, contract))
        objects.extend(elements)
        page_objects.append({"elements": content_hashes})
    # 文档字段（不含 pages——doc_hash 的子 hash 走第二个位置参数）与线上一并成形
    fields: DocumentFields = {"file_type": file_type}
    if title is not None:
        fields["title"] = title
    if doc_metadata is not None:
        fields["doc_metadata"] = dict(doc_metadata)
    document: dict[str, Any] = {**fields, "pages": page_hashes}
    return WireDocument(
        document=document,
        pages=page_objects,
        objects=objects,
        doc_hash=dpe_hash.doc_hash(fields, page_hashes, contract),
        page_hashes=page_hashes,
    )


def text_element(text: str, **extra: Any) -> dict[str, Any]:
    """一个最小的 NarrativeText 元素对象（可带 metadata 等额外合法字段）。"""
    return {"category": "NarrativeText", "text": text, **extra}


def optional_identity(target: Target) -> Identity | None:
    """有 full 身份就带上（受鉴权保护的服务端照常跑只读检查），没有就不带。"""
    return target.config.identities.get("full")


# ---------------------------------------------------------------------- 断言


def _kebab(code: str) -> str:
    return code.removeprefix("DPE_").lower().replace("_", "-")


def has_dpe_vary(response: httpx.Response) -> bool:
    """响应是否带 ``Vary: DPE-Hash-Contract``（值级判定：Vary 按逗号分隔 token 比较，
    字段名与 token 均不区分大小写——只挂 ``Accept-Encoding`` 之类不算数，HTTP §3.1）。"""
    tokens: set[str] = set()
    for value in response.headers.get_list("vary"):
        tokens.update(part.strip().lower() for part in value.split(",") if part.strip())
    return "dpe-hash-contract" in tokens


def assert_doc_headers(response: httpx.Response, doc_hash: str) -> None:
    """HTTP §3.3：文档资源的成功响应头——DPE-Doc-Hash 为权威值，ETag 为强校验器，
    Vary 必须含 ``DPE-Hash-Contract``。"""
    expected_etag = f'"{doc_hash}"'
    assert response.headers.get("DPE-Doc-Hash") == doc_hash, (
        f"DPE-Doc-Hash 是 {response.headers.get('DPE-Doc-Hash')!r}，期望 {doc_hash!r}"
    )
    assert response.headers.get("ETag") == expected_etag, (
        f"ETag 是 {response.headers.get('ETag')!r}，期望 {expected_etag!r}"
    )
    assert has_dpe_vary(response), "documents 路径的响应必须带 Vary: DPE-Hash-Contract（HTTP §3.1）"


def assert_problem(
    response: httpx.Response, *, status: int, code: str, documents_path: bool = True
) -> dict[str, Any]:
    """HTTP §5：DPE 错误响应的完整形态（problem+json 媒体类型、problem 体按构造规则、
    DPE-Error-Code 头、Vary 面）。

    ``code``/``retryable`` 的取值表在 core.md §6；``type``/``title`` 的构造规则在 HTTP §5。
    """
    where = f"{response.request.method} {response.request.url}"
    assert response.status_code == status, (
        f"{where}：预期 {status}，实际 {response.status_code}（{response.text[:200]}）"
    )
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/problem+json"), (
        f"{where}：错误响应必须是 application/problem+json（HTTP §2），实际 {content_type!r}"
    )
    body: dict[str, Any] = response.json()
    assert body.get("code") == code, f"{where}：code 是 {body.get('code')!r}，期望 {code!r}"
    assert body.get("type") == f"urn:dpe:error:{_kebab(code)}", f"{where}：type 不符构造规则"
    assert body.get("title") == _kebab(code).replace("-", " "), f"{where}：title 不符构造规则"
    assert body.get("retryable") is (code in _RETRYABLE_CODES), (
        f"{where}：retryable 是 {body.get('retryable')!r}，{code} 应为 {code in _RETRYABLE_CODES}"
    )
    assert response.headers.get("DPE-Error-Code") == code, f"{where}：DPE-Error-Code 头缺失或不等"
    assert has_dpe_vary(response) is documents_path, (
        f"{where}：Vary: DPE-Hash-Contract 的面与路径不符"
        "（documents 路径必带、其余不带，HTTP §3.1）"
    )
    return body


def assert_head_error(response: httpx.Response, *, status: int, code: str) -> None:
    """HTTP §3.3/§5：HEAD 出错只有 DPE-Error-Code 头、没有 problem 体。"""
    where = f"HEAD {response.request.url}"
    assert response.status_code == status, f"{where}：预期 {status}，实际 {response.status_code}"
    # httpx 不读取 HEAD 的响应体；此断言表达规范要求，不作为「无体」的可证伪证据（§3.3）
    assert response.content == b"", f"{where}：HEAD 响应不得带内容"
    assert response.headers.get("DPE-Error-Code") == code, (
        f"{where}：DPE-Error-Code 是 {response.headers.get('DPE-Error-Code')!r}，期望 {code!r}"
    )
    assert has_dpe_vary(response), (
        "documents 路径的错误响应也带 Vary: DPE-Hash-Contract（HTTP §3.1）"
    )


def assert_commit_success(
    response: httpx.Response, *, status: int, status_text: str, doc_hash: str
) -> dict[str, Any]:
    """HTTP §4.8：commit 成功响应——created/updated/unchanged 的状态码与
    体（status/doc_hash/delta）。"""
    where = f"PUT {response.request.url}"
    assert response.status_code == status, (
        f"{where}：预期 {status}，实际 {response.status_code}（{response.text[:200]}）"
    )
    body: dict[str, Any] = response.json()
    assert body.get("status") == status_text, f"{where}：status 是 {body.get('status')!r}"
    assert body.get("doc_hash") == doc_hash, (
        f"{where}：体内 doc_hash 是 {body.get('doc_hash')!r}，按 dpe-hash 期望 {doc_hash!r}"
    )
    assert_doc_headers(response, doc_hash)
    return body


def commit(
    target: Target,
    uri: str,
    document: WireDocument,
    *,
    identity: Identity,
    if_match: str | None = None,
    if_none_match_star: bool = False,
    force: bool = False,
    extra_body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """发一次 commit：前置条件按 HTTP §3.2 的载体（条件头；force 在请求体）。"""
    assert not (if_match is not None and if_none_match_star), "条件头互斥（HTTP §3.2）"
    headers = _if_match_headers(if_match)
    if if_none_match_star:
        headers["If-None-Match"] = "*"
    body = document.commit_body()
    if force:
        body["force"] = True
    if extra_body:
        body.update(extra_body)
    return target.send(
        "PUT", "documents", identity=identity, params={"uri": uri}, headers=headers, json=body
    )


def delete(
    target: Target,
    uri: str,
    *,
    identity: Identity,
    if_match: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """发一次 delete（HTTP §4.9）：CAS 走 ``If-Match``（HTTP §3.2）。

    ``headers`` 原样附加以外的条件头（如构造语法违例），不与 ``if_match`` 并用。
    """
    assert if_match is None or headers is None or "If-Match" not in headers
    merged: dict[str, str] = dict(headers or {})
    if if_match is not None:
        merged["If-Match"] = f'"{if_match}"'
    return target.send(
        "DELETE", "documents", identity=identity, params={"uri": uri}, headers=merged
    )


def move(
    target: Target,
    from_uri: str,
    to_uri: str,
    *,
    identity: Identity,
    base_hash: str | None = None,
    extra_body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """发一次 move（HTTP §4.9）：前置条件在请求体中（HTTP §3.2）。"""
    body: dict[str, Any] = {"from_uri": from_uri, "to_uri": to_uri}
    if base_hash is not None:
        body["base_hash"] = base_hash
    if extra_body:
        body.update(extra_body)
    return target.send("POST", "move", identity=identity, json=body)


__all__ = [
    "WRONG_HASH",
    "WireDocument",
    "assert_commit_success",
    "assert_doc_headers",
    "assert_head_error",
    "assert_problem",
    "assert_staging_head_error",
    "breakpoint_query",
    "build_document",
    "chunk_upload",
    "commit",
    "commit_with_session",
    "commit_without_session",
    "delete",
    "expected_delta",
    "expires_at_header",
    "has_dpe_vary",
    "image_element",
    "inside_scoped",
    "limits_of",
    "list_all",
    "move",
    "negotiate",
    "optional_identity",
    "outside_scoped",
    "parse_rfc3339",
    "payload_limit_within_budget",
    "seed",
    "session_of",
    "skeleton_multiset",
    "stage_all",
    "text_element",
    "unique_blob",
    "upload_blob",
    "upload_element",
    "upload_offset",
    "upload_page",
]


# ------------------------------------------------- 暂存会话、逐层上传与分块（#54 追加；只追加）


def limits_of(target: Target, *, identity: Identity | None = None) -> Mapping[str, int]:
    """capabilities 的 ``limits``（检查据此推导规模与分块大小，不写死数值；HTTP §4.1）。"""
    response = target.send("GET", "capabilities", contract=None, identity=identity)
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    limits = response.json()["limits"]
    assert isinstance(limits, dict), f"limits 必须是对象：{limits!r}"
    return cast(Mapping[str, int], limits)


def _if_match_headers(if_match: str | None) -> dict[str, str]:
    """``If-Match`` 条件头（HTTP §3.2）：与 ``commit`` 共用同一组装口径。"""
    return {} if if_match is None else {"If-Match": f'"{if_match}"'}


def commit_without_session(
    target: Target,
    uri: str,
    document: WireDocument,
    *,
    identity: Identity,
    pages: Sequence[Mapping[str, Any]] = (),
    objects: Sequence[Mapping[str, Any]] = (),
    if_match: str | None = None,
    force: bool = False,
) -> httpx.Response:
    """不开暂存会话的 commit：只内联给定的页 / 元素对象，其余按本文档当前状态引用
    （core §3.3 第 6 步）。用于验证「不开会话的引用提交」这一路径（#250）。"""
    headers = _if_match_headers(if_match)
    body: dict[str, Any] = {"document": document.document}
    if pages:
        body["pages"] = list(pages)
    if objects:
        body["objects"] = list(objects)
    if force:
        body["force"] = True
    return target.send(
        "PUT", "documents", identity=identity, params={"uri": uri}, headers=headers, json=body
    )


def negotiate(
    target: Target,
    uri: str,
    document: WireDocument,
    *,
    identity: Identity,
    attach_pages: bool = True,
) -> httpx.Response:
    """``POST negotiate``（HTTP §4.5）：``file_uri`` + ``document``（可附带 ``pages``）。"""
    body: dict[str, Any] = {"file_uri": uri, "document": document.document}
    if attach_pages and document.pages:
        body["pages"] = document.pages
    return target.send("POST", "negotiate", identity=identity, json=body)


def session_of(response: httpx.Response) -> tuple[str, datetime]:
    """从 negotiate 响应取出 ``(session_id, expires_at)``（RFC 3339 UTC）。"""
    session = response.json()["staging_session"]
    session_id = session["id"]
    assert isinstance(session_id, str) and session_id, f"staging_session.id 异常：{session_id!r}"
    return session_id, parse_utc_timestamp(session["expires_at"], "staging_session.expires_at")


def parse_utc_timestamp(value: object, what: str) -> datetime:
    """必带的过期时间：RFC 3339 UTC 字符串、以 ``Z`` 结尾（HTTP §4.5 / §4.6，同契约 1 §3.3）。"""
    assert isinstance(value, str) and value.endswith("Z"), (
        f"{what} 必须是以 Z 结尾的 RFC 3339 UTC 时间戳，实际 {value!r}"
    )
    return parse_rfc3339(value)


def parse_rfc3339(value: str) -> datetime:
    """解析 RFC 3339 时间戳（UTC）为 ``datetime``（比较用；不校验精度与写法差异）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def expires_at_header(response: httpx.Response) -> datetime:
    """响应头 ``DPE-Session-Expires``（成功操作续期后的过期时间，HTTP §4.5/§4.6）。"""
    value = response.headers.get("DPE-Session-Expires")
    assert value is not None, "响应缺少 DPE-Session-Expires 头"
    return parse_utc_timestamp(value, "DPE-Session-Expires")


def upload_offset(response: httpx.Response) -> int:
    """响应头 ``DPE-Upload-Offset``（本会话已收字节数，HTTP §4.7）。"""
    value = response.headers.get("DPE-Upload-Offset")
    assert value is not None, "响应缺少 DPE-Upload-Offset 头"
    return int(value)


def upload_page(
    target: Target,
    session_id: str,
    page: Mapping[str, Any],
    page_hash: str,
    *,
    identity: Identity,
) -> httpx.Response:
    """上传页对象（HTTP §4.6；体为该对象本身）。"""
    return target.send(
        "PUT", f"staging/{session_id}/pages/{page_hash}", identity=identity, json=dict(page)
    )


def upload_element(
    target: Target,
    session_id: str,
    element: Mapping[str, Any],
    content_hash_value: str,
    *,
    identity: Identity,
) -> httpx.Response:
    """上传元素对象（HTTP §4.6）。"""
    return target.send(
        "PUT",
        f"staging/{session_id}/objects/{content_hash_value}",
        identity=identity,
        json=dict(element),
    )


def upload_blob(
    target: Target, session_id: str, data: bytes, *, identity: Identity
) -> httpx.Response:
    """整体上传 blob（HTTP §4.7；路径的 ``sha256:`` 由字节算出）。"""
    return target.send(
        "PUT",
        f"staging/{session_id}/blobs/{dpe_hash.blob_ref(data)}",
        identity=identity,
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def chunk_upload(
    target: Target,
    session_id: str,
    *,
    kind: str,
    ref: str,
    data: bytes,
    identity: Identity,
    chunk_size: int,
) -> list[httpx.Response]:
    """按序分块上传（HTTP §4.7）：返回每个块的响应（中间块应为 202 + ``DPE-Upload-Offset``）。"""
    assert chunk_size > 0
    responses: list[httpx.Response] = []
    total = len(data)
    for start in range(0, total, chunk_size):
        piece = data[start : start + chunk_size]
        end = start + len(piece) - 1
        responses.append(
            target.send(
                "PUT",
                f"staging/{session_id}/{kind}/{ref}",
                identity=identity,
                content=piece,
                headers={"Content-Range": f"bytes {start}-{end}/{total}"},
            )
        )
    return responses


def breakpoint_query(
    target: Target, session_id: str, *, kind: str, ref: str, identity: Identity
) -> httpx.Response:
    """断点查询（HTTP §4.7）：``HEAD`` 同一 URL；只读、不续期。"""
    return target.send("HEAD", f"staging/{session_id}/{kind}/{ref}", identity=identity)


def stage_all(
    target: Target, session_id: str, document: WireDocument, *, identity: Identity
) -> None:
    """把文档的全部页与元素对象逐层上传进会话（不含 blob；blob 用例自行上传）。"""
    for page, page_hash in zip(document.pages, document.page_hashes, strict=True):
        response = upload_page(target, session_id, page, page_hash, identity=identity)
        assert response.status_code in (200, 201), (
            f"页上传失败：{response.status_code} {response.text[:200]}"
        )
    for element in document.objects:
        ref = dpe_hash.content_hash(cast(ElementObject, element))
        response = upload_element(target, session_id, element, ref, identity=identity)
        assert response.status_code in (200, 201), (
            f"元素上传失败：{response.status_code} {response.text[:200]}"
        )


def assert_staging_head_error(response: httpx.Response, *, status: int, code: str) -> None:
    """``staging/`` 路径的 HEAD 错误：只有 ``DPE-Error-Code`` 头、没有体，
    且不带 Vary（HTTP §3.1）。"""
    where = f"HEAD {response.request.url}"
    assert response.status_code == status, f"{where}：预期 {status}，实际 {response.status_code}"
    assert response.content == b"", f"{where}：HEAD 响应不得带内容"
    assert response.headers.get("DPE-Error-Code") == code, (
        f"{where}：DPE-Error-Code 是 {response.headers.get('DPE-Error-Code')!r}，期望 {code!r}"
    )
    assert not has_dpe_vary(response), "staging 路径不带 Vary: DPE-Hash-Contract（HTTP §3.1）"


# ------------------------------------------------- 越权 URI 与越界载荷（#56 追加；只追加）

#: 越界载荷检查的预算：合法服务端的 max_payload_bytes 更大时这类检查超预算 skip（不判红）
PAYLOAD_BUDGET = 64 * 1024 * 1024


def outside_scoped(target: Target, scoped: Identity, name: str) -> str:
    """scoped 身份**无写授权**的 URI：取测试命名空间下的 URI，与 scoped 前缀的写法无关。

    测试命名空间（``uri-prefix``）与 scoped 可写前缀都由操作者配置且无互斥约束：命名空间落在
    scoped 前缀内时无法构造越权写，显式 skip 并计入计数（合规配置不得被判失败）。不用「在 scoped
    前缀后拼接后缀」的写法：前缀不以 ``/`` 结尾时，拼出的 URI 仍按字符串前缀落在授权范围内。
    """
    assert scoped.prefix is not None, "scoped 身份必须声明 prefix"
    if target.uri_prefix.startswith(scoped.prefix):
        pytest.skip("测试命名空间落在 scoped 可写前缀内，无法构造越权写")
    return target.uri(f"outside/{uuid.uuid4().hex}/{name}")


def payload_limit_within_budget(target: Target, identity: Identity) -> int:
    """``max_payload_bytes``；超过 ``PAYLOAD_BUDGET`` 时显式 skip（不构造越界请求）。"""
    limit = limits_of(target, identity=identity)["max_payload_bytes"]
    if limit > PAYLOAD_BUDGET:
        pytest.skip(f"max_payload_bytes={limit} 超过本检查预算 {PAYLOAD_BUDGET}，不构造越界请求")
    return limit


# ------------------------------------------------- 各章共用的铺垫（#57 追加；只追加）

#: 合法但必然比较不上的 doc_hash（条件头与 base_hash 按值比较，core §5.2）
WRONG_HASH = "dpe1:" + "0" * 64


def seed(target: Target, uri: str, document: WireDocument, *, identity: Identity) -> WireDocument:
    """以快路径新建一份文档（``If-None-Match: *``），供检查铺垫；返回该文档。"""
    response = commit(target, uri, document, identity=identity, if_none_match_star=True)
    assert response.status_code == 201, f"铺垫失败：{response.status_code} {response.text[:200]}"
    return document


def inside_scoped(scoped: Identity, name: str) -> str:
    """scoped 身份**有写授权**的 URI（其可写前缀之下，每次唯一）。

    注意它在测试命名空间（``uri-prefix``）之外：以此铺垫的文档留在操作者配置的 scoped 前缀下。
    """
    assert scoped.prefix is not None, "scoped 身份必须声明 prefix"
    return f"{scoped.prefix}{uuid.uuid4().hex}/{name}"


def commit_with_session(
    target: Target,
    uri: str,
    document: WireDocument,
    session_id: str,
    *,
    identity: Identity,
    if_match: str | None = None,
) -> httpx.Response:
    """引用暂存会话的 commit（只带文档对象，HTTP §4.8）。

    给出 ``if_match`` 时以它为前置条件更新，否则以 ``If-None-Match: *`` 新建。
    """
    headers = {"If-Match": f'"{if_match}"'} if if_match is not None else {"If-None-Match": "*"}
    return target.send(
        "PUT",
        "documents",
        identity=identity,
        params={"uri": uri},
        headers=headers,
        json={"document": document.document, "staging_session": session_id},
    )


# ------------------------------------------------- 读回、delta 与完整翻页（#59 追加；只追加）


def skeleton_multiset(target: Target, uri: str, *, identity: Identity) -> Counter[str]:
    """经 get_skeleton 读回并展开为元素 content_hash 多重集（delta 的计数单位，core §3.3）。"""
    response = target.send("GET", "documents", identity=identity, params={"uri": uri})
    assert response.status_code == 200, f"读回 {uri} 失败：{response.status_code}"
    return Counter(h for page in response.json()["pages"] for h in page["elements"])


def expected_delta(before: Counter[str], after: Counter[str]) -> dict[str, int]:
    """core §3.3 的 delta：新旧元素多重集之差。

    added = 新∖旧、removed = 旧∖新、retained = 交集，均按多重集计。
    """
    return {
        "added": sum((after - before).values()),
        "removed": sum((before - after).values()),
        "retained": sum((after & before).values()),
    }


def list_all(
    target: Target,
    prefix: str,
    *,
    identity: Identity,
    contract: str = dpe_hash.CONTRACT,
    max_pages: int = 100,
) -> dict[str, str]:
    """``list`` 前缀下的全部条目（file_uri → doc_hash），翻页至 ``next_cursor`` 为空——不假定
    服务端的分页大小（``list_page_max``）。逐条核对条目确以 ``prefix`` 开头（HTTP §4.4 的码点前缀
    匹配），违例时给出明确原因。"""
    out: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        params = {"prefix": prefix} if cursor is None else {"prefix": prefix, "cursor": cursor}
        response = target.send(
            "GET", "documents", identity=identity, contract=contract, params=params
        )
        assert response.status_code == 200, f"list 返回 {response.status_code}"
        body = response.json()
        for entry in body["documents"]:
            uri = entry["file_uri"]
            assert uri.startswith(prefix), (
                f"list 返回了不以前缀 {prefix!r} 开头的条目 {uri!r}（HTTP §4.4 码点前缀匹配）"
            )
            out[uri] = entry["doc_hash"]
        cursor = body["next_cursor"]
        if cursor is None:
            return out
    raise AssertionError(f"list 分页未在 {max_pages} 页内结束")


# ------------------------------------------------- 元素与 blob 构造（#259 追加；只追加）


def image_element(blob_ref: str, **extra: Any) -> dict[str, Any]:
    """引用一个 blob 的最小 Image 元素对象（可带 metadata 等额外合法字段）。"""
    return {"category": "Image", "mime_type": "image/png", "blob": blob_ref, **extra}


def unique_blob() -> tuple[bytes, str]:
    """每次唯一的 blob 字节与其引用：断言不受同一持久服务端上其他用例或上一轮遗留的影响。"""
    blob = f"blob-{uuid.uuid4().hex}".encode()
    return blob, dpe_hash.blob_ref(blob)
