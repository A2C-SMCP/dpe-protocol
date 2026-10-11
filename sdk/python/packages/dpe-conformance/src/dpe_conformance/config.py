"""跑分器配置：身份、服务端钩子与会话参数，全部中性词汇。

配置文件是 TOML（``tomllib``，仅标准库），封闭 schema——未知键一律拒绝，不出现任何服务端
私有概念。凭证不进命令行与日志：身份经 ``env`` 引用环境变量或给 ``authorization`` 字面量
（字面量仅限低权限/测试凭证），跑分器只把它放上 ``Authorization`` 请求头。示例（完整说明见
包 README）::

    schema = 1
    uri-prefix = "dpe://conformance/"

    [identities.full]                # 全量写权
    env = "DPE_CONF_FULL_AUTH"
    [identities.scoped]              # 仅前缀可写（服务端按该前缀授予写权）
    authorization = "Bearer scoped"
    prefix = "dpe://acme/scoped/"
    [identities.no-force]            # 无 force 权限（force 的 403 检查；须对测试命名空间可写，
    env = "DPE_CONF_NO_FORCE_AUTH"   # 否则该检查显式 skip 并计数）

    [hooks.self-write]               # 服务端自身写入（core §2.4）
    http = "http://127.0.0.1:8000/__hooks__/self-write"
    [hooks.expire-session]           # 会话立即过期；与 command 二选一
    command = ["my-server", "expire-session"]
    [hooks.clock-advance]            # 推进服务端时钟 N 秒（会话 TTL 按推进后的时间判定）
    http = "http://127.0.0.1:8000/__hooks__/clock/advance"

    [session]
    staging-ttl-seconds = 30         # 服务端以短 TTL 运行时的声明

身份与服务端钩子都是可选的：未配置时，依赖它们的检查显式 skip 并计数。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dpe_hash

#: 配置 schema 版本；不兼容的格式变化时递增
SCHEMA_VERSION = 1

#: 身份角色（封闭枚举）：full = 全量写权；scoped = 仅前缀可写；no-force = 无 force 权限
ROLES = ("full", "scoped", "no-force")

#: 缺省的测试 URI 命名空间（file_uri scheme 任意，core §1.1）
DEFAULT_URI_PREFIX = "dpe://conformance/"

#: 缺省的配置文件名（CLI 在 CWD 下自动发现）
DEFAULT_CONFIG_NAME = "dpe-conformance.toml"


class ConfigError(ValueError):
    """配置不合法；CLI 以退出码 2 报告。"""


@dataclass(frozen=True)
class Identity:
    """一个调用者身份：``authorization`` 原样放上请求头，``prefix`` 是 scoped 身份可写的前缀。"""

    role: str
    authorization: str
    prefix: str | None = None


@dataclass(frozen=True)
class HookEndpoint:
    """服务端钩子的入口：HTTP 或命令，二选一（载荷与成功判据见包 README 的钩子契约）。"""

    http: str | None = None
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ConformanceConfig:
    """一次运行的全部配置；无配置运行时各项为空（检查相应 skip）。"""

    identities: Mapping[str, Identity]
    self_write: HookEndpoint | None
    expire_session: HookEndpoint | None
    staging_ttl_seconds: int | None
    uri_prefix: str
    #: 时钟推进钩子：载荷 ``{"advance_seconds": N}``，与服务端 TTL 判定对齐（见包 README）
    clock_advance: HookEndpoint | None = None

    @staticmethod
    def empty() -> ConformanceConfig:
        """无配置运行。"""
        return ConformanceConfig({}, None, None, None, DEFAULT_URI_PREFIX)


def find_config(explicit: Path | None) -> Path | None:
    """定位配置文件：显式路径必须存在；否则在 CWD 下找缺省文件名（不存在即无配置）。"""
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"配置文件不存在：{explicit}")
        return explicit
    candidate = Path(DEFAULT_CONFIG_NAME)
    return candidate if candidate.is_file() else None


def load_config(path: Path) -> ConformanceConfig:
    """读取并校验配置；任何违例抛 ``ConfigError``。"""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"不是合法 TOML：{path}（{exc}）") from None
    _reject_unknown(raw, {"schema", "identities", "hooks", "session", "uri-prefix"}, "顶层")
    if raw.get("schema") != SCHEMA_VERSION:
        raise ConfigError(f"schema 必须是 {SCHEMA_VERSION}，实际 {raw.get('schema')!r}")
    identities = _parse_identities(raw.get("identities", {}))
    hooks = _parse_hooks(raw.get("hooks", {}))
    return ConformanceConfig(
        identities=identities,
        self_write=hooks.get("self-write"),
        expire_session=hooks.get("expire-session"),
        clock_advance=hooks.get("clock-advance"),
        staging_ttl_seconds=_parse_session(raw.get("session", {})),
        uri_prefix=_parse_uri_prefix(raw.get("uri-prefix")),
    )


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}出现未定义的键：{'、'.join(unknown)}（允许：{'、'.join(sorted(allowed))}）"
        )


def _require_table(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}必须是表，实际 {type(value).__name__}")
    return value


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}必须是非空字符串，实际 {value!r}")
    return value


def _parse_identities(raw: Any) -> Mapping[str, Identity]:
    table = _require_table(raw, "identities")
    identities: dict[str, Identity] = {}
    for role, entry in table.items():
        where = f"identities.{role}"
        if role not in ROLES:
            raise ConfigError(f"未知身份角色 {role!r}（允许：{'、'.join(ROLES)}）")
        fields = _require_table(entry, where)
        _reject_unknown(fields, {"authorization", "env", "prefix"}, where)
        has_literal = "authorization" in fields
        has_env = "env" in fields
        if has_literal == has_env:
            raise ConfigError(f"{where}必须且只能给 authorization 或 env 之一")
        if has_literal:
            authorization = _require_string(fields["authorization"], f"{where}.authorization")
        else:
            name = _require_string(fields["env"], f"{where}.env")
            value = os.environ.get(name)
            if value is None:
                raise ConfigError(f"{where}.env 引用的环境变量 {name!r} 未设置")
            authorization = _require_string(value, f"环境变量 {name}")
        prefix: str | None = None
        if role == "scoped":
            prefix = _validate_uri(
                _require_string(fields.get("prefix"), f"{where}.prefix"), f"{where}.prefix"
            )
        elif "prefix" in fields:
            raise ConfigError(f"{where}.prefix 只对 scoped 身份有意义")
        identities[role] = Identity(role=role, authorization=authorization, prefix=prefix)
    return identities


def _parse_hooks(raw: Any) -> Mapping[str, HookEndpoint]:
    table = _require_table(raw, "hooks")
    hooks: dict[str, HookEndpoint] = {}
    for name, entry in table.items():
        where = f"hooks.{name}"
        if name not in ("self-write", "expire-session", "clock-advance"):
            raise ConfigError(
                f"未知钩子 {name!r}（允许：self-write、expire-session、clock-advance）"
            )
        fields = _require_table(entry, where)
        _reject_unknown(fields, {"http", "command"}, where)
        has_http = "http" in fields
        has_command = "command" in fields
        if has_http == has_command:
            raise ConfigError(f"{where}必须且只能给 http 或 command 之一")
        if has_http:
            url = _require_string(fields["http"], f"{where}.http")
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"{where}.http 必须是 http(s) URL，实际 {url!r}")
            hooks[name] = HookEndpoint(http=url)
        else:
            command = fields["command"]
            if not isinstance(command, list) or not command:
                raise ConfigError(f"{where}.command 必须是非空字符串数组")
            argv = tuple(_require_string(item, f"{where}.command") for item in command)
            hooks[name] = HookEndpoint(command=argv)
    return hooks


def _parse_session(raw: Any) -> int | None:
    table = _require_table(raw, "session")
    _reject_unknown(table, {"staging-ttl-seconds"}, "session")
    value = table.get("staging-ttl-seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"session.staging-ttl-seconds 必须是正整数，实际 {value!r}")
    return value


def _parse_uri_prefix(raw: Any) -> str:
    if raw is None:
        return DEFAULT_URI_PREFIX
    return _validate_uri(_require_string(raw, "uri-prefix"), "uri-prefix")


def _validate_uri(value: str, where: str) -> str:
    """校验 file_uri（core §1.1）且必须是规范化不动点（与 connector 契约对前缀的要求同理）。"""
    try:
        normalized = dpe_hash.normalize_file_uri(value)
    except ValueError as exc:
        raise ConfigError(f"{where}不是合法 file_uri：{value!r}（{exc}）") from None
    if normalized != value:
        raise ConfigError(f"{where}必须是规范化不动点：{value!r} 规范化为 {normalized!r}")
    return value
