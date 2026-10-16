"""13 个工作负载（#237 §3）：两种模式各在一套全新的参考服务端、同一份合成数据上依次执行。

为免每个工作负载都重做一次首轮全量，同一模式的 W1–W12 在同一个实例上**顺序**执行：每个工作负载
从上一个结束时的收敛状态出发；W9a、W9b 之后（以及 W10 若失败）跑一轮恢复轮（不计分）回到收敛
状态；W11 的计分轮本身就是恢复；W12 留下的冲突在 ``report`` 策略下不会自愈，所以排在最后，并在
它之后源再变一次、再跑一轮（``W12-after``），实测冲突之后的变化能否推上去。W13 只对项目模式、
单独按规模逐档放大。所有变化都经模拟 Jira 的数据修改表达（与真实 Jira 一样会更新 ``updated``）。

两种模式自始至终用 ``conflict_policy.commit = report``：只有 W12 会出现他人写入，其余工作负载
不受影响；这样 W12 不必中途改实例定义（策略变化会被运行器当作配置变化而改跑全量轮）。
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dpe_sdk import Document
from dpe_sdk.protocol import BaseHash
from dpe_sdk.transport import DpeClient
from fake_jira import serving

from bench._harness import reference_server
from bench.runner import Instance, Mode, RoundStats, snapshot
from bench.synth import Dataset, Sampler, build, make_comment, make_issue

__all__ = ["WORKLOADS", "run_scale", "run_tier"]

#: 工作负载编号 → 一句话说明（报告用）
WORKLOADS: dict[str, str] = {
    "W1": "首轮全量",
    "W2": "无变化轮",
    "W3": "1 个工单加 1 条评论",
    "W4": "批量改 50 个工单的状态",
    "W5": "同一批 20 个工单连续 10 轮改状态（累计）",
    "W6": "热点工单：2,000 条评论后再加 1 条",
    "W7": "新建 1 个、删除 1 个工单",
    "W8": "1 个工单换项目",
    "W9a": "1 个工单在枚举后读到 404（另有 9 个工单变化）",
    "W9b": "1 个工单读取返回 5xx（另有 9 个工单变化）",
    "W10": "Jira 限流（每秒 10 个请求）下跑 W3",
    "W11": "写入生效、确认前崩溃，源回到原状后重跑",
    "W12": "他人改写 remote 上的 1 个工单（所在文档），源随后也改",
    "W12-after": "W12 之后：被改写的工单与同项目另一个工单再变",
}
_POLICY = {"commit": "report", "delete": "report"}
_RATE = 10.0
_HOT_COMMENTS = 2000


@dataclass
class _Clock:
    """改动用的 ``updated`` 时间戳：严格递增、确定性（不取当前时间）。"""

    tick: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def next(self) -> str:
        n = next(self.tick)
        day, minute = divmod(n, 24 * 60)
        month, date = 1 + day // 28 % 12, 1 + day % 28
        return f"2027-{month:02d}-{date:02d}T{minute // 60:02d}:{minute % 60:02d}:00.000+0000"


class _Source:
    """在模拟 Jira 上表达源变化（每次变化都更新 ``updated``）。"""

    def __init__(self, dataset: Dataset) -> None:
        self.ds = dataset
        self.jira = dataset.jira
        self.clock = _Clock()
        self.comment_ids = itertools.count(900_000_000)
        self.status_seq = itertools.count(1)

    def issue_ids(self, project: str | None = None) -> list[str]:
        ids = sorted(self.jira.issues, key=int)
        if project is None:
            return ids
        return [i for i in ids if self.jira.issues[i]["key"].rsplit("-", 1)[0] == project]

    def key(self, issue_id: str) -> str:
        return str(self.jira.issues[issue_id]["key"])

    def add_comments(self, issue_id: str, count: int = 1) -> None:
        sampler = Sampler(self.ds.profile, f"{self.ds.seed}:comment:{issue_id}:{count}")
        for _ in range(count):
            comment = make_comment(sampler, next(self.comment_ids), 0)
            stamp = self.clock.next()
            comment["created"] = comment["updated"] = stamp
            self.jira.comments[issue_id].append(comment)
        self.jira.touch(int(issue_id), self.clock.next())

    def set_status(self, issue_ids: list[str]) -> None:
        """把这些工单改成一个全新的状态（合成数据与之前的改动里都没出现过）：每次都是真实的内容
        变化——取值没变的「改动」不改变内容，插件与运行器正确地不写入，就测不到要测的东西。"""
        n = next(self.status_seq)
        status = {"id": str(30000 + n), "name": f"Bench {n}"}
        for issue_id in issue_ids:
            self.jira.touch(int(issue_id), self.clock.next(), status=status)

    def create(self, project: str) -> str:
        issue_id = self.ds.next_issue_id()
        numbers = [int(self.key(i).rsplit("-", 1)[1]) for i in self.issue_ids(project)]
        key = f"{project}-{max(numbers, default=0) + 1}"
        issue, comments = make_issue(
            self.ds.profile, self.ds.seed, issue_id, key, self.ds.meta[project], self.ds.blobs
        )
        issue["fields"]["updated"] = self.clock.next()
        self.jira.issues[issue["id"]] = issue
        self.jira.comments[issue["id"]] = comments
        return str(issue["id"])

    def move(self, issue_id: str, project: str) -> None:
        numbers = [int(self.key(i).rsplit("-", 1)[1]) for i in self.issue_ids(project)]
        self.jira.rekey(
            int(issue_id), f"{project}-{max(numbers, default=0) + 1}", self.clock.next()
        )


def _doc_uri(instance: Instance, source: _Source, issue_id: str) -> str:
    """工单所在文档的 URI：工单模式是工单文档，项目模式是项目文档。"""
    key = source.key(issue_id)
    if instance.mode == "issue":
        return f"{instance.prefix}issue/{key}"
    return f"{instance.prefix}project/{key.rsplit('-', 1)[0]}"


def _issues_in(source: _Source, instance: Instance, uri: str) -> int:
    """一篇文档承载的工单数（失败影响面）。"""
    if instance.mode == "issue":
        return 1 if "/issue/" in uri else 0
    project = uri.rsplit("/", 1)[-1]
    return len(source.issue_ids(project))


def _impact(source: _Source, instance: Instance, stats: RoundStats) -> int:
    uris = {f["file_uri"] for f in stats.failures if "file_uri" in f}
    return sum(_issues_in(source, instance, uri) for uri in uris)


def _foreign_rewrite(remote: str, uri: str) -> None:
    """另一个写入者：带 CAS 用一篇不同的内容覆盖（W12）。"""
    with DpeClient(remote) as client:
        base = client.head(uri)
        assert base is not None, uri
        document = Document.model_validate(
            {
                "file_type": "jira_issue",
                "pages": [{"elements": [{"category": "NarrativeText", "text": "他人写入"}]}],
            }
        )
        client.commit(uri, document, BaseHash(base))


Result = dict[str, Any]


def _sum(rounds: list[RoundStats]) -> Result:
    """多轮累计：可加的指标求和，状态类指标取最后一轮。"""
    total = rounds[-1].as_dict()
    for key in (
        "seconds",
        "docs_added",
        "docs_removed",
        "dpe_requests",
        "dpe_writes",
        "dpe_request_bytes",
        "jira_requests",
        "jira_response_bytes",
        "jira_429",
        "doc_hash_changes",
        "docs_changed",
        "new_pages",
        "new_contents",
    ):
        total[key] = sum(r.as_dict()[key] for r in rounds)
        if key == "seconds":
            total[key] = round(total[key], 3)
    total["rounds"] = len(rounds)
    total["plugin_rss_kib"] = max((r.plugin_rss_kib or 0) for r in rounds)
    return total


def _run_mode(
    tmp: Path, mode: Mode, dataset: Dataset, only: set[str] | None, log: Callable[[str], None]
) -> dict[str, Result]:
    source = _Source(dataset)
    results: dict[str, Result] = {}

    def want(name: str) -> bool:
        return only is None or name in only

    with serving(dataset.jira) as site, reference_server("--prefix", "/r/1") as server:
        instance = Instance(
            tmp / mode, mode, dataset.jira, site, server.url, dataset.projects, policy=_POLICY
        )
        (tmp / mode).mkdir(parents=True, exist_ok=True)
        try:

            def measure(name: str, stats: RoundStats, **extra: Any) -> None:
                results[name] = {**stats.as_dict(), **extra}
                log(f"{mode} {name}: {stats.outcome} {stats.seconds:.1f}s")

            def recover() -> None:
                stats = instance.round()
                if stats.outcome != "succeeded":
                    raise RuntimeError(f"{mode} 恢复轮失败：{stats.report}")

            ids = source.issue_ids()
            measure("W1", instance.round())
            if want("W2"):
                measure("W2", instance.round())
            target = ids[len(ids) // 2]
            if want("W3"):
                source.add_comments(target)
                uri = _doc_uri(instance, source, target)
                measure("W3", instance.round(watch=(uri,)))
            if want("W4"):
                batch = ids[: min(50, len(ids))]
                source.set_status(batch)
                uri = _doc_uri(instance, source, batch[-1])
                measure("W4", instance.round(watch=(uri,)))
            if want("W5"):
                batch = ids[-min(20, len(ids)) :]
                rounds = []
                for _ in range(10):
                    source.set_status(batch)
                    rounds.append(instance.round())
                results["W5"] = _sum(rounds)
                log(f"{mode} W5: {rounds[-1].outcome}")
            if want("W6"):
                hot = ids[1]
                source.add_comments(hot, _HOT_COMMENTS)
                setup = instance.round()
                source.add_comments(hot)
                measure("W6", instance.round(), setup=setup.as_dict())
            if want("W7"):
                project = dataset.projects[0]
                created = source.create(project)
                doomed = source.issue_ids(project)[2]
                dataset.jira.remove(int(doomed))
                measure("W7", instance.round(), created=source.key(created))
            if want("W8"):
                if len(dataset.projects) < 2:
                    results["W8"] = {"skipped": "本档只有 1 个项目"}
                else:
                    mover = source.issue_ids(dataset.projects[0])[3]
                    source.move(mover, dataset.projects[1])
                    measure("W8", instance.round())
            if want("W9a") or want("W9b"):
                project = dataset.projects[0]
                changed = source.issue_ids(project)[4:14]
                source.set_status(changed)
                bad = changed[len(changed) // 2]  # 放在中间：看得出失败之前已推上去的部分
                dataset.jira.gone.add(source.key(bad))
                stats = instance.round()
                measure("W9a", stats, impacted_issues=_impact(source, instance, stats))
                dataset.jira.gone.clear()
                recover()
                source.set_status(changed)
                dataset.jira.fail_ids.add(bad)
                measure("W9b", instance.round())
                dataset.jira.fail_ids.clear()
                recover()
            if want("W10"):
                dataset.jira.set_rate(_RATE)
                try:
                    source.add_comments(ids[5])
                    measure("W10", instance.round())
                finally:
                    dataset.jira.set_rate(None)
                if results["W10"]["outcome"] != "succeeded":
                    recover()
            if want("W11"):
                victim = ids[6]
                saved = (
                    copy.deepcopy(dataset.jira.issues[victim]),
                    copy.deepcopy(dataset.jira.comments[victim]),
                )
                source.add_comments(victim)
                held = instance.crash_on_next_write()
                dataset.jira.issues[victim], dataset.jira.comments[victim] = saved
                # 崩溃轮没有采集：以崩溃后的 remote 为基线，恢复轮的变化才是「刷新」本身
                instance.last = snapshot(instance.upstream, instance.prefix)
                stats = instance.round()
                measure("W11", stats, crashed_write=held.target)
            if want("W12"):
                victim = ids[7]
                uri = _doc_uri(instance, source, victim)
                _foreign_rewrite(server.url, uri)
                # 以他人写入后的 remote 为基线：本轮的变化只算运行器自己做的
                instance.last = snapshot(instance.upstream, instance.prefix)
                source.add_comments(victim)
                stats = instance.round()
                measure("W12", stats, impacted_issues=_impact(source, instance, stats))
                # 冲突之后：被改写的工单与同一文档里的另一个工单再变（工单模式里另一个工单
                # 在别的文档），看能否推上去——report 策略下冲突不自愈
                project = source.key(victim).rsplit("-", 1)[0]
                neighbour = next(i for i in source.issue_ids(project) if i != victim)
                source.add_comments(victim)
                source.add_comments(neighbour)
                stats = instance.round()
                measure("W12-after", stats, impacted_issues=_impact(source, instance, stats))
        finally:
            instance.close()
    return results


def run_tier(
    tmp: Path,
    profile: dict[str, Any],
    issues: int,
    projects: int,
    seed: int,
    only: set[str] | None,
    log: Callable[[str], None],
) -> dict[str, dict[str, Result]]:
    """一个规模档：两种模式各用一份同种子生成的数据跑 W1–W12。返回 工作负载 → 模式 → 指标。"""
    out: dict[str, dict[str, Result]] = {}
    for mode in ("issue", "project"):
        dataset = build(profile, issues, projects, seed)
        for name, result in _run_mode(tmp, mode, dataset, only, log).items():
            out.setdefault(name, {})[mode] = result
    return out


def run_scale(
    tmp: Path,
    profile: dict[str, Any],
    sizes: list[int],
    seed: int,
    log: Callable[[str], None],
) -> list[Result]:
    """W13：项目模式下单个项目逐档放大，直到首轮失败（或跑完给定的档）。"""
    rows: list[Result] = []
    for size in sizes:
        dataset = build(profile, size, 1, seed)
        with serving(dataset.jira) as site, reference_server("--prefix", "/r/1") as server:
            directory = tmp / f"scale-{size}"
            directory.mkdir(parents=True, exist_ok=True)
            instance = Instance(
                directory, "project", dataset.jira, site, server.url, dataset.projects
            )
            try:
                stats = instance.round()
            finally:
                instance.close()
        row = {"issues": size, **stats.as_dict()}
        rows.append(row)
        log(f"W13 {size}: {stats.outcome} {stats.seconds:.1f}s")
        if stats.outcome != "succeeded":
            break
    return rows
