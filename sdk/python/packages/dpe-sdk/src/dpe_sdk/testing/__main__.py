"""独立监听的参考服务端：``python -m dpe_sdk.testing``（需要 extra ``server``，即 uvicorn）。

供外部进程（Rust SDK 集成测试 #25、conformance 跑分器）当作 HTTP 对手使用。绑定端口后向 stdout
打印一行 JSON ``{"url": "<remote base URL>"}``，之后 stdout 不再输出（日志走 stderr）；
``--port 0`` 由系统分配空闲端口，调用方从这一行读取实际地址。SIGINT / SIGTERM 正常退出。

一致性测试钩子（多身份授权、短 TTL、多契约等）由 #45 接入，这里只开放限额。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Sequence
from typing import Any

from dpe_sdk.testing._engine import Engine, EngineConfig
from dpe_sdk.testing._http import create_app

#: 可由命令行覆盖的限额（HTTP 绑定 §4.1），缺省取 ``EngineConfig`` 的默认值
_LIMITS = (
    "max_payload_bytes",
    "page_max_bytes",
    "staging_ttl_seconds",
    "blob_max_bytes",
    "blob_chunk_bytes",
    "batch_head_max",
    "list_page_max",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dpe_sdk.testing", description="DPE 内存版参考服务端（HTTP 绑定）"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（缺省 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 由系统分配（缺省 0）")
    parser.add_argument("--prefix", default="", help="remote 的路径前缀，如 /r/42（缺省为根）")
    parser.add_argument(
        "--public-url", help="remote 的对外 base URL（反向代理之后时，用于 Content-Location）"
    )
    parser.add_argument("--log-level", default="warning", help="uvicorn 日志级别（缺省 warning）")
    for name in _LIMITS:
        parser.add_argument(f"--{name.replace('_', '-')}", type=int, dest=name)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print(
            "独立监听需要 uvicorn：pip install 'dpe-sdk[server]'",
            file=sys.stderr,
        )
        return 2
    limits: dict[str, Any] = {n: getattr(args, n) for n in _LIMITS if getattr(args, n) is not None}
    try:
        engine = Engine(EngineConfig(**limits))
        app = create_app(engine, prefix=args.prefix, public_url=args.public_url)
    except ValueError as exc:
        parser.error(str(exc))

    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    # 先 listen 再公布地址：公布之后到达的连接进入 backlog，不会被拒绝
    sock.listen(128)
    host, port = sock.getsockname()[:2]
    authority = f"[{host}]:{port}" if family == socket.AF_INET6 else f"{host}:{port}"
    print(json.dumps({"url": f"http://{authority}{args.prefix}"}), flush=True)

    config = uvicorn.Config(app, lifespan="on", log_level=args.log_level, access_log=False)
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
