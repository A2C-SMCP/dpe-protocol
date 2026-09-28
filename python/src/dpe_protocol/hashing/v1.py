"""``algo_version=v1`` 三层 hash（hash-contract-v1 §3–§5）。

本模块只接受模型对象，不接受裸 hash 参数：hash 值只在同一 ``hash_strategy_uri`` 下可比，
对外入口统一走 :func:`dpe_protocol.hashing.compute_hashes`。
"""

from collections.abc import Iterable

from dpe_protocol.hashing.primitives import concat_parts, digest, json_stable, text_bytes
from dpe_protocol.schema import DocElement, Document, ElementCategory

_HTML_IDENTITY_CATEGORIES = frozenset({ElementCategory.TABLE, ElementCategory.FORMULA})


def element_hash_parts(ele: DocElement) -> list[bytes]:
    """按 ``category`` 多态分派 hash 输入。全库恰好四种形态：Image / Table / Formula / 其它。"""
    meta = ele.ele_metadata
    if ele.category == ElementCategory.IMAGE:
        channel = meta.image_url or meta.image_base64 or meta.image_path or ""
        return [text_bytes(ele.text), text_bytes(channel), text_bytes(meta.image_mime_type)]
    if ele.category in _HTML_IDENTITY_CATEGORIES:
        return [text_bytes(ele.text), text_bytes(meta.text_as_html)]
    return [text_bytes(ele.text)]


def element_hash(ele: DocElement) -> str:
    return digest(concat_parts(element_hash_parts(ele)))


def page_hash(title: str | None, content_hashes: Iterable[str]) -> str:
    """页标题 + element ``content_hash`` 序列（数组顺序，非 ``seq_in_page`` 排序）。"""
    return digest(concat_parts([text_bytes(title), *(h.encode("utf-8") for h in content_hashes)]))


def doc_hash(doc: Document, page_hashes_by_number: dict[int, str]) -> str:
    """文档元信息 + 按 ``page.number`` 升序排列的 ``page_hash`` 序列。

    ``title`` 位当前恒为空串（内核 ``Document`` 未声明 ``title``），但占位必须保留。
    """
    meta_json = json_stable(doc.doc_metadata.model_dump(mode="json"))
    parts = [str(doc.file_uri).encode("utf-8"), doc.file_type.value.encode("utf-8"), b"", meta_json.encode("utf-8")]
    parts.extend(page_hashes_by_number[n].encode("utf-8") for n in sorted(page_hashes_by_number))
    return digest(concat_parts(parts))
