"""remote URL 的文法（HTTP 绑定 §1）：一致性向量 vectors/remote_url.json 逐例经三个入口断言。

入口：``check_remote_url`` 本身、``DpeClient`` / ``AsyncDpeClient`` 构造（传输层）、实例定义的
``remote.url``（connector 契约 §4.4，含报告用的 ``peek_identity``）。三者是同一个判定。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.remote_url import check_remote_url
from dpe_sdk.run import InstanceFailure
from dpe_sdk.run.definition import parse_definition, peek_identity
from dpe_sdk.transport import AsyncDpeClient, DpeClient

DEFINITION: dict[str, Any] = {
    "definition_version": 1,
    "id": "acme-repo",
    "remote": {"url": "https://dpe.example.com/remotes/acme"},
    "uri_prefix": "git://acme/repo/",
    "plugin": {"manifest": "plugins/git/dpe-connector.json", "command": ["dpe-git-connector"]},
    "config": {},
}


@pytest.fixture(scope="module")
def vector(vectors_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vectors_dir / "remote_url.json").read_text(encoding="utf-8"))
    assert data["kind"] == "remote_url"
    assert data["valid"] and data["invalid"]
    return data


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _definition(url: str) -> dict[str, Any]:
    data = copy.deepcopy(DEFINITION)
    data["remote"]["url"] = url
    return data


def _peek(tmp_path: Path, url: str) -> str | None:
    path = tmp_path / "instance.json"
    path.write_text(json.dumps(_definition(url)), encoding="utf-8")
    identity = peek_identity(path)
    assert identity.id == "acme-repo"
    return identity.remote_url


def test_valid_remote_url_is_accepted_verbatim(vector: dict[str, Any]) -> None:
    for url in vector["valid"]:
        assert check_remote_url(url) == url, url


def test_invalid_remote_url_is_rejected(vector: dict[str, Any]) -> None:
    for case in vector["invalid"]:
        url = case["input"]
        with pytest.raises(ValueError) as info:
            check_remote_url(url)
        # 消息不回显 URL（userinfo 里可能是凭证）
        assert not url or url not in str(info.value), url


@pytest.mark.anyio
async def test_clients_follow_the_grammar(vector: dict[str, Any]) -> None:
    for url in vector["valid"]:
        with DpeClient(url) as client:
            assert client.remote == url
        async with AsyncDpeClient(url) as async_client:
            assert async_client.remote == url
    for case in vector["invalid"]:
        with pytest.raises(ValueError):
            DpeClient(case["input"])
        with pytest.raises(ValueError):
            AsyncDpeClient(case["input"])


def test_definition_follows_the_grammar(tmp_path: Path, vector: dict[str, Any]) -> None:
    for url in vector["valid"]:
        assert parse_definition(_definition(url), tmp_path).remote.url == url
        assert _peek(tmp_path, url) == url
    for case in vector["invalid"]:
        url = case["input"]
        with pytest.raises(InstanceFailure) as info:
            parse_definition(_definition(url), tmp_path)
        assert (info.value.code, info.value.level) == ("definition_invalid", "instance"), url
        assert not url or url not in str(info.value), url
        assert _peek(tmp_path, url) is None, url
