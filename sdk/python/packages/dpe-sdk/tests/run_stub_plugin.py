"""桩插件：供 ``test_run_host`` 以真实子进程驱动 ``PluginHost``。

用法：``python run_stub_plugin.py <mode>``。正常握手的身份为 ``stub 1.0``；``echo`` 方法原样
返回 params，``shutdown`` 响应后以 0 退出，读到 stdin EOF 即退出。各 mode 只改变下列行为：

- ``version_mismatch`` / ``invalid_config``：initialize 以 -32004 / -32005 拒绝；
- ``pick_unknown_version``：选定运行器未提供的版本；``identity_mismatch``：自报 version 9.9；
- ``oversize`` / ``bad_framing`` / ``empty_line`` / ``unsolicited`` / ``wrong_id``：响应 echo 时
  输出超限行 / 非 JSON 行 / 空行 / 主动通知 / 未发出的 id；
- ``crash`` / ``exit0``：收到 echo 时以 3 / 0 退出，不响应；
- ``env_probe``：initialize 时把环境变量名（JSON 数组）与 ``STUB_TOKEN`` 的值写到 stderr；
- ``stderr_flood``：initialize 时向 stderr 写超长行、非 UTF-8 字节与含凭证值的行；
- ``hang_cancel``：第一个 echo 挂起，收到 cancel 后以 -32001 响应；
- ``hang_deaf``：第一个 echo 挂起且不理 cancel；下一个 echo 先补发它的迟到响应；
- ``stubborn``：忽略 SIGTERM、shutdown 与 stdin EOF，并留一个持有 stdout 的子进程（pid 写 stderr）；
- ``crash_orphan``：initialize 时派生持有 stdio 的子进程，收到 echo 时以 3 退出；
- ``deaf_stdin``：initialize 响应后不再读 stdin；
- ``bad_init``：initialize 结果缺少 ``plugin``；``init_hang``：initialize 挂起，收到 cancel 后以
  -32001 响应；
- ``stderr_edge``：向 stderr 写一行，凭证 ``SECRETVALUE1234567890`` 跨 8 KiB 截断边界；
- ``deep_error``：响应 echo 时返回 data 嵌套 800 层、内含 ``SECRETVALUE`` 的错误；
- ``leak_error``：响应 echo 时返回 message 与 data 都含 ``SECRETVALUE`` 的错误。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

MODE = sys.argv[1]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(id_: Any, value: Any) -> None:
    send({"jsonrpc": "2.0", "id": id_, "result": value})


def error(id_: Any, code: int, message: str, data: Any = None) -> None:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    send({"jsonrpc": "2.0", "id": id_, "error": err})


def raw(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def initialize(id_: Any, params: dict[str, Any]) -> None:
    if MODE == "version_mismatch":
        error(id_, -32004, "no common version")
        return
    if MODE == "invalid_config":
        error(id_, -32005, "repo does not exist")
        return
    if MODE == "env_probe":
        sys.stderr.write(json.dumps(sorted(os.environ)) + "\n")
        sys.stderr.write("token=" + os.environ.get("STUB_TOKEN", "") + "\n")
        sys.stderr.flush()
    if MODE == "stderr_flood":
        sys.stderr.buffer.write(b"x" * 20000 + b"\n")
        sys.stderr.buffer.write(b"bad \xff\xfe bytes\n")
        sys.stderr.buffer.write(b"leak SECRETVALUE end\n")
        sys.stderr.buffer.write(b"tail without newline")
        sys.stderr.flush()
    if MODE == "init_hang":
        return
    if MODE == "bad_init":
        result(id_, {"protocol_version": params["protocol_versions"][0]})
        return
    if MODE == "stderr_edge":
        sys.stderr.write("y" * 8180 + "SECRETVALUE1234567890" + "z" * 100 + "\n")
        sys.stderr.flush()
    if MODE in ("stubborn", "crash_orphan"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        sys.stderr.write(f"child={child.pid}\n")
        sys.stderr.flush()
    if MODE == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    version = params["protocol_versions"][0]
    if MODE == "pick_unknown_version":
        version = "dpe-connector/9"
    result(
        id_,
        {
            "protocol_version": version,
            "plugin": {"name": "stub", "version": "9.9" if MODE == "identity_mismatch" else "1.0"},
            "capabilities": {"supports_cursor": True, "future_capability": 1},
        },
    )


def main() -> None:
    max_bytes = 0
    hung: Any = None
    hung_before = False
    for line in sys.stdin:
        message = json.loads(line)
        method, id_, params = message.get("method"), message.get("id"), message.get("params", {})
        if method == "initialize":
            max_bytes = params["max_message_bytes"]
            initialize(id_, params)
            if MODE == "init_hang":
                hung = id_
            if MODE == "deaf_stdin":
                time.sleep(120)
        elif method == "cancel":
            if MODE in ("hang_cancel", "init_hang") and hung == params["id"]:
                error(hung, -32001, "cancelled")
                hung = None
        elif method == "shutdown":
            if MODE == "stubborn":
                continue
            result(id_, {})
            sys.exit(0)
        elif method == "echo":
            if MODE == "oversize":
                raw(b'{"pad":"' + b"x" * max_bytes + b'"}\n')
            elif MODE == "bad_framing":
                raw(b"not json\n")
            elif MODE == "empty_line":
                raw(b"\n")
            elif MODE == "unsolicited":
                send({"jsonrpc": "2.0", "method": "log", "params": {}})
            elif MODE == "wrong_id":
                result(id_ + 100, params)
            elif MODE == "deep_error":
                nested: Any = "SECRETVALUE"
                for _ in range(800):
                    nested = [nested]
                error(id_, -32003, "deep", nested)
            elif MODE == "leak_error":
                error(id_, -32603, "token SECRETVALUE", {"detail": ["x SECRETVALUE"]})
            elif MODE in ("crash", "crash_orphan"):
                sys.exit(3)
            elif MODE == "exit0":
                sys.exit(0)
            elif MODE in ("hang_cancel", "hang_deaf") and not hung_before:
                hung, hung_before = id_, True
            else:
                if MODE == "hang_deaf" and hung is not None:
                    result(hung, {"late": True})
                    hung = None
                result(id_, params)
        elif method == "fail":
            error(id_, params["code"], "requested failure", params.get("data"))
        else:
            error(id_, -32601, "method not found")
    if MODE == "stubborn":
        time.sleep(120)


if __name__ == "__main__":
    main()
