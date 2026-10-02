"""工程骨架冒烟测试：包可导入、带类型标记、能读到仓库的一致性向量。"""

from __future__ import annotations

import json
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import dpe_hash


def test_version_matches_distribution() -> None:
    assert dpe_hash.__version__ == version("dpe-hash")


def test_ships_py_typed() -> None:
    assert files("dpe_hash").joinpath("py.typed").is_file()


def test_reads_repository_vectors(vectors_dir: Path) -> None:
    manifest = json.loads((vectors_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract"] == "dpe1"
