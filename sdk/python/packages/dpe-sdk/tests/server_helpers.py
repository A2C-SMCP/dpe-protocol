"""独立监听的参考服务端子进程（``python -m dpe_sdk.testing``）的测试辅助：多个测试模块共用。

这是 Rust SDK 集成测试（#25）与 conformance 跑分器同款的真实设施用法：子进程绑定空闲端口，
stdout 第一行给出 remote URL（启用钩子时另带钩子通道的 base URL），之后由客户端驱动。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest


@contextmanager
def spawn_server(*extra: str) -> Iterator[dict[str, Any]]:
    """启动参考服务端子进程，产出公布的一行 JSON（``url`` 与（启用钩子时的）``hooks``）。

    另给 ``stderr``：一个读子进程 stderr 至当下的函数（启动告警等）。
    """
    # stderr 落到临时文件：管道无人读取时，服务端日志写满缓冲会让子进程阻塞
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", *extra],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        assert proc.stdout is not None
        stdout = proc.stdout
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline()))
        reader.start()
        reader.join(timeout=20)
        try:
            if not lines or not lines[0]:
                proc.kill()
                proc.wait(timeout=5)
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")

            def stderr() -> str:
                log.seek(0)
                return log.read()

            info: dict[str, Any] = json.loads(lines[0])
            info["stderr"] = stderr
            yield info
        finally:
            proc.terminate()
            proc.wait(timeout=10)
