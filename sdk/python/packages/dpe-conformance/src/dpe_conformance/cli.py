"""``dpe-conformance`` 命令行入口。

用法::

    dpe-conformance https://host/r/1 [--section smoke] [-o 同 --junit-xml]
        [--junit-xml report.xml] [--counts-json counts.json] [--config PATH]

退出码：0 = 全部通过（skip 不算失败）；1 = 有失败；2 = 用法/配置/运行故障（含「没有任何
检查被执行」——防止章节拼写错误静默假绿）。计数 JSON 的 ``totals.executed`` / ``skipped``
供 CI 断言执行数不低于基线。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dpe_conformance import __version__
from dpe_conformance.config import ConfigError, find_config, load_config
from dpe_conformance.runner import list_sections, run_suite


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpe-conformance",
        description="DPE 服务端一致性跑分器：对任意 remote 黑盒运行协议检查。",
        epilog=f"可用章节：{'、'.join(list_sections())}",
    )
    parser.add_argument(
        "remote", metavar="REMOTE-URL", help="被测 remote 的 base URL（如 https://host/r/1）"
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="配置文件（缺省：CWD 下的 dpe-conformance.toml，存在时加载）",
    )
    parser.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="NAME",
        help="只运行指定章节，可重复；缺省全部（可用章节见 --help 末尾）",
    )
    parser.add_argument("--junit-xml", type=Path, metavar="PATH", help="JUnit XML 的输出路径")
    parser.add_argument(
        "--counts-json", type=Path, metavar="PATH", help="「已执行 / 跳过」计数 JSON 的输出路径"
    )
    parser.add_argument("--version", action="version", version=f"dpe-conformance {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    parsed = urlparse(args.remote)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _fail(f"REMOTE-URL 必须是 http(s) 绝对 URL，实际 {args.remote!r}")

    try:
        config_path = find_config(args.config)
        if config_path is not None:
            load_config(config_path)  # 提前校验：配置错误在开跑前以退出码 2 报告
    except ConfigError as exc:
        return _fail(str(exc))

    available = list_sections()
    unknown = [name for name in args.section if name not in available]
    if unknown:
        return _fail(f"未知章节 {'、'.join(unknown)}（可用：{'、'.join(available)}）")
    sections = list(args.section) or list(available)
    if not sections:
        return _fail("套件里没有任何章节")

    with tempfile.TemporaryDirectory(prefix="dpe-conformance-") as tmp:
        counts_path = Path(tmp) / "counts.json"
        code, counts, message = run_suite(
            target=args.remote,
            config_path=config_path,
            sections=sections,
            junit_xml=args.junit_xml,
            counts_path=counts_path,
        )
        if counts is None:
            return _fail(message or "套件未产出计数")
        if args.junit_xml is not None and not args.junit_xml.is_file():
            return _fail(f"JUnit XML 未写出：{args.junit_xml}")
        if args.counts_json is not None:
            args.counts_json.write_text(
                json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    _print_summary(args.remote, sections, counts)
    if message is not None:
        print(f"dpe-conformance: {message}", file=sys.stderr)
    return code


def _print_summary(remote: str, sections: Sequence[str], counts: Mapping[str, Any]) -> None:
    totals = counts["totals"]
    print(f"目标    {remote}")
    print(f"章节    {'、'.join(sections)}")
    print(
        f"执行 {totals['executed']}"
        f"（通过 {totals['passed']}，失败 {totals['failed']}，错误 {totals['errors']}）"
        f"，跳过 {totals['skipped']}"
    )
    if totals.get("collection_errors"):
        print(
            f"收集错误 {totals['collection_errors']}（章节未能导入/收集；详见运行输出与 JUnit XML）"
        )
    per_section = counts.get("sections", {})
    if len(per_section) > 1:
        for name in sorted(per_section):
            bucket = per_section[name]
            print(f"  {name}: 执行 {bucket['executed']}，跳过 {bucket['skipped']}")


def _fail(message: str) -> int:
    print(f"dpe-conformance: {message}", file=sys.stderr)
    return 2
