"""向量一致性：dpe_hash 必须逐字节通过 vectors/ 全部向量（含 dpe2 升级演练与 relations）。

零依赖，可直接 ``python3 tests/test_vectors.py`` 运行，也兼容 pytest。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dpe_hash import document_hashes, jcs  # noqa: E402

VECTORS_DIR = Path(__file__).resolve().parents[3] / "vectors"


def iter_vectors() -> list[dict]:
    files = sorted(VECTORS_DIR.glob("*.json"))
    assert files, f"no vectors found in {VECTORS_DIR}"
    return [json.loads(f.read_text(encoding="utf-8")) for f in files if f.name != "manifest.json"]


def _resolve(computed: dict[str, Any], ref: str) -> Any:
    """按 vectors/README.md 的引用语法取值：``<文档键>.<路径>``。"""
    key, *path = ref.split(".")
    node: Any = computed[key]
    for part in path:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def test_document_vectors() -> None:
    count = 0
    for vec in iter_vectors():
        if vec["kind"] != "document":
            continue
        contracts = sorted(vec["expected"][next(iter(vec["documents"]))])
        for contract in contracts:
            computed: dict[str, Any] = {}
            for key, document in vec["documents"].items():
                got = document_hashes(document, contract)
                expected = vec["expected"][key][contract]
                assert got == expected, f"{vec['name']}/{key}/{contract}:\n got {got}\n exp {expected}"
                computed[key] = got
                count += 1
            # relations 是向量要证明的规范性质，消费方一并断言（vectors/README.md）
            for rel in vec.get("relations", []):
                if "equal" in rel:
                    values = [_resolve(computed, r) for r in rel["equal"]]
                    assert len(set(values)) == 1, f"{vec['name']}/{contract}: equal 不成立 {rel['equal']}"
                else:
                    values = [_resolve(computed, r) for r in rel["distinct"]]
                    assert len(set(values)) == len(values), f"{vec['name']}/{contract}: distinct 不成立 {rel['distinct']}"
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


def test_manifest_constants_match() -> None:
    """manifest 导出的契约常量必须与 SDK 常量一致（#3 S1）。"""
    from dpe_hash import FILE_TYPES

    manifest = json.loads((VECTORS_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert FILE_TYPES == set(manifest["file_types"])  # manifest 按规范声明顺序，比较集合


if __name__ == "__main__":
    test_document_vectors()
    test_jcs_vectors()
    test_manifest_constants_match()
    print("python dpe-hash: all vectors ok")
