"""基准测试入口（#237）：一条命令复跑。

    uv run python -m bench run --tiers 200,2000 --out <目录>          # W1–W12，两种模式
    uv run python -m bench run --tiers 200 --workloads W1,W3 --out …  # 只跑部分工作负载
    uv run python -m bench run --tiers none --scale 1000,5000,20000 --out …  # 只跑 W13
    uv run python -m bench real --site https://<站点> --project <KEY> \\
        --email-file <路径> --token-file <路径> --out <目录>  # 真实 Jira 只读校准（W1、W2）

``run`` 产出 ``<目录>/results.json`` 与 ``<目录>/report.md``；``real`` 只产出数字（失败条目不带
URI），结果留在本机，不要提交（规模是内部信息）。
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench._harness import reference_server
from bench.report import render
from bench.runner import Instance, Mode
from bench.synth import DEFAULT_PROFILE, TIERS, load_profile
from bench.workloads import run_scale, run_tier

#: 真实校准时一轮的最多尝试次数（暂时性失败后重跑）
_REAL_ATTEMPTS = 3


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _ints(text: str) -> list[int]:
    return [] if text in ("", "none") else [int(part) for part in text.split(",")]


def _meta(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "seed": args.seed,
        "profile": Path(args.profile).name,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
    }


def _run(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.profile))
    only = set(args.workloads.split(",")) | {"W1"} if args.workloads else None
    results: dict[str, Any] = {"meta": _meta(args), "tiers": {}, "scale": []}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dpe-jira-bench-") as tmp:
        for tier in _ints(args.tiers):
            projects = TIERS.get(tier, max(1, tier // 1000))
            _log(f"== {tier} 个工单 / {projects} 个项目")
            results["tiers"][str(tier)] = run_tier(
                Path(tmp) / str(tier), profile, tier, projects, args.seed, only, _log
            )
            _write(out, results)
        if args.scale:
            results["scale"] = run_scale(
                Path(tmp) / "scale", profile, _ints(args.scale), args.seed, _log
            )
            _write(out, results)
    _log(f"结果：{out / 'results.json'}，报告：{out / 'report.md'}")
    return 0


def _write(out: Path, results: dict[str, Any]) -> None:
    """每个档跑完就落盘：长跑中途中断也留下已完成的部分。"""
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(render(results), encoding="utf-8")


def _real(args: argparse.Namespace) -> int:
    """真实 Jira 只读校准：两种模式各跑首轮全量与无变化轮，remote 是本机参考服务端。"""
    secrets = {
        "JIRA_EMAIL": {"file": str(Path(args.email_file).expanduser().resolve())},
        "JIRA_API_TOKEN": {"file": str(Path(args.token_file).expanduser().resolve())},
    }
    rows: dict[str, Any] = {}
    modes: tuple[Mode, ...] = ("issue", "project")
    with tempfile.TemporaryDirectory(prefix="dpe-jira-real-") as tmp:
        for mode in modes:
            with reference_server("--prefix", "/r/1") as server:
                directory = Path(tmp) / mode
                directory.mkdir()
                instance = Instance(
                    directory,
                    mode,
                    None,
                    args.site,
                    server.url,
                    [args.project],
                    secrets=secrets,
                    runner={"scan_timeout": args.scan_timeout},
                )
                try:
                    for name in ("W1", "W2"):
                        rows.setdefault(name, {})[mode] = _real_round(instance, mode, name)
                finally:
                    instance.close()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "real.json").write_text(
        json.dumps({"meta": _meta(args), "rounds": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _log(f"结果：{out / 'real.json'}（只留在本机，不要提交）")
    return 0


def _real_round(instance: Instance, mode: str, name: str) -> dict[str, Any]:
    """真实 Jira 上的一轮：暂时性失败（网络、限流）按运营的做法重跑，至多 ``_REAL_ATTEMPTS`` 次。

    全量轮中止后游标不推进，重跑仍是全量（已推上去的文档按 hash 跳过）；记下每次尝试的结果与
    累计耗时——首轮没有中间进度本身就是要量的成本。
    """
    attempts: list[dict[str, Any]] = []
    for _ in range(_REAL_ATTEMPTS):
        stats = instance.round()
        result = stats.as_dict()
        # 只留数字：失败条目的 URI 带真实 key
        result["failures"] = [
            {k: v for k, v in f.items() if k != "file_uri"} for f in result["failures"]
        ]
        attempts.append(result)
        _log(f"{mode} {name}: {stats.outcome} {stats.seconds:.1f}s")
        for failure in stats.failures[:5]:  # 诊断只进本机日志（可能带真实 key）
            _log(f"  {failure.get('code')}: {failure.get('message')}")
        retryable = all(f.get("retryable") for f in stats.failures)
        if stats.outcome == "succeeded" or not retryable:
            break
    return {
        **attempts[-1],
        "attempts": len(attempts),
        "attempt_outcomes": [a["outcome"] for a in attempts],
        "total_seconds": round(sum(a["seconds"] for a in attempts), 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="合成数据：W1–W12（两种模式）与 W13")
    run.add_argument("--tiers", default="200,2000", help="工单数，逗号分隔；none 即不跑")
    run.add_argument("--workloads", default="", help="只跑这些工作负载（W1 总会跑）")
    run.add_argument("--scale", default="", help="W13 的工单数档，逗号分隔")
    run.add_argument("--profile", default=str(DEFAULT_PROFILE))
    run.add_argument("--seed", type=int, default=237)
    run.add_argument("--out", required=True)
    real = sub.add_parser("real", help="真实 Jira 只读校准（W1、W2）")
    real.add_argument("--site", required=True)
    real.add_argument("--project", required=True)
    real.add_argument("--email-file", required=True)
    real.add_argument("--token-file", required=True)
    real.add_argument(
        "--scan-timeout",
        type=float,
        default=3600,
        help="运行器单次 scan 的时限（秒）；缺省 300 秒不够真实网络下组装大批次，两种模式取同一值",
    )
    real.add_argument("--profile", default=str(DEFAULT_PROFILE))
    real.add_argument("--seed", type=int, default=237)
    real.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return _run(args) if args.command == "run" else _real(args)


if __name__ == "__main__":
    raise SystemExit(main())
