"""按脱敏 profile 生成合成 Jira 数据（#237 §2）：确定性、与生成顺序无关。

- 每个工单用独立的随机源（种子 = ``<seed>:<工单 id>``），同一 profile + 种子 + 规模得到逐字节相同的
  数据；改动某个工单（工作负载）不影响其他工单的生成结果；
- 数值按 profile 的分位数做分段线性的逆 CDF 采样；文本从固定词表拼出指定长度（中英混排，与真实
  工单的字符分布接近，内容无意义）；
- 正文按 profile 的元素类别占比生成 ADF（段落、标题、列表、代码块、表格、媒体），经插件自己的
  ADF 转换后得到相应类别的元素；
- 附件字节不常驻内存（``LazyBlobs``）：按（附件 id, 大小）现算，图片带 PNG 文件头。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.fake import BenchJira, LazyBlobs

__all__ = [
    "DEFAULT_PROFILE",
    "TIERS",
    "Dataset",
    "Sampler",
    "SynthProject",
    "build",
    "load_profile",
    "make_comment",
    "make_issue",
]

DEFAULT_PROFILE = Path(__file__).resolve().parent / "profiles" / "profile-a.json"
#: 规模档：工单数 → 项目数（#237 §2）
TIERS: dict[int, int] = {20: 1, 200: 1, 2000: 5, 20000: 20}
#: 项目 key：PA、PB、…（中性名字，不对应任何真实项目）
_PROJECT_KEYS = [f"P{chr(ord('A') + i)}" for i in range(26)]
_WORDS = (
    "数据",
    "任务",
    "调度",
    "失败",
    "重试",
    "节点",
    "配置",
    "字段",
    "同步",
    "表",
    "分区",
    "延迟",
    "告警",
    "修复",
    "验证",
    "上线",
    "回滚",
    "依赖",
    "接口",
    "权限",
    "日志",
    "指标",
    "采集",
    "清洗",
    "合并",
    "导出",
    "备份",
    "版本",
    "发布",
    "测试",
    "需求",
    "评审",
    "the",
    "job",
    "table",
    "schema",
    "partition",
    "retry",
    "timeout",
    "node",
    "config",
    "sync",
    "export",
    "metric",
    "alert",
    "fix",
    "check",
    "deploy",
    "rollback",
    "pipeline",
    "query",
    "index",
    "cache",
    "worker",
    "queue",
    "batch",
    "stream",
    "spark",
    "hive",
    "kafka",
    "sql",
    "api",
)
_ISSUE_TYPES = [("10001", "Task"), ("10002", "Bug"), ("10003", "Story"), ("10004", "Sub-task")]
_STATUSES = [("1", "To Do"), ("3", "In Progress"), ("10001", "In Review"), ("10002", "Done")]
_PRIORITIES = [("1", "Highest"), ("2", "High"), ("3", "Medium"), ("4", "Low")]
_RESOLUTIONS = [("10000", "Done"), ("10001", "Won't Do")]
_PEOPLE = [(f"acct-{i:03d}", f"成员{i:02d}") for i in range(24)]
_LABELS = [f"label-{i}" for i in range(12)]
_EPOCH = "2026-09-01T08:00:00.000+0000"


def load_profile(path: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    profile: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return profile


class Sampler:
    """一个工单的随机源 + profile：分位数逆 CDF 采样与文本生成。"""

    def __init__(self, profile: dict[str, Any], seed: str) -> None:
        self.profile = profile
        self.points: list[float] = profile["quantiles"]
        self.rng = random.Random(seed)

    def value(self, quantiles: list[float]) -> float:
        """分段线性逆 CDF：在相邻分位点之间线性插值。"""
        u = self.rng.random()
        for i in range(1, len(self.points)):
            if u <= self.points[i]:
                lo, hi = self.points[i - 1], self.points[i]
                t = 0.0 if hi == lo else (u - lo) / (hi - lo)
                return quantiles[i - 1] + t * (quantiles[i] - quantiles[i - 1])
        return quantiles[-1]

    def count(self, quantiles: list[float]) -> int:
        return max(0, round(self.value(quantiles)))

    def chance(self, rate: float) -> bool:
        return self.rng.random() < rate

    def text(self, length: int) -> str:
        """恰好 ``length`` 个字符的文本（至少 1 个）。"""
        length = max(1, length)
        parts: list[str] = []
        size = 0
        while size < length:
            word = self.rng.choice(_WORDS)
            parts.append(word)
            size += len(word) + 1
        return " ".join(parts)[:length]

    def pick(self, options: list[tuple[str, str]]) -> dict[str, str]:
        option_id, name = self.rng.choice(options)
        return {"id": option_id, "name": name}


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _block(sampler: Sampler, category: str, length: int) -> dict[str, Any]:
    """一个 ADF 块，经插件的 ADF 转换后产出 ``category`` 类别的元素。"""
    if category == "Title":
        return {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": sampler.text(length)}],
        }
    if category == "ListItem":
        return {
            "type": "bulletList",
            "content": [{"type": "listItem", "content": [_paragraph(sampler.text(length))]}],
        }
    if category == "CodeSnippet":
        return {
            "type": "codeBlock",
            "attrs": {"language": "sql"},
            "content": [{"type": "text", "text": sampler.text(length)}],
        }
    if category == "Table":
        cells = [sampler.text(max(1, length // 4)) for _ in range(4)]
        return {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "content": [_paragraph(cell)]} for cell in row
                    ],
                }
                for row in (cells[:2], cells[2:])
            ],
        }
    if category == "Image":
        media_id = f"{sampler.rng.getrandbits(64):016x}"
        return {
            "type": "mediaSingle",
            "content": [{"type": "media", "attrs": {"id": media_id, "type": "file", "alt": ""}}],
        }
    return _paragraph(sampler.text(length))


def _adf(sampler: Sampler, blocks: int, lengths: list[float], share: dict[str, float]) -> Any:
    if blocks == 0:
        return None
    categories = list(share) or ["NarrativeText"]
    weights = [share[c] for c in categories] if share else [1.0]
    content = [
        _block(sampler, sampler.rng.choices(categories, weights)[0], sampler.count(lengths))
        for _ in range(blocks)
    ]
    return {"type": "doc", "version": 1, "content": content}


def _stamp(minutes: int) -> str:
    """基准时刻之后若干分钟的 Jira 时间戳（确定性，不取当前时间）。"""
    day, rest = divmod(minutes, 24 * 60)
    hour, minute = divmod(rest, 60)
    month_day = 1 + day % 28
    month = 9 + (day // 28) % 3
    return f"2026-{month:02d}-{month_day:02d}T{hour:02d}:{minute:02d}:00.000+0000"


def make_comment(sampler: Sampler, comment_id: int, minute: int) -> dict[str, Any]:
    profile = sampler.profile["comments"]
    blocks = max(1, sampler.count(profile["elements_per_comment"]))
    author_id, author = sampler.rng.choice(_PEOPLE)
    body = _adf(sampler, blocks, profile["text_length"], {"NarrativeText": 1.0})
    return {
        "id": str(comment_id),
        "author": {"accountId": author_id, "displayName": author},
        "body": body,
        "created": _stamp(minute),
        "updated": _stamp(minute),
    }


def make_issue(
    profile: dict[str, Any],
    seed: int,
    issue_id: int,
    key: str,
    project: SynthProject,
    blobs: LazyBlobs,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """一个合成工单（Jira 响应形态）与它的评论；附件大小登记进 ``blobs``。"""
    sampler = Sampler(profile, f"{seed}:{issue_id}")
    body = profile["body"]
    fields_profile = profile["fields"]
    rates: dict[str, float] = fields_profile["present_rate"]
    lists: dict[str, list[float]] = fields_profile["list_length"]
    created_minute = issue_id % 5000
    fields: dict[str, Any] = {
        "summary": sampler.text(sampler.count(profile["summary_length"])),
        "description": _adf(
            sampler, sampler.count(body["elements"]), body["text_length"], body["category_share"]
        ),
        "created": _stamp(created_minute),
        "updated": _stamp(created_minute + 1),
    }
    if sampler.chance(rates.get("issuetype", 1.0)):
        fields["issuetype"] = sampler.pick(_ISSUE_TYPES)
    if sampler.chance(rates.get("status", 1.0)):
        fields["status"] = sampler.pick(_STATUSES)
    if sampler.chance(rates.get("priority", 0.0)):
        fields["priority"] = sampler.pick(_PRIORITIES)
    for person in ("assignee", "reporter"):
        if sampler.chance(rates.get(person, 0.0)):
            account, name = sampler.rng.choice(_PEOPLE)
            fields[person] = {"accountId": account, "displayName": name}
    if sampler.chance(rates.get("labels", 0.0)):
        count = max(1, sampler.count(lists["labels"]))
        fields["labels"] = sampler.rng.sample(_LABELS, min(count, len(_LABELS)))
    for name, pool in (("components", project.components), ("fixVersions", project.versions)):
        if pool and sampler.chance(rates.get(name, 0.0)):
            count = min(len(pool), max(1, sampler.count(lists[name])))
            fields[name] = [
                {"id": c["id"], "name": c["name"]} for c in sampler.rng.sample(pool, count)
            ]
    if sampler.chance(rates.get("parent", 0.0)):
        # 父工单：同项目前 10 个工单之一（Epic 一类的少数父工单承载多数子工单）
        fields["parent"] = {"key": f"{project.key}-{1 + sampler.rng.randrange(10)}"}
    if sampler.chance(rates.get("resolution", 0.0)):
        fields["resolution"] = sampler.pick(_RESOLUTIONS)
    if sampler.chance(rates.get("duedate", 0.0)):
        fields["duedate"] = f"2026-12-{1 + issue_id % 28:02d}"
    attachments = []
    attachment_profile = profile["attachments"]
    for n in range(sampler.count(attachment_profile["per_issue"])):
        attachment_id = str(issue_id * 100 + n)
        size = max(1, sampler.count(attachment_profile["size_bytes"]))
        image = sampler.chance(attachment_profile["image_share"])
        name = f"file-{attachment_id}.{'png' if image else 'pdf'}"
        blobs.sizes[attachment_id] = size
        attachments.append(
            {
                "id": attachment_id,
                "filename": name,
                "mimeType": "image/png" if image else "application/pdf",
                "size": size,
                "content": f"/rest/api/3/attachment/content/{attachment_id}",
            }
        )
    if attachments:
        fields["attachment"] = attachments
    comments = [
        make_comment(sampler, issue_id * 10000 + n, created_minute + 2 + n)
        for n in range(sampler.count(profile["comments"]["per_issue"]))
    ]
    return {"id": str(issue_id), "key": key, "fields": fields}, comments


@dataclass
class SynthProject:
    key: str
    id: str
    components: list[dict[str, Any]]
    versions: list[dict[str, Any]]


def render_blob(attachment_id: str, size: int) -> bytes:
    """附件字节：PNG 文件头 + 由附件 id 决定的填充，长度恰为 ``size``。"""
    head = b"\x89PNG\r\n\x1a\n"
    filler = (attachment_id.encode() + b"-") * (size // (len(attachment_id) + 1) + 1)
    return (head + filler)[:size]


@dataclass
class Dataset:
    """一份合成数据：模拟 Jira 本体、项目 key 列表与生成参数（工作负载据此构造变化）。"""

    jira: BenchJira
    projects: list[str]
    profile: dict[str, Any]
    seed: int
    blobs: LazyBlobs
    meta: dict[str, SynthProject]

    def next_issue_id(self) -> int:
        return max(int(i) for i in self.jira.issues) + 1


def build(profile: dict[str, Any], issues: int, projects: int, seed: int = 237) -> Dataset:
    """生成 ``issues`` 个工单，平均分到 ``projects`` 个项目（项目各带 5 个组件、6 个版本）。"""
    blobs = LazyBlobs(render_blob)
    jira = BenchJira(blobs=blobs)
    jira.projects = {}
    synth: list[SynthProject] = []
    for index in range(projects):
        key = _PROJECT_KEYS[index]
        project_id = str(10000 + index)
        components = [
            {"id": str(project_id_base), "name": f"{key}-组件{n}", "description": ""}
            for n, project_id_base in enumerate(
                range(int(project_id) * 10, int(project_id) * 10 + 5)
            )
        ]
        versions = [
            {
                "id": str(int(project_id) * 10 + 5 + n),
                "name": f"v0.{n}.0",
                "released": n < 4,
                "archived": False,
            }
            for n in range(6)
        ]
        jira.projects[key] = {
            "id": project_id,
            "key": key,
            "name": f"项目 {key}",
            "description": "合成项目",
        }
        jira.components[key] = components
        jira.versions[key] = versions
        synth.append(SynthProject(key, project_id, components, versions))
    for n in range(issues):
        project = synth[n % projects]
        issue_id = 100001 + n
        key = f"{project.key}-{n // projects + 1}"
        issue, comments = make_issue(profile, seed, issue_id, key, project, blobs)
        jira.issues[issue["id"]] = issue
        jira.comments[issue["id"]] = comments
    return Dataset(jira, [p.key for p in synth], profile, seed, blobs, {p.key: p for p in synth})
