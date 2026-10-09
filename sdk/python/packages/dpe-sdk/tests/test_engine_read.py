"""参考服务端引擎的只读操作：capabilities、head / batch_head、get_skeleton、list。"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import Engine, EngineConfig, IfAbsent
from engine_helpers import inline, make_engine, text_doc

C = dpe_hash.CONTRACT


def _put(engine: Engine, uri: str, text: str = "x") -> str:
    req = inline(text_doc([text]))
    return engine.commit("u", uri, req.body(), C, IfAbsent()).doc_hash


def test_capabilities_reflect_config() -> None:
    engine = make_engine(
        contracts=("dpe1", dpe_hash.DRILL_CONTRACT), max_payload_bytes=1024, list_page_max=3
    )
    caps = engine.capabilities()
    assert caps.protocol == "dpe/1"
    assert caps.hash_contracts == ["dpe1", "dpe2"]
    assert caps.limits.max_payload_bytes == 1024
    assert caps.limits.list_page_max == 3
    assert caps.limits.staging_ttl_seconds >= 3600
    assert caps.features == ["move"]


def test_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        EngineConfig(contracts=())
    with pytest.raises(ValueError):
        EngineConfig(contracts=("dpe9",))
    with pytest.raises(ValueError):
        EngineConfig(staging_ttl_seconds=60)


def test_head() -> None:
    engine = make_engine()
    assert engine.head("u", "test://a", C) is None
    doc_hash = _put(engine, "test://a")
    head = engine.head("u", "test://a", C)
    assert head is not None and head.doc_hash == doc_hash
    for contract in (None, "dpe2"):
        with pytest.raises(errors.ContractUnsupportedError):
            engine.head("u", "test://a", contract)


def test_batch_head() -> None:
    engine = make_engine()
    doc_hash = _put(engine, "test://a")
    body = json.dumps({"uris": ["test://a", "test://missing"]}).encode()
    heads = engine.batch_head("u", body, C).heads
    assert heads[0] is not None and heads[0].doc_hash == doc_hash
    assert heads[1] is None


@pytest.mark.parametrize(
    "body",
    [b"[", b"[]", b"{}", b'{"uris": "x"}', b'{"uris": [1]}', b'{"uris": [], "extra": 1}'],
)
def test_batch_head_rejects_bad_bodies(body: bytes) -> None:
    engine = make_engine()
    with pytest.raises(errors.ValidationError):
        engine.batch_head("u", body, C)


def test_batch_head_item_location() -> None:
    engine = make_engine()
    with pytest.raises(errors.ValidationError) as info:
        engine.batch_head("u", b'{"uris": ["a:1", 2]}', C)
    assert info.value.path == "/uris/1"


def test_batch_head_order_and_limits() -> None:
    engine = make_engine(batch_head_max=2, max_payload_bytes=64)
    with pytest.raises(errors.ValidationError):
        engine.batch_head("u", json.dumps({"uris": ["a:1", "a:2", "a:3"]}).encode(), C)
    # I-JSON 先于契约声明；传输层上限最先
    with pytest.raises(errors.ValidationError):
        engine.batch_head("u", b"{", None)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.batch_head("u", b"[]", None)
    with pytest.raises(errors.PayloadTooLargeError):
        engine.batch_head("u", b" " * 65, None)


def test_get_skeleton_absent() -> None:
    engine = make_engine()
    assert engine.get_skeleton("u", "test://none", C) is None
    with pytest.raises(errors.ContractUnsupportedError):
        engine.get_skeleton("u", "test://none", "dpe9")


def test_list_prefix_is_code_point_not_segment() -> None:
    engine = make_engine()
    for uri in ("test://a/x", "test://ab", "test://a", "test://b/x"):
        _put(engine, uri)
    listed = [d.file_uri for d in engine.list_documents("u", C, prefix="test://a").documents]
    assert listed == ["test://a", "test://a/x", "test://ab"]
    listed = [d.file_uri for d in engine.list_documents("u", C, prefix="test://a/").documents]
    assert listed == ["test://a/x"]
    assert len(engine.list_documents("u", C).documents) == 4


def test_list_order_is_code_point_order() -> None:
    """file_uri 只含 ASCII（core §1.1，非 ASCII 须先百分号编码），码点序即字节序。"""
    engine = make_engine()
    uris = ["test://h/Z", "test://h/a", "test://h/%C3%A9", "test://h/~", "test://h/!"]
    for uri in uris:
        _put(engine, uri)
    listed = [d.file_uri for d in engine.list_documents("u", C).documents]
    assert listed == sorted(uris)  # Python 的 str 比较即码点序


def test_list_pagination() -> None:
    engine = make_engine()
    hashes = {f"test://p/{i}": _put(engine, f"test://p/{i}", str(i)) for i in range(5)}
    seen: list[str] = []
    cursor = None
    while True:
        page = engine.list_documents("u", C, prefix="test://p/", cursor=cursor, limit=2)
        assert len(page.documents) <= 2
        seen.extend(d.file_uri for d in page.documents)
        for d in page.documents:
            assert d.doc_hash == hashes[d.file_uri]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == sorted(hashes)


def test_list_pagination_is_stable_under_writes() -> None:
    engine = make_engine()
    for i in range(4):
        _put(engine, f"test://s/{i}")
    first = engine.list_documents("u", C, limit=2)
    assert first.next_cursor is not None
    _put(engine, "test://s/0a")  # 插在已返回的范围内，不影响后续页
    rest = engine.list_documents("u", C, cursor=first.next_cursor, limit=10)
    assert [d.file_uri for d in rest.documents] == ["test://s/2", "test://s/3"]


@pytest.mark.parametrize("limit", [0, -1, 4, True])
def test_list_limit_range(limit: int) -> None:
    engine = make_engine(list_page_max=3)
    with pytest.raises(errors.ValidationError):
        engine.list_documents("u", C, limit=limit)


def test_list_limit_defaults_to_max() -> None:
    engine = make_engine(list_page_max=2)
    for i in range(3):
        _put(engine, f"test://d/{i}")
    page = engine.list_documents("u", C)
    assert len(page.documents) == 2 and page.next_cursor is not None


@pytest.mark.parametrize("cursor", ["", "garbage", "c1.%%%", "c1.//8=", "c1.dGVzdA"])
def test_list_rejects_unknown_cursor(cursor: str) -> None:
    engine = make_engine()
    with pytest.raises(errors.ValidationError):
        engine.list_documents("u", C, cursor=cursor)


def test_list_contract() -> None:
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    doc = text_doc(["x"])
    engine.commit("u", "test://a", inline(doc).body(), C, IfAbsent())
    listed = engine.list_documents("u", "dpe2").documents
    assert listed[0].doc_hash == inline(doc, "dpe2").doc_hash
    with pytest.raises(errors.ContractUnsupportedError):
        engine.list_documents("u", "dpe9")


def test_config_requires_positive_limits_and_unique_contracts() -> None:
    for name in (
        "max_payload_bytes",
        "page_max_bytes",
        "blob_max_bytes",
        "blob_chunk_bytes",
        "batch_head_max",
        "list_page_max",
        "missing_max",
    ):
        for bad in (0, -1, True):
            overrides: dict[str, Any] = {name: bad}
            with pytest.raises(ValueError):
                EngineConfig(**overrides)
    with pytest.raises(ValueError):
        EngineConfig(contracts=("dpe1", "dpe1"))


def test_list_prefix_is_not_normalized() -> None:
    """前缀按原样匹配规范化后的 URI（core §3，#63）：规范化（core §1.1）不作用于前缀。"""
    engine = make_engine()
    _put(engine, "TEST://Docs/X")
    _put(engine, "test://docs/dir%2f")
    listed = engine.list_documents("u", C, prefix="test://docs/").documents
    assert [d.file_uri for d in listed] == ["test://docs/X", "test://docs/dir%2F"]
    # 前缀按原样：大小写不同即不匹配；不完整的转义、非 ASCII 也不报错
    assert engine.list_documents("u", C, prefix="TEST://").documents == []
    assert len(engine.list_documents("u", C, prefix="test://docs/dir%2").documents) == 1
    assert engine.list_documents("u", C, prefix="test://é").documents == []


def test_sorted_index_tracks_creates_deletes_and_moves() -> None:
    engine = make_engine()
    hashes = {u: _put(engine, u, u) for u in ("test://c", "test://a", "test://b")}
    engine.delete("u", "test://b", C, hashes["test://b"])
    engine.move(
        "u",
        json.dumps(
            {"from_uri": "test://c", "to_uri": "test://0", "base_hash": hashes["test://c"]}
        ).encode(),
        C,
    )
    store = engine._store
    assert store.uris == sorted(store.docs) == ["test://0", "test://a"]
    assert [d.file_uri for d in engine.list_documents("u", C).documents] == store.uris


@pytest.mark.parametrize(
    ("prefix", "after", "limit", "expected"),
    [
        ("test://b", None, 10, ["test://b/1", "test://b/2"]),
        ("test://b", "test://a/9", 10, ["test://b/1", "test://b/2"]),  # cursor 小于前缀区间
        ("test://b", "test://b/2", 10, []),  # cursor 越过前缀区间
        ("test://b", "test://b/1", 10, ["test://b/2"]),
        ("test://b", None, 2, ["test://b/1", "test://b/2"]),  # 恰好 limit 条
        ("test://z", None, 10, []),
        ("", "test://b/2", 1, ["test://c"]),
    ],
)
def test_page_after_boundaries(
    prefix: str, after: str | None, limit: int, expected: list[str]
) -> None:
    engine = make_engine()
    for uri in ("test://a/1", "test://b/1", "test://b/2", "test://c"):
        _put(engine, uri)
    assert engine._store.page_after(prefix, after, limit) == expected


def test_list_exact_limit_and_stale_cursor() -> None:
    engine = make_engine()
    hashes = {u: _put(engine, u, u) for u in ("test://p/1", "test://p/2", "test://p/3")}
    page = engine.list_documents("u", C, prefix="test://p/", limit=3)
    assert len(page.documents) == 3 and page.next_cursor is None
    first = engine.list_documents("u", C, prefix="test://p/", limit=2)
    assert first.next_cursor is not None
    # 已返回的最后一项被删除：旧 cursor 照常翻页
    engine.delete("u", "test://p/2", C, hashes["test://p/2"])
    rest = engine.list_documents("u", C, prefix="test://p/", cursor=first.next_cursor)
    assert [d.file_uri for d in rest.documents] == ["test://p/3"]
    assert rest.next_cursor is None
