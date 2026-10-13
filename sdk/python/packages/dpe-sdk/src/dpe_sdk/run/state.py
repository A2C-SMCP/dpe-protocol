"""独立运行器的状态目录（connector 契约 §4.4、§5.2、§7.2）。

每个实例的本地状态按 ``id`` 存放在 ``<状态目录>/instances/<id>/``：

- ``lock``：实例排他锁（§5.2）。非阻塞取得，取不到即 ``instance_busy``；用操作系统文件锁
  （POSIX ``flock``、Windows ``msvcrt.locking``），持有进程异常退出时自动释放。
- ``state.json``：游标、同步状态与意图日志（§7.2）、内容身份指纹（§4.4）与游标被扣住的
  起始时刻（§7.4 ``cursor_held_since``）。整份写临时文件后 ``os.replace``，``fsync`` 后才返回——
  对进程崩溃持久（§7.2 只要求到这个强度）。文件不存在即缓存丢失（首次运行或已按 §5.1 删除），
  以全量轮开始（§6.8）；读取失败与内容损坏（非 UTF-8、非 JSON、结构或版本不认识）都 MUST NOT
  当作缓存丢失（§4.4「状态目录故障」），见下。

``<状态目录>/registry.json`` 是「本运行器管理的实例」的登记（§3、§4.4）：启动实例时在目录级锁
（``.registry.lock``）内读登记、做前缀不重叠检查、写回，三步原子完成。登记与检查不能退化——
登记文件损坏时不当作空登记放行。

本地状态目录的故障（含登记、锁文件与 ``state.json``）一律是轮级 ``state_unavailable``——坏的是
本地状态目录，不是 remote：I/O 读写失败 ``retryable: true``；内容损坏 ``retryable: false``（重跑
不会自愈，由使用者按 §5.1 删除后以全量轮重建），且不覆盖损坏的内容。实例状态先写、写成功才生效
（``InstanceStore``）。

内容身份（§1：remote URL、URI 前缀、实例配置（JCS）、插件 name / version）变化时，游标、同步
状态与意图一并丢弃（§4.4「视为缓存丢失」）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import dpe_hash

from dpe_sdk.pusher import StateSnapshot
from dpe_sdk.run.errors import InstanceFailure, RoundFailure

__all__ = [
    "InstanceLock",
    "InstanceState",
    "InstanceStore",
    "StateDirectory",
    "content_identity",
    "default_state_dir",
]

_STATE_VERSION = 1
_REGISTRY_VERSION = 1


def default_state_dir() -> Path:
    """平台惯例的状态目录：``$XDG_STATE_HOME/dpe-run``（缺省 ``~/.local/state/dpe-run``）；
    Windows 为 ``%LOCALAPPDATA%\\dpe-run``。调用方可另行指定（§4.4 SHOULD 可由调用方指定）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "dpe-run"
    xdg = os.environ.get("XDG_STATE_HOME")
    base_dir = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "state"
    return base_dir / "dpe-run"


def content_identity(
    *,
    remote_url: str,
    uri_prefix: str,
    config: Mapping[str, Any],
    plugin_name: str,
    plugin_version: str,
) -> str:
    """实例内容身份的指纹（§1）：五项按 JCS 序列化后取 sha256。凭证、启动命令、冲突策略与
    运行器调优参数不属于内容身份，不进指纹。"""
    canonical = dpe_hash.jcs(
        {
            "remote_url": remote_url,
            "uri_prefix": uri_prefix,
            "config": dict(config),
            "plugin": {"name": plugin_name, "version": plugin_version},
        }
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- 文件锁与原子写


def _try_lock(handle: IO[bytes]) -> bool:
    """非阻塞取得排他锁；已被他人持有返回 False。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _lock_blocking(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _open_lock_file(path: Path) -> IO[bytes]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+b")


def _write_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """写临时文件、``fsync``、``os.replace``：读者只会看到完整的旧版本或新版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # 失败不留半成品；清理失败不掩盖原始异常
            tmp.unlink(missing_ok=True)
        raise


class InstanceLock:
    """已取得的实例排他锁；``release`` 或进程退出即释放。"""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle: IO[bytes] | None = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()  # 关闭描述符即释放 flock / msvcrt 锁
            self._handle = None

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------- 实例状态


@dataclass
class InstanceState:
    """``state.json`` 的内容（§4.4、§7.2）。``held_since`` 为 §7.4 的 ``cursor_held_since``。"""

    identity: str
    cursor: str | None = None
    synced: dict[str, str] = field(default_factory=dict)
    intents: dict[str, str | None] = field(default_factory=dict)
    held_since: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "identity": self.identity,
            "cursor": self.cursor,
            "synced": self.synced,
            "intents": self.intents,
            "held_since": self.held_since,
        }

    @classmethod
    def from_json(cls, data: Any) -> InstanceState | None:
        """形状或版本不对即返回 ``None``，由调用方按内容损坏处理（§4.4，不当作缓存丢失），
        不做部分恢复。"""
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return None
        identity, cursor, synced, intents, held = (
            data.get("identity"),
            data.get("cursor"),
            data.get("synced"),
            data.get("intents"),
            data.get("held_since"),
        )
        if (
            not isinstance(identity, str)
            or not (cursor is None or isinstance(cursor, str))
            or not isinstance(synced, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in synced.items())
            or not isinstance(intents, dict)
            or not all(
                isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in intents.items()
            )
            or not (held is None or isinstance(held, str))
        ):
            return None
        return cls(identity, cursor, dict(synced), dict(intents), held)


class InstanceStore:
    """一个实例的持久状态；同时是推送器的 ``StateStore``（§7.2 的同步状态与意图日志）。

    **先写、写成功才生效**：每次变更都在 ``state.json`` 落盘之后才替换内存中的状态，写失败时
    内存保持原样（同推送器 #134 的口径）。推送器据此保证「意图持久化后才发写」。写失败与
    非「文件不存在」的读失败都是 ``state_unavailable``（轮级，可重试）——坏的是本地状态目录，
    不是 remote。状态只属于一个实例、一个 remote（内容身份含 remote URL），``load`` /
    ``save`` 的 ``remote`` 参数因此不再区分命名空间。
    """

    def __init__(self, path: Path, state: InstanceState, *, reset: bool) -> None:
        self._path = path
        self._state = state
        #: 打开时是否按缓存丢失处理（首次、内容不可识别或内容身份变化）——本轮必为全量轮
        self.reset = reset

    @classmethod
    def open(cls, path: Path, identity: str) -> InstanceStore:
        """读回状态（§4.4）。

        - 文件不存在：首次运行（或已按 §5.1 删除），即缓存丢失；
        - 读取失败（权限、I/O）：``state_unavailable``，``retryable: true``；
        - 内容损坏（非 UTF-8、非 JSON、结构或版本不符）：``state_unavailable``，
          ``retryable: false``——二者都 MUST NOT 当作缓存丢失，损坏的内容原样保留；
        - 内容身份不同：缓存丢失，丢弃游标、同步状态与意图（落盘由 ``commit_identity`` 完成）。

        ``held_since`` 记录的是同一实例 ``id`` 的游标被扣住的时刻，与内容身份无关，予以保留。
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(path, InstanceState(identity), reset=True)
        except OSError as exc:
            raise _unavailable(f"读取实例状态失败：{exc}") from None
        try:
            loaded = InstanceState.from_json(json.loads(raw.decode("utf-8")))
        except ValueError:  # 含 UnicodeDecodeError 与 JSON 解析失败
            loaded = None
        if loaded is None:
            raise _corrupt(f"实例状态内容损坏，无法解析：{path}")
        if loaded.identity == identity:
            return cls(path, loaded, reset=False)
        return cls(path, InstanceState(identity, held_since=loaded.held_since), reset=True)

    def commit_identity(self) -> None:
        """在启动插件之前持久化内容身份指纹（§4.4）。

        指纹缺失或与当前内容身份不同（``reset``）时，把重置后的状态——空的游标、同步状态与意图，
        加上新指纹——一次原子写入：丢弃旧状态与写入新指纹同时完成，中途失败时下次启动仍会重新
        切换。指纹未变时不写。
        """
        if self.reset:
            self._commit(self._state)
            self.reset = False

    @property
    def cursor(self) -> str | None:
        return self._state.cursor

    @property
    def held_since(self) -> str | None:
        return self._state.held_since

    def load(self, remote: str) -> StateSnapshot | None:
        return StateSnapshot(synced=dict(self._state.synced), intents=dict(self._state.intents))

    def save(self, remote: str, snapshot: StateSnapshot) -> None:
        self._commit(
            dataclasses.replace(
                self._state, synced=dict(snapshot.synced), intents=dict(snapshot.intents)
            )
        )

    def finish_round(self, *, succeeded: bool, cursor: str | None, started_at: str) -> str | None:
        """一轮收尾的唯一一次落盘：游标与 ``cursor_held_since`` 一并写入，返回报告应给出的
        ``cursor_held_since``。

        - ``succeeded`` 时以 ``cursor`` 为新游标（§6.8，``None`` 即不缓存、下一轮全量）并清空
          ``held_since``；否则游标保持不变，``held_since`` 没有时以本轮 ``started_at`` 为起点
          （§7.4）。只在已取得实例锁的轮上调用（``instance_busy`` 不读写该记录）。
        - 同步状态与意图由推送器在本轮内逐次保存，早于这次写入：游标不会先于同步状态前进。
        """
        if succeeded:
            state = dataclasses.replace(self._state, cursor=cursor, held_since=None)
        elif self._state.held_since is None:
            state = dataclasses.replace(self._state, held_since=started_at)
        else:
            state = self._state
        self._commit(state)
        return state.held_since

    def _commit(self, state: InstanceState) -> None:
        try:
            _write_atomic(self._path, state.to_json())
        except OSError as exc:
            raise _unavailable(f"写入实例状态失败：{exc}") from None
        self._state = state


def _unavailable(message: str) -> RoundFailure:
    """本地状态读写的 I/O 失败（§4.4「读取失败」）：重跑可能成功。"""
    return RoundFailure("state_unavailable", message, retryable=True)


def _corrupt(message: str) -> RoundFailure:
    """本地状态内容损坏（§4.4：读取成功但无法解析或结构不符）：重跑不会自愈，由使用者按 §5.1
    删除该实例的状态后以全量轮重建。MUST NOT 当作缓存丢失，也不覆盖损坏的内容。"""
    return RoundFailure("state_unavailable", message, retryable=False)


# ---------------------------------------------------------------------- 状态目录


class StateDirectory:
    """独立运行器的状态目录（§4.4）。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _instance_dir(self, instance_id: str) -> Path:
        return self.path / "instances" / instance_id

    def lock_instance(self, instance_id: str) -> InstanceLock:
        """非阻塞取得实例排他锁（§5.2）；已有轮在进行即 ``instance_busy``（轮级，可重试）。"""
        try:
            handle = _open_lock_file(self._instance_dir(instance_id) / "lock")
        except OSError as exc:
            raise _unavailable(f"无法打开实例锁文件：{exc}") from None
        try:
            acquired = _try_lock(handle)
        except OSError as exc:  # 锁本身不可用（如网络文件系统的 ENOLCK），不是「已被占用」
            handle.close()
            raise _unavailable(f"无法取得实例锁：{exc}") from None
        if not acquired:
            handle.close()
            raise RoundFailure("instance_busy", f"实例 {instance_id} 已有轮在进行", retryable=True)
        return InstanceLock(handle)

    @contextlib.contextmanager
    def _registry_lock(self) -> Iterator[None]:
        handle = _open_lock_file(self.path / ".registry.lock")
        try:
            _lock_blocking(handle)
            yield
        finally:
            handle.close()

    def _read_registry(self) -> dict[str, dict[str, str]]:
        path = self.path / "registry.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:  # 含 UnicodeDecodeError
            data = None
        instances = data.get("instances") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("version") != _REGISTRY_VERSION
            or not isinstance(instances, dict)
            or not all(
                isinstance(entry, dict)
                and isinstance(entry.get("remote_url"), str)
                and isinstance(entry.get("uri_prefix"), str)
                for entry in instances.values()
            )
        ):
            # 登记是前缀不重叠检查的依据，损坏时不能当作空登记放行（§4.4 内容损坏）
            raise _corrupt(f"状态目录登记文件不可识别：{path}")
        return instances

    # TODO(#52 Part 2): 接入 CLI 之前，按 #112 的 remote.url 文法比较（当前按 §3 的字面相同），
    # 并提供注销路径（§5.1「删除」），否则已删除的实例会永久占住前缀
    def register(self, instance_id: str, *, remote_url: str, uri_prefix: str) -> None:
        """登记实例并检查前缀不重叠（§3、§4.4）：同一 ``remote.url`` 下任意两个实例的前缀不得
        互为前缀（含相等）；重叠即 ``definition_invalid``。读登记、检查、写回在目录级锁内完成；
        登记读写失败或登记不可识别是 ``state_unavailable``（损坏的登记不当作空登记放行）。"""
        try:
            self._register(instance_id, remote_url, uri_prefix)
        except OSError as exc:
            raise _unavailable(f"读写状态目录登记失败：{exc}") from None

    def _register(self, instance_id: str, remote_url: str, uri_prefix: str) -> None:
        with self._registry_lock():
            instances = self._read_registry()
            for other_id, entry in instances.items():
                if other_id == instance_id or entry["remote_url"] != remote_url:
                    continue
                other = entry["uri_prefix"]
                if other.startswith(uri_prefix) or uri_prefix.startswith(other):
                    raise InstanceFailure(
                        "definition_invalid",
                        f"URI 前缀 {uri_prefix!r} 与同一 remote 下的实例 {other_id}"
                        f"（{other!r}）重叠",
                    )
            instances[instance_id] = {"remote_url": remote_url, "uri_prefix": uri_prefix}
            _write_atomic(
                self.path / "registry.json",
                {"version": _REGISTRY_VERSION, "instances": instances},
            )

    def open_instance(self, instance_id: str, identity: str) -> InstanceStore:
        """打开实例状态（须已持有该实例的锁）。"""
        return InstanceStore.open(self._instance_dir(instance_id) / "state.json", identity)
