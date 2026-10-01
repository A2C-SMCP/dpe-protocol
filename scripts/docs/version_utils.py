"""版本号读取工具。

pyproject.toml 的 project.version 是文档版本的单一真实来源（由 bump-my-version 维护）。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
PYPROJECT_TOML: Final[Path] = PROJECT_ROOT / "pyproject.toml"


def get_project_version() -> str:
    """从 pyproject.toml 读取文档版本号，如 "0.1.0" 或 "0.1.1-dev"。"""
    with PYPROJECT_TOML.open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    if not isinstance(version, str):
        raise RuntimeError(f"project.version 必须是字符串，实际为 {type(version)}")
    return version


def is_dev_version(version: str) -> bool:
    """开发中版本（X.Y.Z-dev）尚未发布，不得占用 latest 别名。"""
    return version.endswith("-dev")


if __name__ == "__main__":
    try:
        print(get_project_version())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
