"""数据源：发现 ``root`` 下的 ``.md`` 文件，把一个文件读成文档（含图片字节的快照）。

- **发现**：递归、不跟随符号链接、按路径字节序；隐藏目录与文件（``.`` 开头）缺省跳过；
  读不了的目录记下来（由调用方产出条目级 error，不静默跳过，契约 §6.5）。
- **文档**：``file_type`` 为 ``md``，不写 ``title``（身份只在 ``file_uri`` 里）；页与元素见
  ``markdown``。Image 元素的字节在**读文件的这一刻**整体读进内存，作为本轮的快照：句柄指向
  这份字节，轮内任意偏移重复读取都一致（§6.7 快照一致性），源文件之后再变也不影响。
- **图片只读 ``root`` 之内的普通文件**：路径解析后（含 ``..``、绝对路径）落在 ``root`` 之外，
  或是符号链接，就不当作图片，整段按 ``NarrativeText`` 原样保留——插件不替 md 作者把
  ``root`` 之外的本地文件（如 ``~/.ssh``）读出来送上 remote；隐藏路径、管道、目录同理（见
  ``_read_image``）。图片文件不存在时产出**不带 ``blob`` 与 ``mime_type`` 的 Image**（源确实
  没有字节，§6.6 合法），而不是让整篇失败：失效的链接是确定的内容，不是暂时读不到。
- **摘要**：md 字节的 sha256，后接每张图片的定长记录（类型 + 字节的 sha256）——只用于判断
  内容是否变了与改名配对，不是 DPE hash，也不进入任何内容（hash 由运行器计算，§6.6）。每轮都读内容、
  比摘要，不靠 mtime/size 跳过：时间戳有精度、可被保留（``cp -p``），符号链接还有目标，
  凭它跳过会漏掉真实的变化（§6.8：增量轮 MUST 产出一切变化）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dpe_markdown_dir.markdown import parse_markdown

__all__ = ["Built", "ContentError", "Found", "SourceError", "build_document", "discover"]

#: 扩展名 → mime_type：固定表（``mimetypes`` 依赖系统注册表，同一文件在不同机器上可能得到
#: 不同的值，会制造伪变更）
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
_DEFAULT_MIME = "application/octet-stream"


class SourceError(Exception):
    """读取源文件失败（文件在读的过程中消失、无权限）：条目级 ``source_unavailable``，可重试。"""


class ContentError(SourceError):
    """源内容无法映射（md 不是 UTF-8）：条目级 ``content_invalid``，不可重试——重跑不会成功。"""


@dataclass(frozen=True)
class Found:
    """发现的一个条目：绝对路径与身份字节（相对 ``root`` 的路径，以 ``/`` 分隔）。"""

    path: str
    identifier: bytes


@dataclass
class Built:
    """读出的一篇文档：页（Image 已带句柄或不带 blob）、句柄 → 字节、内容摘要。"""

    pages: list[dict[str, Any]]
    digest: str
    blobs: dict[str, bytes] = field(default_factory=dict)


def discover(root: str, include_hidden: bool = False) -> tuple[list[Found], list[Found]]:
    """返回（``.md`` 文件, 读不了的目录），各按身份字节序。``root`` 自身读不了即抛出。"""
    found: list[Found] = []
    unreadable: list[Found] = []

    def walk(directory: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            if directory == root:
                raise SourceError(f"读取 root 失败：{exc}") from None
            unreadable.append(Found(directory, _identifier(root, directory)))
            return
        for entry in entries:
            if not include_hidden and entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path)
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                    found.append(Found(entry.path, _identifier(root, entry.path)))
            except OSError:
                unreadable.append(Found(entry.path, _identifier(root, entry.path)))

    walk(root)
    found.sort(key=lambda item: item.identifier)
    unreadable.sort(key=lambda item: item.identifier)
    return found, unreadable


def _identifier(root: str, path: str) -> bytes:
    return os.fsencode(os.path.relpath(path, root)).replace(os.fsencode(os.sep), b"/")


def _hidden(path: str) -> bool:
    return any(part.startswith(".") and part not in (".", "..") for part in path.split("/"))


class _NotAnImage(Exception):
    """引用不当作图片读取（越出 root、隐藏路径、符号链接、不是普通文件、路径本身不合法）。"""


def _read_image(root: str, base: str, ref: str, include_hidden: bool) -> bytes | None:
    """按「只读 root 之内的普通文件」读一张图片：返回字节；不存在返回 ``None``；不当作图片
    抛 ``_NotAnImage``；读失败（如无权限，修好即可读）抛 ``SourceError``。

    先判定、再读，判定只看文件本身的类型，不依赖各平台不同的 errno：

    1. 路径按真实路径（``..``、绝对路径、符号链接目录都已解开）必须在 ``root`` 之内；
       ``include_hidden`` 为 false 时路径段不得以 ``.`` 开头（不替 md 作者读出 ``.env`` 等）；
    2. ``lstat`` 判类型：不存在（含中间段不是目录）→ 缺失；不是普通文件（符号链接、目录、
       管道、socket……）→ 不当作图片；路径本身不合法（NUL、超长）→ 不当作图片——这些都由
       md 里写的路径确定，不是暂时读不到；
    3. 打开（不跟随最后一段的符号链接、不阻塞），对打开的文件再核对：是普通文件，且与第 2 步
       ``lstat`` 判定的是同一个文件（设备号 + inode）——判定之后最后一段被换掉，读不到别的
       文件。**不防**判定与打开之间中间目录被换成指向 root 之外的符号链接（那需要逐段以
       ``dir_fd`` + ``O_NOFOLLOW`` 打开）；示例取简单，见 README「已知限制」。

    「不当作图片」只用于由路径本身确定的情形（不存在的中间段、NUL、超长、符号链接循环）；
    其余 I/O 错误（如网络盘的 ``EIO`` / ``ESTALE``）是暂时读不到，报 ``SourceError``——否则
    一次偶发错误会把 Image 改写成文本，制造伪变更。
    """
    try:
        target = os.path.normpath(os.path.join(base, ref))
        real_root = os.path.realpath(root)
        real = os.path.realpath(target)
        if os.path.commonpath([real_root, real]) != real_root:
            raise _NotAnImage
        if not include_hidden and (_hidden(ref) or _hidden(os.path.relpath(real, real_root))):
            raise _NotAnImage  # 字面路径与真实路径都不得经过隐藏段
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except ValueError:  # 路径含 NUL 等
        raise _NotAnImage from None
    except OSError as exc:
        if exc.errno in (errno.ENAMETOOLONG, errno.ELOOP):  # 由路径本身确定
            raise _NotAnImage from None
        raise SourceError(f"图片 {ref} 读取失败：{exc}") from None
    if not stat.S_ISREG(info.st_mode):
        raise _NotAnImage
    try:
        return _read_regular(target, info)
    except _NotRegular:
        raise _NotAnImage from None  # 判定之后被替换：读到的不是判定过的那个文件
    except FileNotFoundError:
        return None  # 判定之后才被删除
    except OSError as exc:
        raise SourceError(f"图片 {ref} 读取失败：{exc}") from None


class _NotRegular(Exception):
    """打开的不是 ``lstat`` 判定过的那个普通文件（中途被换成符号链接、管道等）。"""


def _read_regular(path: str, info: os.stat_result) -> bytes:
    """读一个已经 ``lstat`` 判定为普通文件的路径——md 正文与图片共用这一个读法：

    打开时不跟随最后一段的符号链接、不阻塞（遇到管道不会卡住），对打开的文件核对仍是普通
    文件、且与判定时是同一个文件（设备号 + inode），否则抛 ``_NotRegular``——判定之后最后一段
    被换成指向 root 之外或隐藏文件的符号链接，也读不到它。其余 ``OSError`` 原样抛出。
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | _NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise _NotRegular
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


#: 打开时不阻塞（遇到 FIFO 等特殊文件不会卡在 open 上；普通文件不受影响）
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
#: 摘要里每张图片记一条定长记录：1 字节类型 + 32 字节——与 md 字节的摘要分开拼接，不会因为
#: 拼接产生歧义（不同的文件得到相同摘要）
_IMAGE_BYTES, _IMAGE_MISSING, _IMAGE_TEXT = b"B", b"M", b"T"
_NO_DIGEST = bytes(32)


def build_document(
    root: str, path: str, new_handle: Callable[[], str], include_hidden: bool = False
) -> Built:
    """读一个 md 文件成文档。``new_handle`` 给出新句柄（在插件进程内唯一）。

    md 正文与图片同样按「只读普通文件」读取（``_read_regular``）：发现之后、读取之前它被换成
    符号链接或管道，就不读——那已不是发现时的那个 md 文件，本轮报可重试的错误，下一轮的
    发现会按它现在的样子处理（符号链接不会被发现为 md 文件）。
    """
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise _NotRegular
        data = _read_regular(path, info)
    except _NotRegular:
        raise SourceError("已不是普通文件（发现之后被替换）") from None
    except OSError as exc:
        raise SourceError(f"读取失败：{exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ContentError("不是 UTF-8 文本") from None
    pages, images = parse_markdown(text)
    digest = hashlib.sha256(hashlib.sha256(data).digest())
    blobs: dict[str, bytes] = {}
    base = os.path.dirname(path)
    for image in images:
        element = pages[image.page][image.element]
        try:
            content = _read_image(root, base, image.path, include_hidden)
        except _NotAnImage:
            # 不当作图片：整段按普通文本原样保留
            pages[image.page][image.element] = {"category": "NarrativeText", "text": image.source}
            digest.update(_IMAGE_TEXT + _NO_DIGEST)
            continue
        if content is None:
            # 源确实没有字节：不带 blob 的 Image（§6.6）；没有字节也就没有 mime_type
            digest.update(_IMAGE_MISSING + _NO_DIGEST)
            continue
        handle = new_handle()
        blobs[handle] = content
        element["mime_type"] = _MIME.get(os.path.splitext(image.path)[1].lower(), _DEFAULT_MIME)
        element["blob"] = {"handle": handle, "size": len(content)}
        digest.update(_IMAGE_BYTES + hashlib.sha256(content).digest())
    return Built(
        pages=[{"elements": elements} for elements in pages],
        digest=digest.hexdigest()[:32],
        blobs=blobs,
    )
