"""把 ``results.json`` 渲染成 Markdown 对比表（#237 §5）：每个规模档、每个工作负载一张两列表。

只渲染「能量化的差异」；结论与「结构性差异」由人写在报告正文里（数字之外的判断不自动生成）。
"""

from __future__ import annotations

from typing import Any

from bench.workloads import WORKLOADS

__all__ = ["render"]

#: 表格行：(指标键, 显示名)
_ROWS: tuple[tuple[str, str], ...] = (
    ("outcome", "结果"),
    ("incremental", "增量轮"),
    ("seconds", "耗时（秒）"),
    ("dpe_requests", "DPE 请求数"),
    ("dpe_writes", "DPE 写请求数"),
    ("dpe_request_bytes", "DPE 请求体字节（解压后）"),
    ("jira_requests", "Jira 请求数"),
    ("jira_response_bytes", "Jira 响应字节"),
    ("jira_429", "Jira 429 次数"),
    ("doc_hash_changes", "doc_hash 变化的文档数"),
    ("new_pages", "新出现的 page_hash 数"),
    ("new_contents", "新出现的 content_hash 数"),
    ("impacted_issues", "失败波及的工单数"),
    ("plugin_rss_kib", "插件 RSS 峰值（KiB）"),
    ("cursor_bytes", "游标字节数"),
    ("server_docs", "服务端文档数"),
    ("server_pages", "服务端 page 对象数"),
    ("server_contents", "服务端 content 对象数"),
)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _table(modes: dict[str, dict[str, Any]]) -> list[str]:
    issue, project = modes.get("issue", {}), modes.get("project", {})
    if "skipped" in issue or "skipped" in project:
        return [f"跳过：{issue.get('skipped') or project.get('skipped')}", ""]
    lines = ["| 指标 | 工单 = 文档 | 项目 = 文档 |", "| --- | ---: | ---: |"]
    for key, label in _ROWS:
        if key in issue or key in project:
            lines.append(f"| {label} | {_cell(issue.get(key))} | {_cell(project.get(key))} |")
    latency = {**issue.get("latency_seconds", {}), **project.get("latency_seconds", {})}
    if latency:
        lines.append(
            "| 变更可见延迟（秒） | "
            f"{_cell(next(iter(issue.get('latency_seconds', {}).values()), None))} | "
            f"{_cell(next(iter(project.get('latency_seconds', {}).values()), None))} |"
        )
    failures = [
        f"{mode}：" + "、".join(sorted({f["code"] for f in result.get("failures", [])}))
        for mode, result in (("工单", issue), ("项目", project))
        if result.get("failures")
    ]
    if failures:
        lines.append("")
        lines.append("失败：" + "；".join(failures))
    return [*lines, ""]


def render(results: dict[str, Any]) -> str:
    meta = results["meta"]
    lines = [
        "# Jira 映射粒度基准：工单 = 文档 vs 项目 = 文档（数据表）",
        "",
        f"- 种子 `{meta['seed']}`，profile `{meta['profile']}`，生成于 {meta['generated_at']}；",
        f"- 环境：{meta['platform']}，Python {meta['python']}；",
        "- 参考服务端缺省限额（单请求体 8 MiB），运行器缺省单条消息上限（32 MiB）；",
        "- 两种模式都产出项目信息：工单模式另有每个项目一篇项目文档（正式插件的缺省行为）。",
        "",
    ]
    for tier, workloads in results["tiers"].items():
        lines += [f"## {int(tier):,} 个工单", ""]
        for name, modes in workloads.items():
            lines += [f"### {name} {WORKLOADS.get(name, '')}", "", *_table(modes)]
    scale = results.get("scale") or []
    if scale:
        lines += [
            "## W13 单文档规模（仅项目模式，单个项目逐档放大）",
            "",
            "| 工单数 | 结果 | 耗时（秒） | DPE 请求体字节 | 服务端 page 对象数 | 失败 |",
            "| ---: | --- | ---: | ---: | ---: | --- |",
        ]
        for row in scale:
            codes = "、".join(sorted({f["code"] for f in row.get("failures", [])})) or "—"
            lines.append(
                f"| {row['issues']:,} | {row['outcome']} | {_cell(row['seconds'])} | "
                f"{_cell(row['dpe_request_bytes'])} | {_cell(row['server_pages'])} | {codes} |"
            )
        lines.append("")
    return "\n".join(lines)
