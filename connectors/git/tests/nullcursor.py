"""透明包装插件（#213 场景）：标志文件存在时把结束批（无 ``next``）的 ``cursor`` 改成 ``null``，
模拟「插件本轮不给游标、运行器保留旧游标」（connector §6.4 / §6.8）；其余报文原样转发。

用法：``python nullcursor.py <标志文件> <插件命令…>``。标志文件路径走 argv——插件的环境变量受
connector §4.3 的白名单约束，不能靠环境变量传。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path


def main() -> int:
    flag = Path(sys.argv[1])
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    stdin = child.stdin

    def pump() -> None:
        for line in sys.stdin.buffer:
            stdin.write(line)
            stdin.flush()
        stdin.close()

    threading.Thread(target=pump, daemon=True).start()
    for line in child.stdout:
        if flag.exists() and line.strip():
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            result = message.get("result") if isinstance(message, dict) else None
            if isinstance(result, dict) and "next" not in result and result.get("cursor"):
                result["cursor"] = None
                line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
