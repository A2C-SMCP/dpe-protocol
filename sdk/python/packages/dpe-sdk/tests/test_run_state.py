"""状态目录（connector 契约 §4.4、§5.2、§7.2）：实例锁、前缀登记、内容身份与持久化。"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.pusher import StateSnapshot
from dpe_sdk.run import (
    InstanceFailure,
    RoundFailure,
    StateDirectory,
    content_identity,
    default_state_dir,
)
from dpe_sdk.run import state as state_module

REMOTE = "https://dpe.example.com/r/1"


def identity(
    *,
    remote_url: str = REMOTE,
    uri_prefix: str = "dpe://src/",
    config: dict[str, Any] | None = None,
    plugin_name: str = "p",
    plugin_version: str = "1",
) -> str:
    return content_identity(
        remote_url=remote_url,
        uri_prefix=uri_prefix,
        config={"repo": "x"} if config is None else config,
        plugin_name=plugin_name,
        plugin_version=plugin_version,
    )


def test_default_state_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX 约定")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_state_dir() == tmp_path / "dpe-run"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")  # 非绝对路径按未设置处理
    assert default_state_dir() == Path.home() / ".local" / "state" / "dpe-run"


def test_content_identity_covers_exactly_the_identity_fields() -> None:
    base = identity()
    assert identity(config={"repo": "x"}) == base  # JCS：同一内容同一指纹
    assert identity(remote_url=REMOTE + "/") != base
    assert identity(uri_prefix="dpe://other/") != base
    assert identity(config={"repo": "y"}) != base
    assert identity(plugin_name="q") != base
    assert identity(plugin_version="2") != base


# ---------------------------------------------------------------------- 实例锁


def test_instance_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    lock = directory.lock_instance("a")
    with pytest.raises(RoundFailure) as busy:
        directory.lock_instance("a")
    assert busy.value.code == "instance_busy" and busy.value.retryable
    directory.lock_instance("b").release()  # 不同实例互不影响
    lock.release()
    with directory.lock_instance("a"):
        pass


def test_lock_is_released_when_the_holder_process_dies(tmp_path: Path) -> None:
    """锁由操作系统文件锁承载：持有进程被杀死后自动释放（§5.2）。"""
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from dpe_sdk.run import StateDirectory
        lock = StateDirectory(Path({str(tmp_path)!r})).lock_instance("a")
        print("locked", flush=True)
        time.sleep(60)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(RoundFailure):
            StateDirectory(tmp_path).lock_instance("a")
        holder.kill()
        holder.wait(10)
        StateDirectory(tmp_path).lock_instance("a").release()
    finally:
        if holder.poll() is None:
            holder.kill()


# ---------------------------------------------------------------------- 前缀登记


def test_registration_rejects_overlapping_prefixes_on_the_same_remote(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    directory.register("a", remote_url=REMOTE, uri_prefix="dpe://src/docs/")
    directory.register("a", remote_url=REMOTE, uri_prefix="dpe://src/docs/")  # 重复登记自己
    directory.register("b", remote_url=REMOTE, uri_prefix="dpe://src/other/")
    directory.register("c", remote_url=REMOTE + "2", uri_prefix="dpe://src/")  # 不同 remote
    for prefix in ("dpe://src/docs/", "dpe://src/", "dpe://src/docs/sub/"):
        with pytest.raises(InstanceFailure) as overlap:
            directory.register("d", remote_url=REMOTE, uri_prefix=prefix)
        assert overlap.value.code == "definition_invalid"


def test_corrupt_registry_is_not_treated_as_empty(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    directory.register("a", remote_url=REMOTE, uri_prefix="dpe://src/")
    (tmp_path / "registry.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RoundFailure) as broken:
        directory.register("b", remote_url=REMOTE, uri_prefix="dpe://src/")
    assert broken.value.code == "state_unavailable"


# ---------------------------------------------------------------------- 实例状态


def test_state_persists_and_identity_change_drops_the_cache(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    store = directory.open_instance("a", identity())
    assert store.reset and store.cursor is None
    store.save(REMOTE, StateSnapshot(synced={"dpe://src/x": "dpe1:" + "1" * 64}, intents={}))
    store.finish_round(succeeded=True, cursor="c1", started_at="2026-10-12T00:00:00Z")
    store.finish_round(succeeded=False, cursor="c2", started_at="2026-10-12T01:00:00Z")

    reopened = directory.open_instance("a", identity())
    assert not reopened.reset and reopened.cursor == "c1"  # 失败轮不推进游标
    assert reopened.held_since == "2026-10-12T01:00:00Z"
    snapshot = reopened.load(REMOTE)
    assert snapshot is not None and snapshot.synced == {"dpe://src/x": "dpe1:" + "1" * 64}

    changed = directory.open_instance("a", identity(config={"repo": "y"}))
    assert changed.reset and changed.cursor is None
    assert changed.load(REMOTE) == StateSnapshot()
    assert changed.held_since == "2026-10-12T01:00:00Z"  # 按实例 id 计，不随内容身份丢弃


def test_held_since_keeps_the_first_failed_round_until_success(tmp_path: Path) -> None:
    store = StateDirectory(tmp_path).open_instance("a", identity())
    assert store.finish_round(succeeded=False, cursor=None, started_at="t1") == "t1"
    assert store.finish_round(succeeded=False, cursor=None, started_at="t2") == "t1"
    assert store.finish_round(succeeded=True, cursor=None, started_at="t3") is None


@pytest.mark.parametrize(
    "content",
    [b"garbage", b"\xff\xfe garbage", b'{"version": 99}', b'{"version": 1, "identity": 3}'],
)
def test_corrupt_state_is_not_a_cache_loss(tmp_path: Path, content: bytes) -> None:
    """§4.4：读取成功但无法解析或结构不符即内容损坏——state_unavailable、不可重试，不降级为
    全量轮，也不覆盖损坏的内容。"""
    directory = StateDirectory(tmp_path)
    directory.open_instance("a", identity()).finish_round(
        succeeded=True, cursor="c1", started_at="t"
    )
    path = tmp_path / "instances" / "a" / "state.json"
    path.write_bytes(content)
    with pytest.raises(RoundFailure) as corrupt:
        directory.open_instance("a", identity())
    assert corrupt.value.code == "state_unavailable" and corrupt.value.retryable is False
    assert path.read_bytes() == content


def test_identity_fingerprint_is_committed_before_use(tmp_path: Path) -> None:
    """§4.4：指纹缺失或变化时先落盘（与丢弃旧状态原子完成）；未变时不写。"""
    directory = StateDirectory(tmp_path)
    path = tmp_path / "instances" / "a" / "state.json"
    first = directory.open_instance("a", identity())
    first.commit_identity()
    assert path.exists() and not first.reset
    first.finish_round(succeeded=True, cursor="c1", started_at="t")
    before = path.read_bytes()
    directory.open_instance("a", identity()).commit_identity()  # 未变：不写
    assert path.read_bytes() == before
    changed = directory.open_instance("a", identity(config={"repo": "y"}))
    assert changed.reset
    changed.commit_identity()
    reopened = directory.open_instance("a", identity(config={"repo": "y"}))
    assert not reopened.reset and reopened.cursor is None  # 旧游标已随切换丢弃


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="依赖 POSIX 权限位且非 root"
)
def test_state_io_failures_are_state_unavailable_and_keep_memory(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    store = directory.open_instance("a", identity())
    store.finish_round(succeeded=True, cursor="c1", started_at="t")
    instance_dir = tmp_path / "instances" / "a"
    instance_dir.chmod(0o500)  # 只读：无法写临时文件
    try:
        with pytest.raises(RoundFailure) as failed:
            store.finish_round(succeeded=True, cursor="c2", started_at="t")
        assert failed.value.code == "state_unavailable" and failed.value.retryable
        assert store.cursor == "c1"  # 写失败不生效
        (instance_dir / "state.json").chmod(0o000)
        with pytest.raises(RoundFailure) as unreadable:
            directory.open_instance("a", identity())
        assert unreadable.value.code == "state_unavailable"
    finally:
        instance_dir.chmod(0o700)
        (instance_dir / "state.json").chmod(0o600)


def test_non_utf8_state_files(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    directory.open_instance("a", identity()).finish_round(
        succeeded=True, cursor="c1", started_at="t"
    )
    directory.register("a", remote_url=REMOTE, uri_prefix="dpe://src/")
    (tmp_path / "registry.json").write_bytes(b"\xff\xfe")
    with pytest.raises(RoundFailure) as broken:
        directory.register("b", remote_url=REMOTE, uri_prefix="dpe://other/")
    assert broken.value.code == "state_unavailable" and broken.value.retryable is False


def test_lock_errors_other_than_busy_are_state_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(handle: object) -> bool:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(state_module, "_try_lock", no_locks)
    with pytest.raises(RoundFailure) as failed:
        StateDirectory(tmp_path).lock_instance("a")
    assert failed.value.code == "state_unavailable"


def test_failed_atomic_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateDirectory(tmp_path).open_instance("a", identity())

    def broken_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(RoundFailure):
        store.finish_round(succeeded=True, cursor="c1", started_at="t")
    assert list((tmp_path / "instances" / "a").glob("*.tmp")) == []


def test_crash_before_the_identity_switch_is_retried_on_next_start(tmp_path: Path) -> None:
    """丢弃旧状态与写入新指纹是同一次原子写入：在它之前崩溃（这里即不调用 commit_identity），
    下次以新身份启动时重新切换，旧游标不会在新内容身份下生效；旧状态原样保留在磁盘上。"""
    directory = StateDirectory(tmp_path)
    old = directory.open_instance("a", identity())
    old.commit_identity()
    old.finish_round(succeeded=True, cursor="old-cursor", started_at="t")

    directory.open_instance("a", identity(config={"repo": "y"}))  # 切换前「崩溃」：未落盘

    retried = directory.open_instance("a", identity(config={"repo": "y"}))
    assert retried.reset and retried.cursor is None  # 重新切换，旧游标不生效
    assert directory.open_instance("a", identity()).cursor == "old-cursor"  # 磁盘未被改动


def test_finish_round_reports_whether_a_new_cursor_was_persisted(tmp_path: Path) -> None:
    """cursor_persisted 以收尾落盘为准（§7.4，#169）；插件给 null 时按 keep_cached 保留（§6.8）。"""
    store = StateDirectory(tmp_path).open_instance("a", identity())
    store.finish_round(succeeded=True, cursor="c1", started_at="t", keep_cached=True)
    assert store.cursor == "c1" and store.cursor_persisted
    store.finish_round(succeeded=True, cursor="c1", started_at="t", keep_cached=True)
    assert store.cursor_persisted  # 与已存值相同：无须重写，同样算已持久化
    store.finish_round(succeeded=True, cursor=None, started_at="t", keep_cached=True)
    assert store.cursor == "c1" and not store.cursor_persisted  # 保留旧游标，不是本轮持久化的
    store.finish_round(succeeded=False, cursor="c2", started_at="t", keep_cached=True)
    assert store.cursor == "c1" and not store.cursor_persisted
    store.finish_round(succeeded=True, cursor=None, started_at="t")
    assert store.cursor is None and not store.cursor_persisted  # 不支持游标：不缓存


def test_cursor_not_persisted_when_its_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateDirectory(tmp_path).open_instance("a", identity())

    def broken(path: Path, data: Any) -> None:
        raise OSError("磁盘只读")

    monkeypatch.setattr(state_module, "_write_atomic", broken)
    with pytest.raises(RoundFailure):
        store.finish_round(succeeded=True, cursor="c1", started_at="t", keep_cached=True)
    assert store.cursor_lost and not store.cursor_persisted


def test_defect_while_writing_never_reports_an_unsaved_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘时出运行器自身的缺陷（不只是 OSError）：新游标没落盘就不报已持久化（§7.4）。"""
    store = StateDirectory(tmp_path).open_instance("a", identity())
    store.finish_round(succeeded=True, cursor="c1", started_at="t")

    def broken(path: Path, data: Any) -> None:
        raise TypeError("序列化缺陷")

    monkeypatch.setattr(state_module, "_write_atomic", broken)
    with pytest.raises(TypeError):
        store.finish_round(succeeded=True, cursor="c2", started_at="t", keep_cached=True)
    assert store.round_closed and not store.cursor_persisted
    assert store.reported_held_since == "t"


def test_held_clear_failure_keeps_cursor_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """游标不变、只是扣住记录的清除写不进去：游标本就在盘上，仍算已持久化（§7.4）。"""
    store = StateDirectory(tmp_path).open_instance("a", identity())
    store.finish_round(succeeded=True, cursor="c1", started_at="t0")
    store.finish_round(succeeded=False, cursor=None, started_at="t1")  # 扣住
    real = state_module._write_atomic

    def no_writes(path: Path, data: Any) -> None:
        raise OSError("磁盘只读")

    monkeypatch.setattr(state_module, "_write_atomic", no_writes)
    with pytest.raises(RoundFailure):
        store.finish_round(succeeded=True, cursor="c1", started_at="t2", keep_cached=True)
    assert not store.cursor_lost and store.cursor_persisted
    monkeypatch.setattr(state_module, "_write_atomic", real)
