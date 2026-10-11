"""配置加载：封闭 schema 的逐条违例与合法装配。"""

from __future__ import annotations

from pathlib import Path

import pytest
from dpe_conformance.config import (
    DEFAULT_URI_PREFIX,
    ConfigError,
    ConformanceConfig,
    find_config,
    load_config,
)

_BASE = """
schema = 1

[identities.full]
authorization = "Bearer full"

[identities.scoped]
authorization = "Bearer scoped"
prefix = "dpe://acme/scoped/"

[hooks.self-write]
http = "http://127.0.0.1:1/__hooks__/self-write"
"""


def _load(tmp_path: Path, text: str) -> ConformanceConfig:
    path = tmp_path / "dpe-conformance.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_full_config(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        _BASE.replace("schema = 1", 'schema = 1\nuri-prefix = "dpe://conf/"')
        + """
[hooks.expire-session]
command = ["my-server", "expire-session"]

[hooks.inject-error]
http = "http://127.0.0.1:1/__hooks__/inject-error"

[session]
staging-ttl-seconds = 30
""",
    )
    assert set(config.identities) == {"full", "scoped"}
    assert config.identities["full"].authorization == "Bearer full"
    assert config.identities["scoped"].prefix == "dpe://acme/scoped/"
    assert config.self_write is not None and config.self_write.http is not None
    assert config.expire_session is not None
    assert config.expire_session.command == ("my-server", "expire-session")
    assert config.inject_error is not None and config.inject_error.http is not None
    assert config.staging_ttl_seconds == 30
    assert config.uri_prefix == "dpe://conf/"


def test_empty_config_defaults() -> None:
    config = ConformanceConfig.empty()
    assert not config.identities
    assert config.self_write is None and config.expire_session is None
    assert config.inject_error is None
    assert config.staging_ttl_seconds is None
    assert config.uri_prefix == DEFAULT_URI_PREFIX


def test_identity_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CONF_AUTH", "Bearer from-env")
    config = _load(
        tmp_path,
        'schema = 1\n[identities.full]\nenv = "TEST_CONF_AUTH"\n',
    )
    assert config.identities["full"].authorization == "Bearer from-env"


def test_identity_env_missing_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_CONF_ABSENT", raising=False)
    with pytest.raises(ConfigError, match="未设置"):
        _load(tmp_path, 'schema = 1\n[identities.full]\nenv = "TEST_CONF_ABSENT"\n')


@pytest.mark.parametrize(
    ("fragment", "match"),
    [
        ("schema = 2\n", "schema"),
        ("", "schema"),
        ("schema = 1\nunknown = 1\n", "未定义的键"),
        ('schema = 1\n[identities.master]\nauthorization = "x"\n', "未知身份角色"),
        (
            'schema = 1\n[identities.full]\nauthorization = "x"\nenv = "Y"\n',
            "只能给 authorization 或 env",
        ),
        ("schema = 1\n[identities.full]\n", "只能给 authorization 或 env"),
        ('schema = 1\n[identities.scoped]\nauthorization = "x"\n', "prefix"),
        (
            'schema = 1\n[identities.full]\nauthorization = "x"\nprefix = "dpe://a/"\n',
            "只对 scoped",
        ),
        (
            'schema = 1\n[identities.scoped]\nauthorization = "x"\nprefix = "no-scheme"\n',
            "file_uri",
        ),
        (
            'schema = 1\n[identities.scoped]\nauthorization = "x"\nprefix = "dpe://HOST/a"\n',
            "规范化不动点",
        ),
        ('schema = 1\n[hooks.clock]\nhttp = "http://x/"\n', "未知钩子"),
        (
            'schema = 1\n[hooks.self-write]\nhttp = "http://x/"\ncommand = ["a"]\n',
            "只能给 http 或 command",
        ),
        ("schema = 1\n[hooks.self-write]\n", "只能给 http 或 command"),
        ('schema = 1\n[hooks.self-write]\nhttp = "ftp://x/"\n', r"http\(s\)"),
        ("schema = 1\n[hooks.self-write]\ncommand = []\n", "非空字符串数组"),
        ("schema = 1\n[session]\nstaging-ttl-seconds = 0\n", "正整数"),
        ("schema = 1\n[session]\nstaging-ttl-seconds = true\n", "正整数"),
        ('schema = 1\nuri-prefix = "no-scheme"\n', "file_uri"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path: Path, fragment: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, fragment)


def test_find_config_explicit_missing_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        find_config(tmp_path / "missing.toml")


def test_find_config_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert find_config(None) is None
    (tmp_path / "dpe-conformance.toml").write_text("schema = 1\n", encoding="utf-8")
    assert find_config(None) == Path("dpe-conformance.toml")


def test_clock_advance_hook_and_default(tmp_path: Path) -> None:
    """时钟推进钩子：可配置（HTTP/命令二选一），缺省为 None（相关检查 skip）。"""
    assert ConformanceConfig.empty().clock_advance is None
    config = _load(
        tmp_path,
        'schema = 1\n[hooks.clock-advance]\nhttp = "http://x/clock/advance"\n',
    )
    assert config.clock_advance is not None
    assert config.clock_advance.http == "http://x/clock/advance"
