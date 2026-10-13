"""独立运行器的输出（connector 契约 §7.4–§7.6，独立运行器规范档）。

- **轮报告**（§7.4）：同步一轮的调用结束时，向 stdout 写出恰好一个单行紧凑 JSON 对象；
  其他实例操作（如删除）不输出轮报告（#147），结果由退出码与日志表达。
- **结构化日志**（§7.5）：stderr 上的 NDJSON，每行含 ``ts`` / ``level`` / ``event`` /
  ``instance`` / ``round``；全部文本经 ``Redactor`` 脱敏（§4.3「不外泄」）。
- **退出码**（§7.6）：``succeeded`` 0、``partial`` 1、实例级 3、``instance_busy`` 4、其余
  轮级 / 协议级（含 ``state_unavailable``）2、运行器内部缺陷 70；同一次调用出现多类失败时按
  优先序 70 > 3 > 2 > 4 > 1 取（``exit_code``）。用法错误 64 由命令行自己处理。
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Sequence
from typing import IO, Any

from dpe_sdk.run.errors import RunFailure
from dpe_sdk.run.round import LogLevel, Outcome, RoundCounts, utc_now
from dpe_sdk.run.secrets import Redactor

__all__ = [
    "EXIT_INSTANCE",
    "EXIT_INSTANCE_BUSY",
    "EXIT_INTERNAL",
    "EXIT_PARTIAL",
    "EXIT_ROUND",
    "EXIT_SUCCEEDED",
    "EXIT_USAGE",
    "NdjsonLog",
    "build_report",
    "exit_code",
]

EXIT_SUCCEEDED = 0
EXIT_PARTIAL = 1
EXIT_ROUND = 2
EXIT_INSTANCE = 3
EXIT_INSTANCE_BUSY = 4
#: 运行器内部缺陷（``runner_internal_error``，§7.6「其他非零退出码」，同 BSD ``EX_SOFTWARE``）
EXIT_INTERNAL = 70
#: 命令行用法错误（同 BSD ``EX_USAGE``），不输出轮报告
EXIT_USAGE = 64

_REPORT_VERSION = 1
#: 报告中 ``failures`` 的上限，超出部分只计数（``failures_truncated``）
_MAX_REPORTED_FAILURES = 1000


def exit_code(outcome: Outcome, failures: Sequence[RunFailure]) -> int:
    """§7.6：按轮结果与本次调用的全部失败给出退出码，多类失败按优先序 70 > 3 > 2 > 4 > 1 取。

    运行器缺陷最先暴露、不被其他失败掩盖；实例级失败须改定义、清单或凭证才能恢复，先于其余
    轮级与协议级失败；``instance_busy`` 只在未取得锁时出现；``partial`` 最低。条目级失败只
    决定 ``partial``，不参与比较。
    """
    if any(failure.code == "runner_internal_error" for failure in failures):
        return EXIT_INTERNAL
    if any(failure.level == "instance" for failure in failures):
        return EXIT_INSTANCE
    if outcome == "succeeded":
        return EXIT_SUCCEEDED
    if outcome == "partial":
        return EXIT_PARTIAL
    if any(
        failure.level in ("round", "protocol") and failure.code != "instance_busy"
        for failure in failures
    ):
        return EXIT_ROUND
    if any(failure.code == "instance_busy" for failure in failures):
        return EXIT_INSTANCE_BUSY
    return EXIT_ROUND


def build_report(
    *,
    instance_id: str | None,
    uri_prefix: str | None,
    remote_url: str | None,
    plugin: dict[str, str] | None,
    protocol_version: str | None,
    started_at: str,
    ended_at: str,
    outcome: Outcome,
    incremental: bool | None,
    cursor_persisted: bool,
    cursor_held_since: str | None,
    counts: RoundCounts,
    failures: Sequence[RunFailure],
) -> dict[str, Any]:
    """§7.4 的轮报告（字段齐全；失败过多时截断并给出被省略的条数）。"""
    report: dict[str, Any] = {
        "report_version": _REPORT_VERSION,
        "instance": {"id": instance_id, "uri_prefix": uri_prefix},
        "remote_url": remote_url,
        "plugin": plugin,
        "protocol_version": protocol_version,
        "started_at": started_at,
        "ended_at": ended_at,
        "outcome": outcome,
        "incremental": incremental,
        "cursor_persisted": cursor_persisted,
        "cursor_held_since": cursor_held_since,
        "counts": counts.as_dict(),
        "failures": [failure.to_report() for failure in failures[:_MAX_REPORTED_FAILURES]],
    }
    if len(failures) > _MAX_REPORTED_FAILURES:
        report["failures_truncated"] = len(failures) - _MAX_REPORTED_FAILURES
    return report


def write_report(stream: IO[str], report: dict[str, Any]) -> None:
    """单行紧凑 JSON（UTF-8、LF 结尾）写到 stdout；stdout 不输出其他内容。"""
    stream.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


class NdjsonLog:
    """§7.5 的结构化日志：每条一行紧凑 JSON 写到 stderr，写前经 ``redactor`` 脱敏。

    ``instance`` 为实例 ``id``（定义无法解析或缺少合法的 ``id`` 时为 ``null``，与轮报告一致）；
    ``round`` 在 ``begin_round`` 之后为本次调用的不透明标识——同步一轮自取得实例锁起生成，
    插件启动期的事件归入本轮；取锁之前与删除操作的事件为 ``null``（§7.5）。
    """

    def __init__(self, stream: IO[str], redactor: Redactor | None = None) -> None:
        self._stream = stream
        self.redactor = redactor or Redactor()
        self.instance: str | None = None
        self.round: str | None = None

    def begin_round(self) -> str:
        self.round = uuid.uuid4().hex
        return self.round

    def emit(self, event: str, level: LogLevel, fields: dict[str, Any] | None = None) -> None:
        line: dict[str, Any] = {
            "ts": utc_now(),
            "level": level,
            "event": event,
            "instance": self.instance,
            "round": self.round,
        }
        if fields:
            # 附加字段不得覆盖日志行自己的字段（item.failed 的失败元素整体嵌套在 failure 里，
            # 其中的 level 是失败层级，与日志级别不同，#147）
            extra = self.redactor.redact_json(fields)
            line.update({k: v for k, v in extra.items() if k not in line})
        # 日志通道坏了（如采集端关掉管道）不得改变本轮的结果、报告与退出码
        with contextlib.suppress(OSError, ValueError):
            self._stream.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._stream.flush()

    def failure(self, failure: RunFailure) -> None:
        """记一条失败（任何层级都用 ``item.failed``，§7.5）；失败元素（§7.4）整体嵌套在
        ``failure`` 字段里，字段原样不改名（#147）。"""
        failure.redact(self.redactor)
        level: LogLevel = "warn" if failure.level == "item" else "error"
        self.emit("item.failed", level, {"failure": failure.to_report()})

    # 宿主回调（PluginHost 的 on_started / on_exited / on_stderr）

    def plugin_started(self, pid: int) -> None:
        self.emit("plugin.started", "info", {"pid": pid})

    def plugin_exited(self, exit_code: int | None, signal: int | None) -> None:
        level: LogLevel = "info" if exit_code == 0 else "warn"
        self.emit("plugin.exited", level, {"exit_code": exit_code, "signal": signal})

    def plugin_stderr(self, line: str, truncated: bool) -> None:
        self.emit("plugin.stderr", "info", {"line": line, "truncated": truncated})
