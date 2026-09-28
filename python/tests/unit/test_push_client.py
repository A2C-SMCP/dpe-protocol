import gzip
import json
from typing import Any

import pytest

from dpe_protocol import DocElement, DocPage, Document, DPEPushClient, ElementCategory, ElementMetadata
from dpe_protocol.hashing import compute_hashes
from dpe_protocol.push import DPEPushError, DPEValidationError, NoCompatibleHashStrategyError
from dpe_protocol.testing import FakeRobotServer
from tests.conftest import make_doc


def _bodies(server: FakeRobotServer, path_suffix: str) -> list[dict[str, Any]]:
    return [json.loads(gzip.decompress(r.content)) for r in server.requests if r.url.path.endswith(path_suffix)]


async def test_first_push_creates_document_with_all_contents(client: DPEPushClient, server: FakeRobotServer) -> None:
    doc = make_doc("欢迎加入。", "第一条")
    result = await client.push(doc)

    assert result.status == "created"
    assert result.negotiate is not None and result.negotiate.status == "new"
    assert result.contents_sent == 4
    assert server.documents["dpe://acme/handbook"].doc_hash == result.doc_hash


async def test_second_push_only_sends_missing_contents(client: DPEPushClient, server: FakeRobotServer) -> None:
    first = await client.push(make_doc("欢迎加入。", "第一条"))
    second = await client.push(make_doc("欢迎加入。", "第二条"), base_doc_hash=first.doc_hash)

    assert second.status == "updated"
    assert second.contents_sent == 1
    commit = _bodies(server, ":commit")[-1]
    assert [c["element"]["text"] for c in commit["contents"]] == ["第二条"]
    assert commit["manifest"]["base_doc_hash"] == first.doc_hash
    assert server.documents["dpe://acme/handbook"].document.pages[0].elements[2].text == "第二条"


async def test_unchanged_document_stops_after_negotiate(client: DPEPushClient, server: FakeRobotServer) -> None:
    first = await client.push(make_doc("a"))
    again = await client.push(make_doc("a"))

    assert again.status == "unchanged"
    assert again.doc_hash == first.doc_hash
    assert len(_bodies(server, ":commit")) == 1


async def test_wire_format_has_no_hash_fields_in_contents(client: DPEPushClient, server: FakeRobotServer) -> None:
    await client.push(make_doc("a"))
    request = next(r for r in server.requests if r.url.path.endswith(":commit"))
    body = json.loads(gzip.decompress(request.content))

    assert request.headers["Content-Type"] == "application/vnd.tfrs.dpe.v1+json"
    assert request.headers["Authorization"] == "Bearer t0ken"
    assert request.headers["X-TFRS-Robot-Id"] == "rid-1"
    assert request.headers["Idempotency-Key"]
    for item in body["contents"]:
        assert not {"content_hash", "seq_in_page", "ele_id"} & item["element"].keys()
    # doc_metadata 必须带全量声明字段（含 null），否则服务端会用 now() 填 created_at
    assert body["manifest"]["doc_metadata"]["created_at"] is None


async def test_skip_negotiate_recovers_from_insufficient_content(
    client: DPEPushClient, server: FakeRobotServer
) -> None:
    result = await client.push(make_doc("a"), skip_negotiate=True)

    assert result.status == "created"
    assert not _bodies(server, ":negotiate")
    first, second = _bodies(server, ":commit")
    assert first["contents"] == [] and len(second["contents"]) == 3


async def test_retries_rate_limit_then_succeeds(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.faults = [(429, "DPE_RATE_LIMITED", {"Retry-After": "1"}), (503, "DPE_ROBOT_UNAVAILABLE", {})]
    result = await client.push(make_doc("a"))
    assert result.status == "created"


async def test_non_retryable_error_is_raised_with_code(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.faults = [(400, "DPE_MANIFEST_INVALID", {})]
    with pytest.raises(DPEPushError) as exc:
        await client.push(make_doc("a"))
    assert exc.value.code == "DPE_MANIFEST_INVALID" and exc.value.http_status == 400 and not exc.value.retryable


async def test_image_path_rejected_locally(client: DPEPushClient, server: FakeRobotServer) -> None:
    doc = make_doc("a")
    doc.pages[0].elements[-1].ele_metadata = ElementMetadata(image_path="r1/x.png")
    with pytest.raises(DPEValidationError):
        await client.push(doc)
    assert not _bodies(server, ":negotiate")


async def test_no_compatible_hash_strategy(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.accepted_strategies = ["hash-strategy://default?algo_version=v9"]
    with pytest.raises(NoCompatibleHashStrategyError):
        await client.push(make_doc("a"))


async def test_payload_too_large_fails_before_sending(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.max_payload_bytes = 200
    with pytest.raises(DPEPushError) as exc:
        await client.push(make_doc("a"))
    assert exc.value.code == "DPE_PAYLOAD_TOO_LARGE"
    assert all(r.method == "GET" for r in server.requests)


def _big_doc(n: int, *, size: int = 400, prefix: str = "") -> Document:
    return Document.model_validate(
        {
            "file_uri": "dpe://acme/big",
            "file_type": "txt",
            "pages": [
                DocPage(
                    number=p,
                    title=f"p{p}",
                    elements=[
                        DocElement(category=ElementCategory.NARRATIVE_TEXT, text=f"{prefix}{p}-{i}-" + "x" * size)
                        for i in range(3)
                    ],
                )
                for p in range(n)
            ],
        }
    )


async def test_large_first_push_is_sharded(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.max_payload_bytes = 4000
    doc = _big_doc(6)
    result = await client.push(doc)

    assert result.shards > 1
    assert result.contents_sent == 18
    stored = server.documents["dpe://acme/big"].document
    assert [e.text for p in stored.pages for e in p.elements] == [e.text for p in doc.pages for e in p.elements]
    for body in _bodies(server, ":commit"):
        assert len(json.dumps(body, ensure_ascii=False).encode()) <= 4000
        # 每一片都是完整文档结构：页一个不少
        assert len(body["manifest"]["pages"]) == 6


async def test_sharded_update_never_drops_existing_elements(client: DPEPushClient, server: FakeRobotServer) -> None:
    await client.push(_big_doc(6))
    old_hashes = set(server.documents["dpe://acme/big"].contents)
    commits_before = len(_bodies(server, ":commit"))

    server.max_payload_bytes = 4000
    await client.discover(refresh=True)  # 能力文档有缓存，服务端调整上限后需刷新
    updated = _big_doc(6)
    for page in updated.pages:  # 每页新增一个大 element，缺口超过单次上限
        page.elements.append(_big_doc(1, prefix=f"new{page.number}").pages[0].elements[0])
    result = await client.push(updated)

    assert result.shards > 1 and result.contents_sent == 6
    kept = old_hashes & compute_hashes(updated).all_content_hashes()
    assert kept == old_hashes
    for body in _bodies(server, ":commit")[commits_before:]:
        shard_hashes = {e["content_hash"] for p in body["manifest"]["pages"] for e in p["elements"]}
        # 旧元素在每一片中都保留：不会先删后建，也就不会重复传输与重复学习
        assert kept <= shard_hashes
    assert result.counts is not None and result.counts.elements_removed == 0
    assert [e.text for p in server.documents["dpe://acme/big"].document.pages for e in p.elements] == [
        e.text for p in updated.pages for e in p.elements
    ]


async def test_single_element_over_budget_cannot_be_sharded(client: DPEPushClient, server: FakeRobotServer) -> None:
    server.max_payload_bytes = 4000
    with pytest.raises(DPEPushError) as exc:
        await client.push(_big_doc(1, size=5000))
    assert exc.value.code == "DPE_PAYLOAD_TOO_LARGE"
