"""``dpe-run``：独立运行器的命令行（connector 契约「独立运行器规范档」§4.4、§5.2、§7.4–§7.6）。

用法::

    dpe-run [--state-dir DIR] DEFINITION            # 同步一轮
    dpe-run [--state-dir DIR] --delete DEFINITION   # 删除实例（§5.1，#147）

**同步一轮**（每次调用至多一轮，§5.2）：解析实例定义 → 取得实例排他锁（取不到即
``instance_busy``，退出 4）→ 实例级校验（清单、定义与清单交叉校验、配置 schema、运行器调优参数、
前缀登记、remote 凭证）→ 打开实例状态（内容身份变化即全量轮）→ 取得 remote 能力 → 构造插件环境
并启动插件、握手 → 跑一轮（``run_round``）→ 向 stdout 写恰好一个轮报告（§7.4）、stderr 写
NDJSON 日志（§7.5），按 §7.6 退出。已取得锁的调用无论成败都维护 ``cursor_held_since``。
每次同步调用在 outcome 最终确定后发出恰好一条 ``round.finished``，字段与轮报告一致；轮标识
自取得实例锁起生成，插件启动期的事件归入本轮（§7.5）。

**删除**：先取实例锁，注销登记（释放前缀）并丢弃本地状态；幂等（无可删除的状态同样成功）；
不触碰 remote 上已投递的文档（§5.1）。不输出轮报告，结果由退出码与日志表达。

命令行用法错误退出 64 且不输出报告；运行器自身的未预期异常以 70 退出（§7.6「其他非零退出码」，
不与 ``partial`` 的 1 混淆）；多类失败同时出现时按 70 > 3 > 2 > 4 > 1 取（``report.exit_code``）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import signal
import sys
import traceback
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, NoReturn

import dpe_sdk
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.pusher import AsyncStatefulPusher
from dpe_sdk.run.definition import (
    DefinitionIdentity,
    InstanceDefinition,
    check_against_manifest,
    load_definition,
    load_definition_id,
    peek_identity,
    resolve_command,
)
from dpe_sdk.run.errors import InstanceFailure, RoundFailure, RunFailure
from dpe_sdk.run.host import DEFAULT_MAX_MESSAGE_BYTES, PluginHost
from dpe_sdk.run.manifest import load_manifest, validate_config
from dpe_sdk.run.report import (
    EXIT_INTERNAL,
    EXIT_SUCCEEDED,
    EXIT_USAGE,
    NdjsonLog,
    build_report,
    encode_report,
    exit_code,
    write_report,
)
from dpe_sdk.run.round import (
    LogLevel,
    Outcome,
    RoundCounts,
    RoundOptions,
    RoundProgress,
    finished_level,
    negotiate_capabilities,
    run_round,
    utc_now,
)
from dpe_sdk.run.secrets import Redactor, build_plugin_env, resolve_remote_credential
from dpe_sdk.run.state import (
    InstanceStore,
    StateDirectory,
    content_identity,
    default_state_dir,
)
from dpe_sdk.transport import AsyncDpeClient

__all__ = ["main"]

#: 轮报告本身写不出（如 stdout 已关闭）：§7.6「其他非零退出码」，不用 70（同 BSD ``EX_IOERR``）
EXIT_REPORT_UNWRITABLE = 74


#: 单条消息上限相对远端 ``max_payload_bytes`` 的余量（JSON 转义与信封，§6.2 SHOULD 留余量）
_MESSAGE_HEADROOM = 1 << 20


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """用法错误以 64 退出（§7.6），而不是 argparse 缺省的 2（那是 ``failed`` 的轮级码）。"""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="dpe-run", description="DPE connector 独立运行器：同步一轮，或删除实例")
    parser.add_argument("definition", type=Path, help="实例定义文件（connector 契约 §4.4）")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="状态目录（缺省为平台惯例目录，见 dpe_sdk.run.state.default_state_dir）",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="删除实例：注销登记并丢弃本地状态，不触碰 remote（§5.1）",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """命令行入口；返回退出码（§7.6）。

    **信号**（#175）：整个调用期间由这里接管 SIGINT 与 SIGTERM，处理器从不抛出——只记下第一个
    信号，并请求取消正在运行的那一轮（``_Interrupts``）。于是：取消只在下一次 ``await`` 生效，
    同步部分（写报告、删除状态）不会被拦腰打断；第二个信号不会打断插件进程组的清理；报告写完
    之后到进程退出前的信号也不会把进程直接杀掉。调用已经完成（报告已写出 / 删除已完成）就以
    它的结果退出，否则不写报告、以 128 + 信号号退出（§7.6「被信号终止，没有报告」）。
    """
    out = stdout or _utf8_lf(sys.stdout)
    err = stderr or _utf8_lf(sys.stderr)
    env = os.environ if environ is None else environ
    interrupts = _Interrupts()
    with _signals_handled(interrupts):
        code = _run(argv, out, err, env, interrupts)
    return code


def _run(
    argv: Sequence[str] | None,
    out: IO[str],
    err: IO[str],
    env: Mapping[str, str],
    interrupts: _Interrupts,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        err.write(f"{parser.format_usage()}dpe-run: error: {exc}\n")
        return EXIT_USAGE
    # 兜底的边界由结构给出（#175）：这里只兜进入 _sync 之前的阶段（状态目录定位）与删除流程；
    # 同步一轮由 _sync 自己恰好收尾一次（_conclude 从不抛出），不再被这里包住、不会二次收尾。
    # 兜底拿不到扩展过凭证的脱敏器，只记异常类型，不写 traceback 原文（§4.3）
    try:
        state_dir = StateDirectory(args.state_dir or default_state_dir())
    except Exception as exc:
        log = NdjsonLog(err)
        _log_crash(log, exc, detail=False)
        if args.delete:
            return EXIT_INTERNAL
        # 同步一轮仍尽力输出 failed 报告（§7.4「每次调用恰好一个报告」不因缺陷豁免，#147）
        return _report_crash(out, log, exc)
    if interrupts.received is not None:  # 还没开始做事就收到信号：不开始
        return _interrupted(err, interrupts.received)
    try:
        if args.delete:
            try:
                return asyncio.run(
                    _interruptible(_delete(args.definition, state_dir, err), interrupts)
                )
            except Exception as exc:  # 删除流程的缺陷：不输出报告
                _log_crash(NdjsonLog(err), exc, detail=False)
                return EXIT_INTERNAL
        return asyncio.run(
            _interruptible(_sync(args.definition, state_dir, env, out, err), interrupts)
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        # 被取消：调用没有完成（完成了的会正常返回退出码），不写报告
        return _interrupted(err, interrupts.received or signal.SIGINT)


@dataclass
class _Interrupts:
    """本次调用收到的第一个信号，与取消正在运行的那一轮的入口（``_interruptible`` 登记）。"""

    received: signal.Signals | None = None
    cancel: Callable[[], None] | None = None

    def on_signal(self, signum: int, frame: object) -> None:
        """信号处理器：从不抛出；只有第一个信号请求取消，之后的只是重复请求、被忽略。"""
        if self.received is not None:
            return
        self.received = signal.Signals(signum)
        if self.cancel is not None:
            self.cancel()


@contextlib.contextmanager
def _signals_handled(interrupts: _Interrupts) -> Iterator[None]:
    """在调用期间接管 SIGINT / SIGTERM，结束时恢复原处理器（进程内多次调用 main 也安全）。
    非主线程无法设置信号处理器：保持原样（由宿主进程自己的处理决定）。"""
    previous: dict[signal.Signals, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        current = signal.getsignal(sig)
        if current is None:
            # 原处理器不是 Python 装的（嵌入式宿主用 C 装的）：恢复不了，就不接管，保持原样
            continue
        with contextlib.suppress(ValueError, OSError):  # 非主线程等
            signal.signal(sig, interrupts.on_signal)
            previous[sig] = current
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)


async def _interruptible(coro: Coroutine[Any, Any, int], interrupts: _Interrupts) -> int:
    """运行一次调用，并登记「收到信号就取消它」的入口。

    取消沿 ``async with`` / ``with`` 传播：插件宿主关闭插件（含进程组清理）、实例锁释放；
    ``_sync`` 只捕获 ``Exception``，取消不当作本轮结果——不收尾、不写报告。取消经
    ``call_soon_threadsafe`` 投递，只在下一次 ``await`` 时生效：同步部分照常做完，做完了的调用
    正常返回它的退出码。
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None

    def cancel() -> None:
        loop.call_soon_threadsafe(task.cancel)  # 从信号处理器投递到事件循环，线程安全

    interrupts.cancel = cancel
    try:
        if interrupts.received is not None:
            # 登记之前已经收到：不开始这次调用（在任务内部 cancel 只置标记，coro 若一路不让出
            # 就做完了，结果会被丢弃——可能已经写出报告，却以「被中断」退出）
            coro.close()
            raise asyncio.CancelledError
        return await coro
    finally:
        interrupts.cancel = None


def _interrupted(err: IO[str], sig: signal.Signals) -> int:
    """被信号中断：只记一条日志（不打印多行 traceback），以 128 + 信号号退出（如 SIGINT 130）。"""
    NdjsonLog(err).emit("runner.interrupted", "warn", {"signal": sig.name})
    return 128 + int(sig)


def _utf8_lf(stream: IO[str]) -> IO[str]:
    """把标准流固定为 UTF-8、LF 换行（§7.4 报告「UTF-8，LF 结尾」，§7.5 日志同样逐行解析）：
    不随平台 locale、``PYTHONIOENCODING`` 改变编码，Windows 文本模式也不把 ``\n`` 转成
    ``\r\n``。无法编码的字符（如文件名里的孤立代理项）转为反斜杠转义——输出仍是合法 UTF-8 与
    合法 JSON，报告不会因此写不出。"""
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")
    return stream


# ---------------------------------------------------------------------- 运行器调优参数


def runner_options(runner: Mapping[str, Any]) -> tuple[RoundOptions, int | None]:
    """解析 ``definition.runner`` 中认识的键（§4.4：不认识的键忽略，内容不改变契约行为）。

    认识的键取值不合法时为 ``definition_invalid``。返回轮选项与（可选的）``max_message_bytes``。
    """

    def positive_int(key: str) -> int | None:
        value = runner.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InstanceFailure("definition_invalid", f"runner.{key} 必须是正整数")
        return value

    def positive_number(key: str) -> float | None:
        value = runner.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise InstanceFailure("definition_invalid", f"runner.{key} 必须是正数")
        return float(value)

    defaults = RoundOptions()
    retries = runner.get("conflict_retries")
    if retries is not None and (
        isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 2
    ):
        # §7.2：重读与重新提交 SHOULD 不超过 2 次
        raise InstanceFailure("definition_invalid", "runner.conflict_retries 必须是 0–2 的整数")
    options = RoundOptions(
        max_items=positive_int("max_items") or defaults.max_items,
        scan_timeout=positive_number("scan_timeout") or defaults.scan_timeout,
        blob_timeout=positive_number("blob_timeout") or defaults.blob_timeout,
        conflict_retries=defaults.conflict_retries if retries is None else retries,
        state_retries=positive_int("state_retries") or defaults.state_retries,
    )
    return options, positive_int("max_message_bytes")


# ---------------------------------------------------------------------- 同步一轮


@dataclass
class _Outcome:
    """一次「同步一轮」调用的报告素材。"""

    started_at: str
    definition: InstanceDefinition | None = None
    #: 报告与日志的实例信息：完整解析成功时取自定义，否则逐个成员取值（§7.4）
    identity: DefinitionIdentity = field(default_factory=DefinitionIdentity)
    plugin: dict[str, str] | None = None
    protocol_version: str | None = None
    outcome: Outcome = "failed"
    cursor_persisted: bool = False
    cursor_held_since: str | None = None
    counts: RoundCounts = field(default_factory=RoundCounts)
    failures: list[RunFailure] = field(default_factory=list)
    #: 报告中省略的失败明细数（收尾缺陷时的精简报告），计入 failures_truncated
    omitted: int = 0
    #: run_round 就地记下的已确定事实（结束批的 incremental），中途中断时同样可取
    progress: RoundProgress = field(default_factory=RoundProgress)
    #: 轮首 scan 是否带了游标（round.finished 的日志级别据此判断）
    cursor_given: bool = False
    #: 已打开的实例状态（轮开始前失败时用它维护 cursor_held_since）
    store: InstanceStore | None = None
    #: run_round 是否已正常返回（其结果已入账，游标与 cursor_held_since 已由它落盘）
    round_finished: bool = False

    @property
    def incremental(self) -> bool | None:
        """§7.3：取自结束批（唯一来源是 ``progress``）；未收到结束批为 ``None``。"""
        return self.progress.incremental


@dataclass(frozen=True)
class _Prepared:
    """收尾要输出的全部内容，在任何输出之前算好。"""

    line: str
    code: int
    level: LogLevel
    fields: dict[str, Any]


def _prepare(result: _Outcome) -> _Prepared:
    """按最终结果算出轮报告的编码、退出码（§7.6）与 ``round.finished`` 的级别和字段（取自
    报告，§7.5）。只计算、不输出：这里的任何缺陷都发生在输出之前。"""
    identity = result.identity
    report = build_report(
        instance_id=identity.id,
        uri_prefix=identity.uri_prefix,
        remote_url=identity.remote_url,
        plugin=result.plugin,
        protocol_version=result.protocol_version,
        started_at=result.started_at,
        ended_at=utc_now(),
        outcome=result.outcome,
        incremental=result.incremental,
        cursor_persisted=result.cursor_persisted,
        cursor_held_since=result.cursor_held_since,
        counts=result.counts,
        failures=result.failures,
        omitted=result.omitted,
    )
    return _Prepared(
        line=encode_report(report),
        code=exit_code(result.outcome, result.failures),
        level=finished_level(
            result.outcome, cursor_given=result.cursor_given, incremental=result.incremental
        ),
        fields={key: report[key] for key in _FINISHED_FIELDS},
    )


_FINISHED_FIELDS = ("outcome", "incremental", "cursor_persisted", "cursor_held_since", "counts")


def _conclude(result: _Outcome, out: IO[str], log: NdjsonLog) -> int:
    """一次「同步一轮」调用的唯一收尾：恰好一条 ``round.finished``、至多一份轮报告（§7.4、
    §7.5），返回退出码（§7.6）。

    分两段（#169）：先 ``_prepare`` 算好全部输出，再一次性输出。准备阶段出缺陷时还没有任何
    输出——记日志，改用只保留已确定事实的精简结果再准备一次（``_crash_outcome``）；再失败就
    只尽力发出 ``round.finished``、不写报告（74）。输出一旦开始，任何异常都不再引起第二次
    收尾：写不出报告即 74（§7.6 此时不用 70），属运行器缺陷的同时记日志。
    """
    try:
        prepared = _prepare(result)
    except Exception as exc:
        _log_crash(log, exc)
        failed = _crash_outcome(result, exc)
        try:
            prepared = _prepare(failed)
        except Exception as again:
            _log_crash(log, again)
            log.emit("round.finished", "error", _minimal_finished(failed), trusted=True)
            return EXIT_REPORT_UNWRITABLE
    log.emit("round.finished", prepared.level, prepared.fields, trusted=True)
    try:
        write_report(out, prepared.line)
    except (OSError, ValueError):  # stdout 已关闭等
        return EXIT_REPORT_UNWRITABLE
    except Exception as exc:  # 运行器自身的缺陷：记日志，不静默
        _log_crash(log, exc)
        return EXIT_REPORT_UNWRITABLE
    return prepared.code


def _crash_outcome(result: _Outcome, exc: BaseException) -> _Outcome:
    """收尾准备阶段出缺陷时的精简结果：保留实例信息与本轮已确定的事实（``incremental``、
    计数、游标是否已持久化、扣住记录，§7.3 的例外须如实取值），失败明细（最可能是出问题的
    那部分）只列这一个运行器缺陷。"""
    return _Outcome(
        started_at=result.started_at,
        identity=result.identity,
        plugin=result.plugin,
        protocol_version=result.protocol_version,
        progress=result.progress,
        cursor_persisted=result.cursor_persisted,
        cursor_held_since=result.cursor_held_since,
        counts=result.counts,
        cursor_given=result.cursor_given,
        failures=[_internal_error(exc)],
        # 省略的明细如实计数（§7.4 failures_truncated），不让 counts 与 failures 看似矛盾
        omitted=len(result.failures) + result.omitted,
    )


def _minimal_finished(result: _Outcome) -> dict[str, Any]:
    """两次准备都失败时 ``round.finished`` 的字段：不经报告构造，只用已确定的简单值。"""
    return {
        "outcome": "failed",
        "incremental": result.incremental,
        "cursor_persisted": result.cursor_persisted,
        "cursor_held_since": result.cursor_held_since,
        "counts": result.counts.as_dict(),
    }


def _failed(result: _Outcome, log: NdjsonLog, failure: RunFailure) -> None:
    log.failure(failure)
    result.failures.append(failure)
    result.outcome = "failed"


async def _sync(
    path: Path, directory: StateDirectory, environ: Mapping[str, str], out: IO[str], err: IO[str]
) -> int:
    """同步一轮，并自己收尾：运行器缺陷（含锁外的，如取锁时的未预期异常）同样带着已解析的
    实例信息、经已扩展凭证的脱敏器记日志，输出 failed 报告后以 70 退出。"""
    result = _Outcome(started_at=utc_now())
    log = NdjsonLog(err)
    try:
        await _sync_steps(path, directory, environ, result, log)
    except Exception as exc:
        _log_crash(log, exc)
        _failed(result, log, _internal_error(exc))
    return _conclude(result, out, log)


async def _sync_steps(
    path: Path,
    directory: StateDirectory,
    environ: Mapping[str, str],
    result: _Outcome,
    log: NdjsonLog,
) -> None:
    """同步一轮的各步，结果记在 ``result`` 上（运行器缺陷记为 ``runner_internal_error``）。"""

    def failed(failure: RunFailure) -> None:
        _failed(result, log, failure)

    try:
        definition = load_definition(path)
    except InstanceFailure as failure:
        # 报告与日志的 instance 逐个成员取值：只有缺失或不合法的成员为 null（§7.4、§7.5）
        result.identity = peek_identity(path)
        log.instance = result.identity.id
        failed(failure)
        return
    result.definition = definition
    result.identity = DefinitionIdentity(
        definition.id, definition.uri_prefix, definition.remote.url
    )
    log.instance = definition.id
    try:
        lock = directory.lock_instance(definition.id)
    except RoundFailure as failure:
        failed(failure)  # instance_busy 或状态目录故障：未取得锁，不读写 held 记录
        return
    with lock:
        # 轮标识自取得锁起生成：插件启动期的事件归入本轮（§7.5）
        log.begin_round()
        try:
            await _run_locked(definition, directory, environ, result, log)
        except RunFailure as failure:
            failed(failure)
        except Exception as exc:
            # 运行器自身的缺陷：仍按「已取得锁、结果不是 succeeded」收尾——维护
            # cursor_held_since、输出轮报告——再以 70 退出（§7.6「其他非零退出码」）
            _log_crash(log, exc)
            failed(_internal_error(exc))
        if not result.round_finished:
            # 轮未开始（实例级或轮级失败在 run_round 之前）或中途崩溃：已取得锁，同样维护
            # cursor_held_since（§7.4）。本轮已正常收尾时它已由 run_round 落盘，不再改写
            result.cursor_held_since = _hold(result, log)
        if result.store is not None:
            # 游标是否已持久化只有一个来源：收尾落盘本身（#169）。正常收尾、返回之前才中断
            # （§7.3 的例外）都如实取它；轮未开始或中途失败时 hold 按失败轮收尾，必为 false
            result.cursor_persisted = result.store.cursor_persisted


def _report_crash(out: IO[str], log: NdjsonLog, exc: BaseException) -> int:
    """进入 ``_sync`` 之前（或其收尾本身）的缺陷：failed 报告（实例信息不可得为 ``null``、
    counts 全 0、cursor_held_since 为 ``null``）与 ``round.finished``，退出 70；报告本身写不出
    时退出 74（§7.6：此时不用 70）。"""
    result = _Outcome(started_at=utc_now())
    result.failures.append(_internal_error(exc))
    return _conclude(result, out, log)


def _internal_error(exc: BaseException) -> RoundFailure:
    return RoundFailure(
        "runner_internal_error",  # §7.4 运行器自有码（#147）
        f"运行器内部缺陷：{type(exc).__name__}",
        retryable=False,
    )


def _log_crash(log: NdjsonLog, exc: BaseException, *, detail: bool = True) -> None:
    """未预期异常记为单条 NDJSON（``runner.crashed``），traceback 作为字段且经脱敏（§4.3、
    §7.5）——不直接打印多行 traceback，以免破坏日志格式或泄露插件回显的凭证。

    ``detail`` 为 false 时只记异常类型：用于拿不到扩展过凭证的脱敏器的兜底路径——异常消息
    与 traceback 可能带着凭证原文，宁可少记也不外泄（#175）。
    """
    if not detail:
        text = type(exc).__name__
    else:
        try:
            text = "".join(traceback.format_exception(exc))
        except Exception:  # 格式化本身出错：至少留下类型（emit 从不抛出）
            text = type(exc).__name__
    log.emit("runner.crashed", "error", {"traceback": text})


def _hold(result: _Outcome, log: NdjsonLog) -> str | None:
    """轮未正常收尾时维护 ``cursor_held_since``（§7.4，#147）。

    取得锁之后、读到实例状态之前就结束的调用（实例级校验失败、读状态失败）取 ``None``，不读写
    该记录；读到了状态的按 ``InstanceStore.hold``（新实例同样取 ``None``、不写任何内容）。
    收尾那次落盘已经执行过（``run_round`` 已写入游标与该记录，之后才中断）时不再改写。
    已读到记录而写不进去时不影响轮结果、不计入 ``failures``，只记日志（§7.2），报告照常取本轮
    计算出的值。
    """
    store = result.store
    if store is None:
        return None
    if store.round_closed:
        return store.reported_held_since  # 收尾时计算出的值（写失败时未必已落盘）
    try:
        return store.hold(result.started_at)
    except RunFailure as failure:
        failure.redact(log.redactor)
        log.emit("state.held_write_failed", "warn", {"failure": failure.to_report()})
        return store.reported_held_since  # 本轮计算出的值
    except Exception as exc:
        # 写记录时的运行器缺陷：如实记为 runner_internal_error，报告照常取本轮计算出的值
        _log_crash(log, exc)
        _failed(result, log, _internal_error(exc))
        return store.reported_held_since


async def _run_locked(
    definition: InstanceDefinition,
    directory: StateDirectory,
    environ: Mapping[str, str],
    result: _Outcome,
    log: NdjsonLog,
) -> None:
    """取得锁之后的全部步骤；失败以 ``RunFailure`` 抛出（``run_round`` 自己的失败在结果里）。

    顺序（§4.4、§5.2，#147）：本地可判定的实例级校验（清单、配置、调优参数、remote 凭证、插件
    凭证与命令）→ 读实例状态 → 前缀登记（含重叠检查；内容身份切换时更新登记）→ 持久化内容身份
    指纹 → 取 remote 能力 → 启动插件 → 一轮。未通过校验的新实例不留下登记、指纹与任何状态；
    前缀重叠先于网络请求判定，不被 remote 的可用性掩盖。
    """
    manifest = load_manifest(definition.manifest_path)
    check_against_manifest(definition, manifest)
    validate_config(manifest, definition.config)
    options, max_message_bytes = runner_options(definition.runner)
    credential = resolve_remote_credential(definition, environ)
    # 整个 Authorization 值与其中的凭证本体（如 ``Bearer <token>`` 的 token）都脱敏
    remote_secrets = [credential, *credential.split(None, 1)[1:]] if credential else []
    # 先只加 remote 凭证：插件环境构造失败（如 secret_missing）时，日志同样不得泄露它
    log.redactor = log.redactor.extended(remote_secrets)
    plugin_env = build_plugin_env(manifest, definition, environ)
    redactor: Redactor = plugin_env.redactor.extended(remote_secrets)
    log.redactor = redactor
    argv = resolve_command(definition, plugin_env.env)

    store = result.store = directory.open_instance(
        definition.id,
        content_identity(
            remote_url=definition.remote.url,
            uri_prefix=definition.uri_prefix,
            config=definition.config,
            plugin_name=manifest.name,
            plugin_version=manifest.version,
        ),
    )
    # 先更新登记（含前缀变化与重叠检查），再写入新指纹（§4.4：更新登记在写新指纹之前完成）
    directory.register(
        definition.id, remote_url=definition.remote.url, uri_prefix=definition.uri_prefix
    )
    store.commit_identity()

    provider = StaticCredentialProvider(credential) if credential else None
    async with AsyncDpeClient(definition.remote.url, credential=provider) as client:
        await negotiate_capabilities(client)
        limits = (await client.core()).limits
        host = PluginHost(
            max_message_bytes=max(
                max_message_bytes or DEFAULT_MAX_MESSAGE_BYTES,
                limits.max_payload_bytes + _MESSAGE_HEADROOM,
            ),
            redactor=redactor,
            on_stderr=log.plugin_stderr,
            on_started=log.plugin_started,
            on_exited=log.plugin_exited,
        )
        async with host:
            await host.start(argv, plugin_env.env)
            init = await host.initialize(
                manifest=manifest,
                uri_prefix=definition.uri_prefix,
                config=definition.config,
                runner={"name": "dpe-run", "version": dpe_sdk.__version__},
                remote_limits={
                    "max_payload_bytes": limits.max_payload_bytes,
                    "page_max_bytes": limits.page_max_bytes,
                    "blob_max_bytes": limits.blob_max_bytes,
                },
            )
            result.plugin = {"name": init.plugin.name, "version": init.plugin.version}
            result.protocol_version = init.protocol_version

            def on_event(event: str, level: LogLevel, fields: dict[str, Any]) -> None:
                if event == "round.started":
                    # 中途崩溃时 round.finished 的日志级别仍要知道是否给了游标
                    result.cursor_given = bool(fields.get("cursor_given"))
                log.emit(event, level, fields)

            round_result = await run_round(
                host=host,
                pusher=AsyncStatefulPusher(client, state=store),
                store=store,
                uri_prefix=definition.uri_prefix,
                policy=definition.conflict_policy,
                supports_cursor=init.capabilities.supports_cursor,
                blob_max_bytes=limits.blob_max_bytes,
                options=options,
                on_event=on_event,
                redact=redactor,
                counts=result.counts,
                failures=result.failures,
                progress=result.progress,
            )
            # 一返回就入账。此后关闭插件或客户端时才出的运行器缺陷（§7.3 的例外）：outcome
            # 改记 failed、退出 70；counts、cursor_persisted 与按 succeeded 清除的
            # cursor_held_since 照实保留，不再改写 held 记录；round.finished 由 _conclude 按最终
            # 结果发出
            result.round_finished = True
            result.started_at = round_result.started_at
            result.outcome = round_result.outcome
            result.cursor_held_since = round_result.cursor_held_since
            # 计数与失败已就地累计在 result 上；cursor_given 已由 round.started 事件记下


# ---------------------------------------------------------------------- 删除


async def _delete(path: Path, directory: StateDirectory, err: IO[str]) -> int:
    """删除（§5.1 独立运行器的删除，#147）：只从定义取 ``id``；先取实例锁；不解析状态内容，
    直接移除状态，登记最后移除（与登记、重叠检查同用目录级锁）；幂等；不读写 remote；不输出
    轮报告。退出码：0 成功（含无可删除的状态）、2 ``state_unavailable``、3 定义不合法、
    4 ``instance_busy``；运行器缺陷 70、被信号中断 128 + 信号号（在 ``main`` 中处理）。"""
    log = NdjsonLog(err)
    try:
        instance_id = load_definition_id(path)
    except InstanceFailure as failure:
        log.failure(failure)
        return exit_code("failed", [failure])
    log.instance = instance_id
    try:
        with directory.lock_instance(instance_id):
            discarded = directory.discard(instance_id)
            unregistered = directory.unregister(instance_id)  # 登记最后移除
    except RoundFailure as failure:  # instance_busy、取锁 / 移除的 I/O 失败、登记损坏
        log.failure(failure)
        return exit_code("failed", [failure])
    found = discarded or unregistered
    log.emit(
        "instance.deleted",
        "info",
        {
            "found": found,  # 排他锁不计入可删除的状态
            "message": "已移除本地状态与登记（未读写 remote）" if found else "无可删除的状态",
        },
    )
    return EXIT_SUCCEEDED
