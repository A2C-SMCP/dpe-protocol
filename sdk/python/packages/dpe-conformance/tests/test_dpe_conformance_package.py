"""工程骨架冒烟测试：包可导入、带类型标记、与 dpe-hash 同版本、套件随包分发。"""

from __future__ import annotations

from importlib.metadata import version
from importlib.resources import files

import dpe_conformance
import dpe_hash


def test_version_matches_distribution() -> None:
    assert dpe_conformance.__version__ == version("dpe-conformance")


def test_ships_py_typed() -> None:
    assert files("dpe_conformance").joinpath("py.typed").is_file()


def test_same_version_as_dpe_hash() -> None:
    assert dpe_conformance.__version__ == dpe_hash.__version__


def test_ships_checks_suite() -> None:
    """套件在 source/editable 与构建出的 wheel 里都随包分发（CLI 以包内路径收集章节）。"""
    assert files("dpe_conformance").joinpath("checks/smoke.py").is_file()
