"""HTTPS 与凭证的本地端到端验证（#68 决策 7、8）。

- 两条链都确实走 TLS：增量轮经「记录代理 → TLS 终结器 → 参考服务端」，``full_snapshot``
  直连 TLS 终结器；终结器记录握手次数，分别断言增加。
- 参考服务端 ``--require-auth --grant`` 下，运行器从环境变量解析凭证；代理记录里
  ``authorization`` 已脱敏，断言失败输出也不出现凭证值。
- 清理：一次运行结束后，本次前缀下的文档被 CAS 删除。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
from _relay import REDACTED
from dpe_sdk.transport import DpeClient
from e2e_harness import (
    Runner,
    ServerOptions,
    _active,
    make_repo,
    reference_server,
    runner,
)
from helpers import requires_git

pytestmark = [requires_git, pytest.mark.scenarios]

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="需要 openssl 命令行生成测试 CA"
)


@requires_openssl
def test_tls_terminator_covers_proxy_and_direct_chains(tmp_path: Path) -> None:
    """增量链（代理 → TLS）与全量链（直连 TLS）都经终结器，握手次数分别增加。"""
    root = tmp_path / "root"
    make_repo(root / "a", "a 初始化")
    with runner(
        tmp_path,
        {"root": str(root), "branches": []},
        local_options=ServerOptions(tls=True),
        force_local=True,
    ) as r:
        assert r.server is not None and r.server.tls is not None
        tls = r.server.tls
        mark = r.proxy.mark()

        first = r.round()
        assert first.changed() == {"created": 1}
        assert tls.handshake_count > 0  # 增量链：代理 → TLS 终结器
        assert r.proxy.mark() > mark  # 确实经过了记录代理

        before_direct = tls.handshake_count
        r.full_snapshot()  # 全量链：dpe-run 直连 TLS 终结器
        assert tls.handshake_count > before_direct
        assert r.has_env("SSL_CERT_FILE")  # 只在本地的 TLS 模式设置
        assert tls.errors == []

        r.assert_converged()


def test_require_auth_and_redaction(tmp_path: Path) -> None:
    """参考服务端要求凭证时运行器照常投递；代理记录与断言输出中的凭证一律脱敏。"""
    authorization = "Bearer local-e2e-secret"
    root = tmp_path / "root"
    make_repo(root / "a", "a 初始化")
    options = ServerOptions(root="git://auth-e2e/", authorization=authorization)
    with runner(
        tmp_path,
        {"root": str(root), "branches": []},
        local_options=options,
        force_local=True,
    ) as r:
        first = r.round()
        assert first.changed() == {"created": 1}
        recorded = [
            q.headers["authorization"] for q in first.requests if "authorization" in q.headers
        ]
        assert recorded and all(value == REDACTED for value in recorded)
        assert authorization not in repr(first.requests)
        assert "local-e2e-secret" not in repr(first.requests)
        assert authorization not in repr(r.proxy.errors)
        assert not r.has_env("SSL_CERT_FILE")  # 明文本地模式不设置
        r.assert_converged()


def test_cleanup_removes_this_runs_prefix(tmp_path: Path) -> None:
    """一次运行结束后，本次前缀（inc 与全部 full{n}）下的文档被 CAS 删除。"""
    root = tmp_path / "root"
    make_repo(root / "a", "a 初始化")
    with reference_server() as server:
        instance = Runner(
            tmp_path,
            server.url,
            {"root": str(root), "branches": []},
            local_remote=True,
            root_prefix=server.root,
        )
        prefix = instance.run_prefix
        with _active(instance):
            instance.round()
            instance.full_snapshot()
            assert instance.snapshot()
        with DpeClient(server.url, transport=httpx.HTTPTransport(proxy=None)) as client:
            page = client.list_page(prefix)
            assert page.documents == []
