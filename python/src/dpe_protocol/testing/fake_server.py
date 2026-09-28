"""dpe-push/1 的内存版 Robot 服务端，供测试与本地联调使用（**不是**生产实现）。

它按 push-protocol-v1 的服务端语义工作：缺口按 ``file_uri`` 限定、服务端重算 hash、
``content_hash`` 不符即拒绝、内容不足回 409、幂等键回放首次响应。

    server = FakeRobotServer()
    client = DPEPushClient("http://robot.test", http_client=server.http_client())
"""

import gzip
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from dpe_protocol.hashing import DEFAULT_HASH_STRATEGY, HashStrategyRef, compute_hashes
from dpe_protocol.hashing.v1 import element_hash
from dpe_protocol.push.models import MEDIA_TYPE, WELL_KNOWN_PATH, Manifest
from dpe_protocol.schema import DocElement, Document

NEGOTIATE_PATH = "/v1/memory/dpe:negotiate"
COMMIT_PATH = "/v1/memory/dpe:commit"


@dataclass
class StoredDocument:
    document: Document
    doc_hash: str
    hash_strategy_uri: str
    #: content_hash → element（本文档旧树，缺口计算只在此范围内进行）
    contents: dict[str, DocElement]


@dataclass
class FakeRobotServer:
    accepted_strategies: list[str] = field(default_factory=lambda: [DEFAULT_HASH_STRATEGY.to_uri()])
    max_payload_bytes: int = 50 * 1024 * 1024
    documents: dict[str, StoredDocument] = field(default_factory=dict)
    #: 依次注入的故障 ``(http_status, code, headers)``，每个请求消费一个
    faults: list[tuple[int, str, dict[str, str]]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    _idempotency: dict[str, httpx.Response] = field(default_factory=dict)

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.faults and request.url.path != WELL_KNOWN_PATH:
            status, code, headers = self.faults.pop(0)
            return _error(status, code, "injected fault", headers=headers)
        if request.method == "GET" and request.url.path == WELL_KNOWN_PATH:
            return self._capabilities()
        body = _read_json(request)
        if request.url.path == NEGOTIATE_PATH:
            return self._negotiate(Manifest.model_validate(body))
        if request.url.path == COMMIT_PATH:
            key = request.headers.get("Idempotency-Key", "")
            if key and key in self._idempotency:
                return self._idempotency[key]
            resp = self._commit(body)
            if key and resp.status_code < 400:
                self._idempotency[key] = resp
            return resp
        return _error(404, "NOT_FOUND", request.url.path)

    # ------------------------------------------------------------------

    def _capabilities(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "protocol_versions": ["1"],
                "accepted_hash_strategies": [
                    {"uri": uri, "status": "stable", "deprecated_at": None} for uri in self.accepted_strategies
                ],
                "default_hash_strategy_uri": self.accepted_strategies[0] if self.accepted_strategies else None,
                "max_payload_bytes": self.max_payload_bytes,
                "content_encodings": ["gzip", "identity"],
                "delta_supported": True,
                "endpoints": {"negotiate": NEGOTIATE_PATH, "commit": COMMIT_PATH},
            },
        )

    def _negotiate(self, manifest: Manifest) -> httpx.Response:
        wanted = [e.content_hash for p in manifest.pages for e in p.elements]
        stored = self.documents.get(manifest.file_uri)
        if stored is None:
            return _ok(
                {"status": "new", "missing_content_hashes": list(dict.fromkeys(wanted)), "server_doc_exists": False}
            )
        base_matched = None if manifest.base_doc_hash is None else manifest.base_doc_hash == stored.doc_hash
        if stored.doc_hash == manifest.doc_hash and stored.hash_strategy_uri == manifest.hash_strategy_uri:
            return _ok(
                {
                    "status": "unchanged",
                    "server_doc_exists": True,
                    "server_doc_hash": stored.doc_hash,
                    "base_doc_hash_matched": base_matched,
                }
            )
        # 跨策略的 hash 不可比：旧树整体视为不可复用
        reusable = stored.contents if stored.hash_strategy_uri == manifest.hash_strategy_uri else {}
        missing = [h for h in dict.fromkeys(wanted) if h not in reusable]
        return _ok(
            {
                "status": "gap",
                "missing_content_hashes": missing,
                "server_doc_exists": True,
                "server_doc_hash": stored.doc_hash,
                "base_doc_hash_matched": base_matched,
            }
        )

    def _commit(self, body: dict[str, Any]) -> httpx.Response:
        manifest = Manifest.model_validate(body["manifest"])
        strategy = HashStrategyRef.parse(manifest.hash_strategy_uri)
        if manifest.hash_strategy_uri not in self.accepted_strategies:
            return _error(
                400,
                "DPE_HASH_STRATEGY_UNSUPPORTED",
                "strategy not accepted",
                data={"accepted": self.accepted_strategies},
            )

        supplied: dict[str, DocElement] = {}
        for index, item in enumerate(body.get("contents", [])):
            ele = DocElement.model_validate(item["element"])
            actual = element_hash(ele)
            if actual != item["content_hash"]:
                return _error(
                    400,
                    "DPE_CONTENT_HASH_MISMATCH",
                    "content hash mismatch",
                    data={"expected": item["content_hash"], "actual": actual, "element_index": index},
                )
            supplied[actual] = ele

        stored = self.documents.get(manifest.file_uri)
        pool = dict(stored.contents) if stored and stored.hash_strategy_uri == manifest.hash_strategy_uri else {}
        pool.update(supplied)
        wanted = [e.content_hash for p in manifest.pages for e in p.elements]
        if missing := [h for h in dict.fromkeys(wanted) if h not in pool]:
            return _error(409, "DPE_INSUFFICIENT_CONTENT", "missing contents", data={"missing_content_hashes": missing})

        document = Document.model_validate(
            {
                "file_uri": manifest.file_uri,
                "file_type": manifest.file_type,
                "doc_metadata": manifest.doc_metadata,
                "pages": [
                    {
                        "number": p.number,
                        "title": p.title,
                        "elements": [pool[e.content_hash].model_dump() for e in p.elements],
                    }
                    for p in manifest.pages
                ],
            }
        )
        # 服务端重算是唯一权威；manifest 中的 page_hash / doc_hash 只作协商提示
        hashes = compute_hashes(document, strategy)
        previous = stored.doc_hash if stored else None
        self.documents[manifest.file_uri] = StoredDocument(
            document=document,
            doc_hash=hashes.doc_hash,
            hash_strategy_uri=manifest.hash_strategy_uri,
            contents={h: pool[h] for h in hashes.all_content_hashes()},
        )
        old_hashes = set(stored.contents) if stored else set()
        new_hashes = hashes.all_content_hashes()
        if previous is None:
            status = "created"
        elif previous == hashes.doc_hash:
            status = "unchanged"
        else:
            status = "updated"
        return _ok(
            {
                "status": status,
                "doc_hash": hashes.doc_hash,
                "hash_strategy_uri": manifest.hash_strategy_uri,
                "counts": {
                    "elements_added": len(new_hashes - old_hashes),
                    "elements_removed": len(old_hashes - new_hashes),
                    "elements_unchanged": len(new_hashes & old_hashes),
                },
            }
        )


def _read_json(request: httpx.Request) -> dict[str, Any]:
    raw = request.content
    if request.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    data: dict[str, Any] = json.loads(raw)
    return data


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload, headers={"Content-Type": MEDIA_TYPE})


def _error(
    status: int, code: str, message: str, *, data: Any = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message, "data": data}, headers=headers)
