"""向量一致性：dpe_hash 必须逐字节通过 vectors/ 全部向量（含 dpe2 升级演练）。

零依赖，可直接 ``python3 tests/test_vectors.py`` 运行，也兼容 pytest。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dpe_hash import document_hashes, jcs  # noqa: E402

VECTORS_DIR = Path(__file__).resolve().parents[3] / "vectors"


def iter_vectors() -> list[dict]:
    files = sorted(VECTORS_DIR.glob("*.json"))
    assert files, f"no vectors found in {VECTORS_DIR}"
    return [json.loads(f.read_text(encoding="utf-8")) for f in files if f.name != "manifest.json"]


def test_document_vectors() -> None:
    count = 0
    for vec in iter_vectors():
        if vec["kind"] != "document":
            continue
        for key, document in vec["documents"].items():
            for contract, expected in vec["expected"][key].items():
                got = document_hashes(document, contract)
                assert got == expected, f"{vec['name']}/{key}/{contract}:\n got {got}\n exp {expected}"
                count += 1
    assert count > 0


def test_jcs_vectors() -> None:
    count = 0
    for vec in iter_vectors():
        if vec["kind"] != "jcs":
            continue
        for case in vec["cases"]:
            canonical = jcs(case["input"])
            assert canonical == case["canonical"], f"{vec['name']}: got {canonical!r}, exp {case['canonical']!r}"
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            assert digest == case["sha256"], f"{vec['name']}: sha256 mismatch for {canonical!r}"
            count += 1
    assert count > 0


def test_manifest_reserved_keys_match() -> None:
    """manifest 导出的保留键集合必须与 SDK 常量一致（#3 S1）。"""
    from dpe_hash import RESERVED_METADATA_KEYS

    manifest = json.loads((VECTORS_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(RESERVED_METADATA_KEYS) == manifest["reserved_metadata_keys"]


if __name__ == "__main__":
    test_document_vectors()
    test_jcs_vectors()
    test_manifest_reserved_keys_match()
    print("python dpe-hash: all vectors ok")
