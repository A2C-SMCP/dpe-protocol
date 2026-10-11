"""套件执行：以 ``python -m pytest`` 子进程运行 ``dpe_conformance.checks``。

pytest 是跑分器的实现细节：子进程隔离让套件与调用方的 pytest 会话、环境变量（``PYTEST_*``）
互不纠缠，失败也不改写调用方进程状态。CLI 与本模块之间以计数 JSON 文件传递结果。

内层运行以 ``-c`` 指向包内的 ``conformance.ini``：配置与 rootdir 完全由该文件确定，途经
目录（site-packages、开发仓库）里的 pytest 配置一律不参与；``-p no:cacheprovider`` 避免
往套件目录写 ``.pytest_cache``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dpe_conformance import plugin
from dpe_conformance.plugin import CONFIG_ENV, COUNTS_ENV, TARGET_ENV

#: 子进程里需要剔除的 pytest 环境变量（会改变内部运行行为）
_PYTEST_ENV_KEYS = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST")

#: 内层 pytest 的专有配置（rootdir 即它所在目录，与调用方环境无关）
_PYTEST_INI = Path(__file__).parent / "conformance.ini"


def list_sections() -> tuple[str, ...]:
    """可用章节：``checks/`` 下的一级模块（.py）与包（目录）。"""
    root = plugin.checks_root()
    names = {
        path.stem for path in root.glob("*.py") if path.name not in ("__init__.py", "conftest.py")
    }
    names |= {
        path.name
        for path in root.iterdir()
        if path.is_dir() and path.name != "__pycache__" and (path / "__init__.py").is_file()
    }
    return tuple(sorted(names))


def section_paths(sections: Sequence[str]) -> list[Path]:
    """章节名 → 传给 pytest 的路径（模块或包目录）。"""
    root = plugin.checks_root()
    paths: list[Path] = []
    for name in sections:
        module = root / f"{name}.py"
        package = root / name
        if module.is_file():
            paths.append(module)
        elif package.is_dir():
            paths.append(package)
        else:
            raise ValueError(f"未知章节：{name!r}")
    return paths


def map_exit(status: int) -> tuple[int, str | None]:
    """pytest 退出码 → CLI 退出码与说明：0 全过（含 skip）；1 有失败；其余是运行故障。"""
    if status == 0:
        return 0, None
    if status == 1:
        return 1, None
    if status == 5:
        return 2, "没有任何检查被执行（章节过滤后为空，或套件未收集到用例）"
    return 2, f"套件运行未完成（pytest 退出码 {status}）"


def run_suite(
    *,
    target: str,
    config_path: Path | None,
    sections: Sequence[str],
    junit_xml: Path | None,
    counts_path: Path,
) -> tuple[int, Mapping[str, Any] | None, str | None]:
    """运行选中章节；返回 ``(CLI 退出码, 计数, 说明)``。pytest 输出直通调用方 stdout/stderr。"""
    args = [
        sys.executable,
        "-m",
        "pytest",
        *(str(path) for path in section_paths(sections)),
        "-c",
        str(_PYTEST_INI),
        "-p",
        "dpe_conformance.plugin",
        "--import-mode=importlib",
        "-p",
        "no:cacheprovider",
    ]
    if junit_xml is not None:
        args.append(f"--junitxml={junit_xml}")
    env = {key: value for key, value in os.environ.items() if key not in _PYTEST_ENV_KEYS}
    # 调用方环境里的同名变量不得穿透：配置/目标/计数一律由本次调用决定
    for name in (TARGET_ENV, CONFIG_ENV, COUNTS_ENV):
        env.pop(name, None)
    env[TARGET_ENV] = target
    env[COUNTS_ENV] = str(counts_path)
    if config_path is not None:
        env[CONFIG_ENV] = str(config_path)
    completed = subprocess.run(args, env=env, check=False)
    code, message = map_exit(completed.returncode)
    counts: Mapping[str, Any] | None = None
    if counts_path.is_file():
        counts = json.loads(counts_path.read_text(encoding="utf-8"))
    elif message is None:
        code, message = 2, "套件未产出计数文件（运行器未完成初始化？）"
    return code, counts, message
