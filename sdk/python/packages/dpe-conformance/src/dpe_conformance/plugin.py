"""供 ``dpe-conformance`` 以子进程拉起的 pytest 插件（``-p dpe_conformance.plugin``）。

运行参数经环境变量接收（CLI 与套件之间的唯一通道，凭证不落命令行）：

- ``DPE_CONFORMANCE_TARGET``：被测 remote 的 base URL（必需）；
- ``DPE_CONFORMANCE_CONFIG``：配置文件路径（可选；缺省无配置）；
- ``DPE_CONFORMANCE_COUNTS``：计数 JSON 的落盘路径（CLI 总是给出）。

夹具：``conformance_target``（会话级：客户端、一次性 URI 命名空间与请求助手）、
``identities`` 与 ``full_identity`` / ``scoped_identity`` / ``no_force_identity``、
``self_write_hook``、``clock_advance_hook``、``inject_error_hook``。未配置的身份或钩子使
依赖它们的用例显式 skip（skip 计入计数——CI
据此断言执行数不低于基线，防止因 skip 而假绿）。

计数在 ``pytest_sessionfinish`` 落盘：``totals`` 含 ``executed``（取得通过/失败结论的
用例，即 passed + failed）、``skipped``、``passed`` / ``failed`` / ``errors`` 与收集错误；
``sections`` 按章节（``checks/`` 下的一级模块或包）分列——用例由所收集文件路径归章
（``_section_of``），不依赖 nodeid 形态。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dpe_hash
import httpx
import pytest

from dpe_conformance.config import ConfigError, ConformanceConfig, Identity, load_config
from dpe_conformance.hooks import HookDriver

TARGET_ENV = "DPE_CONFORMANCE_TARGET"
CONFIG_ENV = "DPE_CONFORMANCE_CONFIG"
COUNTS_ENV = "DPE_CONFORMANCE_COUNTS"
#: 套件根的覆盖（内部；仅测试用）：经环境传给子进程 pytest，让合成套件走真实的套件机制。
CHECKS_ROOT_ENV = "DPE_CONFORMANCE_CHECKS_ROOT"

#: 请求超时（秒）；套件面向本地或 CI 内网服务端
REQUEST_TIMEOUT = 30.0

#: 计数 JSON 的 schema 版本
COUNTS_SCHEMA = 1


@dataclass
class Target:
    """被测 remote 的会话级上下文：客户端、一次性 URI 命名空间与请求助手。"""

    base_url: str
    config: ConformanceConfig
    client: httpx.Client
    uri_prefix: str

    def uri(self, path: str) -> str:
        """在本次运行的一次性命名空间下生成 file_uri（避免与既有状态相撞）。"""
        return self.uri_prefix + path.lstrip("/")

    def send(
        self,
        method: str,
        path: str,
        *,
        identity: Identity | None = None,
        contract: str | None = dpe_hash.CONTRACT,
        **kwargs: Any,
    ) -> httpx.Response:
        """发一个带契约头（缺省 ``dpe1``）与可选身份的请求；``path`` 相对 remote base URL。"""
        headers = dict(kwargs.pop("headers", None) or {})
        if contract is not None:
            headers["DPE-Hash-Contract"] = contract
        if identity is not None:
            headers["Authorization"] = identity.authorization
        return self.client.request(method, path, headers=headers, **kwargs)


class _State:
    """插件的一次运行状态（模块级单例：``-p`` 注册的是模块，不是实例）。"""

    def __init__(self, config: pytest.Config) -> None:
        url = os.environ.get(TARGET_ENV)
        if not url:
            raise pytest.UsageError(f"缺少环境变量 {TARGET_ENV}（应由 dpe-conformance CLI 设置）")
        config_path = os.environ.get(CONFIG_ENV) or None
        try:
            conformance_config = (
                load_config(Path(config_path)) if config_path else ConformanceConfig.empty()
            )
        except ConfigError as exc:
            raise pytest.UsageError(f"配置不合法：{exc}") from None
        counts = os.environ.get(COUNTS_ENV)
        self.counts_path = Path(counts) if counts else None
        self.rootpath = config.rootpath
        self.target = Target(
            base_url=url,
            config=conformance_config,
            client=httpx.Client(base_url=_with_trailing_slash(url), timeout=REQUEST_TIMEOUT),
            uri_prefix=f"{conformance_config.uri_prefix}{uuid.uuid4().hex[:8]}/",
        )
        self.outcomes: dict[str, str] = {}
        self.paths: dict[str, Path] = {}
        self.collection_errors = 0


_state: _State | None = None


def _require_state() -> _State:
    assert _state is not None, "插件未经 pytest_configure 初始化"
    return _state


def _with_trailing_slash(url: str) -> str:
    """httpx 的 base_url 合并按 RFC 3986：前缀路径（如 /r/1）必须以 / 结尾才不被相对路径吞掉。"""
    return url if url.endswith("/") else url + "/"


def pytest_configure(config: pytest.Config) -> None:
    global _state
    _state = _State(config)


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """记录用例文件路径：``location[0]`` 是**相对 config.rootpath** 的路径（pytest 9 的
    ``Node.location`` 语义；给绝对路径的版本原样保留——pathlib 拼接时绝对右操作数胜出），
    以 rootpath 还原成绝对路径后供归章。既不解析 nodeid（其形态随 rootdir 变化，甚至可为空），
    也不借助进程 CWD。
    """
    state = _require_state()
    state.paths[nodeid] = state.rootpath / Path(location[0])


def pytest_unconfigure(config: pytest.Config) -> None:
    global _state
    if _state is not None:
        _state.target.client.close()
        _state = None


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _require_state().collection_errors += 1


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """按用例归并结果：setup 即 skip 的算 skip；有 call 结论的按结论；其余归 error。"""
    state = _require_state()
    if report.when == "setup":
        if report.skipped:
            state.outcomes[report.nodeid] = "skipped"
        elif report.failed:
            state.outcomes[report.nodeid] = "error"
    elif report.when == "call":
        if report.skipped:  # 用例体内 pytest.skip()
            state.outcomes[report.nodeid] = "skipped"
        else:
            state.outcomes[report.nodeid] = "passed" if report.passed else "failed"
    elif report.when == "teardown" and report.failed:
        finished = state.outcomes.get(report.nodeid)
        if finished is None or finished == "passed":
            state.outcomes[report.nodeid] = "error"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = _require_state()
    if state.counts_path is None:
        return
    state.counts_path.write_text(
        json.dumps(collect_counts(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def collect_counts(state: _State) -> dict[str, Any]:
    """按用例归章汇总计数（CLI 读取同一份结构做摘要与基线断言）。"""
    per_section: dict[str, dict[str, int]] = {}
    for nodeid, outcome in state.outcomes.items():
        section = _section_of(state.paths.get(nodeid), nodeid)
        bucket = per_section.setdefault(section, _empty_bucket())
        bucket[_bucket_key(outcome)] += 1
    for bucket in per_section.values():
        _finalize_bucket(bucket)
    totals = _empty_bucket()
    for bucket in per_section.values():
        for key in totals:
            totals[key] += bucket[key]
    _finalize_bucket(totals)
    totals["collection_errors"] = state.collection_errors
    return {
        "schema": COUNTS_SCHEMA,
        "target": state.target.base_url,
        "totals": totals,
        "sections": per_section,
    }


def _empty_bucket() -> dict[str, int]:
    return {"executed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}


def _bucket_key(outcome: str) -> str:
    """用例结果 → 计数桶；executed 是派生量，在 ``_finalize_bucket`` 里由 passed + failed 得出。"""
    return "errors" if outcome == "error" else outcome


def _finalize_bucket(bucket: dict[str, int]) -> None:
    bucket["executed"] = bucket["passed"] + bucket["failed"]


def checks_root() -> Path:
    """套件根目录（``dpe_conformance.checks``）。

    ``DPE_CONFORMANCE_CHECKS_ROOT`` 可把它指到别处——内部机制，仅测试用（让合成套件也能
    走真实的 runner/插件机制），不属于对外配置面。
    """
    override = os.environ.get(CHECKS_ROOT_ENV)
    return Path(override) if override else Path(__file__).parent / "checks"


def _section_of(path: Path | None, nodeid: str) -> str:
    """用例归章：以 ``pytest_runtest_logstart`` 还原出的**绝对文件路径**相对 ``checks/``
    取首段——一级模块取文件名，包章节取包名。

    不在 ``checks/`` 之下的用例（如测试注入的合成章节）按文件名归章；文件路径缺失
    （``pytest_runtest_logstart`` 未触发）时退化为对 nodeid 文件段取文件名。
    """
    if path is None:
        return Path(nodeid.split("::", 1)[0]).stem
    try:
        relative = path.resolve().relative_to(checks_root().resolve())
    except ValueError:
        return path.stem
    parts = relative.parts
    if len(parts) == 1:
        return Path(parts[0]).stem
    return parts[0]


# ---------------------------------------------------------------------- 夹具


@pytest.fixture(scope="session")
def conformance_target() -> Target:
    """被测 remote 的上下文；所有检查从它取客户端与 URI 命名空间。"""
    return _require_state().target


@pytest.fixture(scope="session")
def identities(conformance_target: Target) -> Mapping[str, Identity]:
    """全部已配置身份；一个都没配时显式 skip（计入计数）。"""
    if not conformance_target.config.identities:
        pytest.skip("未配置任何身份（identities）")
    return conformance_target.config.identities


def _identity(target: Target, role: str) -> Identity:
    identity = target.config.identities.get(role)
    if identity is None:
        pytest.skip(f"未配置 {role} 身份（identities.{role}）")
    return identity


@pytest.fixture(scope="session")
def full_identity(conformance_target: Target) -> Identity:
    """全量写权身份。"""
    return _identity(conformance_target, "full")


@pytest.fixture(scope="session")
def scoped_identity(conformance_target: Target) -> Identity:
    """仅前缀可写身份（``prefix`` 给出该前缀）。"""
    return _identity(conformance_target, "scoped")


@pytest.fixture(scope="session")
def no_force_identity(conformance_target: Target) -> Identity:
    """无 force 权限身份。"""
    return _identity(conformance_target, "no-force")


@pytest.fixture(scope="session")
def self_write_hook(conformance_target: Target) -> HookDriver:
    """``self-write`` 钩子；未配置时显式 skip（计入计数）。"""
    endpoint = conformance_target.config.self_write
    if endpoint is None:
        pytest.skip("未配置 self-write 钩子（hooks.self-write）")
    return HookDriver(endpoint, conformance_target.client)


@pytest.fixture(scope="session")
def clock_advance_hook(conformance_target: Target) -> HookDriver:
    """``clock-advance`` 钩子（推进服务端时钟 N 秒）；未配置时显式 skip（计入计数）。"""
    endpoint = conformance_target.config.clock_advance
    if endpoint is None:
        pytest.skip("未配置时钟推进钩子（hooks.clock-advance）")
    return HookDriver(endpoint, conformance_target.client)


@pytest.fixture(scope="session")
def inject_error_hook(conformance_target: Target) -> HookDriver:
    """``inject-error`` 钩子（#123）；未配置时显式 skip（计入计数）。"""
    endpoint = conformance_target.config.inject_error
    if endpoint is None:
        pytest.skip("未配置 inject-error 钩子（hooks.inject-error）")
    return HookDriver(endpoint, conformance_target.client)


__all__ = [
    "CHECKS_ROOT_ENV",
    "CONFIG_ENV",
    "COUNTS_ENV",
    "TARGET_ENV",
    "Target",
    "checks_root",
    "clock_advance_hook",
    "collect_counts",
    "conformance_target",
    "full_identity",
    "identities",
    "inject_error_hook",
    "no_force_identity",
    "scoped_identity",
    "self_write_hook",
]
