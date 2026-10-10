"""测试辅助：现场构造 git 仓库（确定性前提见下），供 conftest 与各测试模块共用。

不使用入库的夹具仓库：嵌套的 ``.git`` 入版本库时 git 只存一个 gitlink（160000），克隆出来是空
目录，做不成（实测）。测试用 ``subprocess`` 调 git 现场构造，覆盖非 ASCII 路径与边界情形。

确定性前提：固定作者/提交者与时间戳（否则每次构造出不同的提交 id）、``git init -b main``
显式钉分支名、``-c commit.gpgsign=false`` 防开发机全局签名、隔离全局配置并清掉继承的
``GIT_DIR`` 等变量——构造出的仓库在不同机器、不同时间得到同一个提交 id。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

#: 固定提交元信息
_FIXED_ENV = {
    "GIT_AUTHOR_NAME": "dpe",
    "GIT_AUTHOR_EMAIL": "dpe@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "dpe",
    "GIT_COMMITTER_EMAIL": "dpe@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="测试需要 PATH 中有 git 可执行文件"
)


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(_FIXED_ENV)
    return env


def run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False, env=git_env()
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 失败：{completed.stderr.decode('utf-8', 'replace')}"
        )
    return completed.stdout.decode("utf-8", "replace")


def init_repo(root: Path, files: Mapping[bytes | str, bytes]) -> str:
    """在 ``root`` 现场构造一个提交，返回提交 id。

    路径可以是 ``str``（UTF-8）或 ``bytes``（非 UTF-8 的边界用例）；在 macOS 上非 UTF-8 的
    文件名无法创建（APFS 拒绝，Errno 92），这类用例由调用方按平台跳过。
    """
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "main", ".")
    for path, content in files.items():
        if isinstance(path, bytes):
            target_path: Path = root
            raw_target = os.path.join(os.fsencode(target_path), path)
            os.makedirs(os.path.dirname(raw_target), exist_ok=True)
            with open(raw_target, "wb") as handle:
                handle.write(content)
        else:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    run_git(root, "add", "-A")
    run_git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return run_git(root, "rev-parse", "HEAD").strip()


def command_for(script: str) -> list[str]:
    """插件的启动命令：与解释器同目录的入口脚本的绝对路径。

    宿主进程的 ``PATH`` 里没有 venv 的 bin 目录（测试直接驱动宿主，不经 shell），因此用
    ``sys.executable`` 的同级目录定位入口脚本；运行器解析含路径分隔符的 argv[0] 时按路径解析
    （契约 §4.4）。
    """
    return [str(Path(sys.executable).parent / script)]


def config_for(repo: Path) -> dict[str, Any]:
    return {"repo": str(repo), "ref": "main"}
