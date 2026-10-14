"""端到端收敛（Issue #66 验收，connector 契约 §8.3 判据 3）：真实 dpe-run、插件与参考服务端。

``dpe-run``（``python -m dpe_sdk.run``）以子进程跑每一轮，插件是本包的 ``dpe-git-connector``
子进程，remote 是独立监听的参考服务端（``python -m dpe_sdk.testing``，真实 TCP）。

从空前缀开始做一次全量轮，此后源依次变化（新提交、新分支、删仓库、改名、改名同时有新提交），
每次变化后跑一轮增量轮：断言该轮 ``succeeded`` 且 ``incremental: true``，并断言 remote 前缀内的
映射 ``file_uri → doc_hash`` 等于「同一源状态在另一个空前缀上跑一次全量轮」的结果——包括删除
与移动的结果（判据 3：R = F(Sn)）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.transport import DpeClient
from helpers import FixtureRepo, command_for, requires_git, run_git

pytestmark = requires_git

MANIFEST = Path(__file__).resolve().parents[1] / "dpe-connector.json"


@contextmanager
def reference_server() -> Iterator[str]:
    """启动参考服务端子进程，产出它公布的 remote URL。"""
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
    """在同一个 remote 上跑 dpe-run 的实例：增量实例（持续）与比对用的一次性全量实例。"""

    def __init__(self, tmp_path: Path, remote: str, root: Path) -> None:
        self.tmp = tmp_path
        self.remote = remote
        self.root = root
        self.fresh = 0
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            # 回环地址不经代理：macOS 上会读系统代理设置，参考服务端在本机
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def run(self, instance_id: str, prefix: str, state: Path, expect: int = 0) -> dict[str, Any]:
        definition = {
            "definition_version": 1,
            "id": instance_id,
            "remote": {"url": self.remote},
            "uri_prefix": prefix,
            "plugin": {"manifest": str(MANIFEST), "command": command_for("dpe-git-connector")},
            "config": {"root": str(self.root), "branches": ["dev"]},
        }
        path = self.tmp / f"{instance_id}.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "dpe_sdk.run", "--state-dir", str(state), str(path)],
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=120,
        )
        assert proc.returncode == expect, proc.stdout + proc.stderr
        report: dict[str, Any] = json.loads(proc.stdout)
        return report

    def incremental(self, expect: int = 0) -> dict[str, Any]:
        return self.run("inc", "git://inc/", self.tmp / "state-inc", expect)

    def full_mapping(self) -> dict[str, str]:
        """同一源状态在一个全新的空前缀上跑一次全量轮：F(S)。"""
        self.fresh += 1
        prefix = f"git://full{self.fresh}/"
        report = self.run(f"full{self.fresh}", prefix, self.tmp / f"state-full{self.fresh}")
        assert report["incremental"] is False
        return self.listing(prefix)

    def listing(self, prefix: str) -> dict[str, str]:
        """remote 上前缀内的映射（按前缀之后的标识比较）。"""
        out: dict[str, str] = {}
        with DpeClient(self.remote) as client:
            cursor: str | None = None
            while True:
                page = client.list_page(prefix, cursor)
                out.update({e.file_uri[len(prefix) :]: e.doc_hash for e in page.documents})
                cursor = page.next_cursor
                if cursor is None:
                    return out


def make_repo(path: Path, message: str) -> FixtureRepo:
    repo = FixtureRepo(path)
    repo.commit(message, date="2026-01-06T10:00:00+00:00", files={"f.txt": message + "\n"})
    repo.set_origin_head("main")
    return repo


def advance(path: Path, message: str, date: str) -> None:
    """在默认分支上加一个提交并同步 origin/main（默认分支内容取自远端引用）。"""
    repo = FixtureRepo.__new__(FixtureRepo)
    repo.root = path
    repo.commit(message, date=date)
    run_git(path, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_incremental_rounds_converge_to_a_full_round(tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_repo(root / "alpha", "alpha 初始化")
    make_repo(root / "beta", "beta 初始化")
    with reference_server() as remote:
        runner = Runner(tmp_path, remote, root)
        first = runner.incremental()
        assert first["outcome"] == "succeeded" and first["incremental"] is False
        assert first["counts"]["created"] == 2

        def step(expected: dict[str, int]) -> None:
            report = runner.incremental()
            assert report["outcome"] == "succeeded", report
            assert report["incremental"] is True and report["cursor_persisted"] is True
            for key, value in expected.items():
                assert report["counts"][key] == value, (key, report["counts"])
            assert runner.listing("git://inc/") == runner.full_mapping()

        # 1. 新提交：只更新那一篇
        advance(root / "alpha", "alpha 二月", "2026-02-06T10:00:00+00:00")
        step({"updated": 1, "created": 0})
        # 2. 只有跟踪分支出现（主线不动）：文档新增一页
        run_git(root / "beta", "branch", "dev")
        run_git(root / "beta", "update-ref", "refs/remotes/origin/dev", "dev")
        step({"updated": 1})
        # 2b. 只有主线前进：分支页不变，月页变化
        advance(root / "beta", "beta 主线前进", "2026-02-07T10:00:00+00:00")
        step({"updated": 1})
        # 3. 仓库目录消失：delete
        shutil.rmtree(root / "beta")
        step({"deleted": 1, "updated": 0})
        # 4. 改名：move（内容未变，不重新投递）
        os.rename(root / "alpha", root / "alpha2")
        step({"moved": 1, "updated": 0, "created": 0})
        # 5. 改名同时有新提交：move 之后在新 URI 上更新
        os.rename(root / "alpha2", root / "alpha3")
        advance(root / "alpha3", "改名后的新提交", "2026-03-06T10:00:00+00:00")
        step({"moved": 1, "updated": 1})
        # 6. 新仓库：created
        make_repo(root / "gamma", "gamma 初始化")
        step({"created": 1})
        assert set(runner.listing("git://inc/")) == {"alpha3", "gamma"}


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位")
def test_move_onto_a_target_created_by_a_failed_round(tmp_path: Path) -> None:
    """目标文档在早先一个失败轮里已经建出（游标未推进），之后用旧游标配出的 move 撞上已存在的
    目标——推送器写前判定发现目标已持有与源相同的内容，以 base 删源、判 moved，实例收敛
    （connector §7.2「move 的目标已持有同一内容」，#165）。

    1. Y 是 X 的克隆（移动键相同）；某轮 Y 作为新文档 created，但同轮另一处出错（读不了的
       子树），本轮 partial、游标不推进；
    2. 删掉 X，下一轮仍从旧游标开始：X 消失、Y 新出现且一一对应 → move X→Y；remote 上源与
       目标都在，且目标内容与源相同 → 不发 move，删源 → succeeded。
    """
    root = tmp_path / "root"
    make_repo(root / "x", "共同的历史")
    with reference_server() as remote:
        runner = Runner(tmp_path, remote, root)
        assert runner.incremental()["counts"]["created"] == 1
        run_git(root, "clone", "-q", str(root / "x"), "y")
        hidden = root / "hidden"
        hidden.mkdir()
        hidden.chmod(0o000)
        try:
            partial = runner.incremental(expect=1)
        finally:
            hidden.chmod(0o700)
            hidden.rmdir()
        assert partial["outcome"] == "partial" and partial["counts"]["created"] == 1
        assert partial["cursor_persisted"] is False
        shutil.rmtree(root / "x")
        # 前提：失败轮建出的 Y 与 X 内容相同（克隆、历史一致）。目标内容不同时仍是
        # DPE_ALREADY_EXISTS 的条目失败，由两个 SDK 的推送器用例覆盖
        converged = runner.incremental()
        assert converged["outcome"] == "succeeded"
        assert converged["counts"]["moved"] == 1
        assert converged["cursor_persisted"] is True
        listing = runner.listing("git://inc/")
        assert set(listing) == {"y"}  # 源已删、目标保留
        assert listing == runner.full_mapping()  # R = F(S)


def test_move_after_own_delete_in_a_failed_round_converges(tmp_path: Path) -> None:
    """本实例已删掉的文档在别处以相同内容重新出现（connector §7.2「move 未达成之后的
    目标 document」，#178，#165 的对称情形）：

    1. x、w 已同步；删掉 x，同轮 w 读不出（条目级 error，发现仍完整）→ delete x 已生效，但本轮
       partial、游标不推进；
    2. w 恢复，x 的克隆以 y 出现，下一轮仍从旧游标开始：x 消失、y 新出现且一一对应 → move x→y，
       并紧跟 y 的 document（§6.5）；remote 上源与目标都不存在、本实例无 x 的 L → y 由 document
       以 if_absent 建出，move 按已达成计 → succeeded，R = F(S)。
    """
    root = tmp_path / "root"
    make_repo(root / "x", "共同的历史")
    make_repo(root / "w", "另一个仓库")
    run_git(tmp_path, "clone", "-q", str(root / "x"), str(tmp_path / "y-later"))
    with reference_server() as remote:
        runner = Runner(tmp_path, remote, root)
        assert runner.incremental()["counts"]["created"] == 2
        objects = root / "w" / ".git" / "objects"
        objects.rename(objects.with_name("objects.away"))
        shutil.rmtree(root / "x")
        try:
            partial = runner.incremental(expect=1)
        finally:
            objects.with_name("objects.away").rename(objects)
        assert partial["outcome"] == "partial", partial
        assert partial["counts"]["deleted"] == 1 and partial["cursor_persisted"] is False
        assert set(runner.listing("git://inc/")) == {"w"}  # x 已在 remote 上删除

        shutil.move(str(tmp_path / "y-later"), str(root / "y"))
        converged = runner.incremental()
        assert converged["outcome"] == "succeeded", converged
        counts = converged["counts"]
        assert (counts["moved"], counts["created"], counts["failed"]) == (1, 1, 0)
        assert converged["cursor_persisted"] is True
        listing = runner.listing("git://inc/")
        assert set(listing) == {"w", "y"}
        assert listing == runner.full_mapping()  # R = F(S)
