"""Git 仓库访问：以命令行 git 为唯一后端（不引入 git 库、不自解析对象格式）。

本连接器把仓库的**提交历史**映射为 DPE 内容（README「映射规则」），因此只遍历提交图——
读取提交元数据与提交说明，**不读树、不读 diff、不读文件内容**。只读命令：

- ``git rev-parse``：仓库根校验、ref 与提交解析（``^{commit}`` 解引用）与祖先判定；
- ``git symbolic-ref``：默认分支取自 ``refs/remotes/origin/HEAD`` 的符号目标（由远端声明）；
  **不看本地 ``HEAD``**——工作区 checkout 会改动它，那是运行环境状态而非源内容，会让整篇
  文档的默认分支随运行环境漂移、制造伪变更（显式配置的 ``default_branch`` 优先）；
- ``git log -z --format=…``：提交**集合**与元数据——git 的输出次序不进内容，页内元素序由本
  模块的 ``order_commits`` 按自有规则给出（只依赖提交图；跨 git 版本稳定）；
- ``git for-each-ref``：空仓库判定（没有任何 ref 即零页文档，契约 1 §5 允许空文档）。

``-z`` 与 ``%x00`` 分隔是必需的：提交说明原样进 DPE，NUL 不能出现在提交消息与 ident 里，用它
分隔无歧义（换行、引号都不会被改写）。

路径与目录一律按**原始字节**处理（``os.fsencode``）：Git 的路径是字节串，可能不是 UTF-8，
仓库标识进入 ``file_uri`` 时由百分号编码表达（见 ``mapping``）。

子进程环境显式构造：``GIT_TERMINAL_PROMPT=0``（不交互，不挂住插件）、``GIT_OPTIONAL_LOCKS=0``
（只读访问不写 ``.git``）、清空全局配置（``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_NOSYSTEM``）并清掉
会改写仓库发现的继承变量，使结果只由仓库自身决定。**不接触任何凭证**：本连接器以本机 git 的
方式读取使用者配置的本地仓库路径，不代为访问远端。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "CommitRecord",
    "GitContentError",
    "GitError",
    "GitRepo",
    "RepoDir",
    "discover_repos",
    "first_parent_chain",
    "looks_like_repo",
    "order_commits",
]

#: git 命令的超时（秒）：枚举必须是有界的，插件不因数据源卡死而挂住整轮
_GIT_TIMEOUT = 120.0

#: ``git log`` 的字段格式：NUL 分隔的 7 个字段/提交——sha、父提交、作者名、作者邮箱、
#: 作者时间、提交时间、提交说明全文。时间取 ``%aI``/``%cI``（严格的 ISO 8601，带偏移）。
_LOG_FORMAT = "%H%x00%P%x00%an%x00%ae%x00%aI%x00%cI%x00%B"

#: 每个提交在 ``git log`` 输出中被 NUL 分隔的字段数（须与 ``_LOG_FORMAT`` 一致）
_LOG_FIELDS = 7


class GitError(Exception):
    """git 命令失败、输出不合法，或仓库状态无法支撑本次枚举（``source_unavailable`` 候选）。"""


class GitContentError(GitError):
    """源内容无法映射为合法 DPE 内容（非 UTF-8 的说明 / 作者，非法时间戳）。

    与「读取失败」分开：这类失败的条目级归属是 ``content_invalid``、**不可重试**——同样的
    扫描重跑不会成功（§6.5 的封闭枚举按定义区分）。
    """


@dataclass(frozen=True)
class CommitRecord:
    """一次提交（元素级映射的源）：元数据 + 提交说明全文；不读树、不读 diff。"""

    sha: str
    parents: tuple[str, ...]
    #: 作者 ident 原样（``Name <email>``；不做 mailmap——改写过的 ident 不是源内容）
    author: str
    #: 作者时间 / 提交时间，RFC 3339 UTC（秒精度，``Z`` 结尾）
    authored_at: str
    committed_at: str
    #: 提交说明全文（原样，不剥离；含 Git 存储的结尾换行）
    message: str


@dataclass(frozen=True)
class RepoDir:
    """``root`` 下发现（或单仓库模式指定）的一个目录：绝对路径 + 身份字节。

    正常是仓库；发现层读不了它时出现在 ``discover_repos`` 的第二个返回值里（出条目级
    error，而不是废掉整轮），``identifier`` 是相对 ``root`` 的原始路径字节——单仓库模式
    的仓库标识是目录名。
    """

    path: str
    identifier: bytes


def looks_like_repo(directory: Path) -> bool:
    """目录是否是仓库候选：有 ``.git``（目录，或 worktree/子模块的 ``.git`` 文件）或形似裸仓库。

    只是发现用的启发式（省去对每个目录跑一次 git）：最终是否可用由 ``GitRepo.open`` 判定，
    不可用的候选在扫描时如实产出条目级错误。读不了目录（权限等）时返回 ``False``——不当作
    仓库候选；发现层下探时会把该子树记为「无法读取」，同样如实上报（不静默跳过）。
    """
    try:
        if (directory / ".git").exists():
            return True
        return (
            (directory / "HEAD").is_file()
            and (directory / "objects").is_dir()
            and (directory / "refs").is_dir()
        )
    except OSError:
        return False


def discover_repos(root: str) -> tuple[list[RepoDir], list[RepoDir]]:
    """递归发现 ``root`` 下的仓库：返回（仓库, 无法读取的子树），各按身份字节序升序。

    规则（README「映射规则」）：

    - 每发现一个仓库即停止下探：仓库工作树内的目录（含子模块）不是独立仓库；
    - 符号链接目录不跟随（可能成环）；``.git`` 目录不进入；
    - ``root`` 自身不当作仓库（它是容器；这个误配在 ``initialize`` 被显式拒绝）；
    - **子孙目录读不了**（权限等）只记录到第二个返回值：调用方出条目级 error 并继续其余
      仓库——与「单个仓库不可用不影响其他仓库」一致（§6.5 条目不静默跳过）。``root``
      自身读不了则没有可枚举的范围，抛 ``GitError``（整轮失败，运行器退避重试）。
    """
    base = Path(root)
    found: list[RepoDir] = []
    unreadable: list[RepoDir] = []

    def walk(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: os.fsencode(entry.name))
        except OSError as exc:
            if directory == base:
                raise GitError(f"读取 root 目录失败：{directory}（{exc}）") from None
            relative = os.path.relpath(directory, base)
            unreadable.append(RepoDir(path=str(directory), identifier=os.fsencode(relative)))
            return
        for entry in children:
            if entry.name == ".git":
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                # 连 stat 都失败的条目：归入「无法读取」如实上报（不静默跳过）
                relative = os.path.relpath(entry.path, base)
                unreadable.append(RepoDir(path=entry.path, identifier=os.fsencode(relative)))
                continue
            if not is_dir:
                continue
            child = Path(entry.path)
            if looks_like_repo(child):
                relative = os.path.relpath(entry.path, base)
                found.append(RepoDir(path=str(child), identifier=os.fsencode(relative)))
                continue
            walk(child)

    walk(base)
    found.sort(key=lambda item: item.identifier)
    unreadable.sort(key=lambda item: item.identifier)
    return found, unreadable


def _utc_timestamp(value: str) -> str:
    """``%aI``/``%cI``（ISO 8601，带偏移）→ RFC 3339 UTC（秒精度，``Z`` 结尾）。

    时间戳不合规是**内容**问题（该字段映射不出合法 DPE 值），归 ``GitContentError``。
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise GitContentError(f"git 返回的时间不是 ISO 8601：{value!r}") from None
    if moment.tzinfo is None:
        raise GitContentError(f"git 返回的时间缺少时区：{value!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def order_commits(records: Sequence[CommitRecord], order_from: str) -> list[CommitRecord]:
    """自有拓扑序（旧在前）：从 ``order_from`` 出发做后序 DFS，父提交按记录顺序。

    规则（README「确定性」；只依赖提交图与元数据，**不依赖 git 的输出次序**）：

    - 访问一个提交时先依「父提交记录顺序」递归访问每个父提交（记录顺序是源内容），最后
      输出自己——父提交因此总是先于子提交（旧在前），被合并带入的提交排在其合并提交之前；
    - 不在 ``records`` 里的父提交是范围边界（如 ``<default>..<branch>`` 被排除的一侧）：
      跳过且不下探（被排除提交的祖先也必被排除）；
    - 每个提交只输出一次；没有任何按时间或 SHA 的隐含序，平局由此完全确定。

    ``records`` 是集合：次序无关，任意输入顺序产出相同结果（测试用打乱输入钉住）。空集合
    产出空列表；集合非空而起点不在其中、或有提交从起点不可达——都是提交集不完整，如实报错，
    不静默丢内容。
    """
    by_sha = {record.sha: record for record in records}
    if not by_sha:
        return []
    if order_from not in by_sha:
        raise GitError(f"排序起点 {order_from} 不在提交集中：提交集不完整")
    ordered: list[CommitRecord] = []
    pushed: set[str] = {order_from}
    done: set[str] = set()
    stack: list[tuple[str, int]] = [(order_from, 0)]
    while stack:
        sha, index = stack[-1]
        parents = by_sha[sha].parents
        if index < len(parents):
            stack[-1] = (sha, index + 1)
            parent = parents[index]
            if parent in by_sha and parent not in pushed:
                pushed.add(parent)
                stack.append((parent, 0))
            continue
        stack.pop()
        done.add(sha)
        ordered.append(by_sha[sha])
    if len(ordered) != len(by_sha):
        missing = next(sha for sha in by_sha if sha not in done)
        raise GitError(f"提交 {missing} 不可从 {order_from} 到达：提交集不完整")
    return ordered


def first_parent_chain(records: Sequence[CommitRecord], tip: str) -> list[str]:
    """``tip`` 的 first-parent 链（旧在前，根端在首个）——按月分页的骨架。

    纯粹的图函数：从 ``tip`` 反复走第一个父提交（父提交的记录顺序是源内容），与 git 的
    输出次序无关。``records`` 必须覆盖整条链（``GitRepo.log(tip)`` 的结果即可）。
    """
    by_sha = {record.sha: record for record in records}
    chain: list[str] = []
    sha: str | None = tip
    while sha is not None:
        record = by_sha.get(sha)
        if record is None:
            raise GitError(f"提交 {sha} 不在读取到的提交集中：提交图不完整")
        chain.append(sha)
        sha = record.parents[0] if record.parents else None
    chain.reverse()
    return chain


class GitRepo:
    """一个本地仓库的只读提交图视图。"""

    def __init__(self, path: str) -> None:
        self.path = path

    # ------------------------------------------------------------------ 打开与校验

    def open(self) -> None:
        """校验配置路径是仓库根（裸仓库则是裸仓库目录本身）且可读取；每轮重跑。

        配置指向子目录时 git 仍能工作，但仓库标识会随配置路径漂移，同一仓库会得到不同身份，
        因此直接拒绝。
        """
        try:
            # 裸仓库（--mirror / --bare）没有工作树，不能用 --show-toplevel 判定
            git_dir = self._run("rev-parse", "--absolute-git-dir").strip()
            is_bare = self._run("rev-parse", "--is-bare-repository").strip() == "true"
            root = git_dir if is_bare else self._run("rev-parse", "--show-toplevel").strip()
        except GitError as exc:
            raise GitError(f"不是可用的 git 仓库：{self.path}（{exc}）") from None
        if not root:
            raise GitError(f"不是可用的 git 仓库：{self.path}")
        # 按 dev/inode 比较而不是字面路径：同一个目录的符号链接形式（macOS 的 /var → /private/var）
        # 与大小写变体（大小写不敏感的文件系统）都要认作仓库根，否则会把可用配置误拒
        try:
            same = os.path.samefile(self.path, root)
        except OSError:
            same = False
        if not same:
            raise GitError(f"repo 必须是仓库根目录（配置为 {self.path}，实际根目录为 {root}）")

    # ------------------------------------------------------------------ 历史结构

    def has_refs(self) -> bool:
        """是否有任何 ref；空仓库（无任何 ref）产零页文档，而不是报错（契约 1 §5）。"""
        return bool(self._run("for-each-ref", "--count=1").strip())

    def default_branch(self, configured: str | None) -> str:
        """默认分支名：显式配置优先，其次 ``refs/remotes/origin/HEAD`` 的符号目标。

        不看本地 ``HEAD``（工作区状态，见模块 docstring）；两者都拿不到时抛 ``GitError``——
        调用方按模式转成 ``invalid_config``（单仓库）或条目级错误（root 下的某个仓库）。
        """
        if configured:
            return configured
        prefix = "refs/remotes/origin/"
        target = self._run(
            "symbolic-ref",
            "--quiet",
            "--end-of-options",
            "refs/remotes/origin/HEAD",
            allow_exit=(1,),
        ).strip()
        if not target or not target.startswith(prefix) or len(target) == len(prefix):
            raise GitError(
                "无法确定默认分支：未配置 default_branch，且仓库没有由远端声明的 "
                "refs/remotes/origin/HEAD"
            )
        return target[len(prefix) :]

    def resolve_ref(self, rev: str) -> str | None:
        """``rev`` → 提交 id（``^{commit}`` 解引用）；不可解析（含非法名字）返回 ``None``。

        ``--end-of-options`` 使 rev 不会被当成选项；``--quiet`` 让不可解析以退出码 1 结束。
        """
        value = (
            self._run(
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{rev}^{{commit}}",
                allow_exit=(1,),
            )
            .strip()
            .lower()
        )
        return value or None

    def resolve_branch(self, name: str) -> str | None:
        """跟踪的分支名 → 提交 id：``origin/<name>`` 优先，其次本地 ``refs/heads/<name>``。

        解析不到返回 ``None``——不是错误：配置的分支不存在（未建、已删）即不产该页，
        与「删除分支 = 删页」一致（README「映射规则」）。
        """
        for ref in (f"refs/remotes/origin/{name}", f"refs/heads/{name}"):
            commit = self.resolve_ref(ref)
            if commit is not None:
                return commit
        return None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """``ancestor`` 是否是 ``descendant`` 的祖先（等于也算）——``since`` 的适用范围判定。"""
        completed = self._exec("merge-base", "--is-ancestor", ancestor, descendant)
        if completed.returncode in (0, 1):
            return completed.returncode == 0
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise GitError(
            f"git merge-base --is-ancestor 失败（退出码 {completed.returncode}）：{message}"
        )

    # ------------------------------------------------------------------ 提交图读取

    def log(self, *revisions: str) -> list[CommitRecord]:
        """读取提交集合与元数据（**次序不定**）；``revisions`` 是 rev-list 的选取范围。

        只取集合与图：git 的输出次序（默认按时间）不进内容——页内元素序由本连接器自己的
        ``order_commits`` 规定（README「确定性」），不依赖 git 的 ``--topo-order`` 等排序
        （跨 git 版本不保证一致，会制造伪变更）。空范围产出空列表。
        """
        raw = self._run_bytes(
            "log",
            "-z",
            f"--format={_LOG_FORMAT}",
            "--end-of-options",
            *revisions,
        )
        fields = raw.split(b"\x00")
        if fields and not fields[-1]:
            fields.pop()  # 末条记录之后的分隔符
        if len(fields) % _LOG_FIELDS != 0:
            raise GitError(f"git log 输出字段数不是 {_LOG_FIELDS} 的倍数：{len(fields)}")
        records: list[CommitRecord] = []
        for offset in range(0, len(fields), _LOG_FIELDS):
            chunk = fields[offset : offset + _LOG_FIELDS]
            # 说明与作者 ident 都按严格 UTF-8 解码：不是 UTF-8 的内容进不了 JSON 字符串，
            # 如实报错（内容类失败，条目级 content_invalid），不做替换或猜测编码
            try:
                author = f"{chunk[2].decode('utf-8')} <{chunk[3].decode('utf-8')}>"
                message = chunk[6].decode("utf-8")
            except UnicodeDecodeError as exc:
                sha = chunk[0].decode("ascii", "replace")
                raise GitContentError(
                    f"提交 {sha} 的说明或作者不是合法 UTF-8（{exc.reason}）"
                ) from None
            records.append(
                CommitRecord(
                    sha=chunk[0].decode("ascii", "replace").lower(),
                    parents=tuple(chunk[1].decode("ascii", "replace").lower().split()),
                    author=author,
                    authored_at=_utc_timestamp(chunk[4].decode("ascii", "replace")),
                    committed_at=_utc_timestamp(chunk[5].decode("ascii", "replace")),
                    message=message,
                )
            )
        return records

    # ------------------------------------------------------------------ 子进程

    def _exec(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", self.path, *args],
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                env=_git_env(),
                check=False,
            )
        except FileNotFoundError:
            raise GitError("找不到 git 可执行文件（PATH 中没有 git）") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} 超过 {_GIT_TIMEOUT:.0f} 秒未完成") from None

    def _run_bytes(self, *args: str, allow_exit: tuple[int, ...] = ()) -> bytes:
        completed = self._exec(*args)
        if completed.returncode != 0 and completed.returncode not in allow_exit:
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args)} 失败（退出码 {completed.returncode}）：{message}")
        return completed.stdout

    def _run(self, *args: str, allow_exit: tuple[int, ...] = ()) -> str:
        raw = self._run_bytes(*args, allow_exit=allow_exit)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitError(f"git {' '.join(args)} 的输出不是 UTF-8") from None


def _git_env() -> dict[str, str]:
    """插件的 git 子进程环境：不交互、不写 ``.git``、不受全局配置影响。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    # 清掉会改写仓库发现的继承变量（git hook 等场景会带进来），使结果只由 repo 与仓库自身决定
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        env.pop(name, None)
    # 插件进程的环境由运行器构造（§4.3）；这里只保留其 PATH/凭据类变量，不额外清理
    return env
