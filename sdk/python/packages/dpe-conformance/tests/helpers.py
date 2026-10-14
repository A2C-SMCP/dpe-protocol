"""测试辅助：以独立进程起参考服务端、以独立进程跑 CLI。

CLI 必须在独立进程里跑（它内部再以子进程拉 pytest）；参考服务端同理（#16：ASGI 或独立
监听，本文件用独立监听并读 stdout 的公告行，与 Rust 集成测试同一手法）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReferenceServer:
    url: str
    hooks: str | None


@contextmanager
def reference_server(*extra: str) -> Iterator[ReferenceServer]:
    """起 ``python -m dpe_sdk.testing``（参考服务端），读它公布的 ``{"url", "hooks"}`` 行。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        stdout = proc.stdout
        assert stdout is not None
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline() or ""), daemon=True)
        reader.start()
        reader.join(timeout=20)
        if not lines or not lines[0].strip():
            raise RuntimeError("参考服务端未输出公告行（启动失败？）")
        info = json.loads(lines[0])
        yield ReferenceServer(url=info["url"], hooks=info.get("hooks"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_cli(
    *args: str, cwd: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """以独立进程运行 ``python -m dpe_conformance <args>``；``cwd`` 之外不读任何环境配置。

    ``cwd`` 是显式的：配置文件缺省就在 CWD 下发现，测试必须落在自己的临时目录里。``env`` 给出时
    作为子进程的完整环境（缺省继承当前进程）。
    """
    return subprocess.run(
        [sys.executable, "-m", "dpe_conformance", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=None if env is None else dict(env),
    )
