"""Git 仓库访问：以命令行 git 为唯一后端（不引入 git 库、不自解析对象格式）。

枚举与读取只用 plumbing：

- ``git rev-parse --verify <ref>^{commit}``：把 ref 解析成提交（``HEAD``、分支、标签、sha 均可）；
- ``git ls-tree -r -z --long <commit>``：整棵树的路径、``blob`` 类型与字节数。``-z`` 是必需的：
  不带它时 git 会把不可打印字节按 C 转义（``"bad_\\377\\376.txt"``），路径就不再用原字节表达；
- ``git cat-file blob <sha>``：读单个 blob 的字节（本包只按顺序读，不分块）。

路径一律按**原始字节**处理（``ls-tree -z`` 的输出按 ``b"\\0"`` 切分、绝不 decode）：Git 的路径
就是字节串，可能不是 UTF-8。非 UTF-8 路径的文件仍会被枚举（file_uri 对字节做百分号编码），
只有 ``file_uri`` 需要是 ASCII。

子进程环境显式构造：``GIT_TERMINAL_PROMPT=0``（任何交互式询问都直接失败，不挂住插件）、
``GIT_OPTIONAL_LOCKS=0``（只读访问不写 ``.git``）、以及空全局配置（``GIT_CONFIG_GLOBAL``/
``GIT_CONFIG_NOSYSTEM``），使结果只由仓库自身决定。**不接触任何凭证**：本连接器以本机 git 的
方式来取内容（路径或 URL 由使用者配置，git 自行处理其协议），连接器不读环境里的 token。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FileEntry",
    "GitError",
    "GitRepo",
]

#: git 命令的超时（秒）：枚举与单文件读取都必须有界，插件不因数据源卡死而挂住整轮
_GIT_TIMEOUT = 120.0


class GitError(Exception):
    """git 命令失败或输出不合法（``source_unavailable`` 的候选）。"""


@dataclass(frozen=True)
class FileEntry:
    """树里的一个 blob：原始路径字节、对象 id 与字节数。"""

    path: bytes
    object_id: str
    size: int


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


class GitRepo:
    """一个 git 仓库在某个提交上的只读视图。"""

    def __init__(self, repo: str, ref: str) -> None:
        self.repo = repo
        self.ref = ref
        self.commit: str | None = None

    # ------------------------------------------------------------------ 打开与校验

    def open(self) -> str:
        """校验仓库与 ref，返回解析出的提交 id（结果缓存）。"""
        if not Path(self.repo).is_dir():
            raise GitError(f"仓库路径不可用：{self.repo}")
        return self.resolve()

    def resolve(self) -> str:
        """（重新）把 ``ref`` 解析成提交 id 并缓存——**轮首调用**。

        宿主可以跨轮保留插件进程（契约 §6.3 只对「内容身份变化」强制重启），此时 ``ref``
        指向的分支可能已经前进：每轮重新解析才能反映新提交。轮内保持钉死（同一批次序列必须
        来自同一个提交快照）。

        ``rev-parse`` 在仓库不可用与 ref 不可解析时都失败，这里先按 ``--git-dir`` 区分两种
        情形再解析 ref，好让失败消息可诊断。
        """
        try:
            # 裸仓库（--mirror / --bare）没有工作树，不能用 --show-toplevel 判定
            git_dir = self._run("rev-parse", "--absolute-git-dir").strip()
            is_bare = self._run("rev-parse", "--is-bare-repository").strip() == "true"
            # 配置的路径必须是仓库根：指向子目录时 git 仍能工作，但 ls-tree 列出的路径是
            # 相对配置目录的（前缀丢失），同一文件会得到不同的 file_uri——身份必须唯一且稳定
            root = git_dir if is_bare else self._run("rev-parse", "--show-toplevel").strip()
        except GitError as exc:
            raise GitError(f"不是可用的 git 仓库：{self.repo}（{exc}）") from None
        if not root:
            raise GitError(f"不是可用的 git 仓库：{self.repo}")
        # 按 dev/inode 比较而不是字面路径：同一个目录的符号链接形式（macOS 的 /var → /private/var）
        # 与大小写变体（大小写不敏感的文件系统）都要认作仓库根，否则会把可用配置误拒
        try:
            same = os.path.samefile(self.repo, root)
        except OSError:
            same = False
        if not same:
            raise GitError(f"repo 必须是仓库根目录（配置为 {self.repo}，实际根目录为 {root}）")
        try:
            resolved = self._run(
                "rev-parse", "--verify", "--quiet", "--end-of-options", f"{self.ref}^{{commit}}"
            )
        except GitError:
            raise GitError(f"ref 无法解析为提交：{self.ref}（空仓库或 ref 不存在）") from None
        self.commit = resolved.strip().lower()
        return self.commit

    # ------------------------------------------------------------------ 枚举与读取

    def list_blobs(self) -> list[FileEntry]:
        """整棵树的 blob，按路径字节序升序（枚举确定，契约 §2）。

        ``ls-tree -r -z --long`` 一条记录的格式是
        ``<mode> SP <type> SP <object> SP+ <size> TAB <path>``；按首个 TAB 切分取路径，按空白
        切分取 mode、类型、oid 与大小。只保留普通文件（mode ``100644`` / ``100755``）：

        - **子模块**（``commit`` 类型，mode ``160000``）不枚举——它的内容不在本仓库里；
        - **符号链接**（mode ``120000``）不枚举——它的「内容」是链接目标的路径字符串，不是源文件
          的内容；枚举它会让 DPE 里出现一篇内容是路径的文档（映射规则见 README）。
        """
        commit = self.open()
        raw = self._run_bytes("ls-tree", "-r", "-z", "--long", commit)
        entries: list[FileEntry] = []
        for record in raw.split(b"\x00"):
            if not record:
                continue
            head, tab, path = record.partition(b"\t")
            if not tab:
                raise GitError(f"ls-tree 输出缺少路径分隔符：{record[:80]!r}")
            fields = head.split()
            if len(fields) < 4:
                raise GitError(f"ls-tree 输出字段不足：{head[:80]!r}")
            mode, kind, object_id, size_text = fields[0], fields[1], fields[2], fields[3]
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                continue
            try:
                size = int(size_text)
            except ValueError:
                raise GitError(f"ls-tree 的 size 不是整数：{size_text!r}") from None
            entries.append(FileEntry(path=path, object_id=object_id.decode("ascii"), size=size))
        entries.sort(key=lambda entry: entry.path)
        return entries

    def read_blob(self, object_id: str, size: int) -> bytes:
        """按对象 id 读一个 blob 的原始字节；读到的长度与 ``size`` 不符即失败。"""
        raw = self._run_bytes("cat-file", "blob", object_id)
        if len(raw) != size:
            raise GitError(f"blob {object_id} 长度 {len(raw)} 与 ls-tree 的 {size} 不符")
        return raw

    # ------------------------------------------------------------------ 子进程

    def _run(self, *args: str) -> str:
        raw = self._run_bytes(*args)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitError(f"git {' '.join(args)} 的输出不是 UTF-8") from None

    def _run_bytes(self, *args: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", self.repo, *args],
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                env=_git_env(),
                check=False,
            )
        except FileNotFoundError:
            raise GitError("找不到 git 可执行文件（PATH 中没有 git）") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} 超过 {_GIT_TIMEOUT:.0f} 秒未完成") from None
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args)} 失败（退出码 {completed.returncode}）：{message}")
        return completed.stdout
