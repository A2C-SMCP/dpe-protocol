"""本地 TLS 终结器（#68）：Python ``ssl`` 加线程转发，放在明文参考服务端前面。

用途：在不给参考服务端加 TLS 参数、也不引入 trustme 等依赖的前提下，验证「运行器 / 代理 →
HTTPS → 参考服务端」的链路。测试 CA 与服务器证书由 openssl 命令行生成
（``generate_test_certificates``）；终结器记录成功握手次数，用例据此断言确实走了 TLS。

客户端到终结器是 TLS，终结器到 upstream 是明文 HTTP。转发语义与 ``e2e_harness.RecordingProxy``
一致：逐请求新建上游连接、按 hop-by-hop 头过滤、非 HEAD 重算 Content-Length。
"""

from __future__ import annotations

import argparse
import json
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from _relay import handler_for, redact

#: 终结器到 upstream 的 HTTP 超时（秒）
TIMEOUT = 120.0


_CERT_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = 127.0.0.1
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
DNS.1 = localhost
IP.1 = 127.0.0.1
"""


@dataclass(frozen=True)
class TestCertificates:
    """测试 CA 与服务器证书的路径。"""

    ca_file: Path
    cert_file: Path
    key_file: Path


def _openssl(*args: str, cwd: Path) -> None:
    completed = subprocess.run(
        ["openssl", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"openssl {' '.join(args)} 失败：{completed.stderr}")


def generate_test_certificates(directory: Path) -> TestCertificates:
    """用 openssl 命令行生成一次性的测试 CA 与带 ``IP:127.0.0.1`` SAN 的服务器证书。"""
    if shutil.which("openssl") is None:
        raise RuntimeError("需要 openssl 命令行生成测试证书")
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "openssl.cnf"
    config.write_text(_CERT_CONFIG, encoding="utf-8")
    ca_key, ca_pem = directory / "ca.key", directory / "ca.pem"
    server_key, csr = directory / "server.key", directory / "server.csr"
    server_pem = directory / "server.pem"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-sha256",
        "-days",
        "2",
        "-config",
        str(config),
        "-extensions",
        "v3_ca",
        "-subj",
        "/CN=DPE-E2E-CA",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_pem),
        cwd=directory,
    )
    _openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-sha256",
        "-config",
        str(config),
        "-subj",
        "/CN=127.0.0.1",
        "-keyout",
        str(server_key),
        "-out",
        str(csr),
        cwd=directory,
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_pem),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "2",
        "-sha256",
        "-extfile",
        str(config),
        "-extensions",
        "v3_server",
        "-out",
        str(server_pem),
        cwd=directory,
    )
    return TestCertificates(ca_file=ca_pem, cert_file=server_pem, key_file=server_key)


class _TLSServer(ThreadingHTTPServer):
    """接受连接时完成 TLS 握手并计数；握手失败按 OSError 交给 socketserver 的 OSError 处理。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        context: ssl.SSLContext,
        owner: TLSProxy,
    ) -> None:
        super().__init__(address, handler)
        self._context = context
        self._owner = owner

    def get_request(self) -> Any:
        sock, addr = super().get_request()
        try:
            tls = self._context.wrap_socket(sock, server_side=True)
        except OSError:
            sock.close()
            raise
        with self._owner._lock:
            self._owner._handshakes += 1
        return tls, addr


class TLSProxy:
    """TLS 终结 + 明文转发到 ``upstream``；``handshake_count`` 是成功握手次数。"""

    def __init__(
        self,
        upstream: str,
        *,
        cert_file: Path,
        key_file: Path,
        timeout: float = TIMEOUT,
        secrets: Sequence[str] = (),
    ) -> None:
        parts = urlsplit(upstream)
        if parts.scheme != "http" or parts.hostname is None:
            raise ValueError(f"TLS 终结器的 upstream 必须是明文 http URL：{upstream!r}")
        self._host = parts.hostname
        self._port = parts.port or 80
        self._base = parts.path
        self._timeout = timeout
        self._secrets = tuple(secrets)
        self._lock = threading.Lock()
        self._handshakes = 0
        #: 终结器自身的故障（如上游连不上、chunked 请求体）：由调用方断言为空
        self.errors: list[str] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_file), str(key_file))
        self._server = _TLSServer(("127.0.0.1", 0), handler_for(self), context, self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def connect(self) -> HTTPConnection:
        return HTTPConnection(self._host, self._port, timeout=self._timeout)

    def on_relayed(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        status: int,
    ) -> None:
        """终结器只转发，不记录请求。"""

    def on_error(self, method: str, path: str, exc: BaseException) -> None:
        self.errors.append(redact(f"{method} {path}: {exc!r}", self._secrets))

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self._server.server_address[1]}{self._base}"

    @property
    def handshake_count(self) -> int:
        with self._lock:
            return self._handshakes

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    """独立运行终结器：打印一行 ``{"url": ...}`` 后前台服务（供手工联调使用）。"""
    parser = argparse.ArgumentParser(prog="tls_terminator", description="本地 TLS 终结器")
    parser.add_argument("--upstream", required=True, help="明文的参考服务端 URL")
    parser.add_argument("--cert", required=True, type=Path, help="服务器证书 PEM")
    parser.add_argument("--key", required=True, type=Path, help="服务器私钥 PEM")
    args = parser.parse_args(argv)
    proxy = TLSProxy(args.upstream, cert_file=args.cert, key_file=args.key)
    print(json.dumps({"url": proxy.url}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
