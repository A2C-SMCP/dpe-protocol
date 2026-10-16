"""基准测试的冒烟用例（#237）：基准代码本身不失效，项目模式的变体是正确的。

真跑 dpe-run + 参考服务端 + 模拟 Jira，规模很小（几十个工单），不测性能：

- 项目模式的增量收敛：经历加评论、改状态、新建、删除、换项目之后，增量实例在 remote 上的结果
  等于同一源状态在空前缀上的全量结果（逐篇比较 doc_hash 与按序的 page_hash）；
- 项目模式的文档形态：每个项目一篇，工单页带 ``issue_id`` / ``issue_key``，正文页带标题；
- W1–W12 两种模式都跑通，失败类工作负载的结果符合预期（W9a 项目模式波及整个项目）；
- 命令行一条命令产出 ``results.json`` 与 ``report.md``。
"""

from __future__ import annotations

import json
from pathlib import Path

from dpe_sdk.transport import DpeClient
from fake_jira import serving

from bench.__main__ import main
from bench._harness import reference_server
from bench.runner import Instance, Snapshot, snapshot
from bench.synth import build, load_profile
from bench.workloads import _Source, run_tier


def _relative(shot: Snapshot, prefix: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        uri[len(prefix) :]: (doc_hash, tuple(page for page, _ in pages))
        for uri, (doc_hash, pages) in shot.items()
    }


def test_project_mode_increments_converge_to_a_full_push(tmp_path: Path) -> None:
    dataset = build(load_profile(), 30, 2, seed=7)
    source = _Source(dataset)
    with serving(dataset.jira) as site, reference_server("--prefix", "/r/1") as server:
        incremental = Instance(
            tmp_path, "project", dataset.jira, site, server.url, dataset.projects
        )
        try:
            assert incremental.round().outcome == "succeeded"
            ids = source.issue_ids()
            source.add_comments(ids[3])
            source.set_status(ids[10:14])
            source.create(dataset.projects[1])
            dataset.jira.remove(int(ids[5]))
            source.move(ids[8], dataset.projects[1])
            stats = incremental.round()
            assert stats.outcome == "succeeded" and stats.report["incremental"] is True
            full = Instance(
                tmp_path, "project", dataset.jira, site, server.url, dataset.projects, name="full"
            )
            try:
                assert full.round().report["incremental"] is False
            finally:
                full.close()
            actual = _relative(snapshot(server.url, incremental.prefix), incremental.prefix)
            expected = _relative(snapshot(server.url, full.prefix), full.prefix)
            assert actual == expected
            assert sorted(actual) == ["project/PA", "project/PB"]
            with DpeClient(server.url) as client:
                skeleton = client.get_skeleton(f"{incremental.prefix}project/PA")
            assert skeleton is not None
            sections = [page.page_metadata or {} for page in skeleton.pages]
            assert [m["section"] for m in sections[:3]] == ["description", "components", "versions"]
            body = sections[3]
            assert body["section"] == "body" and body["issue_key"] == "PA-1"
            assert {"issue_id", "title", "created_at"} <= set(body)
        finally:
            incremental.close()


def test_project_mode_recovers_after_a_failed_round_with_deletes_and_moves(
    tmp_path: Path,
) -> None:
    # 同一轮里 PA 删掉一个工单、移出一个工单，另一个（本身没变的）工单读到 404：PA 整篇失败、
    # 记录回退。故障消失后的下一轮必须重组 PA（不能因为删掉、移出的工单记录没回退而被跳过），
    # 与全量结果一致
    dataset = build(load_profile(), 30, 2, seed=11)
    source = _Source(dataset)
    with serving(dataset.jira) as site, reference_server("--prefix", "/r/1") as server:
        instance = Instance(tmp_path, "project", dataset.jira, site, server.url, dataset.projects)
        try:
            assert instance.round().outcome == "succeeded"
            pa = source.issue_ids("PA")
            dataset.jira.remove(int(pa[2]))
            source.move(pa[3], "PB")
            # 读不到的工单本身不变：旧实现下它的记录照旧，下一轮 PA 的工单集合看起来没变而被跳过
            dataset.jira.gone.add(source.key(pa[4]))
            failed = instance.round()
            assert failed.outcome == "partial"
            assert {f["file_uri"] for f in failed.failures} == {f"{instance.prefix}project/PA"}
            dataset.jira.gone.clear()
            assert instance.round().outcome == "succeeded"
            full = Instance(
                tmp_path, "project", dataset.jira, site, server.url, dataset.projects, name="full"
            )
            try:
                assert full.round().outcome == "succeeded"
            finally:
                full.close()
            actual = _relative(snapshot(server.url, instance.prefix), instance.prefix)
            expected = _relative(snapshot(server.url, full.prefix), full.prefix)
            assert actual == expected
        finally:
            instance.close()


def test_all_workloads_run_in_both_modes(tmp_path: Path) -> None:
    results = run_tier(tmp_path, load_profile(), 24, 2, 237, None, lambda _: None)
    expected = {
        **{name: "succeeded" for name in ("W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8")},
        "W9a": "partial",
        "W9b": "failed",
        "W10": "succeeded",
        "W11": "succeeded",
        "W12": "partial",
        "W12-after": "partial",
    }
    for name, outcome in expected.items():
        for mode in ("issue", "project"):
            assert results[name][mode]["outcome"] == outcome, (name, mode, results[name][mode])
    assert results["W2"]["issue"]["dpe_writes"] == results["W2"]["project"]["dpe_writes"] == 0
    # 失败影响面：工单模式只波及读不到的那 1 个工单，项目模式波及整个项目
    assert results["W9a"]["issue"]["impacted_issues"] == 1
    assert results["W9a"]["project"]["impacted_issues"] > 1
    # 项目模式每轮有变化都要重拉整个项目：W3 的 Jira 请求明显更多
    assert results["W3"]["project"]["jira_requests"] > results["W3"]["issue"]["jira_requests"]


def test_cli_writes_results_and_report(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["run", "--tiers", "12", "--workloads", "W2", "--out", str(out)]) == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert set(results["tiers"]["12"]) == {"W1", "W2"}
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "### W2 无变化轮" in report and "| 项目 = 文档 |" in report
