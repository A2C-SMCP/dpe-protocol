"""测试辅助：现场构造夹具仓库（确定性前提见下），供 conftest 与各测试模块共用。

不使用入库的夹具仓库：嵌套的 ``.git`` 入版本库时 git 只存一个 gitlink（160000），克隆出来是空
目录，做不成（实测）。测试用 ``subprocess`` 调 git 现场构造；**每个提交的作者与时间戳都显式
固定**（否则每次构造出不同的提交 id），``git init -b`` 显式钉分支名、``-c commit.gpgsign=false``
防开发机全局签名、隔离全局配置并清掉继承的 ``GIT_DIR`` 等变量——构造出的提交 id 在不同机器、
不同时间都相同。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

#: 固定的默认作者/提交者（逐提交可换作者，但都来自固定取值）
DEFAULT_AUTHOR = "Alice"
DEFAULT_EMAIL = "alice@example.invalid"

#: 固定提交元信息的环境底座（提交时间由每个提交显式给出）
_FIXED_ENV = {
    "GIT_AUTHOR_NAME": DEFAULT_AUTHOR,
    "GIT_AUTHOR_EMAIL": DEFAULT_EMAIL,
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": DEFAULT_AUTHOR,
    "GIT_COMMITTER_EMAIL": DEFAULT_EMAIL,
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="测试需要 PATH 中有 git 可执行文件"
)


def git_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(_FIXED_ENV)
    if overrides:
        env.update(overrides)
    return env


def run_git(
    cwd: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False, env=git_env(env), input=input
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 失败：{completed.stderr.decode('utf-8', 'replace')}"
        )
    return completed.stdout.decode("utf-8", "replace")


class FixtureRepo:
    """现场构造的夹具仓库：固定分支名、逐提交固定作者与时间戳。"""

    def __init__(self, root: Path, *, default_branch: str = "main") -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        run_git(root, "init", "-q", "-b", default_branch, ".")

    def sha(self, rev: str = "HEAD") -> str:
        return run_git(self.root, "rev-parse", rev).strip()

    def files(self, files: Mapping[str, str | bytes]) -> None:
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        run_git(self.root, "add", "-A")

    def commit(
        self,
        message: str,
        *,
        date: str,
        files: Mapping[str, str | bytes] | None = None,
        author: str = DEFAULT_AUTHOR,
        email: str = DEFAULT_EMAIL,
        allow_empty: bool = True,
        message_file: bool = False,
    ) -> str:
        """在**当前分支**上加一个提交；``date`` 是 ISO 8601（作者与提交者同一时刻）。

        ``message_file`` 经 ``-F`` 从文件读说明——超大说明（超过 argv 上限，如 1 MiB 以上）
        只能走这条路。
        """
        if files:
            self.files(files)
        if message_file:
            message_path = self.root / ".git" / "dpe-test-message.txt"
            message_path.write_text(message, encoding="utf-8")
            args = ["commit", "-q", "-F", str(message_path)]
        else:
            args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.insert(2, "--allow-empty")
        run_git(
            self.root,
            "-c",
            "commit.gpgsign=false",
            *args,
            env={
                "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": author,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_COMMITTER_DATE": date,
            },
        )
        return self.sha()

    def branch(self, name: str, *, at: str | None = None) -> None:
        """从 ``at``（缺省当前 HEAD）创建并切到新分支。"""
        run_git(self.root, "checkout", "-q", "-b", name, *([at] if at else []))

    def checkout(self, name: str) -> None:
        run_git(self.root, "checkout", "-q", name)

    def merge(self, branch: str, message: str, *, date: str, author: str = DEFAULT_AUTHOR) -> str:
        """``--no-ff`` 合并（必产生合并提交），时间戳显式固定。"""
        run_git(
            self.root,
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            message,
            branch,
            env={
                "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": DEFAULT_EMAIL,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": author,
                "GIT_COMMITTER_EMAIL": DEFAULT_EMAIL,
                "GIT_COMMITTER_DATE": date,
            },
        )
        return self.sha()

    def add_remote_branch(self, name: str, *, at: str | None = None) -> None:
        """建一个 ``refs/remotes/origin/<name>``（不建 origin/HEAD）：``at`` 缺省取同名本地分支。"""
        target = at or run_git(self.root, "rev-parse", f"refs/heads/{name}").strip()
        run_git(self.root, "update-ref", f"refs/remotes/origin/{name}", target)

    def set_origin_head(self, name: str) -> None:
        """建 ``refs/remotes/origin/<name>`` 并把 ``origin/HEAD`` 符号指向它（默认分支的来源）。"""
        self.add_remote_branch(name)
        run_git(
            self.root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{name}"
        )


def command_for(script: str) -> list[str]:
    """插件的启动命令：与解释器同目录的入口脚本的绝对路径。

    宿主进程的 ``PATH`` 里没有 venv 的 bin 目录（测试直接驱动宿主，不经 shell），因此用
    ``sys.executable`` 的同级目录定位入口脚本；运行器解析含路径分隔符的 argv[0] 时按路径解析
    （契约 §4.4）。
    """
    return [str(Path(sys.executable).parent / script)]
