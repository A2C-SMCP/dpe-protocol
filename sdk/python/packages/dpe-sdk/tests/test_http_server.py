"""独立监听（``python -m dpe_sdk.testing``）：外部进程经真实 TCP 端口访问参考服务端（#44、#45）。

这是 Rust SDK 集成测试（#25）与 conformance 跑分器的用法：子进程绑定空闲端口，stdout 第一行
给出 remote URL（启用钩子时另带钩子通道的 base URL），之后由同步 httpx 客户端驱动。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from dpe_sdk.models import Document
from dpe_sdk.protocol import IfAbsent, ProtocolCore, Request, Response, drive, fetch_capabilities
from dpe_sdk.testing.__main__ import _authority, _authorizer
from engine_helpers import inline, text_doc

C = "dpe1"


@contextmanager
def _spawn(*extra: str) -> Iterator[dict[str, Any]]:
    """启动参考服务端子进程，产出公布的一行 JSON（``url`` 与（启用钩子时的）``hooks``）。

    另给 ``stderr``：一个读子进程 stderr 至当下的函数（启动告警等）。
    """
    # stderr 落到临时文件：管道无人读取时，服务端日志写满缓冲会让子进程阻塞
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", *extra],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        assert proc.stdout is not None
        stdout = proc.stdout
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline()))
        reader.start()
        reader.join(timeout=20)
        try:
            if not lines or not lines[0]:
                proc.kill()
                proc.wait(timeout=5)
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")

            def stderr() -> str:
                log.seek(0)
                return log.read()

            info: dict[str, Any] = json.loads(lines[0])
            info["stderr"] = stderr
            yield info
        finally:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture
def server() -> Iterator[str]:
    with _spawn("--prefix", "/r/1", "--max-payload-bytes", "65536") as info:
        yield info["url"]


def _put(
    client: httpx.Client,
    url: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    merged = {"DPE-Hash-Contract": C, **(headers or {})}
    return client.put(url, headers=merged, content=body)


def test_standalone_server_is_reachable_from_another_process(server: str) -> None:
    assert server.startswith("http://127.0.0.1:") and server.endswith("/r/1")
    with httpx.Client() as client:

        def send(request: Request) -> Response:
            reply = client.request(
                request.method,
                f"{server}/{request.target}",
                headers=dict(request.headers),
                content=request.body_bytes(),
            )
            return Response(reply.status_code, dict(reply.headers), reply.content)

        capabilities = drive(fetch_capabilities(), send)
        assert capabilities.limits.max_payload_bytes == 65536
        core = ProtocolCore(capabilities)
        document = Document.model_validate(text_doc(["hello"], ["world"]))
        result = drive(core.commit("test://standalone", document, IfAbsent()), send)
        assert result.status == "created"
        assert drive(core.head("test://standalone"), send) == document.doc_hash()
        skeleton = drive(core.get_skeleton("test://standalone"), send)
        assert skeleton is not None and len(skeleton.pages) == 2


def test_cli_rejects_invalid_limits() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dpe_sdk.testing", "--staging-ttl-seconds", "10"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "staging_ttl_seconds" in proc.stderr
    assert proc.stdout == ""


def test_cli_rejects_force_without_grant() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dpe_sdk.testing", "--force", "Bearer nobody"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "--force" in proc.stderr
    assert proc.stdout == ""


def test_hooks_over_tcp_enable_self_write_and_expiry() -> None:
    args = (
        "--prefix", "/r/1",
        "--hooks", "/__hooks__",
        "--session-id-prefix", "st-test-",
    )  # fmt: skip
    with _spawn(*args) as info:
        server, hooks = info["url"], info["hooks"]
        assert hooks == server.removesuffix("/r/1") + "/__hooks__"
        first = inline(text_doc(["first"]))
        write = inline(text_doc(["server"]))
        with httpx.Client() as client:
            created = _put(
                client,
                f"{server}/documents?uri=test%3A%2F%2Fdocs%2Fa",
                first.body(),
                headers={"If-None-Match": "*"},
            )
            assert created.status_code == 201
            # 会话 id 可注入：顺序编号
            negotiated = client.post(
                f"{server}/negotiate",
                headers={"DPE-Hash-Contract": C},
                content=json.dumps(
                    {"file_uri": "test://docs/a", "document": {"file_type": "md", "pages": []}}
                ).encode(),
            )
            assert negotiated.json()["staging_session"]["id"] == "st-test-0"
            # 服务端自身写入：旧 base_hash 失效，head 变化
            response = client.post(
                f"{hooks}/self-write",
                content=json.dumps(
                    {
                        "file_uri": "test://docs/a",
                        "document": write.document,
                        "pages": write.pages,
                        "objects": write.objects,
                        "base_hash": first.doc_hash,
                    }
                ).encode(),
            )
            assert (response.status_code, response.json()["doc_hash"]) == (200, write.doc_hash)
            head = client.head(
                f"{server}/documents?uri=test%3A%2F%2Fdocs%2Fa",
                headers={"DPE-Hash-Contract": C},
            )
            assert head.headers["dpe-doc-hash"] == write.doc_hash
            # 强制过期：会话立刻不可用
            sid = negotiated.json()["staging_session"]["id"]
            expired = client.post(
                f"{hooks}/expire-session", content=json.dumps({"session_id": sid}).encode()
            )
            assert expired.json() == {"expired": True}
            element = inline(text_doc(["z"]))
            element_hash = element.content_hashes[0][0]
            use = client.put(
                f"{server}/staging/{sid}/objects/{element_hash}",
                headers={"DPE-Hash-Contract": C},
                content=json.dumps(element.objects[0]).encode(),
            )
            assert use.status_code == 410
            assert use.headers["dpe-error-code"] == "DPE_SESSION_EXPIRED"


def test_multi_identity_grants_over_tcp() -> None:
    args = (
        "--prefix", "/r/1",
        "--grant", "Bearer full", "*",
        "--grant", "Bearer limited", "test://mine/",
        "--force", "Bearer full",
    )  # fmt: skip
    with _spawn(*args) as info:
        server = info["url"]
        document = inline(text_doc(["x"]))
        mine = f"{server}/documents?uri=test%3A%2F%2Fmine%2Fa"
        other = f"{server}/documents?uri=test%3A%2F%2Fother%2Fa"
        with httpx.Client() as client:
            granted = _put(
                client,
                mine,
                document.body(),
                headers={"Authorization": "Bearer limited", "If-None-Match": "*"},
            )
            assert granted.status_code == 201
            denied = _put(
                client,
                other,
                document.body(),
                headers={"Authorization": "Bearer limited", "If-None-Match": "*"},
            )
            assert denied.status_code == 403
            assert denied.headers["dpe-error-code"] == "DPE_FORBIDDEN"
            # 未列出的凭证（等同无写授权）
            anonymous = _put(
                client,
                mine,
                document.body(),
                headers={"Authorization": "Bearer nobody", "If-None-Match": "*"},
            )
            assert anonymous.status_code == 403
            # force 权限：limited 没有，full 有
            forced = _put(
                client,
                f"{server}/documents?uri=test%3A%2F%2Fmine%2Fb",
                inline(text_doc(["y"])).body(force=True),
                headers={"Authorization": "Bearer limited"},
            )
            assert forced.status_code == 403
            full = _put(
                client,
                f"{server}/documents?uri=test%3A%2F%2Fmine%2Fb",
                inline(text_doc(["y"])).body(force=True),
                headers={"Authorization": "Bearer full"},
            )
            assert full.status_code == 201
            # 无写授权的 URI 提交与当前内容相同的 commit 也是 403（不泄露 unchanged）
            same = _put(client, other, document.body(), headers={"Authorization": "Bearer limited"})
            assert same.status_code == 403


def test_dual_contract_declaration_over_tcp() -> None:
    args = ("--prefix", "/r/1", "--contract", "dpe1", "--contract", "dpe2")  # fmt: skip
    with _spawn(*args) as info:
        server = info["url"]
        target = f"{server}/documents?uri=test%3A%2F%2Fdocs%2Fa"
        request = inline(text_doc(["x"]))
        drill_hash = inline(text_doc(["x"]), "dpe2").doc_hash
        with httpx.Client() as client:
            capabilities = client.get(f"{server}/capabilities").json()
            assert capabilities["hash_contracts"] == ["dpe1", "dpe2"]
            created = _put(client, target, request.body(), headers={"If-None-Match": "*"})
            assert created.status_code == 201
            assert created.headers["vary"] == "DPE-Hash-Contract"
            # 读接口按请求声明的契约返回值（同一内容，两个前缀）
            heads = client.post(
                f"{server}/heads",
                headers={"DPE-Hash-Contract": "dpe2"},
                content=json.dumps({"uris": ["test://docs/a"]}).encode(),
            )
            assert heads.json()["heads"][0]["doc_hash"] == drill_hash
            # 两种前缀的 base_hash 都匹配当前内容（契约升级期的版本令牌）
            overwritten = _put(
                client,
                target,
                inline(text_doc(["y"])).body(),
                headers={"If-Match": f'"{drill_hash}"'},
            )
            assert overwritten.status_code == 200


def test_wildcard_listener_announces_a_connectable_url() -> None:
    with _spawn("--host", "0.0.0.0", "--prefix", "/r/1") as info:
        assert info["url"].startswith("http://127.0.0.1:")
        with httpx.Client() as client:
            assert client.get(f"{info['url']}/capabilities").status_code == 200


def test_public_url_is_announced_verbatim_and_hooks_derive_from_it() -> None:
    """``--public-url`` 是 remote 的对外 base URL（含 prefix）。"""
    # url 原样公布；钩子挂在应用根，地址由它摘掉 prefix 得出
    args = (
        "--prefix", "/r/1",
        "--public-url", "https://dpe.example/dpe/r/1",
        "--hooks", "/__hooks__",
    )  # fmt: skip
    with _spawn(*args) as info:
        assert info["url"] == "https://dpe.example/dpe/r/1"
        assert info["hooks"] == "https://dpe.example/dpe/__hooks__"


def test_public_url_without_the_prefix_omits_the_hooks_address() -> None:
    """``--public-url`` 不以 prefix 结尾时推不出应用的外部根：只公布 url 并提示，不猜地址。"""
    args = (
        "--prefix", "/r/1",
        "--public-url", "https://dpe.example/dpe",
        "--hooks", "/__hooks__",
    )  # fmt: skip
    with _spawn(*args) as info:
        assert info["url"] == "https://dpe.example/dpe"
        assert "hooks" not in info
        assert "推不出钩子通道的地址" in info["stderr"]()


def test_public_url_at_the_root_keeps_the_hooks_address() -> None:
    """remote 挂在应用根（无 --prefix）时，``--public-url`` 本身就是应用的外部根。"""
    args = ("--public-url", "https://dpe.example/dpe", "--hooks", "/__hooks__")
    with _spawn(*args) as info:
        assert info["url"] == "https://dpe.example/dpe"
        assert info["hooks"] == "https://dpe.example/dpe/__hooks__"


def test_authority_brackets_ipv6_and_maps_wildcard_hosts() -> None:
    """公布的 authority：IPv6 加方括号、通配监听换回环地址（真实绑定见通配监听那条用例）。"""
    assert _authority("::", 8000, socket.AF_INET6) == "[::1]:8000"
    assert _authority("::1", 8000, socket.AF_INET6) == "[::1]:8000"
    assert _authority("0.0.0.0", 8000, socket.AF_INET) == "127.0.0.1:8000"
    assert _authority("192.168.1.5", 8000, socket.AF_INET) == "192.168.1.5:8000"


def test_authorizer_grants_are_unions_and_force_needs_a_grant() -> None:
    """同一凭证的多条 ``--grant`` 前缀取并集；``--force`` 的凭证必须被授予过写权限。"""
    auth = _authorizer([["Bearer a", "s3://x/"], ["Bearer a", "test://y/"]], ["Bearer a"])
    assert auth.can_write("Bearer a", "s3://x/1") and auth.can_write("Bearer a", "test://y/1")
    assert not auth.can_write("Bearer a", "other://z") and not auth.can_write("Bearer b", "*")
    assert auth.can_force("Bearer a", "s3://x/1") and not auth.can_force("Bearer b", "s3://x/1")
    with pytest.raises(ValueError, match="--force"):
        _authorizer([["Bearer a", "*"]], ["Bearer b"])


def test_non_loopback_listener_with_hooks_warns() -> None:
    """钩子通道不做认证：绑定非回环地址时提示到 stderr，回环不提示。"""
    with _spawn("--host", "0.0.0.0", "--hooks", "/__hooks__") as info:
        assert "警告" in info["stderr"]()
    with _spawn("--host", "127.0.0.1", "--hooks", "/__hooks__") as info:
        assert "警告" not in info["stderr"]()
