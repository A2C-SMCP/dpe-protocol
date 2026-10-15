"""端到端（#223 PR1）：真实 dpe-run + 插件进程 + 参考服务端 + 模拟 Jira。

``dpe-run``（``python -m dpe_sdk.run``）以子进程跑每一轮，插件是本包的 ``dpe-jira`` 子进程，
remote 是独立监听的参考服务端（``python -m dpe_sdk.testing``，真实 TCP），数据源是本进程里的
模拟 Jira。凭证经实例定义的 ``secrets``（``env`` 来源引用）注入插件，与真实部署相同。

- 正确性：remote 前缀内的 ``file_uri → doc_hash`` 等于对同一批工单在本地映射、再按 hash 契约
  计算的结果——插件产出的内容原样到达 remote；
- 确定性（connector §8.3 判据 1）：同一源状态的两次全量轮得到相同映射；
- 易变内容：只改一个工单的状态，只有它的 doc_hash 变化；
- 凭证不外泄：运行器的报告与日志里没有凭证值；
- 增量收敛（connector §8.3 判据 3）：同一实例依次经历新增、修改、加评论、删除、换 key 的
  移动，每步一轮增量，remote 前缀内的映射等于同一源状态在空前缀上的全量结果；
- 截断防御：搜索后面的页失败的一轮由运行器判为失败，remote 上一篇都不少，恢复后照常收敛。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import document_hashes
from dpe_sdk.transport import DpeClient
from fake_jira import EMAIL, TOKEN, FakeJira, adf, serving

from dpe_jira import PLUGIN_NAME, __version__
from dpe_jira.mapping import issue_document

MANIFEST = Path(__file__).resolve().parents[1] / "dpe-connector.json"


def command() -> list[str]:
    """插件启动命令：与解释器同目录的入口脚本（测试不经 shell，PATH 里没有 venv 的 bin）。"""
    return [str(Path(sys.executable).with_name("dpe-jira"))]


@contextmanager
def reference_server() -> Iterator[str]:
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", "--prefix", "/r/1"],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        assert proc.stdout is not None
        stdout = proc.stdout
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline()))
        reader.start()
        reader.join(timeout=20)
        try:
            if not lines or not lines[0]:
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")
            yield str(json.loads(lines[0])["url"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class Runner:
    def __init__(self, tmp: Path, remote: str, site: str) -> None:
        self.tmp, self.remote, self.site = tmp, remote, site
        self.fresh = 0
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "NO_PROXY": "127.0.0.1,localhost",  # 回环地址不经代理（macOS 会读系统代理）
            "no_proxy": "127.0.0.1,localhost",
            # 运行器从这两个变量解析凭证，再以清单声明的名字注入插件
            "SRC_JIRA_EMAIL": EMAIL,
            "SRC_JIRA_TOKEN": TOKEN,
        }

    def run(self, instance_id: str, prefix: str, expect: int = 0) -> dict[str, Any]:
        definition = {
            "definition_version": 1,
            "id": instance_id,
            "remote": {"url": self.remote},
            "uri_prefix": prefix,
            "plugin": {"manifest": str(MANIFEST), "command": command()},
            "config": {"site": self.site, "projects": ["ABC"]},
            "secrets": {
                "JIRA_EMAIL": {"env": "SRC_JIRA_EMAIL"},
                "JIRA_API_TOKEN": {"env": "SRC_JIRA_TOKEN"},
            },
        }
        path = self.tmp / f"{instance_id}.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "dpe_sdk.run",
                "--state-dir",
                str(self.tmp / f"state-{instance_id}"),
                str(path),
            ],
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=180,
        )
        assert proc.returncode == expect, proc.stdout + proc.stderr
        assert TOKEN not in proc.stdout + proc.stderr
        report: dict[str, Any] = json.loads(proc.stdout)
        return report

    def full_mapping(self) -> dict[str, str]:
        self.fresh += 1
        prefix = f"jira://full{self.fresh}/"
        report = self.run(f"full{self.fresh}", prefix)
        assert report["outcome"] == "succeeded", report
        assert report["incremental"] is False
        return self.listing(prefix)

    def listing(self, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        with DpeClient(self.remote) as client:
            cursor: str | None = None
            while True:
                page = client.list_page(prefix, cursor)
                out.update({e.file_uri[len(prefix) :]: e.doc_hash for e in page.documents})
                cursor = page.next_cursor
                if cursor is None:
                    return out


def expected_mapping(fake: FakeJira) -> dict[str, str]:
    """对模拟 Jira 里的全部工单在本地映射并计算 doc_hash（相对前缀的 URI → doc_hash）。"""
    out: dict[str, str] = {}
    for issue_id, issue in fake.issues.items():
        relative = f"issue/{issue['key']}"
        item = issue_document(relative, issue, fake.comments[issue_id])
        expanded: Any = {**item["document"], "pages": item["pages"]}  # 展开视图
        out[relative] = document_hashes(expanded)["doc_hash"]
    return out


def populate(fake: FakeJira) -> None:
    fake.add_issue(
        10001,
        "ABC-1",
        summary="登录失败",
        description=adf("复现步骤", "预期结果"),
        status={"id": "1", "name": "待办"},
        assignee={"accountId": "acc-1", "displayName": "张三"},
        labels=["login"],
        comments=[
            {
                "id": "2",
                "author": {"accountId": "acc-2", "displayName": "李四"},
                "body": adf("已复现"),
                "created": "2026-09-02T00:00:00.000+0000",
            },
            {
                "id": "1",
                "author": {"accountId": "acc-1", "displayName": "张三"},
                "body": adf("补充日志"),
                "created": "2026-09-01T12:00:00.000+0800",
            },
        ],
    )
    fake.add_issue(10002, "ABC-2", summary="空描述的工单", status={"id": "3", "name": "完成"})
    for n in range(3, 8):
        fake.add_issue(10000 + n, f"ABC-{n}", summary=f"批量工单 {n}", description=adf(f"内容 {n}"))


def test_manifest_matches_plugin_identity() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert (manifest["name"], manifest["version"]) == (PLUGIN_NAME, __version__)


def test_full_rounds_deliver_exact_and_deterministic_documents(tmp_path: Path) -> None:
    fake = FakeJira(search_page_size=3, comment_page_size=1)
    populate(fake)
    with serving(fake) as site, reference_server() as remote:
        runner = Runner(tmp_path, remote, site)
        first = runner.full_mapping()
        assert first == expected_mapping(fake)
        assert runner.full_mapping() == first  # 判据 1：确定性

        fake.issues["10002"]["fields"]["status"] = {"id": "1", "name": "待办"}
        changed = runner.full_mapping()
        assert changed == expected_mapping(fake)
        assert {uri for uri in first if first[uri] != changed[uri]} == {"issue/ABC-2"}


def test_incremental_rounds_converge_and_truncated_enumeration_deletes_nothing(
    tmp_path: Path,
) -> None:
    fake = FakeJira(search_page_size=2, comment_page_size=1)
    populate(fake)
    with serving(fake) as site, reference_server() as remote:
        runner = Runner(tmp_path, remote, site)
        first = runner.run("inc", "jira://inc/")
        assert first["outcome"] == "succeeded" and first["incremental"] is False
        assert first["counts"]["created"] == 7

        def step(expected: dict[str, int]) -> None:
            report = runner.run("inc", "jira://inc/")
            assert report["outcome"] == "succeeded", report
            assert report["incremental"] is True, report
            for key, value in expected.items():
                assert report["counts"][key] == value, (key, report["counts"])
            assert runner.listing("jira://inc/") == runner.full_mapping()

        step({"created": 0, "updated": 0})  # 没有变化
        fake.add_issue(10008, "ABC-8", summary="新工单", description=adf("新内容"))
        step({"created": 1})
        fake.touch(10003, "2026-10-02T08:00:00.000+0000", status={"id": "3", "name": "完成"})
        step({"updated": 1, "created": 0})
        fake.comments["10002"].append(
            {
                "id": "9",
                "author": {"accountId": "acc-9", "displayName": "王五"},
                "body": adf("新评论"),
                "created": "2026-10-03T00:00:00.000+0000",
            }
        )
        fake.touch(10002, "2026-10-03T00:00:00.000+0000")  # 加评论时 Jira 更新 updated
        step({"updated": 1})
        fake.remove(10004)
        step({"deleted": 1})
        # 换 key（移动后仍在范围内，例如 jql 覆盖两个项目）：move 后紧跟的 document 判为 unchanged
        fake.rekey(10005, "XYZ-5", "2026-10-04T00:00:00.000+0000")
        step({"moved": 1, "created": 0, "updated": 0, "unchanged": 1})

        # 截断防御：真实删除了一篇，但搜索第 2 页起失败——本轮失败，一篇都不能少
        before = runner.listing("jira://inc/")
        fake.remove(10006)
        fake.search_fail_from = 2
        failed = runner.run("inc", "jira://inc/", expect=2)
        assert failed["outcome"] == "failed", failed
        assert runner.listing("jira://inc/") == before
        fake.search_fail_from = None
        step({"deleted": 1})
        assert set(runner.listing("jira://inc/")) == {
            f"issue/{key}" for key in ("ABC-1", "ABC-2", "ABC-3", "XYZ-5", "ABC-7", "ABC-8")
        }
