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
_HOOK_ARGS = (
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

#: 收小的限额：让大文档/分块检查在验收里走**完成**路径（规模由 limits 推导，不限值）
_LIMIT_ARGS = (
    "--max-payload-bytes",
    "65536",
    "--page-max-bytes",
    "262144",
    "--blob-max-bytes",
    "131072",
    "--blob-chunk-bytes",
    "16384",
)

#: 只认已授予的凭证（其余 401）：「认证先于一切报文校验」的检查据此执行（HTTP §3.1、§6）
_AUTH_ARGS = ("--require-auth",)

#: 同时声明演练契约 dpex（vectors/README.md）：invalid_objects 中以 dpex 为用例契约的用例据此执行
_CONTRACT_ARGS = ("--contract", "dpe1", "--contract", "dpex")

#: 与 .github/workflows/python-sdk.yml 的 conformance job 同一套参数与配置（改动须两处同步）
_SERVER_ARGS = _HOOK_ARGS + _AUTH_ARGS + _CONTRACT_ARGS + _LIMIT_ARGS
#: 第二轮（只声明 dpe1，只跑 vectors 章）：前提互斥的向量用例在两轮的并集里都被执行（#56）
_SINGLE_CONTRACT_ARGS = _HOOK_ARGS + _AUTH_ARGS + _LIMIT_ARGS

#: 依赖「服务端声明了 dpex」的向量用例（第二轮 skip）与依赖「服务端未声明 dpex」的（第一轮 skip）
_NEEDS_DRILL = {("vectors", "test_invalid_object_vector[page_child_hash_mixed_contract]")}
_NEEDS_NO_DRILL = {
    ("vectors", "test_invalid_object_vector[page_child_hash_drill_contract_unsupported]")
}


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
        text += (
            f'\n[hooks.self-write]\nhttp = "{hooks}/self-write"\n'
            f'\n[hooks.clock-advance]\nhttp = "{hooks}/clock/advance"\n'
            f'\n[hooks.inject-error]\nhttp = "{hooks}/inject-error"\n'
        )
    path = tmp_path / "dpe-conformance.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _testsuite(path: Path) -> ET.Element:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None, f"JUnit XML 里没有 testsuite 元素：{path}"
    return suite


def _cases(path: Path, outcome: str) -> set[tuple[str, str]]:
    """JUnit XML 中结论为 ``outcome``（``skipped`` / ``passed``）的用例，键为 (章节, 用例名)——
    不同章节的同名用例不得互相混淆。"""
    names: set[tuple[str, str]] = set()
    for case in _testsuite(path).iter("testcase"):
        verdicts = {child.tag for child in case} & {"skipped", "failure", "error"}
        if (outcome == "passed" and not verdicts) or outcome in verdicts:
            section = str(case.get("classname")).rsplit(".", 1)[-1]
            names.add((section, str(case.get("name"))))
    return names


def test_full_run_against_reference_server(tmp_path: Path) -> None:
    """有配置：全部章节 282 条检查，执行 281 条（含经错误注入钩子构造的 429 / 503）；唯一的
    skip 是前提为「服务端未声明 dpex」的那条向量用例，由只声明 dpe1 的第二轮执行。"""
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
    assert (totals["executed"], totals["passed"], totals["skipped"]) == (281, 281, 1)
    assert data["sections"]["smoke"]["executed"] == 3
    assert data["sections"]["order"]["executed"] == 54
    assert data["sections"]["security"]["executed"] == 22
    assert data["sections"]["contracts"]["executed"] == 9
    assert data["sections"]["convergence"]["executed"] == 6
    assert data["sections"]["validation"]["executed"] == 8
    assert (data["sections"]["vectors"]["executed"], data["sections"]["vectors"]["skipped"]) == (
        70,
        1,
    )
    assert data["sections"]["large"]["executed"] == 14
    assert data["sections"]["staging"]["executed"] == 12
    assert data["sections"]["write_ops"]["executed"] == 20
    assert data["sections"]["http_binding"] == {
        "executed": 22,
        "passed": 22,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    suite = _testsuite(junit)
    assert suite.get("tests") == "282"
    assert int(suite.get("failures", "0")) == 0
    assert _cases(junit, "skipped") == _NEEDS_NO_DRILL
    assert _cases(junit, "passed") >= _NEEDS_DRILL
    assert "执行 281" in completed.stdout


def test_single_contract_vectors_pass_covers_the_other_side(tmp_path: Path) -> None:
    """第二轮：只声明 dpe1 时只跑 vectors 章——「dpex 前缀不受支持」的用例在这里执行并通过，
    skip 只落在以 dpex 为用例契约的用例上；与全配置轮合起来每条用例都至少执行一次（#56）。"""
    with reference_server(*_SINGLE_CONTRACT_ARGS) as server:
        config = _write_config(tmp_path, server.hooks)
        junit = tmp_path / "junit.xml"
        completed = run_cli(
            server.url,
            "--config",
            str(config),
            "--section",
            "vectors",
            "--junit-xml",
            str(junit),
            cwd=tmp_path,
        )
    assert completed.returncode == 0, completed.stderr
    assert _cases(junit, "skipped") == _NEEDS_DRILL
    assert _cases(junit, "passed") >= _NEEDS_NO_DRILL


def test_missing_hooks_are_skipped_and_counted(tmp_path: Path) -> None:
    """无配置：不依赖身份/钩子的检查执行（9 条）；其余显式 skip（273 条），
    JUnit 与计数都有 skip。"""
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
    assert (totals["executed"], totals["skipped"]) == (9, 273)
    suite = _testsuite(junit)
    assert suite.get("tests") == "282"
    assert int(suite.get("skipped", "0")) == 273


def test_config_auto_discovery_in_cwd(tmp_path: Path) -> None:
    """CWD 下的 dpe-conformance.toml 自动生效：给出 full 身份后 skip 数从 273
    变 58（仅余钩子、scoped、匿名可访问时的认证顺序，以及只在声明 dpex 时适用的向量用例与
    契约升级期一章）。"""
    with reference_server() as server:
        (tmp_path / "dpe-conformance.toml").write_text(
            'schema = 1\n[identities.full]\nauthorization = "Bearer full-auth"\n',
            encoding="utf-8",
        )
        counts = tmp_path / "counts.json"
        completed = run_cli(server.url, "--counts-json", str(counts), cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    totals = json.loads(counts.read_text(encoding="utf-8"))["totals"]
    assert (totals["executed"], totals["skipped"]) == (224, 58)


def test_budget_skips_are_counted(tmp_path: Path) -> None:
    """默认限额（大页对象 ⇒ 元素数超预算）下：large 章 13 执行、预算 skip 1——合规的大规模
    服务端只 skip 不判红，且 skip 计入计数。"""
    with reference_server(*_HOOK_ARGS) as server:  # 不带收小限额：元素数超过上传预算
        config = _write_config(tmp_path, server.hooks)
        counts = tmp_path / "counts.json"
        completed = run_cli(
            server.url,
            "--config",
            str(config),
            "--section",
            "large",
            "--counts-json",
            str(counts),
            cwd=tmp_path,
        )
    assert completed.returncode == 0, completed.stderr
    totals = json.loads(counts.read_text(encoding="utf-8"))["totals"]
    assert (totals["executed"], totals["skipped"]) == (13, 1)


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
    assert (totals["executed"], totals["skipped"]) == (9, 273)


def test_unknown_section_is_rejected(tmp_path: Path) -> None:
    completed = run_cli("http://127.0.0.1:9", "--section", "nope", cwd=tmp_path)
    assert completed.returncode == 2
    assert "nope" in completed.stderr
    assert "smoke" in completed.stderr


def test_bad_remote_url_is_rejected(tmp_path: Path) -> None:
    # 独立进程的冒烟用例；逐例的接受 / 拒绝见 test_cli_remote_url.py（消费 vectors/remote_url.json）
    completed = run_cli("https://user:secret@example.com/r/", cwd=tmp_path)
    assert completed.returncode == 2
    assert "HTTP 绑定 §1" in completed.stderr
    assert "secret" not in completed.stderr


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
