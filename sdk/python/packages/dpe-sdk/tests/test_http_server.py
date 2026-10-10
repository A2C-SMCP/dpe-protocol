"""独立监听（``python -m dpe_sdk.testing``）：外部进程经真实 TCP 端口访问参考服务端（#44）。

这是 Rust SDK 集成测试（#25）与 conformance 跑分器的用法：子进程绑定空闲端口，stdout 第一行
给出 remote URL，之后由同步 httpx 客户端经 ``ProtocolCore`` 跑通快路径。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator

import httpx
import pytest
from dpe_sdk.models import Document
from dpe_sdk.protocol import IfAbsent, ProtocolCore, Request, Response, drive, fetch_capabilities
from engine_helpers import text_doc


@pytest.fixture
def server() -> Iterator[str]:
    # stderr 落到临时文件：管道无人读取时，服务端日志写满缓冲会让子进程阻塞
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", "--prefix", "/r/1",
             "--max-payload-bytes", "65536"],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )  # fmt: skip
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
            yield json.loads(lines[0])["url"]
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_standalone_server_is_reachable_from_another_process(server: str) -> None:
    assert server.startswith("http://127.0.0.1:") and server.endswith("/r/1")
    with httpx.Client() as client:

        def send(request: Request) -> Response:
            reply = client.request(
                request.method,
                f"{server}/{request.target}",
                headers=dict(request.headers),
                content=request.body_bytes(),
            )
            return Response(reply.status_code, dict(reply.headers), reply.content)

        capabilities = drive(fetch_capabilities(), send)
        assert capabilities.limits.max_payload_bytes == 65536
        core = ProtocolCore(capabilities)
        document = Document.model_validate(text_doc(["hello"], ["world"]))
        result = drive(core.commit("test://standalone", document, IfAbsent()), send)
        assert result.status == "created"
        assert drive(core.head("test://standalone"), send) == document.doc_hash()
        skeleton = drive(core.get_skeleton("test://standalone"), send)
        assert skeleton is not None and len(skeleton.pages) == 2


def test_cli_rejects_invalid_limits() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dpe_sdk.testing", "--staging-ttl-seconds", "10"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "staging_ttl_seconds" in proc.stderr
    assert proc.stdout == ""
