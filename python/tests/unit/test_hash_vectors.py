"""hash-contract-v1 一致性向量：期望值由 TFRobot 内核生成（scripts/gen_vectors.py）。"""

import json
from pathlib import Path
from typing import Any

import pytest

from dpe_protocol.hashing import HashStrategyRef, compute_hashes
from dpe_protocol.schema import Document

VECTOR_DIR = Path(__file__).resolve().parents[3] / "vectors" / "v1"
MANIFEST = json.loads((VECTOR_DIR / "manifest.json").read_text(encoding="utf-8"))


def load(case: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((VECTOR_DIR / f"{case}.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("case", MANIFEST["cases"])
def test_vector_matches_kernel(case: str) -> None:
    vector = load(case)
    hashes = compute_hashes(
        Document.model_validate(vector["input"]), HashStrategyRef.parse(vector["hash_strategy_uri"])
    )

    assert [list(p.content_hashes) for p in hashes.pages] == vector["expect"]["content_hashes"]
    assert {str(p.number): p.page_hash for p in hashes.pages} == vector["expect"]["page_hashes"]
    assert hashes.doc_hash == vector["expect"]["doc_hash"]


@pytest.mark.parametrize(("left", "right"), MANIFEST["distinct_pairs"])
def test_negative_pairs_differ(left: str, right: str) -> None:
    assert load(left)["expect"]["doc_hash"] != load(right)["expect"]["doc_hash"]


def test_moving_element_keeps_content_hash() -> None:
    a, b = load("page_order")["expect"], load("page_order_swapped")["expect"]
    assert sorted(a["content_hashes"][0]) == sorted(b["content_hashes"][0])
    assert a["page_hashes"] != b["page_hashes"]


def test_manifest_records_kernel_provenance() -> None:
    # 向量必须能追溯到生成它的内核版本，否则无法判断是否落后于内核
    provenance = MANIFEST["provenance"]
    assert provenance["tfrobot_version"] and provenance["pydantic_version"] and provenance["generated_at"]
