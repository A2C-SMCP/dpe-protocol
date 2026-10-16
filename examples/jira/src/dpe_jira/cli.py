"""进程入口：stdio 上的 NDJSON 循环（connector 契约 §6.1–§6.3）。

``stdin`` 逐行读请求，``stdout`` 只写协议消息（每条立即冲刷），诊断写 ``stderr``；响应
``shutdown`` 后、或读到 ``stdin`` EOF、或收到 SIGTERM 时以退出码 0 退出（§6.3）。单个请求的
意外异常以 ``-32603`` 应答，不带崩进程。
"""

from __future__ import annotations

import json
import signal
import sys
import traceback

from dpe_jira.server import Serve

__all__ = ["main"]


def _error(code: int, message: str, id_: object = None) -> str:
    error = {"code": code, "message": message}
    return json.dumps({"jsonrpc": "2.0", "id": id_, "error": error}, ensure_ascii=False)


def _request_id(line: str) -> object:
    """尽力取出请求 id：内部错误的应答也回显它，运行器才能对上是哪个请求。"""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message.get("id") if isinstance(message, dict) else None


def main(serve: Serve | None = None) -> int:
    """``serve`` 缺省为正式插件（``Serve``）；基准测试的变体传入自己的实例（#237）。"""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")  # 协议是 UTF-8（§6.2）

    def stop(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    serve = Serve() if serve is None else serve
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        try:
            # 空行是分帧违例（§6.2）：如实报解析错误，运行器按协议级处理
            response = serve.handle(line) if line else _error(-32700, "解析错误")
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = _error(-32603, f"插件内部错误：{type(exc).__name__}", _request_id(line))
        if response is not None:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
        if serve.shutdown_requested or serve.fatal:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
