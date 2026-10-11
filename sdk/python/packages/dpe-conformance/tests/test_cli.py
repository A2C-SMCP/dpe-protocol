"""CLI 端到端验收（#26）：对参考服务端（独立进程）运行，核对 JUnit XML 与执行/跳过计数。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from dpe_conformance.cli import _print_summary
from helpers import reference_server, run_cli

#: 参考服务端的钩子与多身份（#45）：full 有 force；scoped 仅前缀可写（无 force）；
#: no-force 可写全命名空间但没有 force——「有写权、无 force 权」的对照（force 的 403 检查）
_SERVER_ARGS = (
    "--hooks",
    "/__hooks__",
    "--grant",
    "Bearer full-auth",
    "*",
    "--force",
    "Bearer full-auth",
    "--grant",
    "Bearer scoped-auth",
    "dpe://conf/scoped/",
    "--grant",
    "Bearer no-force-auth",
    "*",
)


def _write_config(tmp_path: Path, hooks: str | None) -> Path:
    text = """
schema = 1

[identities.full]
authorization = "Bearer full-auth"

[identities.scoped]
authorization = "Bearer scoped-auth"
prefix = "dpe://conf/scoped/"

[identities.no-force]
authorization = "Bearer no-force-auth"
"""
    if hooks is not None:
        text += f'\n[hooks.self-write]\nhttp = "{hooks}/self-write"\n'
    path = tmp_path / "dpe-conformance.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _testsuite(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None, f"JUnit XML 里没有 testsuite 元素：{path}"
    return suite


def test_full_run_against_reference_server(tmp_path: Path) -> None:
    """有配置：全部章节 89 条检查执行、2 条无构造手段显式 skip；JUnit XML 与计数 JSON 一致。"""
    with reference_server(*_SERVER_ARGS) as server:
        config = _write_config(tmp_path, server.hooks)
        junit = tmp_path / "junit.xml"
        counts = tmp_path / "counts.json"
        completed = run_cli(
            server.url,
            "--config",
            str(config),
            "--junit-xml",
            str(junit),
            "--counts-json",
            str(counts),
            cwd=tmp_path,
        )
    assert completed.returncode == 0, completed.stderr
    data = json.loads(counts.read_text(encoding="utf-8"))
    totals = data["totals"]
    assert (totals["executed"], totals["passed"], totals["skipped"]) == (89, 89, 2)
    assert data["sections"]["smoke"]["executed"] == 3
    assert data["sections"]["write_ops"]["executed"] == 20
    assert data["sections"]["http_binding"] == {
        "executed": 19,
        "passed": 19,
        "failed": 0,
        "errors": 0,
        "skipped": 2,
    }
    suite = _testsuite(junit)
    assert suite.get("tests") == "91"
    assert int(suite.get("skipped", "0")) == 2
    assert int(suite.get("failures", "0")) == 0
    assert "执行 89" in completed.stdout


def test_missing_hooks_are_skipped_and_counted(tmp_path: Path) -> None:
    """无配置：不依赖身份/钩子的检查执行（7 条）；其余显式 skip（84 条），JUnit 与计数都有 skip。"""
    with reference_server() as server:
        junit = tmp_path / "junit.xml"
        counts = tmp_path / "counts.json"
        completed = run_cli(
            server.url,
            "--junit-xml",
            str(junit),
            "--counts-json",
            str(counts),
            cwd=tmp_path,
        )
    assert completed.returncode == 0, completed.stderr
    totals = json.loads(counts.read_text(encoding="utf-8"))["totals"]
    assert (totals["executed"], totals["skipped"]) == (7, 84)
    suite = _testsuite(junit)
    assert suite.get("tests") == "91"
    assert int(suite.get("skipped", "0")) == 84


def test_config_auto_discovery_in_cwd(tmp_path: Path) -> None:
    """CWD 下的 dpe-conformance.toml 自动生效：给出 full 身份后 skip 数从 84 变 7
    （仅余钩子、scoped 与两个无构造手段的检查）。"""
    with reference_server() as server:
        (tmp_path / "dpe-conformance.toml").write_text(
            'schema = 1\n[identities.full]\nauthorization = "Bearer full-auth"\n',
            encoding="utf-8",
        )
        counts = tmp_path / "counts.json"
        completed = run_cli(server.url, "--counts-json", str(counts), cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    totals = json.loads(counts.read_text(encoding="utf-8"))["totals"]
    assert (totals["executed"], totals["skipped"]) == (84, 7)


def test_section_filter_selects_smoke(tmp_path: Path) -> None:
    with reference_server() as server:
        completed = run_cli(server.url, "--section", "smoke", cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr


def test_remote_with_path_prefix(tmp_path: Path) -> None:
    """remote 带路径前缀（如 /r/1）时全绿：相对路径不得吞掉前缀（RFC 3986 合并）。"""
    with reference_server("--prefix", "/r/1") as server:
        assert server.url.endswith("/r/1")
        counts = tmp_path / "counts.json"
        completed = run_cli(server.url, "--counts-json", str(counts), cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    totals = json.loads(counts.read_text(encoding="utf-8"))["totals"]
    assert (totals["executed"], totals["skipped"]) == (7, 84)


def test_unknown_section_is_rejected(tmp_path: Path) -> None:
    completed = run_cli("http://127.0.0.1:9", "--section", "nope", cwd=tmp_path)
    assert completed.returncode == 2
    assert "nope" in completed.stderr
    assert "smoke" in completed.stderr


def test_bad_remote_url_is_rejected(tmp_path: Path) -> None:
    completed = run_cli("not-a-url", cwd=tmp_path)
    assert completed.returncode == 2
    assert "http(s)" in completed.stderr


def test_summary_mentions_collection_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """收集错误必须在人类摘要里可见（未指定 --counts-json 时它是唯一线索）。"""
    _print_summary(
        "http://dpe.test/",
        ["smoke"],
        {
            "totals": {
                "executed": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "collection_errors": 2,
            },
            "sections": {},
        },
    )
    out = capsys.readouterr().out
    assert "执行 1" in out
    assert "收集错误 2" in out
