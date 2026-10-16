"""对真实 Jira 只读采样，产出合成数据用的**脱敏 profile**（#237 §2）。

只读：只调用搜索、取工单与取评论（与插件同一个 ``JiraClient``），不写回 Jira。工单经插件自己的
映射（``issue_document``）转成元素后再统计，所以 profile 描述的就是 DPE 层面看到的形状。

**脱敏**：profile 只含数值分布——各量的分位数、比例与非空率；不含任何原文、key、项目名、人名，
也不含状态 / 标签 / 组件等取值的名称与工单总数。范围（JQL）只在命令行给出，不写进输出。

用法::

    uv run python -m bench.profile --site https://<站点> --jql '<范围>' \\
        --email-file ~/.config/dpe-jira/email --token-file ~/.config/dpe-jira/token \\
        --out bench/profiles/profile-a.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dpe_jira.client import JiraClient
from dpe_jira.mapping import ISSUE_FIELDS, issue_document

__all__ = ["QUANTILES", "build_profile", "main", "quantiles"]

#: 分位点：合成时按分段线性的逆 CDF 采样
QUANTILES: tuple[float, ...] = (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0)
#: 字段页里统计非空率的字段（与 ``mapping._FIELDS`` 同序）
FIELDS: tuple[str, ...] = (
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "resolution",
    "duedate",
)


def quantiles(values: list[float]) -> list[float]:
    """``QUANTILES`` 处的取值（最近秩法）；空列表全为 0。"""
    if not values:
        return [0.0 for _ in QUANTILES]
    ordered = sorted(values)
    last = len(ordered) - 1
    return [float(ordered[round(q * last)]) for q in QUANTILES]


def _share(counter: Counter[str]) -> dict[str, float]:
    total = sum(counter.values())
    return {key: round(value / total, 4) for key, value in sorted(counter.items())} if total else {}


def _elements(document: dict[str, Any], section: str) -> list[dict[str, Any]]:
    for page in document["pages"]:
        if page["page_metadata"]["section"] == section:
            elements: list[dict[str, Any]] = page["elements"]
            return elements
    return []


def build_profile(samples: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any]:
    """（工单全文, 全部评论）列表 → 脱敏 profile。"""
    summary_len: list[float] = []
    body_elements: list[float] = []
    body_text_len: list[float] = []
    body_kinds: Counter[str] = Counter()
    comments_per_issue: list[float] = []
    elements_per_comment: list[float] = []
    comment_text_len: list[float] = []
    attachments_per_issue: list[float] = []
    attachment_size: list[float] = []
    image_attachments = 0
    attachments_total = 0
    present: Counter[str] = Counter()
    list_lengths: dict[str, list[float]] = {"labels": [], "components": [], "fixVersions": []}
    for issue, comments in samples:
        document = issue_document("bench://profile/x", issue, comments)
        fields = issue["fields"]
        summary = fields.get("summary")
        summary_len.append(len(summary) if isinstance(summary, str) else 0)
        body = _elements(document, "body")
        body_elements.append(len(body))
        for element in body:
            body_kinds[element["category"]] += 1
            body_text_len.append(len(element.get("text", "")))
        comments_per_issue.append(len(comments))
        per_comment: Counter[str] = Counter()
        for element in _elements(document, "comments"):
            per_comment[element["metadata"]["comment_id"]] += 1
            comment_text_len.append(len(element.get("text", "")))
        elements_per_comment.extend(float(n) for n in per_comment.values())
        attachments = _elements(document, "attachments")
        attachments_per_issue.append(len(attachments))
        for element in attachments:
            attachments_total += 1
            if element["category"] == "Image":
                image_attachments += 1
                attachment_size.append(element["blob"]["size"])
            else:
                attachment_size.append(element["metadata"]["size"])
        for element in _elements(document, "fields"):
            field = element["metadata"]["field"]
            present[field] += 1
            if field in list_lengths:
                list_lengths[field].append(len(element["metadata"]["value"]))
    count = len(samples)
    return {
        "profile_version": 1,
        "quantiles": list(QUANTILES),
        "summary_length": quantiles(summary_len),
        "body": {
            "elements": quantiles(body_elements),
            "text_length": quantiles(body_text_len),
            "category_share": _share(body_kinds),
        },
        "comments": {
            "per_issue": quantiles(comments_per_issue),
            "elements_per_comment": quantiles(elements_per_comment),
            "text_length": quantiles(comment_text_len),
        },
        "attachments": {
            "per_issue": quantiles(attachments_per_issue),
            "size_bytes": quantiles(attachment_size),
            "image_share": round(image_attachments / attachments_total, 4)
            if attachments_total
            else 0.0,
        },
        "fields": {
            "present_rate": {f: round(present[f] / count, 4) if count else 0.0 for f in FIELDS},
            "list_length": {name: quantiles(values) for name, values in list_lengths.items()},
        },
    }


def _all_comments(client: JiraClient, issue_id: str) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    while True:
        page = client.comments(issue_id, len(comments))
        batch = page["comments"]
        comments.extend(batch)
        if not batch or len(comments) >= page["total"]:
            return comments


def sample(client: JiraClient, jql: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """范围内全部工单的全文与评论（只读）。"""
    ids: list[str] = []
    token: str | None = None
    while True:
        page = client.search(f"({jql}) ORDER BY id ASC", ["key"], token)
        ids.extend(issue["id"] for issue in page["issues"])
        token = page.get("nextPageToken")
        if not token:
            break
    samples = []
    for number, issue_id in enumerate(ids, 1):
        samples.append((client.issue(issue_id, ISSUE_FIELDS), _all_comments(client, issue_id)))
        if number % 50 == 0:
            print(f"已读 {number}/{len(ids)}", file=sys.stderr)
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.profile", description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--jql", required=True, help="采样范围（不写进输出）")
    parser.add_argument("--email-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    email = args.email_file.expanduser().read_text(encoding="utf-8").strip()
    token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
    profile = build_profile(sample(JiraClient(args.site, email, token), args.jql))
    args.out.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
