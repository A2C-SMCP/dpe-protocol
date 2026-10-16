"""基准测试的运行设施（#237）：两种模式共用同一个运行器（真实 ``dpe-run``）、参考服务端与记录代理。

- ``Instance``：一个模式（``issue`` = 正式插件，``project`` = ``project_mode``）的 dpe-run 实例，经
  记录代理访问 remote；``round`` 跑一轮并采集指标（``RoundStats``）；
- ``snapshot``：remote 前缀内每篇文档的 doc_hash、按序的 page_hash 与每页的 content_hash——前后两轮
  对比得到变化范围，合起来去重得到服务端对象数。「新出现的 page_hash / content_hash」按前缀内全局
  去重计：变化后的页若与别处已有的页内容相同（例如空评论页），不算新对象；
- 指标口径（#237 §4）：耗时取运行器子进程的墙钟；DPE 请求数与请求体字节（解压后）取代理记录；
  Jira 请求 / 响应字节 / 429 取模拟 Jira 的计数差；插件内存峰值取 ``rsswrap``；游标大小取运行器
  ``state.json`` 里的游标长度；变更可见延迟取「本轮开始 → 该 URI 的最后一个写请求完成」。

指标采集（快照、读状态文件）都在一轮之外进行，不计入耗时。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from dpe_sdk.transport import DpeClient
from fake_jira import EMAIL, TOKEN

from bench._harness import RecordingProxy, Seen
from bench.fake import BenchJira, JiraCounts

__all__ = ["Instance", "Mode", "RoundStats", "Snapshot", "diff", "snapshot"]

Mode = Literal["issue", "project"]
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "dpe-connector.json"
RSSWRAP = Path(__file__).resolve().parent / "rsswrap.py"
#: 一篇文档的快照：(doc_hash, [(page_hash, [content_hash, …]), …])
DocShot = tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]
Snapshot = dict[str, DocShot]


def snapshot(remote: str, prefix: str) -> Snapshot:
    """remote 上前缀内全部文档的骨架快照（按完整 file_uri）。"""
    out: Snapshot = {}
    with DpeClient(remote) as client:
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
            pages = tuple(
                (page_hash, tuple(page.elements))
                for page_hash, page in zip(skeleton.document.pages, skeleton.pages, strict=True)
            )
            out[uri] = (skeleton.doc_hash, pages)
    return out


@dataclass(frozen=True)
class Diff:
    """前后两个快照之间的变化：新出现的对象数（重学范围的代理指标）与文档级变化。"""

    docs_added: int
    docs_removed: int
    docs_changed: int
    new_pages: int
    new_contents: int

    @property
    def doc_hash_changes(self) -> int:
        return self.docs_added + self.docs_removed + self.docs_changed


def _objects(shot: Snapshot) -> tuple[set[str], set[str]]:
    pages = {page for _, doc in shot.values() for page, _ in doc}
    contents = {c for _, doc in shot.values() for _, elements in doc for c in elements}
    return pages, contents


def diff(before: Snapshot, after: Snapshot) -> Diff:
    old_pages, old_contents = _objects(before)
    new_pages, new_contents = _objects(after)
    common = before.keys() & after.keys()
    return Diff(
        docs_added=len(after.keys() - before.keys()),
        docs_removed=len(before.keys() - after.keys()),
        docs_changed=sum(before[u][0] != after[u][0] for u in common),
        new_pages=len(new_pages - old_pages),
        new_contents=len(new_contents - old_contents),
    )


@dataclass
class RoundStats:
    """一轮的指标（#237 §4）。"""

    exit_code: int
    report: dict[str, Any]
    seconds: float
    dpe_requests: int
    dpe_writes: int
    dpe_request_bytes: int
    #: 模拟 Jira 的计数差；对真实 Jira 校准时没有（``None``）
    jira: JiraCounts | None
    plugin_rss_kib: int | None
    cursor_bytes: int | None
    diff: Diff
    server_docs: int
    server_pages: int
    server_contents: int
    #: 关注的 URI → 本轮开始到它最后一个写请求完成的秒数（没写到即不在表里）
    latency: dict[str, float] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return str(self.report.get("outcome", "crashed"))

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = self.report.get("counts", {})
        return counts

    @property
    def failures(self) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = self.report.get("failures", [])
        return failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "outcome": self.outcome,
            "incremental": self.report.get("incremental"),
            "counts": {k: v for k, v in self.counts.items() if v},
            "failures": [
                {k: f[k] for k in ("level", "code", "file_uri", "retryable") if k in f}
                for f in self.failures
            ],
            "seconds": round(self.seconds, 3),
            "dpe_requests": self.dpe_requests,
            "dpe_writes": self.dpe_writes,
            "dpe_request_bytes": self.dpe_request_bytes,
            "jira_requests": None if self.jira is None else self.jira.requests,
            "jira_response_bytes": None if self.jira is None else self.jira.response_bytes,
            "jira_429": None if self.jira is None else self.jira.throttled,
            "plugin_rss_kib": self.plugin_rss_kib,
            "cursor_bytes": self.cursor_bytes,
            "doc_hash_changes": self.diff.doc_hash_changes,
            "docs_added": self.diff.docs_added,
            "docs_removed": self.diff.docs_removed,
            "docs_changed": self.diff.docs_changed,
            "new_pages": self.diff.new_pages,
            "new_contents": self.diff.new_contents,
            "server_docs": self.server_docs,
            "server_pages": self.server_pages,
            "server_contents": self.server_contents,
            "latency_seconds": {k: round(v, 3) for k, v in self.latency.items()},
        }


class Instance:
    """一个模式的 dpe-run 实例（经记录代理），加上为它服务的模拟 Jira 与参考服务端。"""

    def __init__(
        self,
        tmp: Path,
        mode: Mode,
        jira: BenchJira | None,
        site: str,
        upstream: str,
        projects: list[str],
        *,
        policy: dict[str, str] | None = None,
        secrets: dict[str, dict[str, str]] | None = None,
        name: str | None = None,
        runner: dict[str, Any] | None = None,
        timeout: float = 3600,
    ) -> None:
        self.tmp, self.mode, self.jira, self.site = tmp, mode, jira, site
        self.upstream, self.projects, self.policy = upstream, projects, policy
        #: 凭证引用：缺省指向模拟 Jira 的凭证（经环境变量）；真实校准时用 ``file`` 来源
        self.secrets = secrets or {
            "JIRA_EMAIL": {"env": "SRC_JIRA_EMAIL"},
            "JIRA_API_TOKEN": {"env": "SRC_JIRA_TOKEN"},
        }
        self.timeout = timeout
        #: 实例定义的 ``runner`` 调优参数（如 ``scan_timeout``）；缺省用运行器的缺省值
        self.runner = runner
        #: 实例名（定义 id、前缀、状态目录都由它派生）；同一模式要在别的前缀上再跑一份时给出
        self.name = name or f"bench-{mode}"
        self.prefix = f"jira://{self.name}/"
        self.proxy = RecordingProxy(upstream, timeout=timeout)
        self.state = tmp / f"state-{self.name}"
        self.rss_file = tmp / f"rss-{self.name}.txt"
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "NO_PROXY": "127.0.0.1,localhost",  # 回环地址不经代理（macOS 会读系统代理）
            "no_proxy": "127.0.0.1,localhost",
            "SRC_JIRA_EMAIL": EMAIL,
            "SRC_JIRA_TOKEN": TOKEN,
        }
        self.last: Snapshot = {}

    # ---------------------------------------------------------------- 实例定义

    def _command(self) -> list[str]:
        if self.mode == "issue":
            plugin = [str(Path(sys.executable).with_name("dpe-jira"))]
        else:
            plugin = [sys.executable, "-m", "bench.project_mode"]
        return [sys.executable, str(RSSWRAP), str(self.rss_file), *plugin]

    def _definition(self) -> Path:
        plugin: dict[str, Any] = {"manifest": str(MANIFEST), "command": self._command()}
        if self.mode == "project":
            plugin["env"] = {"PYTHONPATH": str(ROOT)}
        definition: dict[str, Any] = {
            "definition_version": 1,
            "id": self.name,
            "remote": {"url": self.proxy.url},
            "uri_prefix": self.prefix,
            "plugin": plugin,
            "config": {"site": self.site, "projects": self.projects},
            "secrets": self.secrets,
        }
        if self.policy is not None:
            definition["conflict_policy"] = self.policy
        if self.runner is not None:
            definition["runner"] = self.runner
        path = self.tmp / f"{self.name}.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        return path

    def _argv(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "dpe_sdk.run",
            "--state-dir",
            str(self.state),
            str(self._definition()),
        ]

    # ---------------------------------------------------------------- 一轮

    def round(self, watch: tuple[str, ...] = ()) -> RoundStats:
        """跑一轮（不论成败都采集指标）；``watch`` 是要统计可见延迟的 URI。"""
        mark = self.proxy.mark()
        jira_before = None if self.jira is None else self.jira.snapshot()
        self.rss_file.unlink(missing_ok=True)
        started = time.monotonic()
        proc = subprocess.run(
            self._argv(),
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=self.timeout,
        )
        seconds = time.monotonic() - started
        if self.proxy.errors:
            raise RuntimeError(f"记录代理出错：{self.proxy.errors}")
        if TOKEN in proc.stdout + proc.stderr:
            raise RuntimeError("凭证出现在运行器输出中")
        try:
            report: dict[str, Any] = json.loads(proc.stdout)
        except ValueError:
            report = {"outcome": "crashed", "stderr_tail": proc.stderr[-2000:]}
        return self._collect(proc.returncode, report, seconds, mark, jira_before, started, watch)

    def crash_on_next_write(self) -> Seen:
        """跑一轮，第一个写请求在服务端生效后、响应返回前 kill -9 运行器（W11，#213）。

        与 git connector 的端到端设施同一手法：代理扣住响应并置位 ``held``，等到它再终止运行器的
        进程组，全程没有 sleep。返回被扣住的写请求。
        """
        self.proxy.hold_next_write()
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(
                self._argv(),
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=self.environ,
                start_new_session=True,
            )

            def watch() -> None:
                proc.wait()
                self.proxy.held.set()  # 运行器先于写请求退出：唤醒等待

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                if not self.proxy.held.wait(self.timeout) or self.proxy.held_request is None:
                    stderr.seek(0)
                    raise RuntimeError(f"运行器没有发出写请求：{stderr.read()[-2000:]}")
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(60)
            finally:
                self.proxy.release()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(60)
                watcher.join(60)
        held = self.proxy.held_request
        assert held is not None
        return held

    def _collect(
        self,
        code: int,
        report: dict[str, Any],
        seconds: float,
        mark: int,
        jira_before: JiraCounts | None,
        started: float,
        watch: tuple[str, ...],
    ) -> RoundStats:
        requests = self.proxy.since(mark)
        writes = [seen for seen in requests if seen.is_write]
        latency: dict[str, float] = {}
        for uri in watch:
            hits = [s.finished for s in writes if _target_uri(s) == uri]
            if hits:
                latency[uri] = max(hits) - started
        after = snapshot(self.upstream, self.prefix)
        pages, contents = _objects(after)
        change = diff(self.last, after)
        self.last = after
        return RoundStats(
            exit_code=code,
            report=report,
            seconds=seconds,
            dpe_requests=len(requests),
            dpe_writes=len(writes),
            dpe_request_bytes=sum(len(seen.body) for seen in requests),
            jira=None
            if self.jira is None or jira_before is None
            else self.jira.snapshot() - jira_before,
            plugin_rss_kib=_read_int(self.rss_file),
            cursor_bytes=self._cursor_bytes(),
            diff=change,
            server_docs=len(after),
            server_pages=len(pages),
            server_contents=len(contents),
            latency=latency,
        )

    def _cursor_bytes(self) -> int | None:
        """运行器为本实例保存的游标的字节数（``<状态目录>/instances/<实例 id>/state.json``）。"""
        path = self.state / "instances" / self.name / "state.json"
        if not path.exists():
            return None  # 还没有成功的一轮
        cursor = json.loads(path.read_text(encoding="utf-8")).get("cursor")
        return None if cursor is None else len(cursor.encode("utf-8"))

    def close(self) -> None:
        self.proxy.close()


def _target_uri(seen: Seen) -> str | None:
    """写请求的目标文档：``documents?uri=``（commit / delete）或 move 请求体里的 ``to_uri``。"""
    if seen.target == "move":
        body = seen.json()
        return body.get("to_uri") if isinstance(body, dict) else None
    # ``Seen.path`` 已解码，这里再按查询串解析一次：前提是基准的 URI 只含 unreserved 字符与 ``:/``
    # （``jira://bench-*/issue/PA-1``），不含 ``+``、``%``、``&``，两次解码结果相同
    values = parse_qs(urlsplit(seen.path).query).get("uri")
    return values[0] if values else None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
