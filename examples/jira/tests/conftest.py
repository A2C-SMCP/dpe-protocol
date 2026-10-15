"""测试公共设置：模拟 Jira 跑在回环地址上，请求不得经过开发机配置的代理。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
