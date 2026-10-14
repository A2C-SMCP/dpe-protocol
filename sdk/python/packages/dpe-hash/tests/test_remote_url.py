"""remote URL 的文法（HTTP 绑定 §1）：``check_remote_url`` 逐例通过 vectors/remote_url.json。

SDK 的客户端构造与实例定义入口见 dpe-sdk 的 ``tests/test_remote_url_entry_points.py``，
跑分器的 REMOTE-URL 参数见 dpe-conformance 的 ``tests/test_cli_remote_url.py``；三者是同一个判定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import DpeHashError, RemoteUrlError, check_remote_url


@pytest.fixture(scope="module")
def vector(vectors_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vectors_dir / "remote_url.json").read_text(encoding="utf-8"))
    assert data["kind"] == "remote_url"
    assert data["valid"] and data["invalid"]
    return data


def test_valid_remote_url_is_accepted_verbatim(vector: dict[str, Any]) -> None:
    for url in vector["valid"]:
        assert check_remote_url(url) == url, url


def test_invalid_remote_url_is_rejected(vector: dict[str, Any]) -> None:
    for case in vector["invalid"]:
        url = case["input"]
        with pytest.raises(RemoteUrlError) as info:
            check_remote_url(url)
        # 消息不回显 URL（userinfo 里可能是凭证）
        assert not url or url not in str(info.value), url


def test_remote_url_error_is_a_config_error_not_a_protocol_error() -> None:
    """配置错误：是 ValueError，但没有规范错误码，不属于 DpeHashError 体系。"""
    assert issubclass(RemoteUrlError, ValueError)
    assert not issubclass(RemoteUrlError, DpeHashError)
