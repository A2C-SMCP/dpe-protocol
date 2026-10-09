"""插件进程宿主（connector 契约 §6.1–§6.3、§6.7 的取消）：asyncio 实现，事件驱动。

一个 ``PluginHost`` 管理一个插件进程（一个实例，§6.1）。IO 建在自定义的
``asyncio.SubprocessProtocol`` 上，全部由事件回调驱动（无轮询、无读取任务）：

- **启动**：直接 exec ``argv``（不经 shell），环境由调用方按 §4.3 构造后原样传入；POSIX 上插件
  自成进程组，终止作用于整个进程组（§6.3）。
- **stdout**：只走协议。收到的字节按 NDJSON 切行、按 ``id`` 把响应分派给等待中的请求；任何违例
  （超限、空行、坏 JSON、插件主动发消息、未知 ``id``）都是协议级失败，立即终止插件。
- **stderr**：持续排空，按行交给 ``on_stderr``：先脱敏、后截断（凭证跨截断边界也不会漏出前缀），
  非 UTF-8 字节替换为 U+FFFD（§7.5）。
- **退出**：``process_exited`` 在插件退出时立即触发，不等管道关闭——插件可能留有持有 stdout 的
  子进程（§6.3）。退出后在宽限期内等待 stdout EOF，让退出前写出的响应先送达，然后归类：非零退出码
  或信号为 ``plugin_crashed``（轮级），以 0 退出为协议级（插件在响应前退出）。
- **写入**：经 transport 非阻塞写出，背压由 ``pause_writing`` / ``resume_writing`` 给出；等待背压
  解除与等待响应在同一个超时内，插件不读 stdin 时请求不会无限挂起。
- **请求**：``initialize`` 必须是第一条；``request`` 超时后发 ``cancel`` 通知（§6.7），宽限期内
  插件作了响应即抛 ``RequestTimedOut``（由调用方按方法归类），不响应即轮级 ``timeout``。

失败一旦发生（协议违例、意外退出、握手失败）即记为致命，此后的请求立即抛出同一个失败；致命失败与
插件返回的错误消息在交出前都经 ``Redactor`` 脱敏（§4.3）。用户回调经事件循环调度，回调抛出的
异常交给事件循环的异常处理器，不影响宿主状态。宿主不决定是否重启插件——那属于编排层（#52）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from dpe_sdk.run.errors import (
    InstanceFailure,
    ProtocolFailure,
    RoundFailure,
    RpcError,
    RunFailure,
)
from dpe_sdk.run.jsonrpc import (
    PROTOCOL_VERSION,
    InitializeResult,
    LineDecoder,
    encode_notification,
    encode_request,
    parse_initialize_result,
    parse_response,
)
from dpe_sdk.run.manifest import Manifest
from dpe_sdk.run.secrets import Redactor

__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "DEFAULT_STDERR_LINE_LIMIT",
    "PluginHost",
    "RequestTimedOut",
]

#: 缺省的单条消息上限（§6.2 示例值，32 MiB）
DEFAULT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
#: 转发插件 stderr 的单行上限（§7.5：SHOULD 不小于 8 KiB）
DEFAULT_STDERR_LINE_LIMIT = 8 * 1024

_POSIX = os.name == "posix"
_SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)

StderrCallback = Callable[[str, bool], None]
StartedCallback = Callable[[int], None]
ExitedCallback = Callable[[int | None, int | None], None]


class RequestTimedOut(Exception):
    """请求超时，已发出 ``cancel`` 且插件在宽限期内作了响应；句柄等不因此失效（§6.7）。"""

    def __init__(self, method: str, timeout: float | None) -> None:
        super().__init__(f"{method} 在 {timeout} 秒内未完成，已取消")
        self.method = method


class _PluginProtocol(asyncio.SubprocessProtocol):
    """把子进程事件转交给宿主；本身不持有状态。"""

    def __init__(self, host: PluginHost) -> None:
        self._host = host

    def pipe_data_received(self, fd: int, data: bytes | str) -> None:
        assert isinstance(data, bytes)
        if fd == 1:
            self._host._on_stdout(data)
        elif fd == 2:
            self._host._on_stderr(data)

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        if fd == 0:
            self._host._on_stdin_lost()
        elif fd == 1:
            self._host._on_stdout_eof()
        elif fd == 2:
            self._host._on_stderr_eof()

    def process_exited(self) -> None:
        self._host._on_process_exited()

    def pause_writing(self) -> None:
        self._host._writable.clear()

    def resume_writing(self) -> None:
        self._host._writable.set()

    def connection_lost(self, exc: Exception | None) -> None:
        # 进程已退出且全部管道已关闭
        self._host._all_closed.set()


class PluginHost:
    """一个插件进程的宿主。用法::

        async with PluginHost(max_message_bytes=...) as host:
            await host.start(argv, env)
            init = await host.initialize(manifest=..., uri_prefix=..., config=...)
            result = await host.request("scan", {...}, timeout=...)

    退出 ``async with`` 时调用 ``close``：先 ``shutdown``、再关 stdin，宽限期内未退出则按进程组
    TERM、KILL。每一步等待都以 ``exit_grace`` 为上限。
    """

    def __init__(
        self,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        stderr_line_limit: int = DEFAULT_STDERR_LINE_LIMIT,
        redactor: Redactor | None = None,
        on_stderr: StderrCallback | None = None,
        on_started: StartedCallback | None = None,
        on_exited: ExitedCallback | None = None,
        exit_grace: float = 5.0,
    ) -> None:
        if stderr_line_limit < 1:
            raise ValueError("stderr_line_limit 必须为正")
        self.max_message_bytes = max_message_bytes
        self._decoder = LineDecoder(max_message_bytes)
        self._stderr_limit = stderr_line_limit
        self._redactor = redactor or Redactor()
        # 截断前多留最长凭证的长度：起点在行长上限之前的凭证完整落在窗口内（见 Redactor.truncate）
        self._stderr_window = stderr_line_limit + self._redactor.max_length
        self._on_stderr_cb = on_stderr
        self._on_started_cb = on_started
        self._on_exited_cb = on_exited
        self._grace = exit_grace

        self._transport: asyncio.SubprocessTransport | None = None
        self._stdin: asyncio.WriteTransport | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._fatal: RunFailure | None = None
        self._initialized = False
        self._closing = False
        self._close_task: asyncio.Future[None] | None = None

        self._writable = asyncio.Event()
        self._writable.set()
        self._stdin_lost = False
        self._stdout_eof = False
        self._process_exited = False
        self._exited_while_closing = False
        self._exit_timer: asyncio.TimerHandle | None = None
        self._exited = asyncio.Event()
        self._all_closed = asyncio.Event()
        self._stderr_buffer = bytearray()
        self._stderr_discarding = False

    # ------------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> PluginHost:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def pid(self) -> int | None:
        return None if self._transport is None else self._transport.get_pid()

    @property
    def returncode(self) -> int | None:
        return None if self._transport is None else self._transport.get_returncode()

    @property
    def failure(self) -> RunFailure | None:
        """已发生的致命失败（协议违例、意外退出、握手失败）；没有时为 ``None``。"""
        return self._fatal

    async def start(self, argv: Sequence[str], env: Mapping[str, str]) -> None:
        """启动插件进程。``argv`` 已解析（见 ``definition.resolve_command``），``env`` 恰为
        §4.3 规定的插件环境——宿主不添加任何变量。"""
        if self._transport is not None:
            raise RuntimeError("插件进程已启动")
        loop = asyncio.get_running_loop()
        program, *args = argv
        try:
            transport, _ = await loop.subprocess_exec(
                lambda: _PluginProtocol(self),
                program,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env),
                start_new_session=_POSIX,
            )
        except OSError as exc:
            raise InstanceFailure(
                "definition_invalid", f"无法启动插件 {program}：{exc.strerror or exc}"
            ) from None
        self._transport = transport
        stdin = transport.get_pipe_transport(0)
        assert isinstance(stdin, asyncio.WriteTransport)
        self._stdin = stdin
        self._callback(self._on_started_cb, transport.get_pid())

    async def close(self, *, graceful: bool = True) -> None:
        """关闭插件（§6.3）：``shutdown`` → 关 stdin → 等待退出 → 进程组 TERM → KILL。

        ``graceful=False`` 时跳过 ``shutdown``。插件退出后若管道仍被其子进程持有，同样按进程组
        终止。可重入：并发调用共享同一次关闭。
        """
        if self._transport is None:
            return
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close(graceful))
        await asyncio.shield(self._close_task)

    async def kill(self) -> None:
        """立即终止插件（进程组 KILL），用于中止本轮（§6.4）。"""
        if self._transport is None:
            return
        # 发信号前同步置关闭标志：主动终止引起的退出不得被归为崩溃，不依赖回调调度顺序
        self._closing = True
        self._signal_group(_SIGKILL)
        await self.close(graceful=False)

    async def _close(self, graceful: bool) -> None:
        transport = self._transport
        assert transport is not None
        # 先置关闭标志：插件响应 shutdown 后退出属正常关闭，不是意外退出
        self._closing = True
        if graceful and not self._exited.is_set() and self._initialized and self._fatal is None:
            with contextlib.suppress(RunFailure, RpcError, TimeoutError):
                _, future = self._register("shutdown", {})
                await asyncio.wait_for(asyncio.shield(future), self._grace)
        if self._stdin is not None:
            self._stdin.close()
        await self._escalate(self._exited)
        # 插件已退出；残留的子进程仍持有管道时，按进程组终止
        await self._escalate(self._all_closed)
        transport.close()
        self._fail_pending(RoundFailure("plugin_crashed", "插件已关闭"))

    async def _escalate(self, event: asyncio.Event) -> None:
        """等待 ``event``：宽限期内未发生则进程组 TERM，再不发生则 KILL；每步都有上限。"""
        for sig in (None, signal.SIGTERM, _SIGKILL):
            if sig is not None:
                self._signal_group(sig)
            if await self._wait(event):
                return

    # ------------------------------------------------------------------ 请求

    async def initialize(
        self,
        *,
        manifest: Manifest,
        uri_prefix: str,
        config: dict[str, Any],
        protocol_versions: Sequence[str] = (PROTOCOL_VERSION,),
        runner: Mapping[str, str] | None = None,
        remote_limits: Mapping[str, int] | None = None,
        timeout: float = 30.0,
    ) -> InitializeResult:
        """握手（§6.3）并核对插件身份（§4.1）。任何失败都记为致命并终止插件：

        - 插件以 ``-32004`` / ``-32005`` 拒绝 → 实例级 ``version_unsupported`` / ``invalid_config``
          （source ``plugin``）；其他错误响应同为实例级；
        - 选定版本不在运行器列表中 → 实例级 ``version_unsupported``（不静默降级）；
        - 选定版本不在清单 ``protocol_versions`` 中、或 name / version 与清单不符 →
          ``plugin_mismatch``；
        - 响应缺少必填字段等握手违例 → 协议级；超时（无论插件是否响应 cancel）→ 轮级 ``timeout``。
        """
        if self._initialized:
            raise RuntimeError("initialize 只能发送一次")
        if remote_limits is not None:
            payload = remote_limits.get("max_payload_bytes")
            if payload is not None and self.max_message_bytes < payload:
                # §6.2：上限 MUST NOT 小于远端 max_payload_bytes——这是运行器配置错误
                raise ValueError("max_message_bytes 不得小于远端的 max_payload_bytes")
        params: dict[str, Any] = {
            "protocol_versions": list(protocol_versions),
            "max_message_bytes": self.max_message_bytes,
            "instance": {"uri_prefix": uri_prefix, "config": config},
        }
        if runner is not None:
            params["runner"] = dict(runner)
        if remote_limits is not None:
            params["remote_limits"] = dict(remote_limits)
        try:
            init = parse_initialize_result(
                await self._request("initialize", params, timeout=timeout)
            )
        except RpcError as exc:
            raise self._abort(
                InstanceFailure(exc.name, f"插件拒绝 initialize：{exc.message}", source="plugin")
            ) from None
        except RequestTimedOut:
            raise self._abort(
                RoundFailure("timeout", f"initialize 在 {timeout} 秒内未完成")
            ) from None
        except RunFailure as failure:
            raise self._abort(failure) from None
        if init.protocol_version not in protocol_versions:
            raise self._abort(
                InstanceFailure(
                    "version_unsupported",
                    f"插件选定的协议版本 {init.protocol_version!r} 不在运行器支持的列表中",
                )
            )
        if init.protocol_version not in manifest.protocol_versions:
            raise self._abort(
                InstanceFailure(
                    "plugin_mismatch", f"插件选定的协议版本 {init.protocol_version!r} 不在清单中"
                )
            )
        if (init.plugin.name, init.plugin.version) != (manifest.name, manifest.version):
            raise self._abort(
                InstanceFailure(
                    "plugin_mismatch",
                    f"插件自报 {init.plugin.name} {init.plugin.version}，"
                    f"清单为 {manifest.name} {manifest.version}",
                )
            )
        self._initialized = True
        return init

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        cancel_grace: float = 5.0,
    ) -> Any:
        """发出请求并等待结果；须在 ``initialize`` 之后。

        插件返回 ``error`` 时抛 ``RpcError``（层级由调用方按方法归类）；致命失败抛 ``RunFailure``；
        超时见 ``RequestTimedOut``。超时时插件仍未读走先前写入的数据（不读 stdin），则判轮级
        ``timeout`` 并终止插件。
        """
        if not self._initialized:
            raise RuntimeError("initialize 完成前不得发送其他请求（§6.3）")
        return await self._request(method, params, timeout=timeout, cancel_grace=cancel_grace)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None,
        cancel_grace: float = 5.0,
    ) -> Any:
        await self._check_stdin()
        id_, future = self._register(method, params)
        try:
            async with asyncio.timeout(timeout):
                await self._writable.wait()
                return await asyncio.shield(future)
        except TimeoutError:
            pass
        if not self._writable.is_set():
            # 写入仍被背压：插件不读 stdin，cancel 也送不到
            raise self._abort(RoundFailure("timeout", f"{method} 超时，插件未读取 stdin"))
        # §6.7：超时后发 cancel 通知；之后到达的任何响应（含迟到的结果）都只表示插件仍在响应
        self._write(encode_notification("cancel", {"id": id_}, self.max_message_bytes))
        try:
            async with asyncio.timeout(cancel_grace):
                await self._writable.wait()
                await asyncio.shield(future)
        except TimeoutError:
            # 插件不响应 cancel：已无法确定它的状态，终止插件（与背压超时一致）
            raise self._abort(
                RoundFailure("timeout", f"{method} 超时，且插件在 {cancel_grace} 秒内未响应 cancel")
            ) from None
        except RpcError:
            pass
        raise RequestTimedOut(method, timeout)

    def _register(self, method: str, params: dict[str, Any]) -> tuple[int, asyncio.Future[Any]]:
        """登记并写出一个请求（不等待背压解除）。"""
        if self._transport is None:
            raise RuntimeError("插件进程未启动")
        if self._fatal is not None:
            raise self._fatal
        id_ = self._next_id
        self._next_id += 1
        data = encode_request(id_, method, params, self.max_message_bytes)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        # 致命失败会落到所有待决请求上，其中可能有已无人等待的（如已放弃的请求）
        future.add_done_callback(_mark_retrieved)
        self._pending[id_] = future
        self._write(data)
        return id_, future

    def _write(self, data: bytes) -> None:
        if self._stdin is not None and not self._stdin.is_closing():
            self._stdin.write(data)

    async def _check_stdin(self) -> None:
        """插件已关闭 stdin（多半已退出）时，等退出归类后抛出致命失败。"""
        if self._fatal is not None:
            raise self._fatal
        if self._stdin_lost:
            await self._wait(self._exited)
            raise self._fatal or self._abort(ProtocolFailure("插件关闭了 stdin"))

    # ------------------------------------------------------------------ 事件处理

    def _on_stdout(self, data: bytes) -> None:
        if self._fatal is not None:
            return  # 已致命失败、插件正被终止：其余输出（含迟到的响应）一律丢弃
        try:
            for line in self._decoder.feed(data):
                self._dispatch(line)
        except ProtocolFailure as failure:
            self._abort(failure)
        except Exception as exc:
            # 插件产出是不可信输入（§6.1）：处理中的任何意外都终止实例并如实上报，不让事件回调
            # 带着已出队的请求逃逸——否则该请求永远不会完成
            self._abort(ProtocolFailure(f"处理插件输出时出错（{type(exc).__name__}）"))

    def _dispatch(self, line: bytes) -> None:
        response = parse_response(line)
        # 先构造结果（含脱敏）再出队：之后只剩不会失败的 set_*，出错时请求仍在待决表中，
        # 由致命失败统一完成，不会悬挂
        error: RpcError | None = None
        if response.error is not None:
            code, message, data = response.error
            error = RpcError(code, self._redactor(message), self._redactor.redact_json(data))
        future = self._pending.pop(response.id, None)
        if future is None:
            raise ProtocolFailure(f"插件响应了未发出的请求 id {response.id}")
        if future.done():
            return
        if error is None:
            future.set_result(response.result)
        else:
            future.set_exception(error)

    def _on_stdout_eof(self) -> None:
        self._stdout_eof = True
        if self._process_exited:
            self._finalize_exit()
        else:
            # stdout 关闭而进程未退出：再也收不到响应
            self._exit_timer = self._loop.call_later(self._grace, self._stdout_closed_alive)

    def _stdout_closed_alive(self) -> None:
        if not self._process_exited and not self._closing:
            self._abort(ProtocolFailure("插件关闭了 stdout 但未退出"))

    def _on_stdin_lost(self) -> None:
        self._stdin_lost = True
        self._writable.set()  # 唤醒等待背压的请求，由退出归类给出结果

    def _on_process_exited(self) -> None:
        self._process_exited = True
        # 以退出发生时是否已在关闭来归类：关闭前已发生的崩溃不得被关闭掩盖
        self._exited_while_closing = self._closing
        returncode = self.returncode
        assert returncode is not None
        if returncode < 0:
            self._callback(self._on_exited_cb, None, -returncode)
        else:
            self._callback(self._on_exited_cb, returncode, None)
        if self._exit_timer is not None:
            self._exit_timer.cancel()
        if self._stdout_eof:
            self._finalize_exit()
        else:
            # 让退出前写出的响应先送达；stdout 被子进程持有时不无限等待
            self._exit_timer = self._loop.call_later(self._grace, self._finalize_exit)

    def _finalize_exit(self) -> None:
        if self._exited.is_set():
            return
        if self._exit_timer is not None:
            self._exit_timer.cancel()
        returncode = self.returncode
        assert returncode is not None
        if self._exited_while_closing:
            self._fail_pending(RoundFailure("plugin_crashed", "插件已退出"))
        elif returncode != 0:
            how = f"信号 {-returncode}" if returncode < 0 else f"退出码 {returncode}"
            self._set_fatal(RoundFailure("plugin_crashed", f"插件异常退出（{how}）"))
        else:
            self._set_fatal(ProtocolFailure("插件意外退出（退出码 0）"))
        self._exited.set()

    def _on_stderr(self, data: bytes) -> None:
        buffer = self._stderr_buffer
        buffer += data
        while True:
            end = buffer.find(b"\n")
            if end == -1:
                if not self._stderr_discarding and len(buffer) > self._stderr_window:
                    self._emit_stderr(bytes(buffer[: self._stderr_window]), cut=True)
                    self._stderr_discarding = True
                if self._stderr_discarding:
                    buffer.clear()
                return
            line = bytes(buffer[:end])
            del buffer[: end + 1]
            if self._stderr_discarding:
                self._stderr_discarding = False
            else:
                cut = len(line) > self._stderr_window
                self._emit_stderr(line[: self._stderr_window], cut=cut)

    def _on_stderr_eof(self) -> None:
        if self._stderr_buffer and not self._stderr_discarding:
            self._emit_stderr(bytes(self._stderr_buffer), cut=False)
        self._stderr_buffer.clear()

    def _emit_stderr(self, raw: bytes, *, cut: bool) -> None:
        """脱敏并按原始字节偏移截断到行长上限（§4.3、§7.5）；``cut`` 表示原始行已超出缓冲窗口。"""
        if self._on_stderr_cb is None:
            return
        text, truncated = self._redactor.truncate(raw.removesuffix(b"\r"), self._stderr_limit)
        self._callback(self._on_stderr_cb, text, cut or truncated)

    # ------------------------------------------------------------------ 内部工具

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def _callback(self, callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is not None:
            self._loop.call_soon(callback, *args)

    def _abort(self, failure: RunFailure) -> RunFailure:
        """记为致命并按进程组终止插件；返回实际生效的致命失败（先发生者优先）。"""
        self._set_fatal(failure)
        self._signal_group(_SIGKILL)
        assert self._fatal is not None
        return self._fatal

    def _set_fatal(self, failure: RunFailure) -> None:
        if self._fatal is None:
            failure.redact(self._redactor)
            self._fatal = failure
        self._fail_pending(self._fatal)

    def _fail_pending(self, failure: RunFailure) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(failure)

    async def _wait(self, event: asyncio.Event) -> bool:
        """在宽限期内等待事件；超时返回 False。"""
        try:
            await asyncio.wait_for(event.wait(), self._grace)
        except TimeoutError:
            return False
        return True

    def _signal_group(self, sig: int) -> None:
        transport = self._transport
        if transport is None or self._all_closed.is_set():
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if _POSIX:
                # 插件以 start_new_session 启动，进程组 id 即其 pid；组长退出后组仍可寻址
                os.killpg(transport.get_pid(), sig)
            elif transport.get_returncode() is None:
                transport.send_signal(sig)


def _mark_retrieved(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
