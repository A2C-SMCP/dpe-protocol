"""响应报文（HTTP 绑定 §4）：解析、未知成员宽容、内嵌对象仍封闭。"""

from __future__ import annotations

from typing import Any

import dpe_hash
import pytest
from dpe_sdk.wire import BatchHeads, Capabilities, CommitResult, ListPage, Missing, Skeleton
from pydantic import ValidationError as PydanticValidationError

H = "dpe1:" + "a" * 64

CAPABILITIES: dict[str, Any] = {
    "protocol": "dpe/1",
    "hash_contracts": ["dpe1"],
    "limits": {
        "max_payload_bytes": 8388608,
        "page_max_bytes": 268435456,
        "staging_ttl_seconds": 86400,
        "blob_max_bytes": 104857600,
        "blob_chunk_bytes": 8388608,
        "batch_head_max": 500,
        "list_page_max": 1000,
    },
    "content_encodings": ["gzip"],
    "features": ["move"],
}


def test_capabilities_round_trip() -> None:
    caps = Capabilities.model_validate(CAPABILITIES)
    assert caps.model_dump() == CAPABILITIES
    assert caps.limits.batch_head_max == 500


def test_envelopes_ignore_unknown_members() -> None:
    data = {**CAPABILITIES, "future": 1, "limits": {**CAPABILITIES["limits"], "new_limit": 2}}
    caps = Capabilities.model_validate(data)
    assert caps.model_dump() == CAPABILITIES


def test_batch_heads_and_list_page() -> None:
    heads = BatchHeads.model_validate({"heads": [{"doc_hash": H}, None]})
    assert heads.heads[0] is not None and heads.heads[0].doc_hash == H
    assert heads.heads[1] is None
    page = ListPage.model_validate(
        {"documents": [{"file_uri": "x:a", "doc_hash": H}], "next_cursor": None}
    )
    assert page.documents[0].file_uri == "x:a"


def test_commit_result_status_is_closed() -> None:
    ok = {"status": "created", "doc_hash": H, "delta": {"added": 1, "removed": 0, "retained": 0}}
    assert CommitResult.model_validate(ok).status == "created"
    with pytest.raises(PydanticValidationError):
        CommitResult.model_validate({**ok, "status": "merged"})


def _skeleton() -> dict[str, Any]:
    page = {"title": "p", "page_metadata": {}, "elements": [H]}
    return {
        "file_uri": "x:a",
        "doc_hash": H,
        "document": {"file_type": "md", "pages": [dpe_hash.object_hash(page, "page")]},
        "pages": [page],
    }


def test_skeleton_round_trip() -> None:
    data = _skeleton()
    assert Skeleton.model_validate(data).model_dump(mode="json") == data


def test_skeleton_objects_stay_closed() -> None:
    data = _skeleton()
    data["pages"][0]["page_number"] = 1
    # 内嵌模型的 DpeHashError 按 pydantic 惯例包装（dpe_sdk.models）
    with pytest.raises(PydanticValidationError) as info:
        Skeleton.model_validate(data)
    assert isinstance(info.value.errors()[0]["ctx"]["error"], dpe_hash.UndefinedFieldError)


def test_missing_defaults_to_empty_layers() -> None:
    assert Missing().model_dump() == {"pages": [], "content_hashes": [], "blobs": []}
