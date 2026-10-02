"""工程骨架冒烟测试：包可导入、带类型标记、与 dpe-hash 同版本。"""

from __future__ import annotations

from importlib.metadata import version
from importlib.resources import files

import dpe_hash
import dpe_sdk


def test_version_matches_distribution() -> None:
    assert dpe_sdk.__version__ == version("dpe-sdk")


def test_ships_py_typed() -> None:
    assert files("dpe_sdk").joinpath("py.typed").is_file()


def test_same_version_as_dpe_hash() -> None:
    assert dpe_sdk.__version__ == dpe_hash.__version__
