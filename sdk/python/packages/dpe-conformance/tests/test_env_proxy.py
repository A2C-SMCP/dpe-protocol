"""跑分器只从环境变量取代理（#194）：规则副本与 SDK 原件一致，客户端按目的地判定，经真实 TCP。

- 规则：逐例消费两个 SDK 共用的 ``sdk/testdata/proxy_env.json``；另比较本包 ``_proxy.py`` 与
  ``dpe_sdk._proxy`` 去掉模块说明后的 AST（副本不得漂移；``dpe_sdk`` 只在测试里出现）；
- 传输层：本地代理（``proxy_helpers``，与 SDK 的代理用例共用）+ 参考服务端，真实 TCP；
- CLI：以独立进程跑冒烟章，经环境变量指定的代理；代理 URL 不合法时在配置阶段报错、不回显值。
"""

from __future__ import annotations

import ast
import json
import os
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dpe_conformance._proxy
import dpe_sdk._proxy
import httpx
import pytest
from dpe_conformance._env_transport import EnvProxyTransport, destination
from dpe_conformance._proxy import proxy_for
from helpers import reference_server, run_cli
from proxy_helpers import ProxyServer, start_proxy

CASES = Path(__file__).resolve().parents[4] / "testdata" / "proxy_env.json"
_TABLE: list[dict[str, Any]] = json.loads(CASES.read_text(encoding="utf-8"))["cases"]

#: 判定会读到的全部变量：真实设施用例先清掉，避免受运行测试的机器环境影响
_PROXY_VARS = [
    name
    for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    for name in (base, base.lower())
] + ["REQUEST_METHOD"]


@pytest.mark.parametrize("case", _TABLE, ids=[c["name"] for c in _TABLE])
def test_shared_proxy_table(case: dict[str, Any]) -> None:
    if case["expected"] == "error":
        with pytest.raises(ValueError):
            proxy_for(case["remote"], case["env"])
    else:
        assert proxy_for(case["remote"], case["env"]) == case["expected"]


def _rules(path: str | None) -> str:
    assert path is not None
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    body = tree.body
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
    return ast.dump(ast.Module(body=body[1:], type_ignores=[]))


def test_rules_are_a_verbatim_copy_of_the_sdk() -> None:
    """除模块说明外与 ``dpe_sdk._proxy`` 逐字相同：改规则须先改原件、再同步副本。"""
    assert _rules(dpe_conformance._proxy.__file__) == _rules(dpe_sdk._proxy.__file__)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8000/__hooks__/x", "http://127.0.0.1/"),
        ("https://Dpe.Example.com/r/1", "https://dpe.example.com/"),
        ("http://[::1]:8080/r", "http://[::1]/"),
        ("https://xn--r8jz45g.jp/r/1", "https://xn--r8jz45g.jp/"),  # 不解码成 Unicode
    ],
)
def test_destination_keeps_only_scheme_and_host(url: str, expected: str) -> None:
    assert destination(url) == expected


def test_idn_destinations_follow_the_sdk() -> None:
    """``xn--`` 主机按线上的 ASCII 形式判定，与 SDK 同一结论（``NO_PROXY`` 的 ``xn--`` 条目
    命中即直连）。"""
    env = {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "xn--r8jz45g.jp"}
    remote = "https://xn--r8jz45g.jp/r/1"
    assert EnvProxyTransport(env).proxy_for(remote) is None
    assert dpe_sdk._proxy.proxy_for(remote, env) is None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def proxy() -> Iterator[ProxyServer]:
    yield from start_proxy("forward")


@pytest.fixture
def remote() -> Iterator[str]:
    with reference_server("--prefix", "/r/1") as server:
        yield server.url


def test_each_destination_is_judged_on_its_own(proxy: ProxyServer, remote: str) -> None:
    """同一个客户端：target 经代理；另一个 origin（如服务端钩子）命中 ``NO_PROXY`` 时直连。"""
    port = httpx.URL(remote).port
    env = {"HTTP_PROXY": proxy.url, "NO_PROXY": "localhost"}
    with httpx.Client(transport=EnvProxyTransport(env)) as client:
        assert client.get(f"{remote}/capabilities").status_code == 200
        assert client.get(f"http://localhost:{port}/r/1/capabilities").status_code == 200
    assert proxy.seen == [f"GET {remote}/capabilities"]


def test_system_proxy_settings_are_ignored(
    monkeypatch: pytest.MonkeyPatch, proxy: ProxyServer, remote: str
) -> None:
    """模拟「系统设了代理、环境变量没设」：httpx 缺省客户端会走它，本传输层直连。"""
    system = {"http": proxy.url, "https": proxy.url, "all": proxy.url}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: system)
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: system)
    with httpx.Client() as plain:  # 先证明模拟生效
        plain.get(f"{remote}/capabilities")
    assert proxy.seen == [f"GET {remote}/capabilities"]
    proxy.seen.clear()
    with httpx.Client(transport=EnvProxyTransport(os.environ)) as client:
        assert client.get(f"{remote}/capabilities").status_code == 200
    assert proxy.seen == []


def _cli_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
    return {**env, **extra}


def test_cli_goes_through_the_env_proxy(tmp_path: Path, proxy: ProxyServer, remote: str) -> None:
    completed = run_cli(
        remote, "--section", "smoke", cwd=tmp_path, env=_cli_env(HTTP_PROXY=proxy.url)
    )
    assert completed.returncode == 0, completed.stderr
    assert proxy.seen and all(
        line.startswith(("GET ", "POST ", "PUT ", "DELETE ", "HEAD ")) and f" {remote}/" in line
        for line in proxy.seen
    )


def test_cli_applies_the_sdk_rules_not_httpx_defaults(
    tmp_path: Path, proxy: ProxyServer, remote: str
) -> None:
    """``NO_PROXY`` 的 CIDR 按本仓规则命中即直连；httpx 缺省不认 CIDR、会走代理——据此证明
    插件确实用了本包的传输层。"""
    completed = run_cli(
        remote,
        "--section",
        "smoke",
        cwd=tmp_path,
        env=_cli_env(HTTP_PROXY=proxy.url, NO_PROXY="127.0.0.0/8"),
    )
    assert completed.returncode == 0, completed.stderr
    assert proxy.seen == []


def test_cli_prechecks_hook_destinations(tmp_path: Path) -> None:
    """target 为 http、钩子为 https：只有钩子会用到不合法的 ``HTTPS_PROXY``，同样在发出任何请求
    之前以退出码 2 结束、不回显值。"""
    config = tmp_path / "dpe-conformance.toml"
    config.write_text(
        'schema = 1\n[hooks.self-write]\nhttp = "https://hooks.invalid/self-write"\n',
        encoding="utf-8",
    )
    completed = run_cli(
        "http://127.0.0.1:9/r",
        "--config",
        str(config),
        "--section",
        "smoke",
        cwd=tmp_path,
        env=_cli_env(HTTPS_PROXY="socks5://user:s3cr3t@127.0.0.1:1080"),
    )
    assert completed.returncode == 2
    output = completed.stdout + completed.stderr
    assert "代理配置不合法" in output and "HTTPS_PROXY" in output
    assert "s3cr3t" not in output


def test_cli_rejects_an_invalid_proxy_url_without_echoing_it(tmp_path: Path) -> None:
    completed = run_cli(
        "http://127.0.0.1:9/r",
        "--section",
        "smoke",
        cwd=tmp_path,
        env=_cli_env(HTTP_PROXY="socks5://user:s3cr3t@127.0.0.1:1080"),
    )
    assert completed.returncode == 2  # 用法 / 配置故障，在发出任何请求之前
    output = completed.stdout + completed.stderr
    assert "代理配置不合法" in output and "HTTP_PROXY" in output
    assert "s3cr3t" not in output
