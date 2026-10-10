"""进程入口：stdio 上的 NDJSON 循环（connector 契约 §6.1–§6.3）。

- ``stdin`` 逐行读请求，``stdout`` 只写协议消息（每条立即冲刷；不得混入日志或 BOM）；
- 诊断日志一律写 ``stderr``（运行器持续排空并转发，§6.1、§7.5）；
- 响应 ``shutdown`` 后以退出码 0 退出；读到 ``stdin`` EOF 也尽快退出（运行器崩溃后不残留孤儿
  进程，§6.3）；SIGTERM 同样干净退出（运行器宽限期内的 TERM 不应留下半截输出）。

**单行崩溃不拖垮进程**：请求处理的意外异常（插件的缺陷）按 JSON-RPC 的 ``-32603`` 如实应答并
继续；只有失败发生在 ``initialize`` 时进程进入致命状态（与运行器的「握手失败即终止实例」对齐，
§7.1 协议级），此后对任何请求一律回错、不产出内容——否则半初始化的进程会让运行器把后续失败
误判为普通错误，实例状态不可诊断。

退出码：正常路径一律 0（轮结果由运行器裁定）；``initialize`` 失败等实例级失败在协议层以错误
响应表达，进程仍以 0 退出——退出码语义属于运行器（§7.6）。
"""

from __future__ import annotations

import json
import logging
import signal
import sys

from dpe_git_connector.server import Serve

__all__ = ["main"]


def _configure_logging() -> None:
    """stderr 上的行日志；stdout 保留给协议（§6.1）。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("dpe_git_connector")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def _reconfigure_utf8(stream: object) -> None:
    """把 stdio 流显式切到 UTF-8；非文本流（如被重定向为管道时仍多是）不可配就跳过。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def _error_line(code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def main() -> int:
    _configure_logging()
    # 协议是 UTF-8（§6.2）；显式重配，避免平台默认编码（LANG 非 UTF-8 时读写都会崩）
    _reconfigure_utf8(sys.stdin)
    _reconfigure_utf8(sys.stdout)

    def _on_signal(signum: int, _frame: object) -> None:
        # 抛 SystemExit 唤醒阻塞中的 stdin 读：只置标志位不会被检查（PEP 475 会自动重试读）
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    serve = Serve()
    for line in sys.stdin:
        line = line.rstrip("\n")
        response: str | None
        try:
            # 空行是协议级违例（§6.2），运行器会终止实例；插件不吞掉它，原样报解析错误
            response = _error_line(-32700, "解析错误") if not line else serve.handle(line)
        except Exception as exc:  # 单行的意外异常不得带崩进程（§6.5 条目级失败继续本轮）
            logging.getLogger("dpe_git_connector").exception("处理请求时未预期异常")
            response = _error_line(-32603, f"插件内部错误：{type(exc).__name__}: {exc}")
        if response is not None:
            sys.stdout.write(response)
            sys.stdout.write("\n")
            sys.stdout.flush()
        if serve.shutdown_requested or serve.fatal:
            break
    return 0
