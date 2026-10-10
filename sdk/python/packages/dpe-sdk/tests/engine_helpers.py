"""参考服务端引擎测试的辅助：请求体构造、测试用授权器与可拨动时钟（经 pytest pythonpath 导入）。"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import dpe_hash
from dpe_sdk.testing import AllowAll, Engine, EngineConfig, UploadChunk


@dataclass(frozen=True)
class Inline:
    """一篇展开文档对应的快路径 commit 请求体与各层 hash。"""

    document: dict[str, Any]
    pages: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    doc_hash: str
    page_hashes: list[str]
    content_hashes: list[list[str]]

    def body(self, **extra: Any) -> bytes:
        payload: dict[str, Any] = {
            "document": self.document,
            "pages": self.pages,
            "objects": self.objects,
        }
        payload.update(extra)
        return json.dumps(payload).encode("utf-8")


def inline(document: Mapping[str, Any], contract: str = dpe_hash.CONTRACT) -> Inline:
    """把展开视图（向量输入形状）拆成线上的文档对象、页对象与元素对象。"""
    pages: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    page_hashes: list[str] = []
    content_hashes: list[list[str]] = []
    for page in document["pages"]:
        hashes = [dpe_hash.object_hash(e, "element", contract) for e in page["elements"]]
        objects.extend(page["elements"])
        wire_page = {**{k: v for k, v in page.items() if k != "elements"}, "elements": hashes}
        pages.append(wire_page)
        page_hashes.append(dpe_hash.object_hash(wire_page, "page", contract))
        content_hashes.append(hashes)
    wire_doc = {**{k: v for k, v in document.items() if k != "pages"}, "pages": page_hashes}
    return Inline(
        document=wire_doc,
        pages=pages,
        objects=objects,
        doc_hash=dpe_hash.object_hash(wire_doc, "document", contract),
        page_hashes=page_hashes,
        content_hashes=content_hashes,
    )


def text_doc(*pages: Sequence[str], file_type: str = "md", **fields: Any) -> dict[str, Any]:
    """由逐页的文本列表构造展开文档（每段文本一个 NarrativeText 元素）。"""
    return {
        "file_type": file_type,
        **fields,
        "pages": [
            {"elements": [{"category": "NarrativeText", "text": t} for t in page]} for page in pages
        ],
    }


def make_engine(**overrides: Any) -> Engine:
    """按 ``EngineConfig`` 的字段覆盖默认值构造引擎（默认授权器放行一切）。"""
    overrides.setdefault("authorizer", AllowAll())
    return Engine(EngineConfig(**overrides))


@dataclass
class FakeClock:
    """可拨动的测试时钟（会话过期与续期）。"""

    now: datetime = field(default_factory=lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def id_factory() -> Callable[[], str]:
    """确定且互不重复的会话 id 生成器（``st-0``、``st-1``…）。"""
    counter = itertools.count()
    return lambda: f"st-{next(counter)}"


def negotiate_body(
    uri: str, document: Mapping[str, Any], pages: Sequence[Mapping[str, Any]] = ()
) -> bytes:
    """negotiate 请求体：file_uri、文档对象与可选的附带页。"""
    payload: dict[str, Any] = {"file_uri": uri, "document": document}
    if pages:
        payload["pages"] = list(pages)
    return json.dumps(payload).encode("utf-8")


def commit_body(document: Mapping[str, Any], **extra: Any) -> bytes:
    """只带文档对象（可加 ``staging_session`` 等）的 commit 请求体。"""
    payload: dict[str, Any] = {"document": document}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def chunks(data: bytes, size: int) -> list[tuple[bytes, UploadChunk]]:
    """把字节按块切分，给出 (块, ``Content-Range`` 对应的范围)。"""
    out: list[tuple[bytes, UploadChunk]] = []
    for start in range(0, len(data), size):
        piece = data[start : start + size]
        out.append((piece, UploadChunk(start, start + len(piece) - 1, len(data))))
    return out
