"""pytest 夹具：覆盖 Issue #96 验收要求的提交历史（合并提交、多分支、跨月、时间乱序）。

夹具的现场构造见 ``helpers``；所有提交的作者与时间戳固定，提交 id 跨机器确定。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from helpers import FixtureRepo


@dataclass(frozen=True)
class History:
    """夹具仓库 ``repo`` 的提交与分支。"""

    path: Path
    commit: dict[str, str]

    def sha(self, name: str) -> str:
        return self.commit[name]


@pytest.fixture
def history(tmp_path: Path) -> History:
    """覆盖合并提交、多分支、跨月与时间乱序的夹具仓库。

    主线（``main``）：``c1``（2026-01）→ ``c2``（2026-01）→ 合并 ``m1``（2026-02）→ ``c3``
    （2026-03）→ 合并 ``m2``（2026-03）。分支 ``feature/early`` 从 ``c1`` 开出：``f1`` 的提交
    时间在 2025-11（早于合并月），``f2`` 在 2026-03（晚于合并时间）——两者都必须随合并提交
    ``m1`` 归入 2026-02。分支 ``dev`` 从 ``c3`` 开出（独有提交跨月），``feature/merged`` 已
    合并但未删（在默认分支上不再有独有提交 → 空页）。
    """
    repo = FixtureRepo(tmp_path / "repo")
    commits: dict[str, str] = {}
    commits["c1"] = repo.commit(
        "根提交：初始化仓库",
        date="2026-01-05T08:30:00+08:00",  # 非 UTC 偏移：验证 UTC 归一
        files={"README.md": "# 仓库\n"},
    )
    commits["c2"] = repo.commit(
        "一月第二次提交\n\n带一段正文。",
        date="2026-01-20T10:00:00+00:00",
        files={"docs/guide.md": "正文\n"},
    )
    repo.branch("feature/early", at=commits["c1"])
    commits["f1"] = repo.commit(
        "分支提交一（早于合并月）",
        date="2025-11-11T00:00:00+00:00",
        files={"early.txt": "f1\n"},
    )
    commits["f2"] = repo.commit(
        "分支提交二（晚于合并时间）",
        date="2026-03-01T00:00:00+00:00",
        author="Bob",
        email="bob@example.invalid",
        files={"early.txt": "f1\nf2\n"},
    )
    repo.checkout("main")
    commits["m1"] = repo.merge(
        "feature/early", "合并 feature/early", date="2026-02-01T10:00:00+00:00"
    )
    commits["c3"] = repo.commit(
        "三月主线提交", date="2026-03-10T10:00:00+00:00", files={"docs/guide.md": "正文\n三月\n"}
    )
    repo.branch("dev", at=commits["c3"])
    commits["d1"] = repo.commit(
        "dev 独有提交一",
        date="2026-03-12T10:00:00+00:00",
        author="Bob",
        email="bob@example.invalid",
        files={"dev.txt": "d1\n"},
    )
    commits["d2"] = repo.commit(
        "dev 独有提交二",
        date="2026-04-01T10:00:00+00:00",
        author="Bob",
        email="bob@example.invalid",
        files={"dev.txt": "d1\nd2\n"},
    )
    repo.checkout("main")
    repo.branch("feature/merged", at=commits["c3"])
    commits["g1"] = repo.commit(
        "feature/merged 的提交", date="2026-03-11T10:00:00+00:00", files={"merged.txt": "g1\n"}
    )
    repo.checkout("main")
    commits["m2"] = repo.merge(
        "feature/merged", "合并 feature/merged", date="2026-03-20T10:00:00+00:00"
    )
    # 像一份克隆：默认分支取自 origin/HEAD，跟踪分支有对应的远端引用
    repo.set_origin_head("main")
    repo.add_remote_branch("dev")
    repo.add_remote_branch("feature/early")
    repo.add_remote_branch("feature/merged")
    return History(path=repo.root, commit=commits)


@dataclass(frozen=True)
class ReposRoot:
    """root 模式夹具：正常仓库、嵌套仓库、空仓库、坏仓库与非仓库目录。"""

    root: Path
    alpha: Path
    beta: Path
    empty: Path
    broken: Path


@pytest.fixture
def repos_root(tmp_path: Path) -> ReposRoot:
    root = tmp_path / "root"
    alpha = FixtureRepo(root / "alpha")
    alpha.commit("alpha 初始化", date="2026-01-06T10:00:00+00:00", files={"a.txt": "a\n"})
    alpha.set_origin_head("main")
    beta = FixtureRepo(root / "nested" / "beta")
    beta.commit("beta 第一次", date="2026-01-06T11:00:00+00:00", files={"b.txt": "b\n"})
    beta.commit("beta 第二次", date="2026-02-06T11:00:00+00:00", files={"b.txt": "b\nb\n"})
    beta.set_origin_head("main")
    empty = FixtureRepo(root / "empty_repo")  # 无任何提交：零页文档
    broken = root / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    plain = root / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("not a repo\n", encoding="utf-8")
    return ReposRoot(root=root, alpha=alpha.root, beta=beta.root, empty=empty.root, broken=broken)
