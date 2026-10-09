"""凭证（connector 契约 §4.3）：来源引用、解析、插件环境构造与脱敏。

两类凭证严格分属：remote 凭证只属于运行器，数据源凭证只属于插件、只以环境变量注入。
插件环境恰由三部分组成——基础环境、已解析的数据源凭证、``plugin.env``——运行器自身的其余
环境变量（其中可能有 remote 凭证）一律不继承。

凭证在每次启动插件时重新解析（``build_plugin_env`` 每次调用都重读来源），不持久化、不跨进程
缓存；凭证值不出现在任何失败消息中，只写凭证名。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, AnyStr

from pydantic import Field, model_validator

from dpe_sdk.run._json import ClosedModel
from dpe_sdk.run.errors import InstanceFailure

if TYPE_CHECKING:
    from dpe_sdk.run.definition import InstanceDefinition
    from dpe_sdk.run.manifest import Manifest

__all__ = [
    "BASE_ENV_NAMES",
    "REDACTED",
    "PluginEnvironment",
    "Redactor",
    "SourceRef",
    "build_plugin_env",
    "is_base_env_name",
    "resolve",
    "resolve_remote_credential",
    "same_source",
]

#: 基础环境（§4.3 第 1 部分）的全平台名单，另加全部 ``LC_*``。清单与实例定义的同名检查按全平台
#: 名单判定，使同一份声明在任何平台上得出相同结论；实际注入时只取本平台适用的部分。
_POSIX_BASE = ("PATH", "HOME", "LANG", "TZ", "TMPDIR")
_WINDOWS_BASE = ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "PATHEXT", "COMSPEC")
BASE_ENV_NAMES = frozenset(_POSIX_BASE + _WINDOWS_BASE)

#: 脱敏时替换凭证值的固定标记（§4.3「不外泄」）
REDACTED = "[REDACTED]"


def is_base_env_name(name: str) -> bool:
    """``name`` 是否属于基础环境（任何平台）。"""
    return name in BASE_ENV_NAMES or name.startswith("LC_")


def _is_injected_base(name: str) -> bool:
    if name in _POSIX_BASE or name.startswith("LC_"):
        return True
    return os.name == "nt" and name in _WINDOWS_BASE


class SourceRef(ClosedModel):
    """凭证来源引用：恰含 ``env`` 或 ``file`` 之一的封闭对象（§4.3）。"""

    env: Annotated[str, Field(strict=True)] | None = None
    file: Annotated[str, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SourceRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("来源引用必须恰含 env 或 file 之一")
        return self


def _file_path(ref: SourceRef, base_dir: Path) -> Path:
    assert ref.file is not None
    return base_dir / ref.file


def resolve(ref: SourceRef, environ: Mapping[str, str], base_dir: Path) -> str | None:
    """解析来源引用；无法解析（变量未设、文件不可读或不是 UTF-8、值为空）时返回 ``None``。

    ``file`` 取全部内容并去掉末尾的一个 LF（或 CRLF）。含 NUL 的值无法放进环境变量，同样视为
    无法解析。相对路径以 ``base_dir``（实例定义所在目录，§4.4）为基准。
    """
    if ref.env is not None:
        value = environ.get(ref.env)
    else:
        try:
            value = _file_path(ref, base_dir).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value or "\0" in value:
        return None
    return value


def same_source(a: SourceRef, b: SourceRef, base_dir: Path) -> bool:
    """两个引用是否指向同一来源：同名 ``env``，或解析为同一路径的 ``file``（§4.3）。

    ``file`` 路径无法解析（如符号链接成环）时抛 ``ValueError``，由定义校验归为不合法。
    """
    if a.env is not None or b.env is not None:
        return a.env is not None and a.env == b.env
    try:
        return _file_path(a, base_dir).resolve() == _file_path(b, base_dir).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"凭证文件路径无法解析：{exc}") from None


#: 多行凭证按行拆出的片段至少这么长才参与匹配：过短的行（如 JSON 的括号）不含凭证信息，
#: 匹配它们只会让 stderr 满屏标记；但凭证的每一行都短于此值时，全部行都参与匹配
_MIN_FRAGMENT = 8


def _needles(values: Iterable[str]) -> list[str]:
    """匹配表：完整凭证值，以及多行凭证的各行（stderr 按行转发，整值永远匹配不到）。"""
    needles = {v for v in values if v}
    for value in list(needles):
        if "\n" in value:
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            long_lines = [line for line in lines if len(line) >= _MIN_FRAGMENT]
            # 每行都很短时整条凭证都由短行组成：宁可多替换，也要全部匹配
            needles.update(long_lines or lines)
    return sorted(needles, key=len, reverse=True)


def _spans(haystack: AnyStr, needles: Sequence[AnyStr]) -> list[tuple[int, int]]:
    """全部匹配区间（含相互重叠的），合并为互不相交的有序区间。

    先在原文上收集所有区间再合并，避免逐个替换或「取最早匹配后跳过」漏掉与之重叠的另一个凭证。
    """
    spans: list[tuple[int, int]] = []
    for needle in needles:
        at = haystack.find(needle)
        while at != -1:
            spans.append((at, at + len(needle)))
            at = haystack.find(needle, at + 1)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for begin, end in spans:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


class Redactor:
    """把已知凭证值替换为固定标记；转发插件 stderr 与写出诊断前使用（§4.3、§7.5）。"""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values = [v for v in dict.fromkeys(values) if v]
        self._needles = _needles(self._values)
        self._byte_needles = [n.encode("utf-8") for n in self._needles]

    def __call__(self, text: str) -> str:
        out: list[str] = []
        i = 0
        for begin, end in _spans(text, self._needles):
            out.append(text[i:begin])
            out.append(REDACTED)
            i = end
        out.append(text[i:])
        return "".join(out)

    def redact_json(self, value: Any) -> Any:
        """脱敏 JSON 值中的全部字符串（含对象键），如插件错误响应的 ``data``；返回副本。

        以显式栈迭代：插件给出的值可能嵌套很深，递归会在解析器还能接受的深度上栈溢出。
        """
        holder: list[Any] = [value]
        stack: list[tuple[Any, Any]] = [(holder, 0)]
        while stack:
            container, key = stack.pop()
            item = container[key]
            if isinstance(item, str):
                container[key] = self(item)
            elif isinstance(item, list):
                copied = list(item)
                container[key] = copied
                stack.extend((copied, index) for index in range(len(copied)))
            elif isinstance(item, dict):
                copied_dict = {self(k): v for k, v in item.items()}
                container[key] = copied_dict
                stack.extend((copied_dict, k) for k in copied_dict)
        return holder[0]

    @property
    def max_length(self) -> int:
        """匹配表中最长项的 UTF-8 字节数：截断前须多保留这么长的原始窗口（见 ``truncate``）。"""
        return max((len(n) for n in self._byte_needles), default=0)

    def truncate(self, raw: bytes, limit: int) -> tuple[str, bool]:
        """脱敏并截断到**原始**字节偏移 ``limit``，返回 ``(文本, 是否截断)``。

        在原始字节上找出全部凭证匹配并合并区间，起点在 ``limit`` 之前的区间整体替换为标记（跨过
        ``limit`` 的也整体替换），``limit`` 之后的字节一律丢弃。输出因此只由 ``[0, limit)`` 内的
        非凭证字节与标记组成——不以脱敏后的偏移截断，否则前面的替换使文本变短，会把窗口外被切断、
        无法匹配的凭证前缀拉进输出。``raw`` 须至少含 ``limit + max_length`` 字节（行更短时为整行），
        使起点在 ``limit`` 之前的凭证完整可见。非 UTF-8 字节替换为 U+FFFD。
        """
        end = min(len(raw), limit)
        out = bytearray()
        i = 0
        for begin, stop in _spans(raw, self._byte_needles):
            if begin >= end:
                break
            out += raw[i:begin]
            out += REDACTED.encode("utf-8")
            i = stop
        if i < end:
            out += raw[i:end]
        return out.decode("utf-8", errors="replace"), len(raw) > limit

    def extended(self, values: Iterable[str]) -> Redactor:
        return Redactor([*self._values, *values])


@dataclass(frozen=True)
class PluginEnvironment:
    """一次插件启动的环境，以及用于脱敏的已注入凭证值。"""

    env: dict[str, str]
    redactor: Redactor


def build_plugin_env(
    manifest: Manifest,
    definition: InstanceDefinition,
    environ: Mapping[str, str] | None = None,
) -> PluginEnvironment:
    """按 §4.3 构造插件进程的环境；每次启动插件前调用，凭证在此重新解析。

    必填凭证无法解析时抛 ``InstanceFailure("secret_missing")``，消息只含凭证名；可选凭证无法
    解析时不注入（不以空字符串顶替）。调用前须已通过 ``check_against_manifest``。
    """
    if environ is None:
        environ = os.environ
    env = {name: value for name, value in environ.items() if _is_injected_base(name)}
    missing: list[str] = []
    injected: list[str] = []
    for declared in manifest.secrets:
        ref = definition.secrets.get(declared.name)
        value = None if ref is None else resolve(ref, environ, definition.base_dir)
        if value is None:
            if declared.required:
                missing.append(declared.name)
            continue
        env[declared.name] = value
        injected.append(value)
    if missing:
        raise InstanceFailure("secret_missing", f"必填凭证无法解析：{', '.join(missing)}")
    env.update(definition.plugin.env)
    return PluginEnvironment(env=env, redactor=Redactor(injected))


def resolve_remote_credential(
    definition: InstanceDefinition, environ: Mapping[str, str] | None = None
) -> str | None:
    """解析 remote 凭证（完整的 ``Authorization`` 头值，§4.4）；每次需要时调用，不缓存。

    未配置时返回 ``None``（不发送 ``Authorization``）；配置了却无法解析时抛
    ``InstanceFailure("secret_missing")``。
    """
    ref = definition.remote.credential
    if ref is None:
        return None
    value = resolve(ref, os.environ if environ is None else environ, definition.base_dir)
    if value is None:
        raise InstanceFailure("secret_missing", "remote 凭证（remote.credential）无法解析")
    return value
