"""凭证解析与插件环境构造（connector 契约 §4.3）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.run import InstanceFailure, Redactor, build_plugin_env
from dpe_sdk.run.definition import InstanceDefinition, parse_definition
from dpe_sdk.run.manifest import parse_manifest
from dpe_sdk.run.secrets import REDACTED, SourceRef, resolve, resolve_remote_credential

MANIFEST = parse_manifest(
    {
        "manifest_version": 1,
        "name": "p",
        "version": "1",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {"type": "object"},
        "secrets": [{"name": "NEEDED"}, {"name": "OPTIONAL", "required": False}],
    }
)


def definition(tmp_path: Path, secrets: dict[str, Any], **extra: Any) -> InstanceDefinition:
    data: dict[str, Any] = {
        "definition_version": 1,
        "id": "i",
        "remote": {"url": "https://r.example.com/r", "credential": {"env": "REMOTE_AUTH"}},
        "uri_prefix": "x://p/",
        "plugin": {"manifest": "m.json", "command": ["p"], "env": {"PLAIN": "v"}},
        "config": {},
        "secrets": secrets,
        **extra,
    }
    return parse_definition(data, tmp_path)


RUNNER_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "LANG": "C.UTF-8",
    "LC_CTYPE": "C",
    "TZ": "UTC",
    "TMPDIR": "/tmp",
    "REMOTE_AUTH": "Bearer remote",
    "SRC_NEEDED": "needed-value",
    "SRC_OPTIONAL": "optional-value",
    "AWS_SECRET_ACCESS_KEY": "unrelated",
}


def test_environment_is_exactly_three_parts(tmp_path: Path) -> None:
    defn = definition(
        tmp_path, {"NEEDED": {"env": "SRC_NEEDED"}, "OPTIONAL": {"env": "SRC_OPTIONAL"}}
    )
    built = build_plugin_env(MANIFEST, defn, RUNNER_ENV)
    assert built.env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "LC_CTYPE": "C",
        "TZ": "UTC",
        "TMPDIR": "/tmp",
        "NEEDED": "needed-value",
        "OPTIONAL": "optional-value",
        "PLAIN": "v",
    }
    assert built.redactor("x needed-value y") == f"x {REDACTED} y"


def test_required_missing_reports_names_only(tmp_path: Path) -> None:
    defn = definition(tmp_path, {"OPTIONAL": {"env": "SRC_OPTIONAL"}})
    with pytest.raises(InstanceFailure) as info:
        build_plugin_env(MANIFEST, defn, RUNNER_ENV)
    assert (info.value.code, info.value.level) == ("secret_missing", "instance")
    assert "NEEDED" in info.value.message
    assert "optional-value" not in info.value.message


def test_optional_missing_is_not_injected(tmp_path: Path) -> None:
    defn = definition(
        tmp_path, {"NEEDED": {"env": "SRC_NEEDED"}, "OPTIONAL": {"env": "UNSET_VARIABLE"}}
    )
    env = build_plugin_env(MANIFEST, defn, RUNNER_ENV).env
    assert "OPTIONAL" not in env


@pytest.mark.parametrize("value", ["", "a\0b"])
def test_unusable_value_is_missing(tmp_path: Path, value: str) -> None:
    defn = definition(tmp_path, {"NEEDED": {"env": "SRC_NEEDED"}})
    with pytest.raises(InstanceFailure) as info:
        build_plugin_env(MANIFEST, defn, {**RUNNER_ENV, "SRC_NEEDED": value})
    assert info.value.code == "secret_missing"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"token\n", "token"),
        (b"token\r\n", "token"),
        (b"token\n\n", "token\n"),  # 只去掉一个行尾
        (b"token", "token"),
        (b"\n", None),
        (b"\xff", None),  # 不是 UTF-8
    ],
)
def test_file_reference(tmp_path: Path, content: bytes, expected: str | None) -> None:
    (tmp_path / "secret").write_bytes(content)
    assert resolve(SourceRef(file="secret"), {}, tmp_path) == expected


def test_file_reference_unreadable(tmp_path: Path) -> None:
    assert resolve(SourceRef(file="absent"), {}, tmp_path) is None
    assert resolve(SourceRef(file=str(tmp_path)), {}, tmp_path) is None  # 目录


def test_credentials_are_reresolved_on_every_start(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("first\n")
    defn = definition(tmp_path, {"NEEDED": {"file": "token"}})
    assert build_plugin_env(MANIFEST, defn, RUNNER_ENV).env["NEEDED"] == "first"
    secret.write_text("rotated\n")
    assert build_plugin_env(MANIFEST, defn, RUNNER_ENV).env["NEEDED"] == "rotated"


def test_remote_credential(tmp_path: Path) -> None:
    defn = definition(tmp_path, {})
    assert resolve_remote_credential(defn, RUNNER_ENV) == "Bearer remote"
    with pytest.raises(InstanceFailure) as info:
        resolve_remote_credential(defn, {})
    assert info.value.code == "secret_missing"
    assert "Bearer" not in info.value.message


def test_redactor_prefers_longer_values() -> None:
    redact = Redactor(["abc", "abcdef", ""])
    assert redact("abcdef abc") == f"{REDACTED} {REDACTED}"
    assert redact.extended(["zz"])("zz") == REDACTED


A = "A" * 40
B = "B" * 40


def test_truncate_cuts_at_raw_offset_after_earlier_redaction() -> None:
    # 前一个凭证被替换使文本变短；被窗口切断的后一个凭证的前缀不得被拉进输出
    redactor = Redactor([A, B])
    line = (A + "y" * 8160 + B + "tail").encode()
    window = line[: 8192 + redactor.max_length]
    text, truncated = redactor.truncate(window, 8192)
    assert truncated is True
    assert "B" not in text
    assert text == REDACTED + "y" * 8152


def test_truncate_replaces_match_straddling_limit_whole() -> None:
    redactor = Redactor([B])
    text, truncated = redactor.truncate(("y" * 10 + B + "z").encode(), 20)
    assert (text, truncated) == ("y" * 10 + REDACTED, True)


def test_truncate_short_line() -> None:
    redactor = Redactor(["abc", "abcdef"])
    assert redactor.truncate(b"x abcdef y abc \xff", 100) == (
        f"x {REDACTED} y {REDACTED} \ufffd",
        False,
    )


def test_redact_json() -> None:
    redact = Redactor(["s3"])
    assert redact.redact_json({"k s3": ["a s3", 1, None, {"x": "s3"}]}) == {
        f"k {REDACTED}": [f"a {REDACTED}", 1, None, {"x": REDACTED}]
    }


def test_overlapping_secrets_are_both_redacted() -> None:
    redact = Redactor(["abcXYZ", "XYZdef"])
    assert redact("abcXYZdef!") == f"{REDACTED}!"
    assert redact.truncate(b"abcXYZdef!", 100) == (f"{REDACTED}!", False)


def test_multiline_secret_is_redacted_line_by_line() -> None:
    pem = "-----BEGIN KEY-----\nAAAABBBBCCCCDDDD\nEEEEFFFFGGGG\n}\n-----END KEY-----"
    redact = Redactor([pem])
    assert redact("key: AAAABBBBCCCCDDDD") == f"key: {REDACTED}"
    assert redact.truncate(b"EEEEFFFFGGGG", 100) == (REDACTED, False)
    assert redact("{ok}") == "{ok}"  # 过短的行不参与匹配
    assert redact(pem) == REDACTED


def test_redact_json_handles_deep_nesting() -> None:
    value: Any = "s3"
    for _ in range(5000):
        value = [value]
    redacted = Redactor(["s3"]).redact_json(value)
    for _ in range(5000):
        redacted = redacted[0]
    assert redacted == REDACTED


def test_multiline_secret_of_short_lines() -> None:
    redact = Redactor(["abc1234\nxyz7890"])
    assert redact("abc1234") == REDACTED
    assert redact("xyz7890") == REDACTED
