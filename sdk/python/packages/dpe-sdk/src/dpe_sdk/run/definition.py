"""实例定义（connector 契约 §4.4，独立运行器规范档）：解析、校验与路径解析。

实例定义是封闭 schema（``runner`` 内部除外），不含凭证值——凭证只以来源引用出现（§4.3）。
相对路径（``plugin.manifest``、``file`` 引用、含路径分隔符的 ``argv[0]``）以定义文件所在目录
为基准。违例一律抛 ``InstanceFailure("definition_invalid")``，不启动插件。

``load_definition`` 只做定义自身的校验，编排层可据此先取得 ``id``、取得实例锁（§5.2），再做
依赖清单的 ``check_against_manifest``。状态目录、排他锁与前缀登记不在本模块。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import dpe_hash
from pydantic import Field, PrivateAttr, ValidationInfo, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from dpe_sdk.run._json import ClosedModel, has_numeric_violation, load_json_file
from dpe_sdk.run.errors import InstanceFailure
from dpe_sdk.run.manifest import Manifest
from dpe_sdk.run.secrets import SourceRef, is_base_env_name, same_source

__all__ = [
    "ConflictPolicy",
    "InstanceDefinition",
    "PluginSpec",
    "RemoteSpec",
    "check_against_manifest",
    "load_definition",
    "parse_definition",
    "resolve_command",
]

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_Str = Annotated[str, Field(strict=True)]


class RemoteSpec(ClosedModel):
    """``remote``：基 URL 与可选的 remote 凭证引用。"""

    url: _Str
    credential: SourceRef | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("remote.url 须为 http(s) 绝对 URL")
        if "@" in parts.netloc:
            raise ValueError("remote.url 不得含 userinfo（凭证经 remote.credential 引用）")
        return url


class PluginSpec(ClosedModel):
    """``plugin``：清单路径、启动命令与非秘密环境变量。"""

    manifest: Annotated[str, Field(strict=True, min_length=1)]
    command: Annotated[list[_Str], Field(min_length=1)]
    env: dict[_Str, _Str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _check_command(cls, command: list[str]) -> list[str]:
        if not command[0]:
            raise ValueError("plugin.command[0] 不得为空")
        if any("\0" in arg for arg in command):
            raise ValueError("plugin.command 不得含 NUL")
        return command

    @field_validator("env")
    @classmethod
    def _check_env(cls, env: dict[str, str]) -> dict[str, str]:
        for name, value in env.items():
            if not name or "=" in name or "\0" in name or "\0" in value:
                raise ValueError(f"plugin.env 的变量 {name!r} 无法作为环境变量")
            if name.startswith("DPE_"):
                raise ValueError(f"plugin.env 的变量 {name!r} 不得以 DPE_ 开头")
            if is_base_env_name(name):
                raise ValueError(f"plugin.env 的变量 {name!r} 不得与基础环境同名")
        return env


class ConflictPolicy(ClosedModel):
    """``conflict_policy``（§7.2）：commit 缺省以源为准，delete 缺省只上报。"""

    commit: Literal["source_wins", "report"] = "source_wins"
    delete: Literal["report", "source_wins"] = "report"


class InstanceDefinition(ClosedModel):
    """一个实例的定义（§4.4）；``base_dir`` 是相对路径的基准（定义文件所在目录）。

    相对路径与凭证同源检查都依赖 ``base_dir``，所以它是校验的必需输入：经 ``parse_definition`` /
    ``load_definition`` 构造，或 ``model_validate(data, context={"base_dir": path})``；缺少时校验
    失败，不会退回进程的当前目录。
    """

    definition_version: Annotated[int, Field(strict=True)]
    id: _Str
    remote: RemoteSpec
    uri_prefix: _Str
    plugin: PluginSpec
    config: dict[str, Any]
    secrets: dict[_Str, SourceRef] = Field(default_factory=dict)
    conflict_policy: ConflictPolicy = ConflictPolicy()
    #: 运行器调优参数：内部不封闭，运行器忽略不认识的键
    runner: dict[str, Any] = Field(default_factory=dict)

    _base_dir: Path = PrivateAttr()

    @field_validator("definition_version")
    @classmethod
    def _check_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError(f"不认识的 definition_version {version}")
        return version

    @field_validator("id")
    @classmethod
    def _check_id(cls, id_: str) -> str:
        if not _ID.fullmatch(id_):
            raise ValueError("id 须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        return id_

    @field_validator("uri_prefix")
    @classmethod
    def _check_prefix(cls, prefix: str) -> str:
        # §3、§6.3：符合 core §1.1 文法，且已是规范化不动点
        try:
            normalized = dpe_hash.normalize_file_uri(prefix)
        except dpe_hash.ValidationError as exc:
            raise ValueError(f"uri_prefix 不是合法的 URI：{exc}") from None
        if normalized != prefix:
            raise ValueError(f"uri_prefix 不是规范化不动点（规范化形式为 {normalized!r}）")
        return prefix

    @field_validator("config")
    @classmethod
    def _check_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        # 配置原样交给插件并参与内容身份比较（§1、§4.2）：越界数值无法原样表达
        if has_numeric_violation(config):
            raise ValueError("config 含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）")
        return config

    @model_validator(mode="after")
    def _bind_base_dir(self, info: ValidationInfo) -> InstanceDefinition:
        base_dir = (info.context or {}).get("base_dir")
        if not isinstance(base_dir, Path):
            raise ValueError('须经 validation context 提供 "base_dir"（相对路径的基准）')
        self._base_dir = base_dir.absolute()
        # §4.3「remote 凭证不可达」：数据源凭证不得与 remote 凭证同源
        remote_ref = self.remote.credential
        if remote_ref is not None:
            for name, ref in self.secrets.items():
                if same_source(ref, remote_ref, self._base_dir):
                    raise ValueError(f"凭证 {name} 的来源与 remote.credential 相同")
        return self

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def manifest_path(self) -> Path:
        return self._base_dir / self.plugin.manifest


def _details(exc: PydanticValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()
    )


def _invalid(message: str) -> InstanceFailure:
    return InstanceFailure("definition_invalid", message)


def parse_definition(data: Any, base_dir: Path) -> InstanceDefinition:
    """校验已解析的实例定义；``base_dir`` 为相对路径的基准。"""
    try:
        return InstanceDefinition.model_validate(data, context={"base_dir": base_dir})
    except PydanticValidationError as exc:
        raise _invalid(f"实例定义不合法：{_details(exc)}") from None


def load_definition(path: Path) -> InstanceDefinition:
    """读取并校验实例定义文件。"""
    try:
        data = load_json_file(path)
    except ValueError as exc:
        raise _invalid(str(exc)) from None
    return parse_definition(data, path.absolute().parent)


def check_against_manifest(definition: InstanceDefinition, manifest: Manifest) -> None:
    """依赖清单的定义校验（§4.4）：``secrets`` 只能含清单声明的名字，``plugin.env`` 不得与
    已声明的凭证同名。"""
    declared = manifest.secret_names
    undeclared = sorted(set(definition.secrets) - declared)
    if undeclared:
        raise _invalid(f"secrets 含清单未声明的凭证：{', '.join(undeclared)}")
    clash = sorted(set(definition.plugin.env) & declared)
    if clash:
        raise _invalid(f"plugin.env 与已声明的凭证同名：{', '.join(clash)}")


def resolve_command(definition: InstanceDefinition, env: Mapping[str, str]) -> list[str]:
    """把 ``plugin.command`` 解析为可直接执行的 argv（不经 shell，§4.4）。

    ``argv[0]`` 含路径分隔符时以 ``base_dir`` 为基准解析，否则在插件环境的 ``PATH`` 中查找；
    找不到可执行文件时抛 ``InstanceFailure("definition_invalid")``。
    """
    program, *args = definition.plugin.command
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    if any(sep in program for sep in separators):
        path = definition.base_dir / program
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise _invalid(f"plugin.command[0] 不是可执行文件：{program}")
        return [str(path), *args]
    found = shutil.which(program, path=env.get("PATH", ""))
    if found is None:
        raise _invalid(f"plugin.command[0] 在 PATH 中找不到：{program}")
    return [found, *args]
