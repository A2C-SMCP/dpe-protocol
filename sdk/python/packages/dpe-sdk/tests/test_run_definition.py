"""实例定义（connector 契约 §4.4）与依赖清单的校验。"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.run import (
    InstanceFailure,
    check_against_manifest,
    load_definition,
    resolve_command,
)
from dpe_sdk.run.definition import InstanceDefinition, parse_definition
from dpe_sdk.run.manifest import parse_manifest
from pydantic import ValidationError as PydanticValidationError

# §4.4 的示例定义
EXAMPLE: dict[str, Any] = {
    "definition_version": 1,
    "id": "acme-repo",
    "remote": {
        "url": "https://dpe.example.com/remotes/acme",
        "credential": {"env": "ACME_DPE_AUTHORIZATION"},
    },
    "uri_prefix": "git://acme/repo/",
    "plugin": {
        "manifest": "plugins/git/dpe-connector.json",
        "command": ["dpe-git-connector"],
        "env": {"GIT_SSL_CAINFO": "/etc/ssl/certs/ca.pem"},
    },
    "config": {"repo": "https://example.com/acme/repo.git"},
    "secrets": {"GIT_TOKEN": {"file": "/run/secrets/git_token"}},
}
MANIFEST = parse_manifest(
    {
        "manifest_version": 1,
        "name": "git-connector",
        "version": "0.1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {"type": "object"},
        "secrets": [{"name": "GIT_TOKEN", "required": False}],
    }
)


def patched(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE)
    for dotted, value in changes.items():
        *parents, last = dotted.split("__")
        target = data
        for key in parents:
            target = target[key]
        if value is None:
            target.pop(last, None)
        else:
            target[last] = value
    return data


def test_example_definition(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE, tmp_path)
    assert definition.base_dir == tmp_path.absolute()
    assert definition.manifest_path == tmp_path / "plugins/git/dpe-connector.json"
    assert definition.conflict_policy.commit == "source_wins"
    assert definition.conflict_policy.delete == "report"
    check_against_manifest(definition, MANIFEST)


def test_optional_members_and_runner_is_open(tmp_path: Path) -> None:
    data = patched(remote__credential=None, plugin__env=None, secrets=None)
    data["runner"] = {"anything": {"nested": True}}
    data["conflict_policy"] = {"delete": "source_wins"}
    assert "credential" not in data["remote"]
    definition = parse_definition(data, tmp_path)
    assert definition.remote.credential is None
    assert definition.plugin.env == {}
    assert definition.runner == {"anything": {"nested": True}}
    assert definition.conflict_policy.commit == "source_wins"


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": 1},
        {"plugin__extra": 1},
        {"remote__extra": 1},
        {"conflict_policy": {"move": "report"}},
        {"conflict_policy": {"commit": "force"}},
        {"definition_version": 2},
        {"definition_version": True},
        {"id": "-leading-dash"},
        {"id": "a" * 129},
        {"id": "has space"},
        {"remote__url": "https://user:pw@dpe.example.com/r"},
        {"remote__url": "ftp://dpe.example.com/r"},
        {"remote__url": "dpe.example.com/r"},
        {"remote__credential": {"env": "A", "file": "b"}},
        {"remote__credential": {}},
        {"remote__credential": "Bearer literal"},
        {"uri_prefix": "Git://acme/repo/"},  # 不是规范化不动点
        {"uri_prefix": "git://acme/re%70o/"},
        {"uri_prefix": "no scheme"},
        {"plugin__command": []},
        {"plugin__command": [""]},
        {"plugin__command": "dpe-git-connector"},
        {"plugin__env": {"DPE_X": "1"}},
        {"plugin__env": {"PATH": "/bin"}},
        {"plugin__env": {"LC_ALL": "C"}},
        {"plugin__env": {"A=B": "1"}},
        {"plugin__env": {"X": 1}},
        {"config": None},
        {"config": []},
        {"secrets": {"GIT_TOKEN": {"value": "literal"}}},
        {"remote__credential": {"env": None}},
    ],
)
def test_invalid_definition(tmp_path: Path, changes: dict[str, Any]) -> None:
    with pytest.raises(InstanceFailure) as info:
        parse_definition(patched(**changes), tmp_path)
    assert (info.value.code, info.value.level) == ("definition_invalid", "instance")


@pytest.mark.parametrize(
    ("secret", "remote"),
    [
        ({"env": "SAME"}, {"env": "SAME"}),
        ({"file": "creds/token"}, {"file": "./creds/../creds/token"}),
    ],
)
def test_secret_must_not_share_remote_credential_source(
    tmp_path: Path, secret: dict[str, str], remote: dict[str, str]
) -> None:
    data = patched(remote__credential=remote, secrets={"GIT_TOKEN": secret})
    with pytest.raises(InstanceFailure) as info:
        parse_definition(data, tmp_path)
    assert info.value.code == "definition_invalid"
    assert "GIT_TOKEN" in info.value.message


def test_secret_file_through_symlink_is_same_source(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("t")
    (tmp_path / "alias").symlink_to(tmp_path / "token")
    data = patched(remote__credential={"file": "token"}, secrets={"GIT_TOKEN": {"file": "alias"}})
    with pytest.raises(InstanceFailure):
        parse_definition(data, tmp_path)


def test_check_against_manifest(tmp_path: Path) -> None:
    undeclared = parse_definition(patched(secrets={"OTHER": {"env": "X"}}), tmp_path)
    with pytest.raises(InstanceFailure) as info:
        check_against_manifest(undeclared, MANIFEST)
    assert info.value.code == "definition_invalid"
    clash = parse_definition(patched(plugin__env={"GIT_TOKEN": "x"}), tmp_path)
    with pytest.raises(InstanceFailure):
        check_against_manifest(clash, MANIFEST)


def test_load_definition_uses_file_directory_as_base(tmp_path: Path) -> None:
    path = tmp_path / "instances" / "acme.json"
    path.parent.mkdir()
    path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    definition = load_definition(path)
    assert definition.base_dir == path.parent.absolute()
    with pytest.raises(InstanceFailure) as info:
        load_definition(tmp_path / "absent.json")
    assert info.value.code == "definition_invalid"


@pytest.mark.skipif(os.name != "posix", reason="可执行位语义")
def test_resolve_command(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "plugin"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    relative = parse_definition(patched(plugin__command=["bin/plugin", "--flag"]), tmp_path)
    assert resolve_command(relative, {}) == [str(tmp_path / "bin/plugin"), "--flag"]

    on_path = parse_definition(patched(plugin__command=["plugin"]), tmp_path)
    assert resolve_command(on_path, {"PATH": str(script.parent)}) == [str(script)]
    # 查找用插件环境的 PATH，不用运行器自身的
    with pytest.raises(InstanceFailure) as info:
        resolve_command(on_path, {"PATH": str(tmp_path)})
    assert info.value.code == "definition_invalid"

    python = parse_definition(patched(plugin__command=["./bin/missing"]), tmp_path)
    with pytest.raises(InstanceFailure):
        resolve_command(python, {"PATH": os.path.dirname(sys.executable)})


def test_model_requires_base_dir_context(tmp_path: Path) -> None:
    # 直接 model_validate 不得绕过 base_dir 绑定与凭证同源检查
    with pytest.raises(PydanticValidationError):
        InstanceDefinition.model_validate(EXAMPLE)
    same = patched(remote__credential={"env": "X"}, secrets={"GIT_TOKEN": {"env": "X"}})
    with pytest.raises(PydanticValidationError):
        InstanceDefinition.model_validate(same, context={"base_dir": tmp_path})
    ok = InstanceDefinition.model_validate(EXAMPLE, context={"base_dir": tmp_path})
    assert ok.base_dir == tmp_path.absolute()


@pytest.mark.parametrize("config", [{"n": 2**53}, {"n": [-(2**53)]}, {"x": float("inf")}])
def test_out_of_range_config_numbers(tmp_path: Path, config: dict[str, Any]) -> None:
    with pytest.raises(InstanceFailure) as info:
        parse_definition(patched(config=config), tmp_path)
    assert info.value.code == "definition_invalid"
    parse_definition(patched(config={"n": 2**53 - 1, "f": 1.5}), tmp_path)


@pytest.mark.parametrize("literal", ["1e400", "1" * 5000])
def test_load_definition_rejects_out_of_range_literals(tmp_path: Path, literal: str) -> None:
    path = tmp_path / "def.json"
    path.write_text(json.dumps(patched(config={"n": 0})).replace('"n": 0', f'"n": {literal}'))
    with pytest.raises(InstanceFailure) as info:
        load_definition(path)
    assert info.value.code == "definition_invalid"


@pytest.mark.skipif(os.name != "posix", reason="符号链接语义")
def test_secret_file_in_symlink_loop_is_definition_invalid(tmp_path: Path) -> None:
    (tmp_path / "loop_a").symlink_to(tmp_path / "loop_b")
    (tmp_path / "loop_b").symlink_to(tmp_path / "loop_a")
    data = patched(
        remote__credential={"file": "loop_a/token"}, secrets={"GIT_TOKEN": {"file": "loop_b/x"}}
    )
    with pytest.raises(InstanceFailure) as info:
        parse_definition(data, tmp_path)
    assert info.value.code == "definition_invalid"
