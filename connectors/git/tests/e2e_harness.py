"""端到端测试设施（#66、#215）：真实 ``dpe-run``、真实插件、参考服务端，加一层记录请求的代理。

- ``reference_server``：参考服务端子进程（``python -m dpe_sdk.testing``，真实 TCP）；
- ``RecordingProxy``：进程内的透明转发代理，记录每个请求（请求体已解压），并能在写请求被
  服务端执行之后**扣住响应**——测试据此确定性地制造「写入已生效、确认前崩溃」：等代理报告已
  扣住，再终止运行器的进程组，不依赖 sleep 时序；
- ``Runner``：写实例定义、以子进程跑 ``dpe-run``（报告取自 stdout，NDJSON 事件取自 stderr）；
- ``Runner.assert_converged``：同一源状态在一个全新的空前缀上全量推送一次（F(S)），与增量实例的
  remote 状态做**页级**比较（每篇的 doc_hash 与按序的 page_hash 列表）。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from dpe_sdk.transport import DpeClient
from helpers import FixtureRepo, command_for, run_git

MANIFEST = Path(__file__).resolve().parents[1] / "dpe-connector.json"
NULLCURSOR = Path(__file__).resolve().parent / "nullcursor.py"
#: 子进程（运行器一轮、等待代理扣住写请求）的超时上限：防止 CI 卡死
TIMEOUT = 120


@contextmanager
def reference_server() -> Iterator[str]:
    """启动参考服务端子进程，产出它公布的 remote URL。"""
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", "--prefix", "/r/1"],
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
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")
            yield str(json.loads(lines[0])["url"])
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
    #: 请求头（名字小写）：断言 CAS 前置条件（If-Match / If-None-Match）
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


_HOP = {"connection", "keep-alive", "transfer-encoding", "host"}


class RecordingProxy:
    """透明转发到 ``upstream``，记录全部请求；``hold_next_write`` 后扣住下一个写请求的响应。"""

    def __init__(self, upstream: str) -> None:
        parts = urlsplit(upstream)
        assert parts.hostname is not None and parts.port is not None
        self._host, self._port, self._base = parts.hostname, parts.port, parts.path
        self.seen: list[Seen] = []
        #: 代理自身出错的原因（如 chunked 请求体、连不上上游）：轮结束时断言为空，失败可读
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._hold = False
        #: 被扣住的那个写请求（不依赖记录顺序：并发请求按完成顺序追加）
        self.held_request: Seen | None = None
        self.held = threading.Event()
        self._release = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _forward(self) -> None:
                try:
                    self._relay()
                except Exception as exc:  # 代理自身的故障：记下原因、回 502，由用例断言
                    proxy.errors.append(f"{self.command} {self.path}: {exc!r}")
                    with contextlib.suppress(OSError):
                        self.send_error(502)

            def _relay(self) -> None:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    raise AssertionError("记录代理不支持 chunked 请求体")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                upstream = HTTPConnection(proxy._host, proxy._port, timeout=TIMEOUT)
                upstream.request(self.command, self.path, body=body or None, headers=headers)
                reply = upstream.getresponse()
                data = reply.read()
                upstream.close()
                raw = body
                if raw and (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                path = unquote(self.path)
                if path.startswith(proxy._base + "/"):
                    path = path[len(proxy._base) + 1 :]
                lowered = {key.lower(): value for key, value in self.headers.items()}
                seen = Seen(self.command, path, raw, reply.status, lowered)
                with proxy._lock:
                    proxy.seen.append(seen)
                    hold = proxy._hold and seen.is_write
                    if hold:
                        proxy._hold = False
                        proxy.held_request = seen
                if hold:
                    # 写入已在服务端生效：通知测试，等它终止运行器后再（向已断开的连接）回响应
                    proxy.held.set()
                    proxy._release.wait(TIMEOUT)
                try:
                    self.send_response(reply.status)
                    skip = _HOP | {"date", "server"}
                    if self.command != "HEAD":  # HEAD 原样保留上游的 Content-Length
                        skip = skip | {"content-length"}
                    for key, value in reply.getheaders():
                        if key.lower() not in skip:
                            self.send_header(key, value)
                    if self.command != "HEAD":
                        self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                except OSError:
                    pass  # 运行器已被终止：响应无人接收

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _forward

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

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


@dataclass
class Runner:
    """在同一个 remote 上跑 dpe-run 的增量实例（经记录代理）与比对用的一次性全量实例（直连）。"""

    tmp: Path
    upstream: str
    config: dict[str, Any]
    proxy: RecordingProxy = field(init=False)
    policy: dict[str, str] | None = None
    #: 存在即让 nullcursor 包装把结束批的 cursor 改成 null（#213 场景）
    nullcursor_flag: Path | None = None
    fresh: int = 0

    def __post_init__(self) -> None:
        self.proxy = RecordingProxy(self.upstream)
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            # 回环地址不经代理：macOS 上会读系统代理设置，参考服务端在本机
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    # ---------------------------------------------------------------- 实例定义

    def _definition(self, instance_id: str, prefix: str, remote: str, *, wrapped: bool) -> Path:
        command = command_for("dpe-git-connector")
        if wrapped and self.nullcursor_flag is not None:
            command = [sys.executable, str(NULLCURSOR), str(self.nullcursor_flag), *command]
        definition: dict[str, Any] = {
            "definition_version": 1,
            "id": instance_id,
            "remote": {"url": remote},
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
        definition = self._definition("inc", "git://inc/", self.proxy.url, wrapped=True)
        proc = subprocess.run(
            self._argv(definition, self.state),
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=TIMEOUT,
        )
        assert not self.proxy.errors, self.proxy.errors
        assert proc.returncode == expect, proc.stdout + proc.stderr
        return Round(json.loads(proc.stdout), _events(proc.stderr), self.proxy.since(mark))

    def crash_on_next_write(self) -> Seen:
        """跑一轮，第一个写请求在服务端生效后、响应返回前 kill -9 运行器（它所在的进程组）。

        确定性：代理扣住响应并置位 ``held``，测试等到它才终止进程，全程没有 sleep；运行器在发出
        写请求之前就退出时，旁路线程同样唤醒等待并立即判失败（附运行器的 stderr）。返回被扣住的
        写请求。

        插件由运行器以独立会话启动（``dpe_sdk.run.host``），不在被终止的进程组里：它随后读到
        stdin EOF 自行退出。插件只读源、不写 remote，因此不影响「写入已生效、未被确认」这一状态。
        """
        definition = self._definition("inc", "git://inc/", self.proxy.url, wrapped=True)
        self.proxy.hold_next_write()
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(
                self._argv(definition, self.state),
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=self.environ,
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
                    pytest.fail(
                        f"运行器没有发出写请求（exited={exited.is_set()}）：{stderr.read()}"
                    )
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(TIMEOUT)
            finally:
                self.proxy.release()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(TIMEOUT)
                # 进程已回收，旁路线程随即结束：等它结束，晚到的唤醒不会落进下一次调用
                watcher.join(TIMEOUT)
        assert not self.proxy.errors, self.proxy.errors
        held = self.proxy.held_request
        assert held is not None and held.is_write
        return held

    # ---------------------------------------------------------------- 比较

    def snapshot(self, prefix: str = "git://inc/") -> dict[str, Pages]:
        """remote 上前缀内每篇文档的页级快照（按前缀之后的标识）。"""
        out: dict[str, Pages] = {}
        with DpeClient(self.upstream) as client:
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
        """同一源状态在全新的空前缀、全新的状态目录上全量推送一次（F(S)，直连，不经代理）。"""
        self.fresh += 1
        prefix = f"git://full{self.fresh}/"
        definition = self._definition(f"full{self.fresh}", prefix, self.upstream, wrapped=False)
        proc = subprocess.run(
            self._argv(definition, self.tmp / f"state-full{self.fresh}"),
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=TIMEOUT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["incremental"] is False
        return self.snapshot(prefix)

    def assert_converged(self) -> dict[str, Pages]:
        """R = F(S)。doc_hash 相同即 page_hash 列表相同（文档对象含页列表），页级快照的作用是
        失败时直接看出哪一页不同，并供用例对单页断言。"""
        actual = self.snapshot()
        assert actual == self.full_snapshot()
        return actual

    def close(self) -> None:
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
def runner(tmp_path: Path, config: dict[str, Any]) -> Iterator[Runner]:
    """参考服务端 + 记录代理 + 运行器。"""
    with reference_server() as remote:
        instance = Runner(tmp_path, remote, config)
        try:
            yield instance
        finally:
            instance.close()


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
    path: Path, per_month: int, months: tuple[str, ...] = ("2026-01", "2026-02")
) -> FixtureRepo:
    """经 ``git fast-import`` 一次生成每月 ``per_month`` 个提交的仓库——整篇内联超过
    ``SMALL_UPDATE_BYTES``，更新走差量路径（#212）。比逐个 ``git commit`` 快一个数量级。"""
    repo = FixtureRepo(path)
    stream: list[str] = []
    for month in months:
        for i in range(per_month):
            stamp = f"{month}-{1 + i * 25 // per_month:02d}T10:{i % 60:02d}:00+00:00"
            note = "这是一段足够长的提交说明，用来把文档撑过快路径的阈值。" * 4
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


def foreign_modify(remote: str, uri: str) -> str:
    """模拟另一个写入者：带 CAS 用一篇不同的内容覆盖，返回写下的 doc_hash。"""
    from dpe_sdk import Document
    from dpe_sdk.protocol import BaseHash

    with DpeClient(remote) as client:
        base = client.head(uri)
        assert base is not None
        document = Document.model_validate(
            {
                "file_type": "git_repo",
                "pages": [{"elements": [{"category": "NarrativeText", "text": "他人写入"}]}],
            }
        )
        return client.commit(uri, document, BaseHash(base)).doc_hash


def foreign_delete(remote: str, uri: str) -> None:
    """模拟另一个写入者：带 CAS 删除。"""
    with DpeClient(remote) as client:
        base = client.head(uri)
        assert base is not None
        client.delete(uri, base)
