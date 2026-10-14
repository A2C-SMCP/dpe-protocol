"""运行器与计数机制：章节发现、退出码映射、环境隔离，及子进程下结果分类/收集错误/计数落盘。

合成章节经 monkeypatch 把 runner 的套件根指到 tmp_path，从而让用例走**真实的 run_suite**
（真实的 pytest 子进程、真实的环境构造与计数文件），而不是手写一遍参数。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dpe_conformance.plugin import CHECKS_ROOT_ENV, CONFIG_ENV, COUNTS_ENV, TARGET_ENV
from dpe_conformance.runner import list_sections, map_exit, run_suite, section_paths

_SYNTHETIC = """
import pytest


def test_passes() -> None:
    assert True


def test_fails() -> None:
    assert False, "预期失败"


def test_skips() -> None:
    pytest.skip("示例 skip")


@pytest.fixture
def boom() -> None:
    raise RuntimeError("fixture 炸了")


def test_errors(boom: None) -> None:
    assert True
"""

_BROKEN = 'raise RuntimeError("import 期炸了")\n'


@pytest.fixture
def synthetic_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把套件根指到 tmp_path（经环境传给子进程 pytest；runner 也在调用时经它取根）。

    章节名即该目录顶层的 ``<name>.py`` 或 ``<name>/`` 包。
    """
    monkeypatch.setenv(CHECKS_ROOT_ENV, str(tmp_path))
    return tmp_path


def _run(sections: list[str], counts_path: Path) -> tuple[int, dict[str, Any] | None, str | None]:
    code, counts, message = run_suite(
        target="http://dpe.test",
        config_path=None,
        sections=sections,
        junit_xml=None,
        counts_path=counts_path,
    )
    return code, dict(counts) if counts is not None else None, message


def test_sections_discovery() -> None:
    assert list_sections() == (
        "capabilities",
        "commit",
        "contracts",
        "convergence",
        "http_binding",
        "large",
        "order",
        "read",
        "security",
        "smoke",
        "staging",
        "validation",
        "vectors",
        "write_ops",
    )


def test_section_paths_unknown_section() -> None:
    with pytest.raises(ValueError, match="未知章节"):
        section_paths(["nope"])


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0, 0), (1, 1), (2, 2), (3, 2), (4, 2), (5, 2)],
)
def test_map_exit(status: int, expected: int) -> None:
    code, message = map_exit(status)
    assert code == expected
    assert (message is None) == (status in (0, 1))


def test_counts_classification_and_file(synthetic_suite: Path, tmp_path: Path) -> None:
    """通过的、失败的、skip 的、夹具错误的各一，计数按定义分桶且归章正确。"""
    (synthetic_suite / "synth.py").write_text(_SYNTHETIC, encoding="utf-8")
    counts_path = tmp_path / "counts.json"
    code, counts, message = _run(["synth"], counts_path)
    assert (code, message) == (1, None)  # 有失败
    assert counts is not None
    totals = counts["totals"]
    assert (totals["passed"], totals["failed"], totals["skipped"], totals["errors"]) == (1, 1, 1, 1)
    assert totals["executed"] == 2  # passed + failed
    assert totals["collection_errors"] == 0
    assert set(counts["sections"]) == {"synth"}


def test_collection_errors_are_counted(synthetic_suite: Path, tmp_path: Path) -> None:
    """收集错误（章节 import 期异常）：计数单列、退出码映射为运行故障。"""
    (synthetic_suite / "bad.py").write_text(_BROKEN, encoding="utf-8")
    (synthetic_suite / "good.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    counts_path = tmp_path / "counts.json"
    code, counts, message = _run(["bad", "good"], counts_path)
    assert code == 2
    assert message is not None and "未完成" in message
    assert counts is not None
    assert counts["totals"]["collection_errors"] == 1
    assert counts["totals"]["executed"] == 0  # 收集错误时 pytest 不执行任何用例


def test_plugin_rejects_invalid_target_when_cli_is_bypassed(
    synthetic_suite: Path, tmp_path: Path
) -> None:
    """绕过 CLI 时插件复核 remote URL（HTTP 绑定 §1）：不合法即用法错误，一条用例都不跑。"""
    (synthetic_suite / "good.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    code, counts, message = run_suite(
        target="http://dpe.test/",  # 尾部 / 不合法
        config_path=None,
        sections=["good"],
        junit_xml=None,
        counts_path=tmp_path / "counts.json",
    )
    assert code == 2
    assert message is not None and "未完成" in message
    assert counts is None


def test_section_attribution_covers_package_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """包章节归章：``location`` 是**相对 rootpath** 的路径（内层 ``-c`` 把 rootpath 钉在套件根），
    必须还原后归章——两个包的同名测试模块不得并桶。只依赖「文件名兜底」的实现下，
    这里会得到 ``{"test_commit"}`` 而告红（本用例即 B1 的判别护栏）。
    """
    suite = tmp_path / "suite"
    for name in ("cas", "merge"):
        package = suite / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "test_commit.py").write_text(
            "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
        )
    (suite / "conformance.ini").write_text("[pytest]\n", encoding="utf-8")
    monkeypatch.setenv(CHECKS_ROOT_ENV, str(suite))
    monkeypatch.setattr("dpe_conformance.runner._PYTEST_INI", suite / "conformance.ini")

    counts_path = tmp_path / "counts.json"
    code, counts, message = _run(["cas", "merge"], counts_path)
    assert (code, message) == (0, None)
    assert counts is not None
    assert set(counts["sections"]) == {"cas", "merge"}
    assert counts["totals"]["executed"] == 2


def test_run_suite_environment_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程环境：PYTEST_* 与调用方的 DPE_CONFORMANCE_* 一律不穿透，参数由本次调用决定。"""
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], *, env: dict[str, str], check: bool) -> SimpleNamespace:
        captured["args"] = args
        captured["env"] = env
        Path(env[COUNTS_ENV]).write_text(json.dumps({"totals": {}}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    monkeypatch.setenv("PYTEST_PLUGINS", "someplugin")
    monkeypatch.setenv(CONFIG_ENV, "/tmp/leaked.toml")

    counts_path = tmp_path / "counts.json"
    code, counts, message = _run(["smoke"], counts_path)
    assert (code, message) == (0, None) and counts is not None
    env = captured["env"]
    assert "PYTEST_ADDOPTS" not in env and "PYTEST_PLUGINS" not in env
    assert CONFIG_ENV not in env  # 调用方环境里的配置不得穿透
    assert env[TARGET_ENV] == "http://dpe.test"
    assert env[COUNTS_ENV] == str(counts_path)
    args = captured["args"]
    ini = args[args.index("-c") + 1]
    assert ini.endswith("conformance.ini")

    config = tmp_path / "dpe-conformance.toml"
    config.write_text("schema = 1\n", encoding="utf-8")
    code, _, _ = run_suite(
        target="http://dpe.test",
        config_path=config,
        sections=["smoke"],
        junit_xml=None,
        counts_path=counts_path,
    )
    assert captured["env"][CONFIG_ENV] == str(config)
