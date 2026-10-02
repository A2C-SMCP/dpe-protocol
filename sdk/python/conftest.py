"""两个包共用的测试夹具。

一致性向量是规范的一部分，测试直接读取仓库根目录的 ``vectors/``，不复制进 SDK。
``DPE_VECTORS_DIR`` 可覆盖默认位置（如在仓库之外运行测试）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _find_vectors_dir() -> Path:
    override = os.environ.get("DPE_VECTORS_DIR")
    if override:
        path = Path(override).resolve()
        if not (path / "manifest.json").is_file():
            raise FileNotFoundError(f"DPE_VECTORS_DIR 下没有 manifest.json：{path}")
        return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vectors"
        if (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError("找不到仓库的 vectors/ 目录，请设置 DPE_VECTORS_DIR")


@pytest.fixture(scope="session")
def vectors_dir() -> Path:
    return _find_vectors_dir()
