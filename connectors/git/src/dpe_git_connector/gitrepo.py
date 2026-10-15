"""Git 仓库访问：以命令行 git 为唯一后端（不引入 git 库；对象格式只在标签与旧编码提交处读取）。

本连接器把仓库的**提交历史**映射为 DPE 内容（README「映射规则」），因此只遍历提交图——
读取提交元数据与提交说明（以及附注标签的说明），**不读树、不读 diff、不读文件内容**。只读命令：

- ``git rev-parse``：仓库根校验、ref 与提交解析（``^{commit}`` 解引用）与祖先判定；
- ``git symbolic-ref``：默认分支取自 ``refs/remotes/origin/HEAD`` 的符号目标（由远端声明）；
  **不看本地 ``HEAD``**——工作区 checkout 会改动它，那是运行环境状态而非源内容，会让整篇
  文档的默认分支随运行环境漂移、制造伪变更（显式配置的 ``default_branch`` 优先）；
- ``git log -z --format=…``：提交**集合**与元数据——git 的输出次序不进内容，页内元素序由本
  模块的 ``order_commits`` 按自有规则给出（只依赖提交图；跨 git 版本稳定）；
- ``git rev-list --max-parents=0``：根提交集合（仓库改名的配对判据，见 ``server``）；
- ``git for-each-ref``：空仓库判定（没有任何 ref 即零页文档，契约 1 §5 允许空文档）与附注
  标签的集合（``refs/tags/*`` 中指向标签对象的 ref，#220）；
- ``git cat-file --batch``：标签对象，以及声明了非 UTF-8 编码的提交的原始字节（``git log``
  的转码随平台而异，见 ``GitRepo._verbatim``）——只有这两处自行读取对象格式。

``-z`` 与 ``%x00`` 分隔是必需的：提交说明原样进 DPE，NUL 不能出现在提交消息与 ident 里，用它
分隔无歧义（换行、引号都不会被改写）。

路径与目录一律按**原始字节**处理（``os.fsencode``）：Git 的路径是字节串，可能不是 UTF-8，
仓库标识进入 ``file_uri`` 时由百分号编码表达（见 ``mapping``）。

子进程环境显式构造：``GIT_TERMINAL_PROMPT=0``（不交互，不挂住插件）、``GIT_OPTIONAL_LOCKS=0``
（只读访问不写 ``.git``）、清空全局配置（``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_NOSYSTEM``）并清掉
会改写仓库发现的继承变量，使结果只由仓库自身决定。**不应用本地的对象替换**
（``GIT_NO_REPLACE_OBJECTS``、``GIT_GRAFT_FILE`` 指向空文件）：``refs/replace`` 与
``info/grafts`` 是本机对提交图的改写，不是源内容（同不做 mailmap）；否则它们会在 tip 不变时
改变映射结果，增量游标也看不出来。浅克隆的边界无法关闭（那是克隆可见的全部历史），由
``shallow_boundary`` 计入仓库状态。**不接触任何凭证**：本连接器以本机 git 的
方式读取使用者配置的本地仓库路径，不代为访问远端。
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "MIN_GIT_VERSION",
    "CommitRecord",
    "GitContentError",
    "GitError",
    "GitRepo",
    "RepoDir",
    "TagRecord",
    "discover_repos",
    "expand_submodules",
    "first_parent_chain",
    "git_version",
    "looks_like_repo",
    "order_commits",
    "parse_git_version",
    "root_commits",
]

#: 诊断日志（stderr，见 cli）：stdout 只走协议
_log = logging.getLogger("dpe_git_connector")

#: git 命令的超时（秒）：枚举必须是有界的，插件不因数据源卡死而挂住整轮
_GIT_TIMEOUT = 120.0

#: ``git log`` 的字段格式：NUL 分隔的 7 个字段/提交——sha、父提交、作者名、作者邮箱、
#: 作者时间、提交时间、提交说明全文。时间取 ``%aI``/``%cI``（严格的 ISO 8601，带偏移）。
_LOG_FORMAT = "%H%x00%P%x00%an%x00%ae%x00%aI%x00%cI%x00%e%x00%B"

#: 每个提交在 ``git log`` 输出中被 NUL 分隔的字段数（须与 ``_LOG_FORMAT`` 一致）
_LOG_FIELDS = 8


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
class TagRecord:
    """一个附注标签（标签页元素的源，#220）：ref 名 + 标签对象里的源事实 + 说明（不含签名）。"""

    #: ref 名去掉 ``refs/tags/``（用户认的名字；可能与对象里的 ``tag`` 头不同）
    name: str
    #: 对象里 ``object`` 头直接记录的 SHA（不剥到底层：嵌套标签就指向那个标签对象）
    target: str
    #: ``Name <email>`` 与 RFC 3339 UTC；老标签没有 ``tagger`` 头时两者都是 ``None``
    tagger: str | None
    tagged_at: str | None
    #: 标签说明原文，去掉末尾的签名块（``strip_signature``）
    message: str


#: ``_cat_objects`` 的对象类型 → 错误消息里的中文名
_KIND_NAMES = {"commit": "提交", "tag": "标签"}

#: git 认定的签名块起始标记（gpg-interface.c 的 OpenPGP / X.509 / SSH 三种格式）。本模块自己
#: 按它切分而不用 ``%(contents:signature)``：SSH 签名自 git 2.34 才被识别，低于它的 git 会把
#: SSH 签名留在说明里，映射就随 git 版本而变
_SIGNATURE_MARKERS = (
    b"-----BEGIN PGP SIGNATURE-----",
    b"-----BEGIN PGP MESSAGE-----",
    b"-----BEGIN SIGNED MESSAGE-----",
    b"-----BEGIN SSH SIGNATURE-----",
)


def strip_signature(message: bytes) -> bytes:
    """去掉标签说明末尾的签名块（签名不进内容，README「映射规则」）。

    边界与 git 的 ``parse_signed_buffer`` 一致：**最后一个**以签名起始标记开头的行及其之后都是
    签名。标记不在行首（缩进、句中引用）不算；正文行首出现标记、末尾另有真签名时只切末尾那块。
    """
    match = len(message)
    position = 0
    while position < len(message):
        if message.startswith(_SIGNATURE_MARKERS, position):
            match = position
        newline = message.find(b"\n", position)
        position = len(message) if newline < 0 else newline + 1
    return message[:match]


def _split_ident(ident: bytes, what: str) -> tuple[bytes, bytes, bytes]:
    """``Name <email> 其余`` → (name, email, 其余)：与 git 的 ``split_ident_line`` 同口径，在第一个
    ``<`` 处切、取其后第一个 ``>``；格式不对抛 ``GitContentError``。"""
    opening = ident.find(b"<")
    closing = ident.find(b">", opening)
    if opening < 0 or closing < 0:
        raise GitContentError(f"{what}格式不对")
    return ident[:opening].strip(b" \t"), ident[opening + 1 : closing], ident[closing + 1 :]


def _ident_time(rest: bytes, what: str) -> str:
    """ident 之后的 ``<秒> <时区>`` → RFC 3339 UTC（时区不影响 UTC 时刻，只校验形状）。"""
    fields = rest.split()
    shaped = len(fields) == 2 and re.fullmatch(rb"[+-]\d{4}", fields[1]) is not None
    if not shaped or not fields[0].isdigit():
        raise GitContentError(f"{what}的时间格式不对")
    try:
        moment = datetime.fromtimestamp(int(fields[0]), UTC)
    except (OverflowError, ValueError, OSError):
        raise GitContentError(f"{what}的时间超出可表示的范围") from None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RepoDir:
    """``root`` 下发现（或单仓库模式指定）的一个目录：绝对路径 + 身份字节。

    正常是仓库；发现层读不了它时出现在 ``discover_repos`` 的第二个返回值里（出条目级
    error，而不是废掉整轮），``identifier`` 是相对 ``root`` 的原始路径字节——单仓库模式
    的仓库标识是目录名。
    """

    path: str
    identifier: bytes


def _is_utf8(encoding: str) -> bool:
    """git 判为 UTF-8 的编码名（``utf-8`` / ``utf8``，不分大小写）：主 log 输出的就是原字节。"""
    return encoding.lower() in ("utf-8", "utf8")


def _decode(raw: bytes, encoding: str, sha: str) -> str:
    """按提交的 encoding 头解码（README「确定性」）：用 Python 标准库的编解码表，与平台、git
    版本、iconv 无关；没有 encoding 头即 UTF-8（git 的缺省）。声明的编码不是 Python 认识的
    文本编码，或按它解不了时抛 ``GitContentError``（条目级 ``content_invalid``）。"""
    name = encoding or "utf-8"
    try:
        return raw.decode(codecs.lookup(name).name)
    except LookupError:  # 不认识的编码名，或不是文本编码（如 base64）
        raise GitContentError(f"提交 {sha} 声明的编码 {encoding!r} 不受支持") from None
    except UnicodeDecodeError as exc:
        declared = f"声明的编码 {encoding}" if encoding else "UTF-8（没有 encoding 头）"
        raise GitContentError(
            f"提交 {sha} 的说明或作者不能按{declared}解码（{exc.reason}）"
        ) from None


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

    - 每发现一个仓库即停止下探：仓库工作树内的普通目录与未登记的嵌套仓库不是独立仓库；
      **已初始化的子模块**例外，由 ``expand_submodules`` 按 ``.gitmodules`` 逐个展开（#221）；
    - **同一仓库只算一篇**：按「共同 git 目录」归并（``_common_git_dir``）——``git worktree
      add`` 出来的 linked worktree 与主仓库共用同一个共同目录。worktree 只是别名，永不成为
      文档：组内只保留主工作树（优先）或裸目录；主仓库不在 ``root`` 下时该仓库不出现，只记
      warn。裸仓库与它的克隆是两个仓库，各成一篇；
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
    repos = _one_per_repository(found)
    submodules, hidden = expand_submodules(repos)
    unreadable += hidden
    unreadable.sort(key=lambda item: item.identifier)
    return sorted(repos + submodules, key=lambda item: item.identifier), unreadable


def expand_submodules(repos: Sequence[RepoDir]) -> tuple[list[RepoDir], list[RepoDir]]:
    """各仓库工作树里**已初始化的子模块**（递归含嵌套子模块）：返回（子模块, 无法读取的项）。

    子模块各成一篇文档（#221），身份 = 父仓库身份 + ``/`` + 子模块路径，映射规则与独立仓库
    相同。判据：父仓库工作树里的 ``.gitmodules`` 登记了该路径，且该路径下有 ``.git``（文件或
    目录——老式子模块的 ``.git`` 是目录）。``.gitmodules`` 由 ``git config --file`` 解析（不
    自写 git-config 解析器；``--no-includes`` 保证只读这一个文件），只决定「发现」，不进内容。

    - 登记了但没有 ``.git``（未初始化）：只记 warn（只带相对路径），不是错误；
    - 路径是绝对路径、含 ``.`` / ``..`` / ``.git`` 段，或经过符号链接：记 warn 并跳过，不跟随；
    - ``.gitmodules`` 读不了或解析失败、子模块目录读不了：记入第二个返回值，调用方出条目级
      error，本轮 delete / move 按 fail-closed 暂缓（与「子孙目录读不了」一致）。
    """
    found: list[RepoDir] = []
    unreadable: list[RepoDir] = []
    pending = list(repos)
    while pending:
        parent = pending.pop()
        for submodule in _submodules_of(parent, unreadable):
            found.append(submodule)
            pending.append(submodule)
    found.sort(key=lambda item: item.identifier)
    unreadable.sort(key=lambda item: item.identifier)
    return found, unreadable


def _submodules_of(parent: RepoDir, unreadable: list[RepoDir]) -> list[RepoDir]:
    """一个仓库 ``.gitmodules`` 里登记、且已初始化的子模块（不递归）。

    「读不了」与「不存在」严格分开（fail-closed）：只有 ``FileNotFoundError`` /
    ``NotADirectoryError`` 算不存在，其余 ``OSError`` 一律记入 ``unreadable``——否则一次权限
    问题就会让已同步的子模块被当成消失而删除。因此不用 ``pathlib`` 的 ``exists`` / ``is_file``
    （Python 3.14 起它们把任何 ``OSError`` 吞成 ``False``），也不让 git 自己去读文件（``git
    config --file`` 对读不了的文件与「没有匹配」同样返回 1）：先读出字节，再经 stdin 交给 git。
    """
    gitmodules = Path(parent.path) / ".gitmodules"
    marker = RepoDir(path=str(gitmodules), identifier=parent.identifier + b"/.gitmodules")
    try:
        status = _lstat(gitmodules)
        if status is None or not (stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode)):
            return []
        if stat.S_ISLNK(status.st_mode):
            _log.warning("%s 是符号链接，不跟随，不展开子模块", _shown(marker.identifier))
            return []
        data = gitmodules.read_bytes()
        # 在父仓库里运行：git config 即便读 stdin 也会从当前目录发现仓库，插件进程的当前目录
        # 可能恰好是损坏的仓库（实测：.git 文件指向不存在的目录时返回 128）
        completed = _exec_git(
            [
                "-C",
                parent.path,
                "config",
                "--no-includes",
                "--file",
                "-",
                "--null",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            label="config --file .gitmodules",
            stdin=data,
        )
    except (OSError, GitError):
        unreadable.append(marker)
        return []
    if completed.returncode == 1:  # 内容已读到：1 只表示没有登记任何子模块路径
        return []
    if completed.returncode != 0:
        unreadable.append(marker)
        return []
    paths: set[bytes] = set()
    for record in completed.stdout.split(b"\x00"):
        _, newline, value = record.partition(b"\n")
        if not newline:
            continue
        relative = _submodule_path(value)
        if relative is None:
            _log.warning(
                "%s：子模块路径不合法（绝对路径或含 . / .. / .git 段），跳过",
                _shown(parent.identifier + b"/" + value),
            )
            continue
        paths.add(relative)  # 规范化之后去重：a//b 与 a/b/ 是同一个子模块
    found: list[RepoDir] = []
    for relative in sorted(paths):
        identifier = parent.identifier + b"/" + relative
        target = Path(parent.path)
        try:
            state = "present"
            for part in relative.split(b"/"):
                target = target / os.fsdecode(part)
                status = _lstat(target)
                if status is None:
                    state = "missing"
                    break
                if stat.S_ISLNK(status.st_mode):
                    state = "symlink"
                    break
            if state == "present" and _lstat(target / ".git") is None:
                state = "missing"
        except OSError:
            unreadable.append(RepoDir(path=str(target), identifier=identifier))
            continue
        if state == "present":
            found.append(RepoDir(path=str(target), identifier=identifier))
        elif state == "symlink":
            _log.warning("%s：子模块路径经过符号链接，不跟随，跳过", _shown(identifier))
        else:
            _log.warning("%s 是未初始化的子模块，不同步", _shown(identifier))
    return found


def _lstat(path: Path) -> os.stat_result | None:
    """``os.lstat``：不存在返回 ``None``，其余 ``OSError``（权限等）照抛，由调用方记为读不了。"""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _submodule_path(raw: bytes) -> bytes | None:
    """``.gitmodules`` 里的路径 → 规范的相对路径字节；不安全或不合法时 ``None``。"""
    if not raw or raw.startswith(b"/"):
        return None
    parts = [part for part in raw.split(b"/") if part]
    if not parts or any(part in (b".", b"..") or part.lower() == b".git" for part in parts):
        return None
    return b"/".join(parts)


def _shown(identifier: bytes) -> str:
    """日志里的身份（相对路径字节）：不是 UTF-8 的字节转义显示，不带绝对路径。"""
    return identifier.decode("utf-8", "backslashreplace")


#: ``_common_git_dir`` 的优先级里 linked worktree 的取值（它永不成为代表）
_LINKED_WORKTREE = 2


def _common_git_dir(directory: Path) -> tuple[str, int]:
    """候选目录的共同 git 目录（真实路径）与它在组内当代表的优先级（越小越优先）。

    只读 git 自己用的文件，不调 git：``.git`` 是文件时取 ``gitdir:`` 指向的目录，其中有
    ``commondir`` 即 linked worktree，共同目录是它所指的主仓库 git 目录；其余（``.git`` 目录、
    ``--separate-git-dir``、子模块的 modules 目录、裸仓库）的共同目录就是自身的 git 目录。
    优先级：0 主工作树（含 ``--separate-git-dir`` 的工作树）；1 没有 ``.git`` 的目录本身
    （裸仓库，或 ``--separate-git-dir`` 指向、本身打不开的 git 目录）；2 linked worktree。
    读不了或格式不对时按独立仓库处理（以自身为键），由 ``GitRepo.open`` 如实判定。
    """
    marker = directory / ".git"
    try:
        if marker.is_dir():
            return os.path.realpath(marker), 0
        if not marker.is_file():
            return os.path.realpath(directory), 1  # 裸仓库
        content = marker.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            return os.path.realpath(directory), 0
        gitdir = directory / content[len("gitdir:") :].strip()
        commondir = gitdir / "commondir"
        if commondir.is_file():
            target = commondir.read_text(encoding="utf-8").strip()
            return os.path.realpath(gitdir / target), _LINKED_WORKTREE
        return os.path.realpath(gitdir), 0
    except (OSError, UnicodeDecodeError):
        return os.path.realpath(directory), 0


def _one_per_repository(found: list[RepoDir]) -> list[RepoDir]:
    """同一共同 git 目录的候选只保留一个（见 ``discover_repos``），保持身份字节序。

    linked worktree 只是仓库的别名，一律不成为文档：代表只在主工作树与裸目录中选，否则
    worktree 的增删会改变代表、引发伪 move。主仓库不在 root 下时该仓库不出现，对每个这样的
    worktree 记一条 warn（部署形态问题，不是读取失败，不产出条目级错误）。
    """
    groups: dict[str, list[tuple[int, RepoDir]]] = {}
    for repo in found:
        common, rank = _common_git_dir(Path(repo.path))
        groups.setdefault(common, []).append((rank, repo))
    kept: list[RepoDir] = []
    for members in groups.values():
        owners = [member for member in members if member[0] != _LINKED_WORKTREE]
        if owners:
            kept.append(min(owners, key=lambda m: (m[0], m[1].identifier))[1])
            continue
        for _, worktree in members:
            _log.warning(
                "%s 是 linked worktree，它的主仓库不在 root 下，不同步；"
                "请把主工作树或裸仓库放进 root",
                _shown(worktree.identifier),
            )
    return sorted(kept, key=lambda item: item.identifier)


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


def root_commits(records: Sequence[CommitRecord]) -> tuple[str, ...]:
    """提交集合中的根提交（无父提交），按 SHA 升序——与 ``GitRepo.root_commits`` 同一口径。

    浅克隆的边界提交在 git 的输出里同样没有父提交（cauterize），两条路径都把它当作根。
    """
    return tuple(sorted(record.sha for record in records if not record.parents))


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

    def shallow_boundary(self) -> tuple[str, ...]:
        """浅克隆的边界提交（按 SHA 升序；非浅克隆为空）。

        边界决定克隆可见的历史：``git fetch --deepen`` 会在 tip 不变时改变提交集合，所以它
        属于仓库状态（``mapping.RepoState``），必须进指纹。
        """
        relative = self._run("rev-parse", "--git-path", "shallow").strip()
        path = Path(self.path) / relative
        try:
            raw = path.read_text(encoding="ascii")
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as exc:
            raise GitError(f"读取浅克隆边界失败：{path}（{exc}）") from None
        return tuple(sorted(line.strip().lower() for line in raw.splitlines() if line.strip()))

    def root_commits(self, tip: str) -> tuple[str, ...]:
        """``tip`` 可达的根提交（按 SHA 升序）：不读提交说明，比 ``log`` 便宜。

        与纯函数 ``root_commits(self.log(tip))`` 结果相同（测试钉住）；只用于给本轮新出现的
        仓库计算移动键——那时还没有读它的提交日志。
        """
        raw = self._run("rev-list", "--max-parents=0", "--end-of-options", tip)
        return tuple(sorted(line.strip().lower() for line in raw.splitlines() if line.strip()))

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
            # 输出编码钉死为 UTF-8、不混入签名校验：仓库自己的 i18n.logOutputEncoding /
            # log.showSignature 是本机配置，不得改变映射。声明了其他编码的提交另取对象的原始
            # 字节（``_verbatim``），不用这里经 iconv 转码的结果
            "--encoding=UTF-8",
            "--no-show-signature",
            f"--format={_LOG_FORMAT}",
            "--end-of-options",
            *revisions,
        )
        fields = raw.split(b"\x00")
        if fields and not fields[-1]:
            fields.pop()  # 末条记录之后的分隔符
        if len(fields) % _LOG_FIELDS != 0:
            raise GitError(f"git log 输出字段数不是 {_LOG_FIELDS} 的倍数：{len(fields)}")
        chunks = [
            fields[offset : offset + _LOG_FIELDS] for offset in range(0, len(fields), _LOG_FIELDS)
        ]
        legacy = [
            chunk[0].decode("ascii", "replace")
            for chunk in chunks
            if chunk[6] and not _is_utf8(chunk[6].decode("ascii", "replace"))
        ]
        verbatim = self._verbatim(legacy) if legacy else {}
        records: list[CommitRecord] = []
        for chunk in chunks:
            # 说明与作者 ident 按提交声明的编码严格解码（无 encoding 头即 UTF-8）：解不了如实
            # 报错（内容类失败，条目级 content_invalid），不做替换或猜测编码
            sha = chunk[0].decode("ascii", "replace")
            encoding = chunk[6].decode("ascii", "replace")
            name, email, body = verbatim.get(sha.lower(), (chunk[2], chunk[3], chunk[7]))
            author = f"{_decode(name, encoding, sha)} <{_decode(email, encoding, sha)}>"
            message = _decode(body, encoding, sha)
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

    def annotated_tags(self) -> tuple[tuple[str, str], ...]:
        """附注标签：``refs/tags/*`` 中直接指向标签对象的 ref → (名字, 标签对象 SHA)，按名字的
        UTF-8 字节序。轻量标签（直接指向提交等）只是名字、不是内容，不在其中（#220）。

        标签由 fetch 直接写进本地 ``refs/tags``，没有远端命名空间可优先：集合以部署方 fetch 的
        结果为准（README「已知限制」）。名字不是 UTF-8 时抛 ``GitContentError``。
        """
        raw = self._run_bytes(
            "for-each-ref", "--format=%(objecttype)%00%(objectname)%00%(refname)", "refs/tags/"
        )
        tags: list[tuple[str, str]] = []
        for line in raw.split(b"\n"):
            if not line:
                continue
            fields = line.split(b"\x00")
            if len(fields) != 3 or not fields[2].startswith(b"refs/tags/"):
                raise GitError("git for-each-ref 的输出格式不对")
            if fields[0] != b"tag":
                continue
            try:
                name = fields[2][len(b"refs/tags/") :].decode("utf-8")
            except UnicodeDecodeError:
                raise GitContentError("有标签的名字不是合法 UTF-8") from None
            tags.append((name, fields[1].decode("ascii").lower()))
        return tuple(sorted(tags, key=lambda tag: tag[0].encode("utf-8")))

    def tags(self, tags: Sequence[tuple[str, str]]) -> list[TagRecord]:
        """读标签对象（``annotated_tags`` 的结果）→ ``TagRecord``，顺序与传入相同。

        对象格式按 git 文档：头部（``object`` / ``type`` / ``tag`` / ``tagger``）、一个空行、说明。
        说明去掉末尾签名块后按 UTF-8 严格解码（标签对象没有 encoding 头的语义，git 也不对它
        转码）；解不了或格式不对抛 ``GitContentError``。
        """
        bodies = self._cat_objects([sha for _, sha in tags], "tag")
        records: list[TagRecord] = []
        for name, sha in tags:
            head, _, message = bodies[sha].partition(b"\n\n")
            lines = head.split(b"\n")
            target = next((line[7:] for line in lines if line.startswith(b"object ")), None)
            if target is None:
                raise GitContentError(f"标签 {name} 的对象缺少 object 头")
            tagger: str | None = None
            tagged_at: str | None = None
            line = next((line for line in lines if line.startswith(b"tagger ")), None)
            try:
                if line is not None:
                    who, email, rest = _split_ident(line[7:], f"标签 {name} 的 tagger 行")
                    tagger = f"{who.decode('utf-8')} <{email.decode('utf-8')}>"
                    tagged_at = _ident_time(rest, f"标签 {name} 的 tagger 行")
                text = strip_signature(message).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitContentError(
                    f"标签 {name} 的说明或 tagger 不是合法 UTF-8（{exc.reason}）"
                ) from None
            records.append(
                TagRecord(
                    name=name,
                    target=target.decode("ascii", "replace").strip().lower(),
                    tagger=tagger,
                    tagged_at=tagged_at,
                    message=text,
                )
            )
        return records

    def _verbatim(self, shas: list[str]) -> dict[str, tuple[bytes, bytes, bytes]]:
        """取声明了非 UTF-8 编码的提交的作者名、邮箱与说明的**原始字节**（README「确定性」）。

        ``git log`` 的输出编码不可移植：即便请求与提交声明相同的编码，musl 上的 git 仍会转码
        （实测），iconv 失败时还会悄悄原样输出。原始字节只能取自对象本身：按 git 文档规定的
        格式（头部、一个空行、说明）取 ``author`` 行与说明——只用于这类旧编码提交。
        """
        bodies = self._cat_objects(shas, "commit")
        out: dict[str, tuple[bytes, bytes, bytes]] = {}
        for sha in shas:
            head, _, message = bodies[sha].partition(b"\n\n")
            author = next((line for line in head.split(b"\n") if line.startswith(b"author ")), None)
            ident = author[len(b"author ") :] if author is not None else b""
            name, email, _ = _split_ident(ident, f"提交 {sha} 的 author 行")
            out[sha.lower()] = (name, email, message)
        return out

    def _cat_objects(self, shas: list[str], kind: str) -> dict[str, bytes]:
        """``git cat-file --batch`` 原样取一批对象的内容（SHA → 字节）；类型须是 ``kind``。"""
        if not shas:
            return {}
        raw = self._run_bytes(
            "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in shas).encode("ascii")
        )
        out: dict[str, bytes] = {}
        position = 0
        for sha in shas:
            newline = raw.find(b"\n", position)
            header = raw[position:newline].split(b" ") if newline >= 0 else []
            if len(header) != 3 or header[1] != kind.encode() or not header[2].isdigit():
                raise GitError(f"git cat-file 读不出{_KIND_NAMES[kind]} {sha}")
            size = int(header[2])
            out[sha] = raw[newline + 1 : newline + 1 + size]
            position = newline + 1 + size + 1  # 对象之后的换行
        return out

    # ------------------------------------------------------------------ 子进程

    def _exec(self, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return _exec_git(["-C", self.path, *args], label=" ".join(args), stdin=stdin)

    def _run_bytes(
        self, *args: str, allow_exit: tuple[int, ...] = (), stdin: bytes | None = None
    ) -> bytes:
        completed = self._exec(*args, stdin=stdin)
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


#: 最低 git 版本：``GIT_CONFIG_GLOBAL``（隔离全局配置，见 ``_git_env``）自 2.32 起才生效，更低的
#: 版本会静默读入使用者的全局配置，映射结果随之变化
MIN_GIT_VERSION = (2, 32, 0)
#: 版本探测的超时：``git --version`` 不读仓库，远小于扫描用的 ``_GIT_TIMEOUT``
_VERSION_TIMEOUT = 10.0
#: ``git --version`` 输出开头的版本号：``2.39.3 (Apple Git-146)``、``2.45.1.windows.1``、``2.32``
_GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def parse_git_version(text: str) -> tuple[int, int, int] | None:
    """解析 ``git --version`` 的输出；认不出时为 ``None``（调用方按无法确定处理）。"""
    match = _GIT_VERSION.match(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def git_version() -> tuple[int, int, int] | None:
    """本机 git 的版本（与扫描用同一环境）；输出认不出为 ``None``。

    git 不可用（不存在、不可执行、超时、退出码非零）抛 ``GitError``，消息带退出码与 stderr 摘要。
    """
    completed = _exec_git(["--version"], label="--version", timeout=_VERSION_TIMEOUT)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git --version 失败（退出码 {completed.returncode}）：{message}")
    return parse_git_version(completed.stdout.decode("utf-8", "replace"))


def _exec_git(
    args: list[str], *, label: str, timeout: float = _GIT_TIMEOUT, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """以插件环境运行一条 git 命令；git 起不来（不存在、不可执行等 ``OSError``）或超时都映射成
    ``GitError``，不让底层异常穿出 initialize / scan。"""
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            timeout=timeout,
            env=_git_env(),
            check=False,
            input=stdin,
        )
    except FileNotFoundError:
        raise GitError("找不到 git 可执行文件（PATH 中没有 git）") from None
    except OSError as exc:
        raise GitError(f"无法执行 git：{exc}") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {label} 超过 {timeout:.0f} 秒未完成") from None


def _git_env() -> dict[str, str]:
    """插件的 git 子进程环境：不交互、不写 ``.git``、不受全局配置影响。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    # 本机对提交图的改写（replace / grafts）不是源内容：映射只由对象本身决定
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    # 清掉会改写仓库发现的继承变量（git hook 等场景会带进来），使结果只由 repo 与仓库自身决定
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        env.pop(name, None)
    # 插件进程的环境由运行器构造（§4.3）；这里只保留其 PATH/凭据类变量，不额外清理。
    # 命令行级配置（GIT_CONFIG_COUNT 系列，部署方经 plugin.env 注入）保留：system / global
    # 已关闭，它是 safe.directory（跨 uid 的仓库）唯一能生效的作用域；会改变映射的输出项由
    # 命令参数显式钉死（log 的 --encoding / --no-show-signature），不靠清理环境
    return env
