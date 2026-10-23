"""端到端测试设施（#66、#215、#68）：真实 ``dpe-run``、真实插件、参考服务端或外部 remote，
加一层记录请求的代理。

- ``reference_server``：本地参考服务端子进程（``python -m dpe_sdk.testing``，真实 TCP）；
- ``TLSProxy``（见 ``tls_terminator``）：本地 TLS 终结器，放在参考服务端前面；
- ``RecordingProxy``：进程内的透明转发代理，记录每个请求（请求体已解压），并能在写请求被
  服务端执行之后**扣住响应**——测试据此确定性地制造「写入已生效、确认前崩溃」：等代理报告已
  扣住，再终止运行器的进程组，不依赖 sleep 时序；
- ``Runner``：写实例定义、以子进程跑 ``dpe-run``（报告取自 stdout，NDJSON 事件取自 stderr）；
- ``Runner.assert_converged``：同一源状态在一个全新的空前缀上全量推送一次（F(S)），与增量实例的
  remote 状态做**页级**比较（每篇的 doc_hash 与按序的 page_hash 列表）。

外部 remote（#68）：设置 ``DPE_E2E_REMOTE_URL`` 即不再起参考服务端，直接连该 URL；凭证值只走
环境变量——``DPE_E2E_CREDENTIAL_ENV`` 给出保存 ``Authorization`` 头原值的变量名，经实例定义的
``remote.credential.env`` 由运行器解析。``DPE_E2E_CA_FILE`` 为显式测试 CA（本地 TLS 模式或私有
CA 的 remote），``DPE_E2E_URI_PREFIX`` 是可配置的根前缀（每次运行追加唯一后缀，
``DPE_E2E_TIMEOUT`` 覆盖子进程与等待的超时秒数）。本地模式（缺省）不受这些变量影响。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import dpe_hash
import httpx
import pytest
from _relay import handler_for, redact, redact_headers, relative_path
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.errors import NotFoundError, PreconditionFailedError
from dpe_sdk.transport import DpeClient
from dpe_sdk.wire import Limits
from helpers import FixtureRepo, command_for, run_git
from tls_terminator import TLSProxy, generate_test_certificates

MANIFEST = Path(__file__).resolve().parents[1] / "dpe-connector.json"
NULLCURSOR = Path(__file__).resolve().parent / "nullcursor.py"

#: 外部模式（#68）：设置后不再起参考服务端
ENV_REMOTE_URL = "DPE_E2E_REMOTE_URL"
#: 保存 ``Authorization`` 头原值的环境变量名（外部模式；本地认证模式使用同名机制）
ENV_CREDENTIAL_ENV = "DPE_E2E_CREDENTIAL_ENV"
#: 显式测试 CA 文件；只有设置时才向子进程传 ``SSL_CERT_FILE``
ENV_CA_FILE = "DPE_E2E_CA_FILE"
#: 可配置的 uri 根前缀；每次运行在其下追加唯一后缀
ENV_URI_PREFIX = "DPE_E2E_URI_PREFIX"
#: 子进程与代理等待的超时秒数
ENV_TIMEOUT = "DPE_E2E_TIMEOUT"

#: 本地认证模式注入凭证值用的环境变量名
LOCAL_CREDENTIAL_ENV = "DPE_E2E_LOCAL_AUTHORIZATION"
#: 缺省根前缀；每次运行追加唯一后缀
DEFAULT_ROOT = "git://"

#: 超时上限（可经 ``DPE_E2E_TIMEOUT`` 配置）：防止 CI 卡死
TIMEOUT = float(os.environ.get(ENV_TIMEOUT, "120"))


def _secret_values(credential: str | None) -> tuple[str, ...]:
    """需要脱敏的凭证片段：完整头值，以及方案名之后的凭证本体（如 ``Bearer`` 后的 token）。"""
    if not credential:
        return ()
    return tuple(dict.fromkeys([credential, *credential.split(None, 1)[1:]]))


def _normalized_root(root: str) -> str:
    """校验根前缀：以 ``/`` 结尾，且已是 ``normalize_file_uri`` 的不动点。"""
    if not root.endswith("/"):
        raise ValueError(f"uri 根前缀必须以 '/' 结尾：{root!r}")
    try:
        normalized = dpe_hash.normalize_file_uri(root)
    except dpe_hash.ValidationError as exc:
        raise ValueError(f"uri 根前缀不是合法的 URI：{exc}") from None
    if normalized != root:
        raise ValueError(f"uri 根前缀不是规范化不动点（规范化形式为 {normalized!r}）")
    return root


# ---------------------------------------------------------------------- 参考服务端


@dataclass(frozen=True)
class ServerOptions:
    """本地参考服务端的启动选项。

    ``authorization`` 非空时以 ``--require-auth --grant <authorization> <root>`` 启动：所有请求
    都要带该凭证，写权限限于根前缀（每次运行的唯一后缀在其下）。
    """

    root: str = DEFAULT_ROOT
    http_prefix: str = "/r/1"
    authorization: str | None = field(default=None, repr=False)
    max_payload_bytes: int | None = None
    tls: bool = False
    credential_env: str = LOCAL_CREDENTIAL_ENV


@dataclass
class ReferenceServer:
    """本地参考服务端的地址与凭证/CA 信息。"""

    url: str
    root: str
    credential_env: str | None = None
    credential: str | None = field(default=None, repr=False)
    ca_file: Path | None = None
    tls: TLSProxy | None = field(default=None, repr=False)


@contextmanager
def reference_server(options: ServerOptions | None = None) -> Iterator[ReferenceServer]:
    """启动参考服务端子进程（可选认证、可选 TLS），产出它的地址与凭证/CA 信息。

    ``tls=True`` 时在它前面加一层本地 TLS 终结器，``url`` 是 ``https://`` 地址。
    """
    options = options or ServerOptions()
    root = _normalized_root(options.root)
    argv = [
        sys.executable,
        "-m",
        "dpe_sdk.testing",
        "--port",
        "0",
        "--prefix",
        options.http_prefix,
    ]
    if options.authorization is not None:
        argv += ["--require-auth", "--grant", options.authorization, root]
    if options.max_payload_bytes is not None:
        argv += ["--max-payload-bytes", str(options.max_payload_bytes)]
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=log, text=True)
        assert proc.stdout is not None
        stdout = proc.stdout
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline()))
        reader.start()
        reader.join(timeout=20)
        try:
            if not lines or not lines[0]:
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")
            remote = str(json.loads(lines[0])["url"])
            tls: TLSProxy | None = None
            ca_file: Path | None = None
            if options.tls:
                with tempfile.TemporaryDirectory(prefix="dpe-e2e-certs-") as cert_dir:
                    certificates = generate_test_certificates(Path(cert_dir))
                    tls = TLSProxy(
                        remote,
                        cert_file=certificates.cert_file,
                        key_file=certificates.key_file,
                        secrets=_secret_values(options.authorization),
                    )
                    ca_file = certificates.ca_file
                    try:
                        yield ReferenceServer(
                            url=tls.url,
                            root=root,
                            credential_env=options.credential_env
                            if options.authorization is not None
                            else None,
                            credential=options.authorization,
                            ca_file=ca_file,
                            tls=tls,
                        )
                    finally:
                        tls.close()
            else:
                yield ReferenceServer(
                    url=remote,
                    root=root,
                    credential_env=options.credential_env
                    if options.authorization is not None
                    else None,
                    credential=options.authorization,
                )
        finally:
            proc.terminate()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------- 记录代理


@dataclass(frozen=True)
class Seen:
    """代理看到的一个请求：方法、remote 之后的路径（含查询串，已解码）、解压后的请求体、状态码。"""

    method: str
    path: str
    body: bytes
    status: int
    #: 请求头（名字小写，``authorization`` 已脱敏）：断言 CAS 前置条件（If-Match / If-None-Match）
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def target(self) -> str:
        """路径去掉查询串：``documents``、``negotiate``、``heads``、``staging/…`` 等。"""
        return self.path.split("?", 1)[0]

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    @property
    def is_write(self) -> bool:
        return (self.method in ("PUT", "DELETE") and self.target == "documents") or (
            self.method == "POST" and self.target == "move"
        )


class RecordingProxy:
    """透明转发到 ``upstream``，记录全部请求；``hold_next_write`` 后扣住下一个写请求的响应。

    客户端到代理是明文 HTTP；上游按 URL 的 scheme 选 HTTP / HTTPS，并按 scheme 补默认端口
    （#68）。``authorization`` 头在记录时脱敏，转发时原样保留。转发本身与 ``TLSProxy``
    共用 ``_relay.RelayingHandler``。
    """

    def __init__(
        self,
        upstream: str,
        *,
        ca_file: Path | None = None,
        timeout: float = TIMEOUT,
        secrets: Sequence[str] = (),
    ) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or parts.hostname is None:
            raise ValueError(f"不支持的 upstream URL：{upstream!r}")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._base = parts.path
        self._timeout = timeout
        self._secrets = tuple(secrets)
        self._ssl_context: ssl.SSLContext | None = None
        if parts.scheme == "https":
            self._ssl_context = ssl.create_default_context(
                cafile=str(ca_file) if ca_file is not None else None
            )
        self.seen: list[Seen] = []
        #: 代理自身出错的原因（如 chunked 请求体、连不上上游）：轮结束时断言为空，失败可读
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._hold = False
        #: 被扣住的那个写请求（不依赖记录顺序：并发请求按完成顺序追加）
        self.held_request: Seen | None = None
        self.held = threading.Event()
        self._release = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def connect(self) -> HTTPConnection:
        if self._scheme == "https":
            assert self._ssl_context is not None
            return HTTPSConnection(
                self._host, self._port, timeout=self._timeout, context=self._ssl_context
            )
        return HTTPConnection(self._host, self._port, timeout=self._timeout)

    def on_relayed(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        status: int,
    ) -> None:
        seen = Seen(method, relative_path(path, self._base), body, status, redact_headers(headers))
        with self._lock:
            self.seen.append(seen)
            hold = self._hold and seen.is_write
            if hold:
                self._hold = False
                self.held_request = seen
        if hold:
            # 写入已在服务端生效：通知测试，等它终止运行器后再（向已断开的连接）回响应
            self.held.set()
            self._release.wait(self._timeout)

    def on_error(self, method: str, path: str, exc: BaseException) -> None:
        self.errors.append(redact(f"{method} {path}: {exc!r}", self._secrets))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{self._base}"

    def mark(self) -> int:
        with self._lock:
            return len(self.seen)

    def since(self, mark: int) -> list[Seen]:
        with self._lock:
            return list(self.seen[mark:])

    def hold_next_write(self) -> None:
        self.held_request = None
        self.held.clear()
        self._release.clear()
        with self._lock:
            self._hold = True

    def release(self) -> None:
        with self._lock:
            self._hold = False
        self._release.set()

    def close(self) -> None:
        self.release()
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------- 运行器


@dataclass
class Round:
    """一轮的结果：轮报告、NDJSON 事件与这一轮经过代理的请求。"""

    report: dict[str, Any]
    events: list[dict[str, Any]]
    requests: list[Seen]

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = self.report["counts"]
        return counts

    def changed(self) -> dict[str, int]:
        """counts 中非零的项。"""
        return {key: value for key, value in self.counts.items() if value}

    def named(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    def codes(self) -> list[str]:
        return [str(failure["code"]) for failure in self.report["failures"]]


#: 一篇文档的页级快照：(doc_hash, 按序的 page_hash 列表)
Pages = tuple[str, tuple[str, ...]]


def _seen_json(request: Seen) -> dict[str, Any]:
    try:
        body = request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def sent_page_hashes(requests: Sequence[Seen]) -> set[str]:
    """本轮送出的页 hash：内联的完整页对象，加上 staging 页上传（含分块）的路径 hash。

    分块上传的请求体只是字节片段，不能当页对象解析；``Content-Range`` 存在即跳过请求体。
    只统计 PUT：断点查询的 GET 路径不算「送出」。
    """
    hashes: set[str] = set()
    for request in requests:
        if "content-range" not in request.headers:
            body = _seen_json(request)
            inline = body.get("pages")
            if isinstance(inline, list):
                hashes.update(
                    dpe_hash.object_hash(page, "page") for page in inline if isinstance(page, dict)
                )
        target = request.target
        if request.method == "PUT" and target.startswith("staging/") and "/pages/" in target:
            hashes.add(target.rsplit("/", 1)[-1])
    return hashes


def sent_element_hashes(requests: Sequence[Seen]) -> set[str]:
    """本轮送出的元素 hash：内联 ``objects`` 与 staging 元素上传的路径 hash。只统计 PUT。"""
    hashes: set[str] = set()
    for request in requests:
        if "content-range" not in request.headers:
            body = _seen_json(request)
            inline = body.get("objects")
            if isinstance(inline, list):
                for obj in inline:
                    if isinstance(obj, dict):
                        hashes.add(dpe_hash.object_hash(obj, "element"))
        target = request.target
        if request.method == "PUT" and target.startswith("staging/") and "/objects/" in target:
            hashes.add(target.rsplit("/", 1)[-1])
    return hashes


@dataclass
class Runner:
    """在同一个 remote 上跑 dpe-run 的增量实例（经记录代理）与比对用的一次性全量实例（直连）。

    ``local_remote`` 表示 remote 是本进程起的参考服务端；外部模式下精确请求序列断言只在本地
    模式成立（#68 决策 5）。``root_prefix`` 是可配置的根前缀，实例构造时在其下追加唯一后缀，
    本次运行的全部前缀（``inc`` 与每次 ``full{n}``）都落在其中，``close`` 时带 CAS 清理。
    """

    tmp: Path
    upstream: str
    config: dict[str, Any]
    local_remote: bool = True
    root_prefix: str = DEFAULT_ROOT
    credential_env: str | None = None
    credential: str | None = field(default=None, repr=False)
    ca_file: Path | None = None
    server: ReferenceServer | None = field(default=None, repr=False)
    policy: dict[str, str] | None = None
    #: 存在即让 nullcursor 包装把结束批的 cursor 改成 null（#213 场景）
    nullcursor_flag: Path | None = None
    fresh: int = 0
    proxy: RecordingProxy = field(init=False)

    def __post_init__(self) -> None:
        self.root_prefix = _normalized_root(self.root_prefix)
        self.run_prefix = f"{self.root_prefix}{secrets.token_hex(4)}/"
        if dpe_hash.normalize_file_uri(self.run_prefix) != self.run_prefix:
            raise ValueError(f"uri 前缀不是规范化不动点：{self.run_prefix!r}")
        if self.credential_env is not None and not self.credential:
            raise ValueError(f"凭证环境变量 {self.credential_env} 没有值")
        self.proxy = RecordingProxy(
            self.upstream,
            ca_file=self.ca_file,
            timeout=TIMEOUT,
            secrets=_secret_values(self.credential),
        )
        # 子进程环境是私有的：其中含凭证原值，不能出现在断言自省的输出里
        self._environ = {
            "PATH": os.environ.get("PATH", ""),
            # 回环地址不经代理：macOS 上会读系统代理设置，参考服务端在本机
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        if self.credential_env is not None and self.credential is not None:
            self._environ[self.credential_env] = self.credential
        if self.ca_file is not None:
            # 只在本地 TLS 模式或显式提供 CA 时设置；否则会整个替换 certifi 的 CA 包
            self._environ["SSL_CERT_FILE"] = str(self.ca_file)

    def has_env(self, name: str) -> bool:
        """子进程环境里是否设置了该变量（只回答有无，不暴露凭证值）。"""
        return name in self._environ

    # ---------------------------------------------------------------- 前缀与客户端

    @property
    def inc_prefix(self) -> str:
        return f"{self.run_prefix}inc/"

    def full_prefix(self, n: int) -> str:
        return f"{self.run_prefix}full{n}/"

    def uri(self, name: str) -> str:
        """增量实例下的一篇文档 URI（外部模式与本地模式一致）。"""
        return f"{self.inc_prefix}{name}"

    def client(self, *, timeout: float | None = None) -> DpeClient:
        """指向 ``upstream`` 的 ``DpeClient``；带凭证，并按需信任测试 CA（#68 决策 1、7）。"""
        provider = StaticCredentialProvider(self.credential) if self.credential else None
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(self.ca_file))
            if self.ca_file is not None
            else True
        )
        # proxy=None：与 dpe-run 子进程的口径一致（子进程环境里只有 NO_PROXY），
        # 避免 pytest 进程继承的 HTTP(S)_PROXY 把回环/测试流量改道
        transport = httpx.HTTPTransport(proxy=None, verify=verify)
        return DpeClient(
            self.upstream,
            credential=provider,
            transport=transport,
            timeout=timeout or TIMEOUT,
        )

    def limits(self) -> Limits:
        with self.client() as client:
            return client.core().limits

    # ---------------------------------------------------------------- 实例定义

    def _definition(self, instance_id: str, prefix: str, remote: str, *, wrapped: bool) -> Path:
        command = command_for("dpe-git-connector")
        if wrapped and self.nullcursor_flag is not None:
            command = [sys.executable, str(NULLCURSOR), str(self.nullcursor_flag), *command]
        remote_spec: dict[str, Any] = {"url": remote}
        if self.credential_env is not None:
            remote_spec["credential"] = {"env": self.credential_env}
        definition: dict[str, Any] = {
            "definition_version": 1,
            "id": instance_id,
            "remote": remote_spec,
            "uri_prefix": prefix,
            "plugin": {"manifest": str(MANIFEST), "command": command},
            "config": self.config,
        }
        if wrapped and self.policy is not None:
            definition["conflict_policy"] = self.policy
        path = self.tmp / f"{instance_id}.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        return path

    def _argv(self, definition: Path, state: Path) -> list[str]:
        return [sys.executable, "-m", "dpe_sdk.run", "--state-dir", str(state), str(definition)]

    # ---------------------------------------------------------------- 增量实例

    @property
    def state(self) -> Path:
        return self.tmp / "state-inc"

    def round(self, expect: int = 0) -> Round:
        """跑增量实例的一轮（经代理），断言退出码。"""
        mark = self.proxy.mark()
        definition = self._definition("inc", self.inc_prefix, self.proxy.url, wrapped=True)
        proc = subprocess.run(
            self._argv(definition, self.state),
            capture_output=True,
            text=True,
            env=self._environ,
            timeout=TIMEOUT,
        )
        assert not self.proxy.errors, redact(
            repr(self.proxy.errors), _secret_values(self.credential)
        )
        assert proc.returncode == expect, redact(
            proc.stdout + proc.stderr, _secret_values(self.credential)
        )
        return Round(json.loads(proc.stdout), _events(proc.stderr), self.proxy.since(mark))

    def crash_on_next_write(self) -> Seen:
        """跑一轮，第一个写请求在服务端生效后、响应返回前 kill -9 运行器（它所在的进程组）。

        确定性：代理扣住响应并置位 ``held``，测试等到它才终止进程，全程没有 sleep；运行器在发出
        写请求之前就退出时，旁路线程同样唤醒等待并立即判失败（附运行器的 stderr）。返回被扣住的
        写请求。

        插件由运行器以独立会话启动（``dpe_sdk.run.host``），不在被终止的进程组里：它随后读到
        stdin EOF 自行退出。插件只读源、不写 remote，因此不影响「写入已生效、未被确认」这一状态。
        """
        definition = self._definition("inc", self.inc_prefix, self.proxy.url, wrapped=True)
        self.proxy.hold_next_write()
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(
                self._argv(definition, self.state),
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=self._environ,
                start_new_session=True,
            )
            exited = threading.Event()

            def watch() -> None:
                proc.wait()
                exited.set()
                self.proxy.held.set()  # 唤醒等待：运行器先于写请求退出

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                if not self.proxy.held.wait(TIMEOUT) or self.proxy.held_request is None:
                    stderr.seek(0)
                    detail = redact(stderr.read(), _secret_values(self.credential))
                    pytest.fail(f"运行器没有发出写请求（exited={exited.is_set()}）：{detail}")
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(TIMEOUT)
            finally:
                self.proxy.release()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(TIMEOUT)
                # 进程已回收，旁路线程随即结束：等它结束，晚到的唤醒不会落进下一次调用
                watcher.join(TIMEOUT)
        assert not self.proxy.errors, redact(
            repr(self.proxy.errors), _secret_values(self.credential)
        )
        held = self.proxy.held_request
        assert held is not None and held.is_write
        return held

    # ---------------------------------------------------------------- 比较

    def snapshot(self, prefix: str | None = None) -> dict[str, Pages]:
        """remote 上前缀内每篇文档的页级快照（按前缀之后的标识）。"""
        prefix = self.inc_prefix if prefix is None else prefix
        out: dict[str, Pages] = {}
        with self.client() as client:
            cursor: str | None = None
            uris: list[str] = []
            while True:
                page = client.list_page(prefix, cursor)
                uris.extend(entry.file_uri for entry in page.documents)
                cursor = page.next_cursor
                if cursor is None:
                    break
            for uri in uris:
                skeleton = client.get_skeleton(uri)
                assert skeleton is not None, uri
                out[uri[len(prefix) :]] = (skeleton.doc_hash, tuple(skeleton.document.pages))
        return out

    def full_snapshot(self) -> dict[str, Pages]:
        """同一源状态在本次运行的新空前缀、全新的状态目录上全量推送一次（F(S)，直连，不经代理）。"""
        self.fresh += 1
        prefix = self.full_prefix(self.fresh)
        definition = self._definition(f"full{self.fresh}", prefix, self.upstream, wrapped=False)
        proc = subprocess.run(
            self._argv(definition, self.tmp / f"state-full{self.fresh}"),
            capture_output=True,
            text=True,
            env=self._environ,
            timeout=TIMEOUT,
        )
        assert proc.returncode == 0, redact(
            proc.stdout + proc.stderr, _secret_values(self.credential)
        )
        assert json.loads(proc.stdout)["incremental"] is False
        return self.snapshot(prefix)

    def assert_converged(self) -> dict[str, Pages]:
        """R = F(S)。doc_hash 相同即 page_hash 列表相同（文档对象含页列表），页级快照的作用是
        失败时直接看出哪一页不同，并供用例对单页断言。"""
        actual = self.snapshot()
        assert actual == self.full_snapshot()
        return actual

    # ---------------------------------------------------------------- 清理

    def cleanup(self) -> None:
        """带 CAS 删除本次运行前缀下的全部文档（#68 决策 3）。"""
        uris: list[str] = []
        with self.client() as client:
            cursor: str | None = None
            while True:
                page = client.list_page(self.run_prefix, cursor)
                uris.extend(entry.file_uri for entry in page.documents)
                cursor = page.next_cursor
                if cursor is None:
                    break
            failures: list[str] = []
            for uri in uris:
                try:
                    self._delete_if_present(client, uri)
                except Exception as exc:
                    failures.append(redact(f"{uri}: {exc!r}", _secret_values(self.credential)))
            if failures:
                raise RuntimeError("清理本次前缀下的文档失败：" + "; ".join(failures))

    @staticmethod
    def _delete_if_present(client: DpeClient, uri: str) -> None:
        for attempt in range(3):
            base = client.head(uri)
            if base is None:
                return
            try:
                client.delete(uri, base)
                return
            except NotFoundError:
                return
            except PreconditionFailedError:
                if attempt == 2:
                    raise

    def close(self) -> None:
        try:
            self.cleanup()
        finally:
            self.proxy.close()


def _events(stderr: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "event" in parsed:
            events.append(parsed)
    return events


@contextmanager
def _active(instance: Runner) -> Iterator[Runner]:
    """让 ``Runner`` 的清理在 finally 中执行；清理失败不掩盖测试体的原始异常。"""
    try:
        yield instance
    finally:
        error = sys.exc_info()[1]
        try:
            instance.close()
        except Exception as exc:
            if error is None:
                raise
            error.add_note(f"e2e 清理失败：{exc!r}")


@contextmanager
def runner(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    local_options: ServerOptions | None = None,
    force_local: bool = False,
) -> Iterator[Runner]:
    """参考服务端（或外部 remote）+ 记录代理 + 运行器。

    - 缺省：起本地参考服务端；``local_options`` 定制它（前缀、认证、TLS、限额）。
    - 设置 ``DPE_E2E_REMOTE_URL`` 且未 ``force_local``：连该外部 remote，凭证与 CA 从环境读取；
      ``local_options`` 被忽略。
    - ``force_local=True``：即使设置了外部模式的变量也起本地参考服务端（TLS / 认证用例）。
    """
    external = "" if force_local else os.environ.get(ENV_REMOTE_URL, "").strip()
    if external:
        credential_env = os.environ.get(ENV_CREDENTIAL_ENV) or None
        credential = None
        if credential_env is not None:
            credential = os.environ.get(credential_env, "")
            if not credential:
                pytest.fail(f"{ENV_CREDENTIAL_ENV} 指向的环境变量 {credential_env} 未设置或为空")
        ca_env = os.environ.get(ENV_CA_FILE) or None
        root = os.environ.get(ENV_URI_PREFIX) or DEFAULT_ROOT
        instance = Runner(
            tmp_path,
            external,
            config,
            local_remote=False,
            root_prefix=root,
            credential_env=credential_env,
            credential=credential,
            ca_file=Path(ca_env) if ca_env is not None else None,
        )
        with _active(instance):
            yield instance
        return
    with reference_server(local_options) as server:
        instance = Runner(
            tmp_path,
            server.url,
            config,
            local_remote=True,
            root_prefix=server.root,
            credential_env=server.credential_env,
            credential=server.credential,
            ca_file=server.ca_file,
            server=server,
        )
        with _active(instance):
            yield instance


# ---------------------------------------------------------------------- 源与他人写入


def make_repo(path: Path, message: str, *, date: str = "2026-01-06T10:00:00+00:00") -> FixtureRepo:
    """一个提交的小仓库，默认分支取自 ``origin/HEAD``（连接器读远端引用）。"""
    repo = FixtureRepo(path)
    repo.commit(message, date=date, files={"f.txt": message + "\n"})
    repo.set_origin_head("main")
    return repo


def advance(path: Path, message: str, date: str) -> str:
    """在当前分支上加一个提交并同步 ``origin/main``（模拟 fetch：连接器读远端引用）。"""
    repo = FixtureRepo.__new__(FixtureRepo)
    repo.root = path
    sha = repo.commit(message, date=date)
    run_git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return sha


def big_repo(
    path: Path,
    per_month: int,
    months: tuple[str, ...] = ("2026-01", "2026-02"),
    *,
    note_bytes: int | None = None,
) -> FixtureRepo:
    """经 ``git fast-import`` 一次生成每月 ``per_month`` 个提交的仓库——整篇内联超过
    ``SMALL_UPDATE_BYTES``，更新走差量路径（#212）。比逐个 ``git commit`` 快一个数量级。

    ``note_bytes`` 给出时用等长的 ASCII 说明（#68 的暂存路径用例按远端限额控制文档大小）。
    """
    repo = FixtureRepo(path)
    note = (
        "这是一段足够长的提交说明，用来把文档撑过快路径的阈值。" * 4
        if note_bytes is None
        else "x" * note_bytes
    )
    stream: list[str] = []
    for month in months:
        for i in range(per_month):
            stamp = f"{month}-{1 + i * 25 // per_month:02d}T10:{i % 60:02d}:00+00:00"
            data = f"{month} 提交 {i}：{note}\n".encode()
            stream += [
                "commit refs/heads/main",
                f"committer Alice <alice@example.invalid> {_epoch(stamp)} +0000",
                f"data {len(data)}",
                data.decode("utf-8"),
            ]
    run_git(path, "fast-import", "--quiet", input="\n".join(stream).encode("utf-8"))
    run_git(path, "reset", "-q", "--hard", "main")
    repo.set_origin_head("main")
    return repo


def _epoch(stamp: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(stamp).timestamp())


def foreign_modify(r: Runner, name: str) -> str:
    """模拟另一个写入者：带 CAS 用一篇不同的内容覆盖，返回写下的 doc_hash。"""
    from dpe_sdk import Document
    from dpe_sdk.protocol import BaseHash

    uri = r.uri(name)
    with r.client() as client:
        base = client.head(uri)
        assert base is not None
        document = Document.model_validate(
            {
                "file_type": "git_repo",
                "pages": [{"elements": [{"category": "NarrativeText", "text": "他人写入"}]}],
            }
        )
        return client.commit(uri, document, BaseHash(base)).doc_hash


def foreign_delete(r: Runner, name: str) -> None:
    """模拟另一个写入者：带 CAS 删除。"""
    uri = r.uri(name)
    with r.client() as client:
        base = client.head(uri)
        assert base is not None
        client.delete(uri, base)
