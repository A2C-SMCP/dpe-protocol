# vector-kind: remote_url
"""REMOTE-URL 按 HTTP 绑定 §1 的文法校验（#181）：逐例消费 vectors/remote_url.json。

不合法的 remote 在开跑前以退出码 2 拒绝，消息不回显 URL；合法的 remote 通过这一步（用未知
``--section`` 让 CLI 在随后的参数检查处早退，不真的开跑）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dpe_conformance.cli import main

_UNKNOWN_SECTION = "no-such-section-181"


@pytest.fixture
def vector(vectors_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((vectors_dir / "remote_url.json").read_text(encoding="utf-8"))
    assert data["kind"] == "remote_url"
    return data


def test_invalid_remote_is_rejected_before_running(
    vector: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for case in vector["invalid"]:
        url = case["input"]
        assert main([url, "--section", _UNKNOWN_SECTION]) == 2, url
        err = capsys.readouterr().err
        assert "REMOTE-URL 不符合 HTTP 绑定 §1 的文法" in err, url
        assert not url or url not in err, url


def test_valid_remote_passes_the_url_check(
    vector: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for url in vector["valid"]:
        assert main([url, "--section", _UNKNOWN_SECTION]) == 2, url
        err = capsys.readouterr().err
        assert "REMOTE-URL" not in err, url
        assert _UNKNOWN_SECTION in err, url
