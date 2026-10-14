"""运行器一致性：connector 线协议夹具的全部场景（connector 契约 §8.4 表）。

真实设施：以子进程运行 ``python -m dpe_sdk.run``，插件为仓库 ``conformance/connector-fixture/``
的夹具（真实子进程、随附清单原样使用），remote 为独立监听的参考服务端（真实 TCP）。每个场景
只由实例配置 ``{"scenario": …}`` 选择，断言逐项对应 §8.4 表中的「运行器预期」。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dpe_hash import blob_ref, content_hash
from dpe_sdk.models import Document
from dpe_sdk.transport import DpeClient
from server_helpers import spawn_server

FIXTURE_DIR = Path(__file__).resolve().parents[5] / "conformance" / "connector-fixture"
PLUGIN = FIXTURE_DIR / "dpe_connector_fixture.py"
P = "dpe://fixture/"


def _load_fixture() -> ModuleType:
    """按路径载入夹具模块（只取它的场景清单与 blob 字节，不经它跑任何东西）。

    不写字节码：夹具目录经 ``website/`` 聚合进文档站点，留下的 ``__pycache__`` 会被一并发布。
    """
    spec = importlib.util.spec_from_file_location("dpe_connector_fixture", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


FIXTURE = _load_fixture()


def text_doc(*pages: list[str]) -> str:
    """夹具产出的 ``txt`` 文档（每页若干 NarrativeText）在 remote 上应有的 doc_hash。"""
    return Document.model_validate(
        {
            "file_type": "txt",
            "pages": [
                {"elements": [{"category": "NarrativeText", "text": t} for t in texts]}
                for texts in pages
            ],
        }
    ).doc_hash()


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str

    @property
    def report(self) -> dict[str, Any]:
        lines = self.stdout.splitlines()
        assert len(lines) == 1, self.stdout + self.stderr  # stdout 恰好一行报告
        report: dict[str, Any] = json.loads(lines[0])
        return report

    def events(self, name: str) -> list[dict[str, Any]]:
        logs = [json.loads(line) for line in self.stderr.splitlines() if line.startswith("{")]
        return [entry for entry in logs if entry["event"] == name]

    def failures(self) -> list[tuple[str, str]]:
        """（source, code）对，按报告顺序。"""
        return [(f["source"], f["code"]) for f in self.report["failures"]]

    def codes(self) -> set[str]:
        return {code for _, code in self.failures()}


class Fixture:
    """一个使用夹具的实例：``run`` 以子进程跑一次 dpe-run。"""

    def __init__(self, tmp: Path, url: str) -> None:
        self.tmp, self.url = tmp, url
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "NO_PROXY": "127.0.0.1,localhost",  # 回环地址不经代理（macOS 会读系统代理）
            "no_proxy": "127.0.0.1,localhost",
        }

    def definition(self, config: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "definition_version": 1,
            "id": "fixture",
            "remote": {"url": self.url},
            "uri_prefix": P,
            "plugin": {
                "manifest": str(FIXTURE_DIR / "dpe-connector.json"),
                "command": [sys.executable, str(PLUGIN)],
            },
            "config": config,
            **extra,
        }

    def run(
        self,
        scenario: str,
        *,
        environ: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Run:
        definition = self.definition({"scenario": scenario, **(config or {})}, **extra)
        path = self.tmp / "instance.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "dpe_sdk.run",
                "--state-dir",
                str(self.tmp / "state"),
                str(path),
            ],
            capture_output=True,
            text=True,
            env={**self.environ, **(environ or {})},
            timeout=120,
        )
        return Run(proc.returncode, proc.stdout, proc.stderr)

    def head(self, name: str) -> str | None:
        with DpeClient(self.url) as client:
            return client.head(P + name)

    def listing(self) -> set[str]:
        with DpeClient(self.url) as client:
            return {e.file_uri for e in client.list_page(P, None).documents}


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[Fixture]:
    with spawn_server("--prefix", "/r/1") as info:
        yield Fixture(tmp_path, info["url"])


def test_fixture_declares_every_table_scenario() -> None:
    """夹具的场景清单与 §8.4 表一一对应（新增场景须先进规范表，再进夹具与本文件）。"""
    assert len(FIXTURE.SCENARIOS) == 22 and len(set(FIXTURE.SCENARIOS)) == 22
    manifest = json.loads((FIXTURE_DIR / "dpe-connector.json").read_text(encoding="utf-8"))
    assert (manifest["name"], manifest["version"]) == (FIXTURE.NAME, FIXTURE.VERSION)


# ---------------------------------------------------------------------- 握手与实例级失败


def test_empty(fixture: Fixture) -> None:
    run = fixture.run("empty")
    assert run.code == 0, run.stderr
    report = run.report
    assert (report["outcome"], report["incremental"]) == ("succeeded", False)
    assert report["failures"] == []
    assert len(run.events("round.finished")) == 1  # 全量轮可观测
    assert run.events("round.started")[0]["cursor_given"] is False


@pytest.mark.parametrize(
    ("scenario", "code", "source"),
    [
        ("version_mismatch", "version_unsupported", None),  # 表中未限定 source
        ("invalid_config", "invalid_config", "plugin"),
        ("identity_mismatch", "plugin_mismatch", None),
    ],
)
def test_handshake_rejections_are_instance_level(
    fixture: Fixture, scenario: str, code: str, source: str | None
) -> None:
    run = fixture.run(scenario)
    assert run.code == 3, run.stderr
    assert run.report["outcome"] == "failed"
    [failure] = run.report["failures"]
    assert (failure["level"], failure["code"]) == ("instance", code)
    if source is not None:
        assert failure["source"] == source


def test_unknown_scenario_is_rejected(fixture: Fixture) -> None:
    """拼错或未实现的场景名以 ``-32005`` 拒绝，不会退化为一轮空同步而被误判通过。"""
    run = fixture.run("no_cursr")
    assert run.code == 3, run.stderr
    assert run.failures() == [("plugin", "invalid_config")]


# ---------------------------------------------------------------------- 条目与批次


def test_multi_batch(fixture: Fixture) -> None:
    run = fixture.run("multi_batch")
    assert run.code == 0, run.stderr
    report = run.report
    assert (report["outcome"], report["cursor_persisted"]) == ("succeeded", True)
    # 产出依次为：建 x、x 改名为 y、删除不存在的 z——落地后前缀内只有 y
    assert fixture.listing() == {P + "y.txt"}
    assert fixture.head("y.txt") == text_doc(["多批次"])
    again = fixture.run("multi_batch")
    assert again.events("round.started")[0]["cursor_given"] is True  # 游标已持久化


def test_segmented(fixture: Fixture) -> None:
    run = fixture.run("segmented")
    assert run.code == 0, run.stderr
    assert run.report["outcome"] == "succeeded"
    assert run.report["counts"]["created"] == 1
    assert fixture.head("seg.txt") == text_doc(["第一段"], ["第二段"], ["第三段"])


def test_unclosed_segment(fixture: Fixture) -> None:
    run = fixture.run("unclosed_segment")
    assert run.code == 1, run.stderr
    report = run.report
    assert report["outcome"] == "partial"
    assert "segment_unclosed" in run.codes()
    assert report["cursor_persisted"] is False  # 游标未推进
    assert fixture.head("open.txt") is None  # 该文档未提交


def test_error_item(fixture: Fixture) -> None:
    run = fixture.run("error_item")
    assert run.code == 1, run.stderr
    assert run.report["outcome"] == "partial"
    [failure] = run.report["failures"]
    assert failure["source"] == "plugin" and failure["code"] == "source_unavailable"
    assert failure["retryable"] is True


def test_out_of_prefix(fixture: Fixture) -> None:
    run = fixture.run("out_of_prefix")
    assert run.code == 1, run.stderr
    assert run.report["outcome"] == "partial"
    assert run.codes() == {"out_of_prefix"} and len(run.failures()) == 2
    assert fixture.listing() == set()  # 均未投递
    with DpeClient(fixture.url) as client:
        assert client.head("dpe://elsewhere/outside.txt") is None


def test_invalid_item(fixture: Fixture) -> None:
    run = fixture.run("invalid_item")
    assert run.code == 1, run.stderr
    assert run.report["outcome"] == "partial"
    assert run.codes() == {"DPE_VALIDATION", "DPE_CATEGORY_UNKNOWN"}
    assert len(run.failures()) == 3
    assert fixture.listing() == set()


# ---------------------------------------------------------------------- blob


def _image_hash(text: str, data: bytes) -> str:
    return content_hash(
        {"category": "Image", "text": text, "mime_type": "image/png", "blob": blob_ref(data)}
    )


def test_blobs(fixture: Fixture, tmp_path: Path) -> None:
    record = tmp_path / "requests.ndjson"
    run = fixture.run("blobs", config={"record": str(record)})
    assert run.code == 0, run.stderr
    assert run.report["outcome"] == "succeeded"
    with DpeClient(fixture.url) as client:
        skeleton = client.get_skeleton(P + "img.txt")
    assert skeleton is not None
    hashes = [h for page in skeleton.pages for h in page.elements]
    # 引用等于字节的 sha256：内联 blob 与需多块读取的句柄 blob 都按字节计算
    assert hashes == [_image_hash("内联", FIXTURE._SMALL), _image_hash("句柄", FIXTURE._BIG)]
    # 句柄 blob 确实经多次 read_blob 读完：偏移从 0 起首尾衔接（每块恰为夹具的块长上限）
    requests = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    offsets = [r["params"].get("offset", 0) for r in requests if r["method"] == "read_blob"]
    assert len(offsets) >= 2
    assert offsets == [i * FIXTURE._SCENARIO_CHUNK for i in range(len(offsets))]


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("blob_size_mismatch", "blob_size_mismatch"),
        ("unknown_handle", "blob_read_failed"),
    ],
)
def test_blob_failures(fixture: Fixture, scenario: str, code: str) -> None:
    run = fixture.run(scenario)
    assert run.code == 1, run.stderr
    assert run.report["outcome"] == "partial"
    assert code in run.codes()
    assert fixture.head("img.txt") is None  # 文档未提交


def test_cancel(fixture: Fixture, tmp_path: Path) -> None:
    record = tmp_path / "requests.ndjson"
    run = fixture.run("cancel", config={"record": str(record)}, runner={"blob_timeout": 1})
    assert run.code == 1, run.stderr
    assert run.report["outcome"] == "partial"
    assert "blob_read_failed" in run.codes()
    requests = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    methods = [r["method"] for r in requests]
    assert "cancel" in methods  # 运行器超时后发出 cancel
    assert methods.index("cancel") > methods.index("read_blob")


# ---------------------------------------------------------------------- 轮级失败


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("crash", "plugin_crashed"),
        ("oversize", "protocol_error"),
        ("bad_framing", "protocol_error"),
        ("scan_error", "source_failed"),
    ],
)
def test_round_failures(fixture: Fixture, scenario: str, code: str) -> None:
    run = fixture.run(scenario)
    assert run.code == 2, run.stderr
    report = run.report
    assert report["outcome"] == "failed"
    assert code in run.codes()
    assert report["cursor_persisted"] is False  # 游标未推进


# ---------------------------------------------------------------------- 游标


def test_fallback_full(fixture: Fixture) -> None:
    first = fixture.run("fallback_full")
    assert first.code == 0, first.stderr
    second = fixture.run("fallback_full")  # 带着上一轮的游标，插件回退全量
    assert second.code == 0, second.stderr
    assert second.events("round.started")[0]["cursor_given"] is True
    assert (second.report["outcome"], second.report["incremental"]) == ("succeeded", False)
    assert [e["level"] for e in second.events("round.finished")] == ["warn"]


def test_no_cursor(fixture: Fixture) -> None:
    first = fixture.run("no_cursor")
    assert first.code == 0, first.stderr
    assert first.report["cursor_persisted"] is False  # 不缓存游标
    second = fixture.run("no_cursor")
    assert second.code == 0, second.stderr
    assert second.events("round.started")[0]["cursor_given"] is False  # 下一轮不带游标


def test_cursor_violation(fixture: Fixture) -> None:
    """声明不支持游标却给出游标：协议违规、游标不缓存，下一轮同样不带——无视声明的运行器会把
    它缓存并回传（no_cursor 验不出这一点，#191）。"""
    for _ in range(2):
        run = fixture.run("cursor_violation")
        assert run.code == 2, run.stderr
        assert run.report["outcome"] == "failed"
        assert "protocol_error" in run.codes()
        assert run.report["cursor_persisted"] is False
        assert run.events("round.started")[0]["cursor_given"] is False
        assert [e["level"] for e in run.events("round.finished")] == ["error"]


# ---------------------------------------------------------------------- 环境


def _env_probe(fixture: Fixture, environ: dict[str, str]) -> Run:
    return fixture.run(
        "env_probe",
        environ=environ,
        plugin={
            "manifest": str(FIXTURE_DIR / "dpe-connector.env-probe.json"),
            "command": [sys.executable, str(PLUGIN)],
            "env": {"FIXTURE_MODE": "probe"},
        },
        secrets={"FIXTURE_TOKEN": {"env": "TOKEN_SOURCE"}},
    )


def test_env_probe_sees_exactly_the_three_parts(fixture: Fixture) -> None:
    """§4.3：插件环境 = 基础白名单中运行器自身具备的变量 + ``plugin.env`` + 声明的凭证；
    运行器的其余变量（含 remote 凭证来源、凭证来源变量本身）一个都不传。"""
    run = _env_probe(fixture, {"TOKEN_SOURCE": "s3cr3t", "UNRELATED": "1", "LC_ALL": "C"})
    assert run.code == 0, run.stderr
    assert "s3cr3t" not in run.stdout + run.stderr  # 凭证值不外泄
    expected = sorted({"PATH", "LC_ALL", "FIXTURE_MODE", "FIXTURE_TOKEN"})
    assert fixture.head("env.txt") == text_doc(expected)


def test_env_probe_missing_secret(fixture: Fixture) -> None:
    run = _env_probe(fixture, {})
    assert run.code == 3, run.stderr
    assert run.codes() == {"secret_missing"}
