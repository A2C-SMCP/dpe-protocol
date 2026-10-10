"""插件进程宿主（connector 契约 §6.1–§6.3、§6.7）：以真实子进程驱动桩插件。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from dpe_sdk.run import (
    InstanceFailure,
    PluginHost,
    ProtocolFailure,
    RequestTimedOut,
    RoundFailure,
    RpcError,
    build_plugin_env,
    check_against_manifest,
)
from dpe_sdk.run.definition import parse_definition
from dpe_sdk.run.manifest import parse_manifest
from dpe_sdk.run.secrets import REDACTED, Redactor

STUB = Path(__file__).with_name("run_stub_plugin.py")
MANIFEST = parse_manifest(
    {
        "manifest_version": 1,
        "name": "stub",
        "version": "1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {"type": "object"},
        "secrets": [{"name": "STUB_TOKEN", "required": False}],
    }
)
T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    async def bounded() -> T:
        # 宿主任何路径都不得悬挂：整个用例有硬上限
        return await asyncio.wait_for(coro, 30)

    return asyncio.run(bounded())


def argv(mode: str) -> list[str]:
    return [sys.executable, str(STUB), mode]


def base_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "")}


async def started(mode: str, **kwargs: Any) -> PluginHost:
    host = PluginHost(exit_grace=1.0, **kwargs)
    await host.start(argv(mode), base_env())
    return host


async def handshake(mode: str, **kwargs: Any) -> PluginHost:
    host = await started(mode, **kwargs)
    await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})
    return host


def with_host(
    mode: str, body: Callable[[PluginHost], Awaitable[T]], **kwargs: Any
) -> tuple[T, PluginHost]:
    async def scenario() -> tuple[T, PluginHost]:
        host = await handshake(mode, **kwargs)
        try:
            return await body(host), host
        finally:
            await host.close()

    return run(scenario())


def _child_pid(lines: list[str]) -> int:
    return int(next(line for line in lines if line.startswith("child=")).split("=")[1])


#: 能否读到进程状态位（Linux 且 procfs 可用）：读不到时不把「观测不到」当成「已终止」。
#: 用功能探针而非 ``isdir("/proc")``——以空目录覆盖 /proc 的隔离运行下路径存在但 procfs 不可用。
_CAN_READ_STATE = sys.platform.startswith("linux") and os.path.isfile("/proc/self/stat")


def _terminated(pid: int) -> bool:
    """进程已不再运行：已从进程表消失，或（Linux + procfs）已是僵尸。

    被杀 ≠ 已回收：孤儿是插件进程的子进程，不是宿主的子进程，宿主无法 ``waitpid``，回收由系统
    init / subreaper 负责，宿主既不可观测也无法等待。僵尸仍能被 ``kill(pid, 0)`` 找到，因此
    「不再运行」的判据还要看 ``/proc/<pid>/stat`` 的状态位；读不到状态位时只认「已消失」，宁可
    等满超时而失败，也不把不可观测当成已终止。仅限 POSIX：Windows 上 ``os.kill(pid, 0)`` 会
    真的终止进程。
    """
    assert os.name == "posix", "存活探测是 POSIX 语义，Windows 的 os.kill 会终止目标进程"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pid 被复用为他人进程等：进程仍在，按未终止继续轮询
        return False
    if not _CAN_READ_STATE:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # comm 可含空格与括号，取最后一对括号之后的状态位
            state = fh.read().decode("ascii", "replace").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):  # 与上一步之间的竞态：条目已消失
        return True
    return state == "Z"


def _wait_terminated(pid: int, timeout: float = 5.0) -> bool:
    """有界等待 ``pid`` 不再运行（被杀到回收之间由系统调度，无法事件驱动地等待）。

    超时返回 ``False``。
    """
    deadline = time.monotonic() + timeout
    while not _terminated(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


# ---------------------------------------------------------------- 握手与正常关闭


def test_handshake_request_and_shutdown() -> None:
    exits: list[tuple[int | None, int | None]] = []

    async def scenario() -> tuple[Any, Any]:
        async with PluginHost(exit_grace=1.0, on_exited=lambda c, s: exits.append((c, s))) as host:
            await host.start(argv("ok"), base_env())
            init = await host.initialize(
                manifest=MANIFEST,
                uri_prefix="stub://x/",
                config={"a": 1},
                runner={"name": "dpe-run", "version": "0"},
                remote_limits={"max_payload_bytes": 1024},
            )
            echoed = await host.request("echo", {"hello": "世界"})
        return init, echoed

    init, echoed = run(scenario())
    assert init.protocol_version == "dpe-connector/1"
    assert init.plugin.name == "stub"
    assert init.capabilities.supports_cursor is True  # 未知能力键被忽略
    assert echoed == {"hello": "世界"}
    assert exits == [(0, None)]  # 响应 shutdown 后以 0 退出


def test_request_before_initialize_is_rejected() -> None:
    async def scenario() -> None:
        async with await started("ok") as host:
            with pytest.raises(RuntimeError):
                await host.request("echo", {})

    run(scenario())


def test_rpc_error_is_returned_to_caller() -> None:
    async def body(host: PluginHost) -> RpcError:
        with pytest.raises(RpcError) as info:
            await host.request("fail", {"code": -32003, "data": {"retryable": True}})
        # 请求级错误不是致命失败：宿主仍可用
        assert await host.request("echo", {"n": 1}) == {"n": 1}
        return info.value

    err, _ = with_host("ok", body)
    assert (err.name, err.retryable) == ("source_failed", True)


@pytest.mark.parametrize(
    ("mode", "code", "source"),
    [
        ("version_mismatch", "version_unsupported", "plugin"),
        ("invalid_config", "invalid_config", "plugin"),
        ("pick_unknown_version", "version_unsupported", "runner"),
        ("identity_mismatch", "plugin_mismatch", "runner"),
    ],
)
def test_initialize_failures_are_instance_level(mode: str, code: str, source: str) -> None:
    async def scenario() -> InstanceFailure:
        async with await started(mode) as host:
            with pytest.raises(InstanceFailure) as info:
                await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})
            return info.value

    failure = run(scenario())
    assert (failure.level, failure.code, failure.source) == ("instance", code, source)


def test_selected_version_must_be_in_manifest() -> None:
    manifest = MANIFEST.model_copy(update={"protocol_versions": ["dpe-connector/2"]})

    async def scenario() -> InstanceFailure:
        async with await started("ok") as host:
            with pytest.raises(InstanceFailure) as info:
                await host.initialize(manifest=manifest, uri_prefix="stub://x/", config={})
            return info.value

    assert run(scenario()).code == "plugin_mismatch"


def test_max_message_bytes_below_remote_payload_is_rejected() -> None:
    async def scenario() -> None:
        async with await started("ok", max_message_bytes=100) as host:
            with pytest.raises(ValueError):
                await host.initialize(
                    manifest=MANIFEST,
                    uri_prefix="stub://x/",
                    config={},
                    remote_limits={"max_payload_bytes": 1000},
                )

    run(scenario())


# ---------------------------------------------------------------- 协议级失败


@pytest.mark.parametrize(
    "mode", ["oversize", "bad_framing", "empty_line", "unsolicited", "wrong_id"]
)
def test_protocol_violations_terminate_instance(mode: str) -> None:
    async def body(host: PluginHost) -> ProtocolFailure:
        with pytest.raises(ProtocolFailure) as info:
            await host.request("echo", {})
        # 失败是致命的：之后的请求立即得到同一个失败
        with pytest.raises(ProtocolFailure):
            await host.request("echo", {})
        return info.value

    failure, host = with_host(mode, body, max_message_bytes=4096)
    assert (failure.level, failure.code) == ("protocol", "protocol_error")
    assert host.returncode is not None  # 插件已被终止


def test_oversize_outgoing_message_is_caller_error() -> None:
    async def body(host: PluginHost) -> None:
        with pytest.raises(ValueError):
            await host.request("echo", {"pad": "x" * 5000})

    with_host("ok", body, max_message_bytes=4096)


# ---------------------------------------------------------------- 进程退出


def test_crash_is_round_level() -> None:
    async def body(host: PluginHost) -> RoundFailure:
        with pytest.raises(RoundFailure) as info:
            await host.request("echo", {})
        return info.value

    failure, host = with_host("crash", body)
    assert (failure.level, failure.code) == ("round", "plugin_crashed")
    assert host.returncode == 3


def test_exit_zero_without_response_is_protocol_error() -> None:
    async def body(host: PluginHost) -> ProtocolFailure:
        with pytest.raises(ProtocolFailure) as info:
            await host.request("echo", {})
        return info.value

    failure, _ = with_host("exit0", body)
    assert failure.code == "protocol_error"


def test_stubborn_plugin_is_killed_with_its_process_group() -> None:
    lines: list[str] = []

    async def scenario() -> float:
        host = await handshake("stubborn", on_stderr=lambda line, _: lines.append(line))
        began = time.monotonic()
        await host.close()  # shutdown 无响应、忽略 EOF 与 SIGTERM，子进程持有 stdout
        return time.monotonic() - began

    elapsed = run(scenario())
    assert elapsed < 10
    if os.name == "posix":
        # 有界等待残留子进程不再运行（回收由系统 init 负责，见 #102）
        child = _child_pid(lines)
        assert _wait_terminated(child), f"残留子进程 {child} 未被终止（仍在运行）"


# ---------------------------------------------------------------- 取消与超时


def test_timeout_sends_cancel_and_plugin_acknowledges() -> None:
    async def body(host: PluginHost) -> None:
        with pytest.raises(RequestTimedOut):
            await host.request("echo", {}, timeout=0.2, cancel_grace=5)
        assert await host.request("echo", {"after": 1}) == {"after": 1}

    with_host("hang_cancel", body)


def test_plugin_ignoring_cancel_is_fatal_timeout() -> None:
    async def body(host: PluginHost) -> RoundFailure:
        with pytest.raises(RoundFailure) as info:
            await host.request("echo", {}, timeout=0.2, cancel_grace=0.2)
        # 插件状态已无法确定：记为致命并终止，后续请求立即得到同一个失败
        assert host.failure is info.value
        with pytest.raises(RoundFailure):
            await host.request("echo", {"next": 1})
        return info.value

    failure, host = with_host("hang_deaf", body)
    assert (failure.level, failure.code) == ("round", "timeout")
    assert host.returncode is not None and host.returncode < 0


# ---------------------------------------------------------------- 环境与 stderr


def test_plugin_environment_is_exactly_the_three_parts(tmp_path: Path) -> None:
    definition = parse_definition(
        {
            "definition_version": 1,
            "id": "probe",
            "remote": {"url": "https://dpe.example.com/r", "credential": {"env": "RUNNER_AUTH"}},
            "uri_prefix": "stub://x/",
            "plugin": {"manifest": "m.json", "command": ["stub"], "env": {"STUB_MODE": "probe"}},
            "config": {},
            "secrets": {"STUB_TOKEN": {"env": "TOKEN_SOURCE"}},
        },
        tmp_path,
    )
    check_against_manifest(definition, MANIFEST)
    runner_environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/home/runner",
        "LC_ALL": "C",
        "RUNNER_AUTH": "Bearer remote-secret",
        "TOKEN_SOURCE": "s3cr3t-token",
        "UNRELATED": "1",
    }
    plugin_env = build_plugin_env(MANIFEST, definition, runner_environ)
    lines: list[str] = []

    async def scenario() -> None:
        async with PluginHost(
            exit_grace=1.0,
            redactor=plugin_env.redactor,
            on_stderr=lambda line, _: lines.append(line),
        ) as host:
            await host.start(argv("env_probe"), plugin_env.env)
            await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})

    run(scenario())
    names = set(json.loads(lines[0])) - {"__CF_USER_TEXT_ENCODING"}  # macOS 自动注入
    assert names == {"PATH", "HOME", "LC_ALL", "STUB_TOKEN", "STUB_MODE"}
    assert lines[1] == f"token={REDACTED}"  # 插件确实拿到凭证，转发时已脱敏


def test_stderr_is_truncated_replaced_and_redacted() -> None:
    lines: list[tuple[str, bool]] = []

    async def scenario() -> None:
        async with PluginHost(
            exit_grace=1.0,
            redactor=Redactor(["SECRETVALUE"]),
            on_stderr=lambda line, truncated: lines.append((line, truncated)),
        ) as host:
            await host.start(argv("stderr_flood"), base_env())
            await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})

    run(scenario())
    assert lines[0] == ("x" * 8192, True)
    assert lines[1] == ("bad �� bytes", False)
    assert lines[2] == (f"leak {REDACTED} end", False)
    assert lines[3] == ("tail without newline", False)


def test_missing_executable_is_definition_invalid() -> None:
    async def scenario() -> InstanceFailure:
        host = PluginHost()
        with pytest.raises(InstanceFailure) as info:
            await host.start(["/nonexistent/plugin"], base_env())
        return info.value

    assert run(scenario()).code == "definition_invalid"


# ---------------------------------------------------------------- 复审回归


def test_crash_with_orphan_holding_pipes_is_detected_on_exit() -> None:
    # 插件退出后其子进程仍持有 stdout/stderr：退出必须被及时识别，不得等管道关闭
    lines: list[str] = []

    async def body(host: PluginHost) -> tuple[RoundFailure, float]:
        began = time.monotonic()
        with pytest.raises(RoundFailure) as info:
            await host.request("echo", {})  # 不设超时：只能靠退出监视结束
        return info.value, time.monotonic() - began

    (failure, elapsed), _ = with_host(
        "crash_orphan", body, on_stderr=lambda line, _: lines.append(line)
    )
    assert failure.code == "plugin_crashed"
    assert elapsed < 5  # 宽限期 1 秒，远小于子进程的 120 秒
    if os.name == "posix":
        # close 按进程组终止了残留子进程：有界等待它不再运行（回收由系统 init 负责，见 #102）
        child = _child_pid(lines)
        assert _wait_terminated(child), f"残留子进程 {child} 未被终止（仍在运行）"


def test_plugin_not_reading_stdin_times_out() -> None:
    async def body(host: PluginHost) -> RoundFailure:
        with pytest.raises(RoundFailure) as info:
            # 远超管道缓冲的请求：写入被背压，超时不能被写入阻塞
            await host.request("echo", {"pad": "x" * 1_000_000}, timeout=0.5)
        assert host.failure is info.value  # 致命：插件已被终止
        return info.value

    failure, host = with_host("deaf_stdin", body)
    assert failure.code == "timeout"
    assert host.returncode is not None


def test_secret_across_stderr_truncation_boundary_is_redacted() -> None:
    lines: list[tuple[str, bool]] = []

    async def scenario() -> None:
        async with PluginHost(
            exit_grace=1.0,
            redactor=Redactor(["SECRETVALUE1234567890"]),
            on_stderr=lambda line, truncated: lines.append((line, truncated)),
        ) as host:
            await host.start(argv("stderr_edge"), base_env())
            await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})

    run(scenario())
    text, truncated = lines[0]
    assert truncated is True
    assert "SECRET" not in text
    assert text == "y" * 8180 + REDACTED  # 截断以原始偏移为准，其后的 z 已丢弃


@pytest.mark.parametrize(
    ("mode", "level", "code"),
    [("bad_init", "protocol", "protocol_error"), ("init_hang", "round", "timeout")],
)
def test_initialize_failure_is_fatal_and_terminates_plugin(
    mode: str, level: str, code: str
) -> None:
    async def scenario() -> tuple[InstanceFailure | Any, PluginHost]:
        host = await started(mode)
        try:
            with pytest.raises((ProtocolFailure, RoundFailure)) as info:
                await host.initialize(
                    manifest=MANIFEST, uri_prefix="stub://x/", config={}, timeout=0.3
                )
            assert host.failure is info.value
            with pytest.raises(RuntimeError):  # 握手未完成，不得发送其他请求
                await host.request("echo", {})
        finally:
            await host.close()
        return info.value, host

    failure, host = run(scenario())
    assert (failure.level, failure.code) == (level, code)
    assert host.returncode is not None and host.returncode < 0  # 被终止，而非自行退出


def test_plugin_text_in_errors_is_redacted() -> None:
    async def body(host: PluginHost) -> RpcError:
        with pytest.raises(RpcError) as info:
            await host.request("echo", {})
        return info.value

    err, _ = with_host("leak_error", body, redactor=Redactor(["SECRETVALUE"]))
    assert err.message == f"token {REDACTED}"
    assert err.data == {"detail": [f"x {REDACTED}"]}


def test_normal_shutdown_leaves_no_failure() -> None:
    async def scenario() -> PluginHost:
        host = await handshake("ok")
        await asyncio.gather(host.close(), host.close())  # 并发关闭共享同一次关闭
        return host

    for _ in range(5):  # shutdown 响应与退出几乎同时到达，反复验证无竞态
        host = run(scenario())
        assert host.failure is None
        assert host.returncode == 0


def test_raising_callbacks_do_not_break_host() -> None:
    def boom(*_: Any) -> None:
        raise RuntimeError("callback failure")

    reported: list[BaseException | None] = []

    async def scenario() -> PluginHost:
        asyncio.get_running_loop().set_exception_handler(
            lambda _, context: reported.append(context.get("exception"))
        )
        host = PluginHost(exit_grace=1.0, on_started=boom, on_exited=boom, on_stderr=boom)
        await host.start(argv("stderr_flood"), base_env())
        await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})
        assert await host.request("echo", {"ok": 1}) == {"ok": 1}
        await host.close()
        await asyncio.sleep(0)  # 让排队的 on_exited 回调运行
        return host

    host = run(scenario())
    assert host.failure is None and host.returncode == 0
    assert reported and all(isinstance(e, RuntimeError) for e in reported)


def test_crash_before_close_is_still_reported() -> None:
    # 插件已崩溃、仍在等待 stdout EOF 的宽限期内调用 close：崩溃不得被当作正常关闭
    async def scenario() -> PluginHost:
        exited = asyncio.Event()
        host = PluginHost(exit_grace=1.0, on_exited=lambda c, s: exited.set())
        await host.start(argv("crash_orphan"), base_env())
        await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})
        request = asyncio.ensure_future(host.request("echo", {}))
        await exited.wait()
        await host.close()
        with pytest.raises(RoundFailure):
            await request
        return host

    host = run(scenario())
    assert host.failure is not None and host.failure.code == "plugin_crashed"


def test_deeply_nested_error_data_completes_the_request() -> None:
    async def body(host: PluginHost) -> RpcError:
        with pytest.raises(RpcError) as info:
            await host.request("echo", {})  # 不设超时：响应必须被分派，不得悬挂
        return info.value

    err, host = with_host("deep_error", body, redactor=Redactor(["SECRETVALUE"]))
    data = err.data
    for _ in range(800):
        data = data[0]
    assert data == REDACTED
    assert host.failure is None


def test_kill_is_not_a_crash() -> None:
    async def scenario() -> PluginHost:
        host = await handshake("ok")
        await host.kill()
        return host

    host = run(scenario())
    assert host.failure is None
    assert host.returncode is not None and host.returncode < 0


def test_kill_before_start_is_noop() -> None:
    async def scenario() -> PluginHost:
        host = PluginHost(exit_grace=1.0)
        await host.kill()
        await host.start(argv("crash"), base_env())
        await host.initialize(manifest=MANIFEST, uri_prefix="stub://x/", config={})
        with pytest.raises(RoundFailure):
            await host.request("echo", {})
        await host.close()
        return host

    host = run(scenario())
    assert host.failure is not None and host.failure.code == "plugin_crashed"
