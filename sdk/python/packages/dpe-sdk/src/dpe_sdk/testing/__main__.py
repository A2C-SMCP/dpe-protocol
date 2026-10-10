"""独立监听的参考服务端：``python -m dpe_sdk.testing``（需要 extra ``server``，即 uvicorn）。

供外部进程（Rust SDK 集成测试 #25、conformance 跑分器）当作 HTTP 对手使用。绑定端口后向 stdout
打印一行 JSON ``{"url": "<remote base URL>"}``（启用钩子时另带 ``"hooks"``：钩子通道的 base URL），
之后 stdout 不再输出（日志走 stderr）；``--port 0`` 由系统分配空闲端口，调用方从这一行读取实际
地址。SIGINT / SIGTERM 正常退出。

一致性测试钩子（#45）全部在这里的启动参数中设定，不进入 DPE 协议面：

- ``--hooks PATH``：启用钩子通道（保留路径，如 ``/__hooks__``；缺省关闭）；
- ``--grant AUTH PREFIX…`` / ``--force AUTH``：多身份与按 URI 前缀的写授权（``AUTH`` 即
  ``Authorization`` 头原值，缺省放行一切）；
- ``--contract NAME``：同时声明多个 hash 契约（第一个为主契约）；
- ``--session-id-prefix P``：可预测的顺序会话 id（``P0``、``P1``…）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import socket
import sys
from collections.abc import Callable, Sequence
from typing import Any

from dpe_sdk.testing._engine import Engine, EngineConfig, PrefixAuthorizer
from dpe_sdk.testing._hooks import AdjustableClock, HooksConfig
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
        "--public-url", help="remote 的对外 base URL（反向代理之后时，也用于公布地址）"
    )
    parser.add_argument("--log-level", default="warning", help="uvicorn 日志级别（缺省 warning）")
    parser.add_argument(
        "--hooks", metavar="PATH", help="启用一致性测试钩子的保留路径，如 /__hooks__（缺省关闭）"
    )
    parser.add_argument(
        "--grant",
        nargs="+",
        action="append",
        metavar=("AUTH", "PREFIX"),
        help="凭证（Authorization 头原值）与其可写前缀，可重复；前缀用 * 表示任意",
    )
    parser.add_argument(
        "--force", action="append", metavar="AUTH", help="有 force 权限的凭证，可重复"
    )
    parser.add_argument(
        "--contract",
        action="append",
        metavar="NAME",
        help="声明的 hash 契约，可重复（第一个为主契约；缺省只声明 dpe1）",
    )
    parser.add_argument(
        "--session-id-prefix", metavar="P", help="会话 id 用顺序编号 P0、P1…（缺省随机）"
    )
    for name in _LIMITS:
        parser.add_argument(f"--{name.replace('_', '-')}", type=int, dest=name)
    return parser


def _authorizer(grants: list[list[str]], forces: list[str]) -> PrefixAuthorizer:
    """``--grant AUTH PREFIX…`` 与 ``--force AUTH`` → ``PrefixAuthorizer``（``*`` 前缀 = 任意）。

    同一凭证出现多条 ``--grant`` 时前缀并集（可重复参数不互相覆盖）。
    """
    prefixes: dict[str, tuple[str, ...]] = {}
    for entry in grants:
        auth, *values = entry
        if not values:
            raise ValueError(f"--grant {auth!r} 至少需要一个前缀（* 表示任意）")
        granted = tuple("" if value == "*" else value for value in values)
        prefixes[auth] = (*prefixes.get(auth, ()), *granted)
    unknown = [auth for auth in forces if auth not in prefixes]
    if unknown:
        raise ValueError(f"--force 的凭证 {unknown[0]!r} 没有对应的 --grant（不会生效）")
    return PrefixAuthorizer(prefixes, frozenset(forces))


def _sequential_ids(prefix: str) -> Callable[[], str]:
    counter = itertools.count()
    return lambda: f"{prefix}{next(counter)}"


def _authority(host: str, port: int, family: socket.AddressFamily) -> str:
    """公布的 authority：监听通配地址时换成可连接的回环地址，IPv6 加方括号。"""
    if family == socket.AF_INET6:
        loopback = "::1" if host in ("::", "::0", "0:0:0:0:0:0:0:0") else host
        return f"[{loopback}]:{port}"
    return f"{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"


def _announcement(
    args: argparse.Namespace,
    host: str,
    port: int,
    family: socket.AddressFamily,
    hooks: HooksConfig | None,
) -> dict[str, str]:
    """公布的一行 JSON：``url`` 是 remote 的 base URL，启用钩子时另给 ``hooks``。

    ``--public-url`` 的语义是 remote 的**对外 base URL**（含 prefix，与 ``create_app`` 一致），
    原样公布；钩子挂在应用根（prefix 之外），要从它摘掉 prefix 才推得出来——摘不掉时（反向代理
    改写了路径）只提示到 stderr，不猜一个错的地址。
    """
    origin = f"http://{_authority(host, port, family)}"
    root: str | None = origin
    if args.public_url is None:
        url = f"{origin}{args.prefix}"
    else:
        url = args.public_url
        if not args.prefix:
            root = args.public_url
        elif args.public_url.endswith(args.prefix):
            root = args.public_url[: -len(args.prefix)]
        else:
            root = None
            if hooks is not None:
                print(
                    "--public-url 不以 --prefix 结尾，推不出钩子通道的地址（钩子挂在应用根下）",
                    file=sys.stderr,
                )
    announcement = {"url": url}
    if hooks is not None and root is not None:
        announcement["hooks"] = f"{root}{hooks.path}"
    return announcement


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
    clock = AdjustableClock()
    hooks = None if args.hooks is None else HooksConfig(path=args.hooks, clock=clock)
    options: dict[str, Any] = {n: getattr(args, n) for n in _LIMITS if getattr(args, n) is not None}
    if hooks is not None:
        options["clock"] = clock  # 拨钟要拨到引擎用的那个时钟
    if args.contract:
        options["contracts"] = tuple(args.contract)
    if args.session_id_prefix is not None:
        options["session_id_factory"] = _sequential_ids(args.session_id_prefix)
    try:
        if args.grant:
            options["authorizer"] = _authorizer(args.grant, args.force or [])
        elif args.force:
            raise ValueError("--force 需要同时给出对应的 --grant")
        engine = Engine(EngineConfig(**options))
        app = create_app(engine, prefix=args.prefix, public_url=args.public_url, hooks=hooks)
    except ValueError as exc:
        parser.error(str(exc))

    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    # 先 listen 再公布地址：公布之后到达的连接进入 backlog，不会被拒绝
    sock.listen(128)
    host, port = sock.getsockname()[:2]
    if hooks is not None and args.host not in ("127.0.0.1", "::1", "localhost"):
        # 钩子通道不做认证：绑定非回环地址等于把「绕过授权写入 + 拨钟」暴露出去，只告警不改默认
        print(
            f"警告：钩子通道已启用且监听 {args.host}——它不做认证，请只在受控网络中使用",
            file=sys.stderr,
        )
    print(json.dumps(_announcement(args, host, port, family, hooks)), flush=True)

    config = uvicorn.Config(app, lifespan="on", log_level=args.log_level, access_log=False)
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
