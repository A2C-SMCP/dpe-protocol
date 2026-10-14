"""``dpe-run`` 命令行（connector 契约独立运行器规范档 §4.4、§5.2、§7.4–§7.6、#147 的删除）。

真实设施：以子进程运行 ``python -m dpe_sdk.run``，插件为脚本化插件（真实子进程），remote 为独立
监听的参考服务端（真实 TCP）。断言只看对外契约：退出码、stdout 的轮报告、stderr 的 NDJSON 日志、
remote 上的文档与状态目录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from dpe_sdk.models import Document
from dpe_sdk.run import StateDirectory
from dpe_sdk.transport import DpeClient
from server_helpers import spawn_server

#: §8.4 的 connector 线协议夹具（仓库 conformance/connector-fixture/，脚本模式）
PLUGIN = (
    Path(__file__).resolve().parents[5]
    / "conformance"
    / "connector-fixture"
    / "dpe_connector_fixture.py"
)
P = "dpe://cli/"
SECRET = "remote-secret-value-0123456789"
PLUGIN_SECRET = "plugin-secret-value-9876543210"


def _text(*texts: str) -> list[dict[str, Any]]:
    return [{"category": "NarrativeText", "text": t} for t in texts]


def doc_item(name: str, *texts: str) -> dict[str, Any]:
    return {
        "kind": "document",
        "file_uri": P + name,
        "document": {"file_type": "md"},
        "pages": [{"elements": _text(*texts)}],
    }


def expected(*texts: str) -> str:
    return Document.model_validate(
        {"file_type": "md", "pages": [{"elements": _text(*texts)}]}
    ).doc_hash()


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str

    @property
    def report(self) -> dict[str, Any]:
        lines = self.stdout.splitlines()
        assert len(lines) == 1, self.stdout  # stdout 恰好一行报告
        report: dict[str, Any] = json.loads(lines[0])
        return report

    @property
    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stderr.splitlines() if line.startswith("{")]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [entry for entry in self.logs if entry["event"] == name]

    def codes(self) -> list[str]:
        return [failure["code"] for failure in self.report["failures"]]


class Instance:
    """一个实例定义目录：清单 + 定义文件；``run`` 以子进程跑一次 dpe-run。"""

    def __init__(self, root: Path, url: str, state: Path, instance_id: str = "inst") -> None:
        self.dir = root / instance_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.manifest: dict[str, Any] = {
            "manifest_version": 1,
            "name": "connector-fixture",
            "version": "1.0",
            "protocol_versions": ["dpe-connector/1"],
            "config_schema": {"type": "object"},
        }
        self.definition: dict[str, Any] = {
            "definition_version": 1,
            "id": instance_id,
            "remote": {"url": url},
            "uri_prefix": P,
            "plugin": {"manifest": "dpe-connector.json", "command": [sys.executable, str(PLUGIN)]},
            "config": {},
        }
        # 回环地址不经代理：macOS 上 urllib 会读系统代理设置，参考服务端在本机
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def write(self) -> Path:
        (self.dir / "dpe-connector.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        path = self.dir / "instance.json"
        path.write_text(json.dumps(self.definition), encoding="utf-8")
        return path

    def command(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "dpe_sdk.run",
            "--state-dir",
            str(self.state),
            *extra,
            str(self.write()),
        ]

    def script(self, script: dict[str, Any]) -> None:
        """插件脚本写进文件、实例配置只引用它：换脚本不改变内容身份（配置不变）。"""
        path = self.dir / "script.json"
        path.write_text(json.dumps(script), encoding="utf-8")
        self.definition["config"] = {**self.definition["config"], "script": str(path)}

    def run(self, script: dict[str, Any] | None = None, *extra: str) -> Run:
        if script is not None:
            self.script(script)
        proc = subprocess.run(
            self.command(*extra),
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=60,
        )
        return Run(proc.returncode, proc.stdout, proc.stderr)


@pytest.fixture
def server() -> Iterator[dict[str, Any]]:
    with spawn_server("--prefix", "/r/1") as info:
        yield info


@pytest.fixture
def instance(tmp_path: Path, server: dict[str, Any]) -> Instance:
    return Instance(tmp_path, server["url"], tmp_path / "state")


# ---------------------------------------------------------------------- 同步一轮


def test_successful_round_reports_logs_and_exits_zero(
    instance: Instance, server: dict[str, Any]
) -> None:
    first = instance.run({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
    assert first.code == 0, first.stderr
    report = first.report
    assert report["report_version"] == 1
    assert report["instance"] == {"id": "inst", "uri_prefix": P}
    assert report["remote_url"] == server["url"]
    assert report["plugin"] == {"name": "connector-fixture", "version": "1.0"}
    assert report["protocol_version"] == "dpe-connector/1"
    assert (report["outcome"], report["incremental"], report["cursor_persisted"]) == (
        "succeeded",
        False,
        True,
    )
    assert report["cursor_held_since"] is None
    assert report["counts"] == {
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "moved": 0,
        "failed": 0,
    }
    assert report["failures"] == []
    assert report["started_at"].endswith("Z") and report["ended_at"].endswith("Z")
    # §7.5：每行都是 JSON，必需字段齐全；最小事件集
    for entry in first.logs:
        assert {"ts", "level", "event", "instance", "round"} <= entry.keys()
        assert entry["instance"] == "inst"
    names = {entry["event"] for entry in first.logs}
    assert {"plugin.started", "plugin.exited", "round.started", "round.finished"} <= names
    assert first.events("round.started")[0]["cursor_given"] is False
    assert first.events("round.started")[0]["round"] is not None
    with DpeClient(server["url"]) as client:
        assert client.head(P + "a.md") == expected("A")

    second = instance.run({"batches": [[]], "cursor": "c2"})
    assert second.code == 0
    assert second.events("round.started")[0]["cursor_given"] is True
    assert second.report["incremental"] is True


def test_partial_round_exits_one_and_holds_the_cursor(instance: Instance) -> None:
    error = {"kind": "error", "code": "internal", "message": "boom", "retryable": False}
    run = instance.run({"batches": [[error]], "cursor": "c1"})
    assert run.code == 1
    assert run.report["outcome"] == "partial" and run.codes() == ["internal"]
    assert run.report["cursor_held_since"] == run.report["started_at"]
    assert any(entry["level"] in ("warn", "error") for entry in run.events("round.finished"))


def test_concurrent_run_of_the_same_instance_is_busy(instance: Instance) -> None:
    instance.script({"batches": [[]], "scan_delay": 3})
    holder = subprocess.Popen(
        instance.command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=instance.environ,
    )
    try:
        assert holder.stderr is not None
        for line in holder.stderr:  # 等到插件启动（此时已持有实例锁）
            if '"plugin.started"' in line:
                break
        busy = subprocess.run(
            instance.command(), capture_output=True, text=True, env=instance.environ, timeout=30
        )
        assert busy.returncode == 4
        report = Run(busy.returncode, busy.stdout, busy.stderr).report
        assert report["outcome"] == "failed" and report["incremental"] is None
        assert report["cursor_held_since"] is None
        assert [f["code"] for f in report["failures"]] == ["instance_busy"]
    finally:
        holder.wait(timeout=30)
    assert holder.returncode == 0


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda i: i.manifest.update(manifest_version=9), "manifest_invalid"),
        (
            lambda i: i.manifest.update(
                config_schema={
                    "type": "object",
                    "required": ["repo"],
                    "properties": {"repo": {"type": "string"}},
                }
            ),
            "config_schema_violation",
        ),
        (
            lambda i: (
                i.manifest.update(secrets=[{"name": "SRC_TOKEN"}]),
                i.definition.update(secrets={"SRC_TOKEN": {"env": "UNSET_SRC_TOKEN"}}),
            ),
            "secret_missing",
        ),
        (lambda i: i.definition.update(runner={"max_items": 0}), "definition_invalid"),
    ],
)
def test_instance_level_failures_exit_three(instance: Instance, mutate: Any, code: str) -> None:
    mutate(instance)
    run = instance.run({"batches": [[]]})
    assert run.code == 3, run.stderr
    assert run.codes() == [code]
    assert run.report["outcome"] == "failed" and run.report["incremental"] is None
    # 全新实例首次即实例级失败：cursor_held_since 为 null，状态目录不留状态文件（#147）
    assert run.report["cursor_held_since"] is None
    assert not (instance.state / "instances" / "inst" / "state.json").exists()
    failure = run.events("item.failed")[0]
    assert failure["level"] == "error"  # 日志级别
    assert failure["failure"]["level"] == "instance" and failure["failure"]["code"] == code


def test_unparsable_definition_reports_null_instance(instance: Instance) -> None:
    path = instance.dir / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "dpe_sdk.run", "--state-dir", str(instance.state), str(path)],
        capture_output=True,
        text=True,
        env=instance.environ,
        timeout=30,
    )
    run = Run(proc.returncode, proc.stdout, proc.stderr)
    assert run.code == 3
    assert run.report["instance"] == {"id": None, "uri_prefix": None}
    assert run.report["remote_url"] is None and run.report["plugin"] is None
    assert run.codes() == ["definition_invalid"]


def test_overlapping_prefix_is_rejected_until_the_other_instance_is_deleted(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    state = tmp_path / "state"
    first = Instance(tmp_path, server["url"], state, "first")
    assert first.run({"batches": [[]]}).code == 0
    second = Instance(tmp_path, server["url"], state, "second")
    second.definition["uri_prefix"] = P + "sub/"
    overlap = second.run({"batches": [[]]})
    assert overlap.code == 3 and overlap.codes() == ["definition_invalid"]

    deleted = first.run(None, "--delete")
    assert deleted.code == 0 and deleted.stdout == ""  # 删除不输出轮报告（#147）
    event = deleted.events("instance.deleted")[0]
    assert event["found"] is True and event["round"] is None
    assert second.run({"batches": [[]]}).code == 0  # 前缀已释放


def test_delete_drops_local_state_and_is_idempotent(instance: Instance) -> None:
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    assert instance.run(None, "--delete").code == 0
    again = instance.run(None, "--delete")
    assert again.code == 0
    assert again.events("instance.deleted")[0]["found"] is False
    rerun = instance.run({"batches": [[]], "cursor": "c2"})
    assert rerun.events("round.started")[0]["cursor_given"] is False  # 状态已丢弃：全量轮


def test_delete_of_a_busy_instance_exits_four(instance: Instance) -> None:
    with StateDirectory(instance.state).lock_instance("inst"):
        run = instance.run(None, "--delete")
    assert run.code == 4 and run.stdout == ""
    busy = run.events("item.failed")[0]["failure"]
    assert busy["code"] == "instance_busy" and busy["level"] == "round"


def test_plugin_crash_is_round_level(instance: Instance) -> None:
    run = instance.run({"batches": [[]], "crash_on_scan": True})
    assert run.code == 2
    assert "plugin_crashed" in run.codes()
    assert run.report["cursor_persisted"] is False


def test_identity_change_starts_a_full_round(instance: Instance) -> None:
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    instance.definition["config"]["extra"] = "changed"  # 实例配置属于内容身份
    changed = instance.run({"batches": [[]], "cursor": "c2"})
    assert changed.events("round.started")[0]["cursor_given"] is False


class _Unauthorized(BaseHTTPRequestHandler):
    """对一切请求回 401 + ``WWW-Authenticate``（remote 拒绝凭证，http.md §6）。"""

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer realm="dpe"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_HEAD = _deny

    def log_message(self, *args: Any) -> None:
        pass


def test_unauthorized_remote_is_instance_level(tmp_path: Path) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/r/1"
        instance = Instance(tmp_path, url, tmp_path / "state")
        instance.definition["remote"]["credential"] = {"env": "DPE_TEST_AUTH"}
        instance.environ["DPE_TEST_AUTH"] = "Bearer rejected-credential-value"
        denied = instance.run({"batches": [[doc_item("a.md", "A")]]})
    finally:
        httpd.shutdown()
    assert denied.code == 3, denied.stderr
    assert denied.codes() == ["remote_unauthorized"]
    assert denied.report["plugin"] is None  # 未启动插件
    assert "rejected-credential-value" not in denied.stdout + denied.stderr
    # §5.2：remote 拒绝凭证要访问 remote 才能判定，晚于登记，属于已登记实例的实例级失败
    registry = json.loads((tmp_path / "state" / "registry.json").read_text())
    assert "inst" in registry["instances"]


def test_credentials_never_appear_in_output(tmp_path: Path) -> None:
    with spawn_server("--prefix", "/r/1", "--grant", f"Bearer {SECRET}", "*") as info:
        instance = Instance(tmp_path, info["url"], tmp_path / "state")
        instance.definition["remote"]["credential"] = {"env": "DPE_TEST_AUTH"}
        instance.manifest["secrets"] = [{"name": "SRC_TOKEN"}]
        instance.definition["secrets"] = {"SRC_TOKEN": {"env": "TEST_SRC_TOKEN"}}
        instance.environ.update(DPE_TEST_AUTH=f"Bearer {SECRET}", TEST_SRC_TOKEN=PLUGIN_SECRET)
        error = {"kind": "error", "code": "internal", "message": PLUGIN_SECRET, "retryable": False}
        run = instance.run({"batches": [[error]], "stderr": ["token=$SRC_TOKEN"]})
    assert run.code == 1, run.stderr
    assert run.events("plugin.stderr")  # 插件 stderr 已转发
    for text in (run.stdout, run.stderr):
        assert SECRET not in text and PLUGIN_SECRET not in text


def test_usage_error_exits_64_without_a_report(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dpe_sdk.run"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 64 and proc.stdout == ""


# ---------------------------------------------------------------------- 运行器自身的缺陷与状态保留


def _main_in_process(instance: Instance) -> Run:
    """进程内调用 ``main``（便于注入运行器内部缺陷）；输出同子进程。"""
    import io

    from dpe_sdk.run.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--state-dir", str(instance.state), str(instance.write())],
        stdout=out,
        stderr=err,
        environ=instance.environ,
    )
    return Run(code, out.getvalue(), err.getvalue())


def test_runner_crash_reports_redacts_and_exits_70(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行器内部缺陷：仍输出 failed 报告、维护 cursor_held_since、单行 NDJSON 记 traceback
    （经脱敏，含 Authorization 值里的裸 token），以 70 退出。"""
    from dpe_sdk.run import cli

    async def broken(**kwargs: Any) -> Any:
        kwargs["on_event"]("round.started", "info", {"cursor_given": False})
        raise ValueError(f"内部缺陷，回显了 token={SECRET}")

    monkeypatch.setattr(cli, "run_round", broken)
    with spawn_server("--prefix", "/r/1", "--grant", f"Bearer {SECRET}", "*") as info:
        instance = Instance(tmp_path, info["url"], tmp_path / "state")
        instance.definition["remote"]["credential"] = {"env": "DPE_TEST_AUTH"}
        instance.environ["DPE_TEST_AUTH"] = f"Bearer {SECRET}"
        instance.script({"batches": [[]]})
        run = _main_in_process(instance)
    assert run.code == 70
    assert run.report["outcome"] == "failed" and run.codes() == ["runner_internal_error"]
    assert run.report["failures"][0]["retryable"] is False
    # 已读到状态（新实例的指纹已在校验通过后写入）：按「已取得锁、结果不是 succeeded」记录
    assert run.report["cursor_held_since"] == run.report["started_at"]
    crashed = run.events("runner.crashed")
    assert len(crashed) == 1 and "ValueError" in crashed[0]["traceback"]
    finished = run.events("round.finished")  # 由 _conclude 按最终结果发出恰好一条
    assert len(finished) == 1 and finished[0]["level"] == "error"
    assert all(line.startswith("{") for line in run.stderr.splitlines())  # 仍是 NDJSON
    assert SECRET not in run.stdout + run.stderr


def test_identity_change_rejected_at_registration_keeps_the_old_state(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    """内容身份变化（改 uri_prefix）而新前缀与另一实例重叠：登记被拒、切换不发生——旧游标、
    同步状态与意图原样保留，cursor_held_since 记在旧状态上；改回后仍是增量轮。"""
    state = tmp_path / "state"
    instance = Instance(tmp_path, server["url"], state)
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    other = Instance(tmp_path, server["url"], state, "other")
    other.definition["uri_prefix"] = P + "taken/"
    assert other.run({"batches": [[]]}).code == 3  # 与 inst 的 dpe://cli/ 重叠
    instance.definition["uri_prefix"] = P + "sub/"
    other_prefix = Instance(tmp_path, server["url"], state, "third")
    other_prefix.definition["uri_prefix"] = "dpe://elsewhere/"
    assert other_prefix.run({"batches": [[]]}).code == 0
    instance.definition["uri_prefix"] = "dpe://elsewhere/x/"  # 与 third 重叠
    rejected = instance.run(None)
    assert rejected.code == 3 and rejected.codes() == ["definition_invalid"]
    assert rejected.report["cursor_held_since"] == rejected.report["started_at"]
    instance.definition["uri_prefix"] = P
    back = instance.run({"batches": [[]], "cursor": "c2"})
    assert back.code == 0
    assert back.events("round.started")[0]["cursor_given"] is True


def test_held_record_write_failure_is_reported(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已读到状态而 held 记录写不进去：不计入 failures，只记 state.held_write_failed，
    报告照常取本轮计算出的值（§7.2、§7.4）。"""
    from dpe_sdk.run import state as state_module

    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    real = state_module._write_atomic

    def flaky(path: Path, data: Any) -> None:
        if path.name == "state.json" and data.get("held_since"):
            raise OSError("磁盘只读")  # 只让扣住记录写不进去
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", flaky)
    instance.definition["remote"] = {"url": "http://127.0.0.1:9/r/1"}  # 读到状态后 remote 不可达
    run = _main_in_process(instance)
    assert run.code == 2
    assert run.codes() == ["remote_unavailable"]  # 扣住记录写失败不计入 failures（§7.2）
    assert run.report["cursor_held_since"] == run.report["started_at"]
    assert len(run.events("state.held_write_failed")) == 1


def test_failure_before_reading_state_does_not_touch_the_held_record(instance: Instance) -> None:
    """取得锁之后、读到状态之前就结束（已登记实例的清单校验失败）：held 取 null，不读写记录。"""
    error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
    held = instance.run({"batches": [[error]], "cursor": "c1"})
    assert held.report["cursor_held_since"] is not None
    instance.manifest["manifest_version"] = 9
    run = instance.run(None)
    assert run.code == 3 and run.report["cursor_held_since"] is None
    instance.manifest["manifest_version"] = 1
    again = instance.run({"batches": [[error]], "cursor": "c2"})
    assert again.report["cursor_held_since"] == held.report["started_at"]  # 记录未被改动


def _delete_with(instance: Instance, definition: dict[str, Any]) -> Run:
    path = instance.dir / "for-delete.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "dpe_sdk.run",
            "--state-dir",
            str(instance.state),
            "--delete",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=instance.environ,
        timeout=30,
    )
    return Run(proc.returncode, proc.stdout, proc.stderr)


def test_delete_needs_only_the_id_and_ignores_broken_state(instance: Instance) -> None:
    """删除只校验 I-JSON、definition_version 与 id：清单丢失、其余成员残缺、状态内容损坏的
    实例照样能删（§5.1，#147）。"""
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    (instance.state / "instances" / "inst" / "state.json").write_text("garbage", encoding="utf-8")
    corrupt = instance.run(None)
    assert corrupt.code == 2 and corrupt.codes() == ["state_unavailable"]
    assert corrupt.report["failures"][0]["retryable"] is False

    deleted = _delete_with(instance, {"definition_version": 1, "id": "inst"})
    assert deleted.code == 0 and deleted.stdout == ""
    assert deleted.events("instance.deleted")[0]["found"] is True
    assert instance.run({"batches": [[]], "cursor": "c2"}).code == 0  # 以全量轮重建


def test_delete_rejects_an_invalid_id(instance: Instance) -> None:
    run = _delete_with(instance, {"definition_version": 1, "id": "-bad"})
    assert run.code == 3 and run.stdout == ""


def test_crash_outside_the_lock_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dpe_sdk.run import cli

    def broken(path: Path) -> Any:
        raise RuntimeError("定义加载器缺陷")

    monkeypatch.setattr(cli, "load_definition", broken)
    instance = Instance(tmp_path, "http://127.0.0.1:9/r/1", tmp_path / "state")
    run = _main_in_process(instance)
    assert run.code == 70
    assert run.report["outcome"] == "failed" and run.codes() == ["runner_internal_error"]
    assert run.report["instance"] == {"id": None, "uri_prefix": None}
    assert run.report["cursor_held_since"] is None and run.report["incremental"] is None
    assert len(run.events("runner.crashed")) == 1
    _finished_matches_report(run)


def test_unresolvable_state_directory_is_a_reported_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取不到缺省状态目录（如容器内无 home）：仍是单行日志 + failed 报告 + 70，不是 Python 的 1。"""
    import io

    from dpe_sdk.run import cli

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(cli, "default_state_dir", no_home)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main([str(tmp_path / "instance.json")], stdout=out, stderr=err, environ={})
    run = Run(code, out.getvalue(), err.getvalue())
    assert run.code == 70 and run.codes() == ["runner_internal_error"]
    assert len(run.events("runner.crashed")) == 1
    _finished_matches_report(run)  # main 的兜底路径同样恰好一条


def test_failed_validation_does_not_register_the_prefix(instance: Instance) -> None:
    """全新实例在实例级校验（如必填凭证缺失）失败：不占用前缀，状态目录不留登记。"""
    instance.manifest["secrets"] = [{"name": "SRC_TOKEN"}]
    instance.definition["secrets"] = {"SRC_TOKEN": {"env": "UNSET_SRC_TOKEN"}}
    run = instance.run({"batches": [[]]})
    assert run.code == 3 and run.codes() == ["secret_missing"]
    registry = instance.state / "registry.json"
    assert not registry.exists() or "inst" not in json.loads(registry.read_text())["instances"]


def test_crash_after_the_round_keeps_its_results(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一轮已正常收尾、关闭插件时才出缺陷（§7.3 的例外）：outcome 记 failed、退出 70，但已落盘的
    事实照实报告——counts、cursor_persisted: true、扣住记录按 succeeded 清除（null）；
    round.finished 只有一条，与报告一致（failed）。"""
    from dpe_sdk.run import PluginHost

    async def broken_exit(self: PluginHost, *exc: object) -> None:
        raise RuntimeError("关闭插件时的缺陷")

    monkeypatch.setattr(PluginHost, "__aexit__", broken_exit)
    instance.script({"batches": [[doc_item("a.md", "A")]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 70
    assert run.report["outcome"] == "failed" and run.codes() == ["runner_internal_error"]
    assert run.report["counts"]["created"] == 1 and run.report["cursor_persisted"] is True
    assert run.report["cursor_held_since"] is None
    _finished_matches_report(run)
    assert run.events("round.finished")[0]["level"] == "error"
    assert StateDirectory(instance.state).open_instance("inst", _identity(instance)).cursor == "c1"


def test_broken_log_pipe_does_not_change_the_result(instance: Instance) -> None:
    """stderr 写失败（采集端关掉管道）：报告与退出码照常。"""
    import io

    from dpe_sdk.run.cli import main

    class Closed(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("stderr 已关闭")

    out = io.StringIO()
    instance.script({"batches": [[]], "cursor": "c1"})
    code = main(
        ["--state-dir", str(instance.state), str(instance.write())],
        stdout=out,
        stderr=Closed(),
        environ=instance.environ,
    )
    assert code == 0
    assert json.loads(out.getvalue())["outcome"] == "succeeded"


def _identity(instance: Instance) -> str:
    from dpe_sdk.run import content_identity

    return content_identity(
        remote_url=instance.definition["remote"]["url"],
        uri_prefix=instance.definition["uri_prefix"],
        config=instance.definition["config"],
        plugin_name=instance.manifest["name"],
        plugin_version=instance.manifest["version"],
    )


def test_already_held_record_is_kept_on_later_failures(instance: Instance) -> None:
    """已扣住（held=T0）的实例后续轮失败：报告与记录都保持 T0，不重写。"""
    error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
    first = instance.run({"batches": [[error]], "cursor": "c1"})
    t0 = first.report["cursor_held_since"]
    assert t0 is not None
    instance.environ["HTTP_PROXY"] = instance.environ["http_proxy"] = "http://127.0.0.1:9"
    instance.environ["NO_PROXY"] = instance.environ["no_proxy"] = ""
    later = instance.run(None)  # 读到状态后 remote 不可达
    assert later.code == 2 and later.report["cursor_held_since"] == t0


def test_unwritable_report_exits_74_without_a_crash(instance: Instance) -> None:
    """stdout 已关闭、轮报告写不出：不当作运行器缺陷，不用 70（§7.6），退出 74。"""
    import io

    from dpe_sdk.run.cli import main

    class Closed(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("stdout 已关闭")

    err = io.StringIO()
    instance.script({"batches": [[]], "cursor": "c1"})
    code = main(
        ["--state-dir", str(instance.state), str(instance.write())],
        stdout=Closed(),
        stderr=err,
        environ=instance.environ,
    )
    assert code == 74
    assert '"runner.crashed"' not in err.getvalue()
    # 报告写不出时日志照常：本次调用恰好一条 round.finished
    assert err.getvalue().count('"event":"round.finished"') == 1


def test_crash_mid_round_reports_what_already_happened(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮中途因运行器缺陷中断：报告保留已发生的计数与条目失败。"""
    from dpe_sdk.run import ItemFailure, cli

    async def broken(**kwargs: Any) -> Any:
        kwargs["on_event"]("round.started", "info", {"cursor_given": False})
        kwargs["counts"].created += 1
        kwargs["failures"].append(
            ItemFailure("internal", "插件报错", source="plugin", file_uri=P + "x.md")
        )
        raise RuntimeError("中途缺陷")

    monkeypatch.setattr(cli, "run_round", broken)
    instance.script({"batches": [[]]})
    run = _main_in_process(instance)
    assert run.code == 70
    assert run.report["counts"]["created"] == 1
    assert run.codes() == ["internal", "runner_internal_error"]
    _finished_matches_report(run)
    assert run.events("round.finished")[0]["counts"]["created"] == 1


def test_delete_requires_an_integer_definition_version(instance: Instance) -> None:
    run = _delete_with(instance, {"definition_version": 1.0, "id": "inst"})
    assert run.code == 3


# ---------------------------------------------------------------------- 输出编码（§7.4）


def test_report_and_logs_are_utf8_lf_regardless_of_locale(instance: Instance) -> None:
    """标准流的编码被设成 latin-1（模拟 Windows / 非 UTF-8 locale）：中文 message 的失败轮仍输出
    UTF-8、LF 结尾的单行报告，不因编码失败退出 74；stderr 的日志同样是 UTF-8 NDJSON。"""
    error = {"kind": "error", "code": "internal", "message": "源端读取失败", "retryable": False}
    instance.script({"batches": [[error]]})
    proc = subprocess.run(
        instance.command(),
        capture_output=True,
        env={**instance.environ, "PYTHONIOENCODING": "latin-1"},
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout.endswith(b"\n") and b"\r" not in proc.stdout
    run = Run(proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8"))
    assert run.report["failures"][0]["message"] == "源端读取失败"
    assert b"\r" not in proc.stderr
    assert run.events("item.failed")[0]["failure"]["message"] == "源端读取失败"


# ---------------------------------------------------------------------- 审查补充（#153）


def test_crash_outside_the_lock_keeps_the_instance_details(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定义已解析、取锁时出运行器缺陷：由同步流程自己收尾，报告带实例与 remote 信息。"""

    def broken(self: StateDirectory, instance_id: str) -> Any:
        raise RuntimeError("取锁时的缺陷")

    monkeypatch.setattr(StateDirectory, "lock_instance", broken)
    run = _main_in_process(instance)
    assert run.code == 70 and run.codes() == ["runner_internal_error"]
    assert run.report["instance"] == {"id": "inst", "uri_prefix": P}
    assert run.report["remote_url"] == instance.definition["remote"]["url"]
    assert run.report["cursor_held_since"] is None  # 未取得锁：不读写 held 记录
    crashed = run.events("runner.crashed")
    assert len(crashed) == 1 and crashed[0]["instance"] == "inst"
    _finished_matches_report(run)


def test_new_instance_rejected_for_overlap_leaves_nothing_behind(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    """全新实例因前缀重叠被拒：cursor_held_since 为 null，不写 state.json，登记里没有它。"""
    state = tmp_path / "state"
    assert Instance(tmp_path, server["url"], state, "first").run({"batches": [[]]}).code == 0
    second = Instance(tmp_path, server["url"], state, "second")
    second.definition["uri_prefix"] = P + "sub/"
    run = second.run({"batches": [[]]})
    assert run.code == 3 and run.codes() == ["definition_invalid"]
    assert run.report["cursor_held_since"] is None
    assert not (state / "instances" / "second" / "state.json").exists()
    assert "second" not in json.loads((state / "registry.json").read_text())["instances"]


def test_runner_internal_error_takes_precedence_over_round_level(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一调用里既有轮级失败（插件崩溃）又有运行器缺陷：退出 70（§7.6），两条失败都列出。"""
    from dpe_sdk.run import PluginHost

    async def broken_exit(self: PluginHost, *exc: object) -> None:
        raise RuntimeError("关闭插件时的缺陷")

    monkeypatch.setattr(PluginHost, "__aexit__", broken_exit)
    instance.script({"batches": [[]], "crash_on_scan": True})
    run = _main_in_process(instance)
    assert run.code == 70
    assert run.codes() == ["plugin_crashed", "runner_internal_error"]


def test_delete_with_a_corrupt_registry_is_state_unavailable(instance: Instance) -> None:
    assert instance.run({"batches": [[]]}).code == 0
    (instance.state / "registry.json").write_text("{broken", encoding="utf-8")
    run = instance.run(None, "--delete")
    assert run.code == 2 and run.stdout == ""
    failure = run.events("item.failed")[0]["failure"]
    assert failure["code"] == "state_unavailable" and failure["retryable"] is False


class _Recording(BaseHTTPRequestHandler):
    """记录收到的每个请求（删除不得读写 remote）。"""

    seen: ClassVar[list[str]] = []

    def _record(self) -> None:
        type(self).seen.append(f"{self.command} {self.path}")
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _record

    def log_message(self, *args: Any) -> None:
        pass


def test_delete_never_contacts_the_remote(tmp_path: Path, server: dict[str, Any]) -> None:
    instance = Instance(tmp_path, server["url"], tmp_path / "state")
    assert instance.run({"batches": [[doc_item("a.md", "A")]]}).code == 0
    _Recording.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        instance.definition["remote"]["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/r/1"
        run = instance.run(None, "--delete")
    finally:
        httpd.shutdown()
    assert run.code == 0 and run.events("instance.deleted")[0]["found"] is True
    assert _Recording.seen == []


def test_lock_io_error_is_state_unavailable(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取锁的 I/O 错误（不是被占用）：state_unavailable、退出 2，不当作 instance_busy（§5.2）。"""
    import errno

    from dpe_sdk.run import state as state_module

    def no_locks(handle: object) -> bool:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(state_module, "_try_lock", no_locks)
    instance.script({"batches": [[]]})
    run = _main_in_process(instance)
    assert run.code == 2 and run.codes() == ["state_unavailable"]
    assert run.report["cursor_held_since"] is None


# ---------------------------------------------------------------------- #155 的口径（#161）


def test_succeeded_round_without_a_new_cursor_keeps_the_cached_one(instance: Instance) -> None:
    """支持游标的插件在成功轮给出 cursor: null：保留已缓存的游标（§6.8），本轮
    cursor_persisted 为 false；下一轮仍是增量轮、从旧游标开始。"""
    record = instance.dir / "requests.ndjson"
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    kept = instance.run({"batches": [[]], "cursor": None})
    assert kept.code == 0 and kept.report["outcome"] == "succeeded"
    assert kept.report["cursor_persisted"] is False
    later = instance.run({"batches": [[]], "cursor": "c2", "record": str(record)})
    assert later.events("round.started")[0]["cursor_given"] is True
    assert later.report["incremental"] is True
    scans = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert scans[0]["method"] == "scan" and scans[0]["params"]["cursor"] == "c1"


def test_plugin_without_cursor_support_caches_nothing(instance: Instance) -> None:
    record = instance.dir / "requests.ndjson"
    first = instance.run({"batches": [[]], "supports_cursor": False})
    assert first.code == 0 and first.report["cursor_persisted"] is False
    second = instance.run({"batches": [[]], "supports_cursor": False, "record": str(record)})
    assert second.events("round.started")[0]["cursor_given"] is False
    scan = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    assert scan["params"].get("cursor") is None


def _finished_matches_report(run: Run) -> None:
    finished = run.events("round.finished")
    assert len(finished) == 1, finished  # 同步一轮的调用恰好一条（§7.5）
    for key in ("outcome", "incremental", "cursor_persisted", "cursor_held_since", "counts"):
        assert finished[0][key] == run.report[key], key


def test_round_finished_is_emitted_once_for_every_sync_call(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    instance = Instance(tmp_path, server["url"], tmp_path / "state")
    _finished_matches_report(instance.run({"batches": [[doc_item("a.md", "A")]], "cursor": "c"}))
    instance.manifest["manifest_version"] = 9  # 实例级失败
    _finished_matches_report(instance.run(None))
    instance.manifest["manifest_version"] = 1
    with StateDirectory(instance.state).lock_instance("inst"):
        busy = instance.run(None)
    assert busy.code == 4
    _finished_matches_report(busy)
    assert busy.events("round.finished")[0]["round"] is None  # 未取得锁：轮外


def test_round_id_starts_when_the_lock_is_taken(instance: Instance) -> None:
    """轮标识自取得实例锁起生成：插件启动期的事件归入本轮（§7.5）；删除的事件为 null。"""
    run = instance.run({"batches": [[]], "stderr": ["启动期输出"]})
    round_id = run.events("round.finished")[0]["round"]
    assert round_id is not None
    assert run.events("plugin.started")[0]["round"] == round_id
    assert run.events("plugin.stderr")[0]["round"] == round_id
    assert {entry["round"] for entry in run.logs} == {round_id}
    deleted = instance.run(None, "--delete")
    assert all(entry["round"] is None for entry in deleted.logs)


def test_invalid_members_are_null_one_by_one(instance: Instance) -> None:
    """定义不合法时报告与日志的 instance 逐个成员取值：只有缺失或不合法的成员为 null（§7.4）。"""
    instance.definition["remote"] = {"url": "ftp://user:secret@example.com/x"}
    bad_remote = instance.run(None)
    assert bad_remote.code == 3
    assert bad_remote.report["instance"] == {"id": "inst", "uri_prefix": P}
    assert bad_remote.report["remote_url"] is None  # 不合法的 URL 不回显（可能带凭证）
    assert {entry["instance"] for entry in bad_remote.logs} == {"inst"}
    assert "secret" not in bad_remote.stdout + bad_remote.stderr
    _finished_matches_report(bad_remote)

    instance.definition["id"] = "-bad"
    bad_id = instance.run(None)
    assert bad_id.report["instance"] == {"id": None, "uri_prefix": P}
    assert {entry["instance"] for entry in bad_id.logs} == {None}


@pytest.mark.parametrize(
    ("outcome", "failures", "code"),
    [
        ("succeeded", [], 0),
        ("partial", [("item", "internal")], 1),
        ("failed", [("round", "remote_unavailable")], 2),
        ("failed", [("protocol", "protocol_error")], 2),
        ("failed", [("instance", "secret_missing")], 3),
        ("failed", [("round", "instance_busy")], 4),
        # 多类并存：70 > 3 > 2 > 4 > 1（§7.6）
        ("failed", [("instance", "remote_unauthorized"), ("round", "runner_internal_error")], 70),
        ("failed", [("round", "plugin_crashed"), ("round", "runner_internal_error")], 70),
        ("failed", [("round", "remote_unavailable"), ("instance", "remote_unauthorized")], 3),
        ("failed", [("round", "instance_busy"), ("round", "state_unavailable")], 2),
        ("failed", [("item", "internal"), ("round", "instance_busy")], 4),
        ("failed", [("item", "internal"), ("round", "runner_internal_error")], 70),
    ],
)
def test_exit_code_priority(outcome: Any, failures: list[tuple[str, str]], code: int) -> None:
    from dpe_sdk.run import InstanceFailure, ItemFailure, ProtocolFailure, RoundFailure
    from dpe_sdk.run.report import exit_code

    def failure(level: str, name: str) -> Any:
        if level == "instance":
            return InstanceFailure(name, "x")
        if level == "protocol":
            return ProtocolFailure("x")
        if level == "item":
            return ItemFailure(name, "x", source="plugin")
        return RoundFailure(name, "x", retryable=False)

    assert exit_code(outcome, [failure(*entry) for entry in failures]) == code


def test_fallback_to_full_round_is_a_warning(instance: Instance) -> None:
    """给了游标、插件却回退为全量轮：round.finished 为 warn（§6.4 MUST 可观测、§7.5）。"""
    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    run = instance.run({"batches": [[]], "cursor": "c2", "incremental": False})
    assert run.code == 0 and run.report["incremental"] is False
    _finished_matches_report(run)
    assert run.events("round.finished")[0]["level"] == "warn"


# ---------------------------------------------------------------------- 收尾的窄窗口（#169）


def test_report_write_crash_emits_round_finished_once(instance: Instance) -> None:
    """写报告时出了 OSError / ValueError 以外的缺陷：仍是恰好一条 round.finished，不补写第二份
    报告（§7.5、§7.4），退出 74（报告写不出）。"""
    import io

    from dpe_sdk.run.cli import main

    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise RuntimeError("写报告时的缺陷")

    err = io.StringIO()
    instance.script({"batches": [[]], "cursor": "c1"})
    code = main(
        ["--state-dir", str(instance.state), str(instance.write())],
        stdout=Broken(),
        stderr=err,
        environ=instance.environ,
    )
    assert code == 74
    assert err.getvalue().count('"event":"round.finished"') == 1
    assert '"event":"runner.crashed"' in err.getvalue()  # 运行器缺陷记进日志，不静默


def test_unserializable_report_is_one_crash_report(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告本身序列化失败（运行器缺陷）：尚未输出任何东西，以同一日志与本轮已落盘的事实再收尾
    一次——唯一一份报告与一条 round.finished，实例与轮标识不丢，游标如实为已持久化，退出 70。"""
    from dpe_sdk.run.report import build_report as real

    calls = {"n": 0}

    def broken(**kwargs: Any) -> Any:
        calls["n"] += 1
        report = real(**kwargs)
        if calls["n"] == 1:
            report["bad"] = object()  # 第一次（正常收尾）不可序列化，兜底那次正常
        return report

    monkeypatch.setattr("dpe_sdk.run.cli.build_report", broken)
    instance.script({"batches": [[]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 70 and run.codes() == ["runner_internal_error"]
    _finished_matches_report(run)
    assert run.report["instance"]["id"] == "inst"
    assert run.report["cursor_persisted"] is True and run.report["cursor_held_since"] is None
    assert run.report["incremental"] is False  # 已收到结束批：如实保留（§7.3）
    assert "failures_truncated" not in run.report  # 成功轮本无失败明细，没有省略
    assert run.events("round.finished")[0]["round"] is not None


def test_crash_after_the_cursor_was_saved_reports_it_as_saved(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_round 收尾落盘（游标已持久化）之后、返回之前出缺陷：failed、70，但 cursor_persisted
    如实为 true、扣住记录按 succeeded 清除（§7.3 例外、§7.4）。"""
    from dpe_sdk.run import InstanceStore

    real = InstanceStore.finish_round

    def then_crash(self: InstanceStore, **kwargs: Any) -> Any:
        real(self, **kwargs)
        raise RuntimeError("收尾落盘之后的缺陷")

    monkeypatch.setattr(InstanceStore, "finish_round", then_crash)
    instance.script({"batches": [[]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 70 and run.report["outcome"] == "failed"
    assert run.report["cursor_persisted"] is True
    assert run.report["cursor_held_since"] is None
    assert run.report["incremental"] is False  # 结束批已收到：取自结束批（§7.3）
    _finished_matches_report(run)
    assert StateDirectory(instance.state).open_instance("inst", _identity(instance)).cursor == "c1"


def test_broken_log_stream_never_duplicates_round_finished(instance: Instance) -> None:
    """日志流的 flush 抛出意外异常（行已写出）：不改变结果，也不经兜底再发一次。"""
    import io

    from dpe_sdk.run.cli import main

    class FlakyFlush(io.StringIO):
        def flush(self) -> None:
            raise RuntimeError("日志流自身出错")

    err, out = FlakyFlush(), io.StringIO()
    instance.script({"batches": [[]], "cursor": "c1"})
    code = main(
        ["--state-dir", str(instance.state), str(instance.write())],
        stdout=out,
        stderr=err,
        environ=instance.environ,
    )
    assert code == 0 and json.loads(out.getvalue())["outcome"] == "succeeded"
    assert err.getvalue().count('"event":"round.finished"') == 1


def test_report_that_cannot_be_prepared_twice_still_finishes_once(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告两次都构造不出（运行器缺陷）：不写报告（74），仍恰好一条 round.finished。"""

    def broken(**kwargs: Any) -> Any:
        raise RuntimeError("报告构造缺陷")

    monkeypatch.setattr("dpe_sdk.run.cli.build_report", broken)
    instance.script({"batches": [[]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 74 and run.stdout == ""
    finished = run.events("round.finished")
    assert len(finished) == 1 and finished[0]["outcome"] == "failed"
    assert finished[0]["cursor_persisted"] is True and finished[0]["round"] is not None


def test_defect_while_holding_the_cursor_is_reported(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮未开始而写扣住记录时出运行器缺陷：记为 runner_internal_error（70），报告照常取本轮
    计算出的 cursor_held_since（§7.4）。"""
    from dpe_sdk.run import state as state_module

    assert instance.run({"batches": [[]], "cursor": "c1"}).code == 0
    real = state_module._write_atomic

    def broken(path: Path, data: Any) -> None:
        if path.name == "state.json" and data.get("held_since"):
            raise TypeError("记录写入缺陷")
        real(path, data)

    monkeypatch.setattr(state_module, "_write_atomic", broken)
    instance.definition["remote"] = {"url": "http://127.0.0.1:9/r/1"}  # 读到状态后 remote 不可达
    run = _main_in_process(instance)
    assert run.code == 70
    assert run.codes() == ["remote_unavailable", "runner_internal_error"]
    assert run.report["cursor_held_since"] == run.report["started_at"]
    _finished_matches_report(run)


def test_log_rendering_defect_never_breaks_the_finish(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日志字段渲染出缺陷（脱敏器出错）：日志从不抛出——改写一条带 render_error 的行，事件
    不静默消失；结果、报告与退出码不变，round.finished 恰好一条。"""
    from dpe_sdk.run.secrets import Redactor

    def broken(self: Redactor, value: Any) -> Any:
        raise RuntimeError("脱敏缺陷")

    monkeypatch.setattr(Redactor, "redact_json", broken)
    instance.script({"batches": [[]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 0 and run.report["outcome"] == "succeeded"
    _finished_matches_report(run)  # round.finished 的字段由运行器生成，不经脱敏器，始终一致


def test_crash_report_counts_the_failure_details_it_omits(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收尾准备出缺陷时精简报告省略失败明细：以 failures_truncated 如实计数（§7.4）。"""
    from dpe_sdk.run.report import build_report as real

    calls = {"n": 0}

    def broken(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("某条失败明细序列化缺陷")
        return real(**kwargs)

    monkeypatch.setattr("dpe_sdk.run.cli.build_report", broken)
    error = {"kind": "error", "code": "internal", "message": "x", "retryable": False}
    instance.script({"batches": [[error, error]], "cursor": "c1"})
    run = _main_in_process(instance)
    assert run.code == 70 and run.codes() == ["runner_internal_error"]
    assert run.report["failures_truncated"] == 2 and run.report["counts"]["failed"] == 2
