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


def test_unrecognizable_state_is_a_cache_loss(tmp_path: Path) -> None:
    directory = StateDirectory(tmp_path)
    directory.open_instance("a", identity()).finish_round(
        succeeded=True, cursor="c1", started_at="t"
    )
    (tmp_path / "instances" / "a" / "state.json").write_text("garbage", encoding="utf-8")
    store = directory.open_instance("a", identity())
    assert store.reset and store.cursor is None


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
    (tmp_path / "instances" / "a" / "state.json").write_bytes(b"\xff\xfe garbage")
    assert directory.open_instance("a", identity()).reset  # 缓存丢失
    directory.register("a", remote_url=REMOTE, uri_prefix="dpe://src/")
    (tmp_path / "registry.json").write_bytes(b"\xff\xfe")
    with pytest.raises(RoundFailure) as broken:
        directory.register("b", remote_url=REMOTE, uri_prefix="dpe://other/")
    assert broken.value.code == "state_unavailable"


def test_lock_errors_other_than_busy_are_state_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(handle: object) -> bool:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(state_module, "_try_lock", no_locks)
    with pytest.raises(RoundFailure) as failed:
        StateDirectory(tmp_path).lock_instance("a")
    assert failed.value.code == "state_unavailable"
