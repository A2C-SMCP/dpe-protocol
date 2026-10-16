"""插件启动包装（#237）：以子进程运行插件、原样继承 stdio，退出时把它的 RSS 峰值（KiB）写进文件。

用法：``python rsswrap.py <输出文件> <插件命令…>``。``SIGTERM`` 转给插件（运行器取消时），退出码
与插件相同（被信号终止时为 128 + 信号号）。只用标准库，运行器把它当成插件本身来启动。
"""

from __future__ import annotations

import resource
import signal
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    out, command = Path(argv[0]), argv[1:]
    proc = subprocess.Popen(command)
    signal.signal(signal.SIGTERM, lambda signum, frame: proc.terminate())
    code = proc.wait()
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss：macOS 以字节计，Linux 以 KiB 计
    out.write_text(str(peak // 1024 if sys.platform == "darwin" else peak), encoding="utf-8")
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
