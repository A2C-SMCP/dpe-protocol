"""各章共用的构造与断言素材：文档生成（期望 hash 只经 dpe-hash）、problem 断言、身份助手。

放在包根而不是 ``checks/``：那是章节目录，``--section`` 的粒度就是它下面的一级模块/包。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import dpe_hash
import httpx
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
) -> WireDocument:
    """由「每页一组元素对象（线上形态）」构造完整文档与全部期望 hash。

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
            dpe_hash.content_hash(cast(ElementObject, element)) for element in elements
        ]
        page_hashes.append(dpe_hash.page_hash(PageFields(), content_hashes))
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
        doc_hash=dpe_hash.doc_hash(fields, page_hashes),
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
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
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


__all__ = [
    "WireDocument",
    "assert_commit_success",
    "assert_doc_headers",
    "assert_head_error",
    "assert_problem",
    "build_document",
    "commit",
    "has_dpe_vary",
    "optional_identity",
    "text_element",
]
