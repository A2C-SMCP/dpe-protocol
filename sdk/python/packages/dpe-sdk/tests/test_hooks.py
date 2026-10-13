"""一致性测试钩子的 HTTP 通道（#45）：默认关闭；启用后驱动自身写入、会话过期与拨钟。

钩子不是 DPE 端点（不在 remote 的 prefix 下，用应用根的保留路径直达），也不做 DPE 认证；
只有 ``create_app(..., hooks=HooksConfig(...))`` 显式启用时才存在。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

import dpe_hash
import httpx
import pytest
from dpe_sdk.testing import AdjustableClock, Engine, HooksConfig, create_app
from engine_helpers import FakeClock, inline, make_engine, text_doc
from http_helpers import PREFIX, REMOTE, Remote, problem, remote

pytestmark = pytest.mark.anyio

C = dpe_hash.CONTRACT
ABSENT = "dpe1:" + "0" * 64
URI = "test://docs/hook"
TARGET = "documents?uri=test%3A%2F%2Fdocs%2Fhook"
HOOKS = "/__hooks__"
#: 应用根（钩子挂在它下面，而不是 remote 的 prefix 下）
ORIGIN = REMOTE.removesuffix(PREFIX)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _hooked(**overrides: Any) -> tuple[Engine, AdjustableClock, HooksConfig]:
    clock = AdjustableClock()
    engine = make_engine(clock=clock, **overrides)
    return engine, clock, HooksConfig(path=HOOKS, clock=clock)


async def _hook(
    r: Remote, path: str, payload: dict[str, Any] | None = None, *, method: str = "POST"
) -> httpx.Response:
    content = None if payload is None else json.dumps(payload).encode()
    return await r.client.request(method, f"{ORIGIN}{path}", content=content)


async def _negotiate(r: Remote) -> str:
    payload = {"file_uri": URI, "document": {"file_type": "md", "pages": []}}
    response = await r.raw("POST", "negotiate", content=json.dumps(payload).encode())
    assert response.status_code == 200, response.text
    sid: str = response.json()["staging_session"]["id"]
    return sid


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# 启用与校验（钩子未启用时行为与规范默认一致）
# ---------------------------------------------------------------------------


async def test_hooks_are_off_by_default() -> None:
    async with remote() as r:
        response = await _hook(r, f"{HOOKS}/self-write", {})
        assert response.status_code == 404
        assert (await r.raw("GET", "capabilities")).status_code == 200


def test_hook_config_is_validated_at_startup() -> None:
    clock = AdjustableClock()
    engine = make_engine(clock=clock)
    with pytest.raises(ValueError, match="以 / 开头"):  # 不以 / 开头
        create_app(engine, hooks=HooksConfig(path="hooks", clock=clock))
    with pytest.raises(ValueError, match="以 / 开头"):  # 尾部 /
        create_app(engine, hooks=HooksConfig(path="/__hooks__/", clock=clock))
    with pytest.raises(ValueError, match="以 / 开头"):  # 含需百分号编码的字符
        create_app(engine, hooks=HooksConfig(path="/__h oks__", clock=clock))
    with pytest.raises(ValueError, match="顶掉 DPE 端点"):  # 根 prefix 下顶掉 DPE 端点
        create_app(engine, hooks=HooksConfig(path="/documents", clock=clock))
    with pytest.raises(ValueError, match="互相遮蔽"):  # 与 DPE prefix 互相遮蔽
        create_app(engine, prefix="/r/1", hooks=HooksConfig(path="/r/1/hook", clock=clock))
    fake = FakeClock()  # 是引擎用的时钟（身份校验通得过），但不是可拨动的时钟
    with pytest.raises(ValueError, match="advance"):  # 拨不动它
        create_app(
            make_engine(clock=cast(Any, fake)),
            hooks=HooksConfig(path=HOOKS, clock=cast(Any, fake)),
        )
    with pytest.raises(ValueError, match="同一个"):  # 拨钟拨不到引擎用的时钟
        create_app(engine, prefix="/r/1", hooks=HooksConfig(path=HOOKS, clock=AdjustableClock()))


async def test_hooks_work_without_a_remote_prefix() -> None:
    """remote 挂在应用根（prefix=""，create_app 与 CLI 的缺省）时钩子照常可用。"""
    engine, _clock, hooks = _hooked()
    app = create_app(engine, hooks=hooks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        assert (await client.get("http://dpe.test/capabilities")).status_code == 200
        response = await client.post(
            f"http://dpe.test{HOOKS}/self-write",
            content=json.dumps(
                {"file_uri": URI, "document": {"file_type": "md", "pages": []}, "force": True}
            ).encode(),
        )
        assert response.status_code == 201


async def test_enabled_hooks_leave_dpe_behavior_unchanged() -> None:
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        capabilities = await r.raw("GET", "capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["hash_contracts"] == ["dpe1"]
        created = await r.raw(
            "PUT", TARGET, headers={"If-None-Match": "*"}, content=inline(text_doc(["x"])).body()
        )
        assert created.status_code == 201
        # 未知的钩子子路径与错误方法
        assert (await _hook(r, f"{HOOKS}/nope", {})).status_code == 404
        assert (await _hook(r, f"{HOOKS}/self-write", None, method="GET")).status_code == 405


# ---------------------------------------------------------------------------
# self-write（core §2.4 同级写入）
# ---------------------------------------------------------------------------


async def test_self_write_hook_runs_the_commit_path() -> None:
    engine, _clock, hooks = _hooked()
    first = inline(text_doc(["first"]))
    write = inline(text_doc(["server"]))
    async with remote(engine, hooks=hooks) as r:
        created = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=first.body())
        assert created.status_code == 201
        response = await _hook(
            r,
            f"{HOOKS}/self-write",
            {
                "file_uri": URI,
                "document": write.document,
                "pages": write.pages,
                "objects": write.objects,
                "base_hash": first.doc_hash,
            },
        )
        assert (response.status_code, response.json()["status"]) == (200, "updated")
        assert response.json()["doc_hash"] == write.doc_hash
        assert response.headers["dpe-doc-hash"] == write.doc_hash
        # head 变化；来源按旧 base_hash 提交 → 412，按新值覆盖成功（同级写入场景）
        head = await r.raw("HEAD", TARGET)
        assert head.headers["dpe-doc-hash"] == write.doc_hash
        late = inline(text_doc(["late"]))
        stale = await r.raw(
            "PUT", TARGET, headers={"If-Match": f'"{first.doc_hash}"'}, content=late.body()
        )
        assert stale.status_code == 412
        fresh = await r.raw(
            "PUT", TARGET, headers={"If-Match": f'"{write.doc_hash}"'}, content=late.body()
        )
        assert fresh.status_code == 200


async def test_self_write_hook_cas_and_validation() -> None:
    engine, _clock, hooks = _hooked()
    empty = {"file_type": "md", "pages": []}
    empty_hash = dpe_hash.object_hash(empty, "document", C)
    async with remote(engine, hooks=hooks) as r:
        base: dict[str, Any] = {"file_uri": URI, "document": empty}
        missing = await _hook(r, f"{HOOKS}/self-write", base)
        assert missing.status_code == 428
        assert problem(missing)["code"] == "DPE_PRECONDITION_REQUIRED"
        created = await _hook(r, f"{HOOKS}/self-write", {**base, "force": True})
        assert created.status_code == 201  # force 显式给出，服务端身份同样受它约束
        other = {"file_type": "txt", "pages": []}
        again = await _hook(
            r, f"{HOOKS}/self-write", {"file_uri": URI, "document": other, "if_absent": True}
        )
        # 前置条件由请求体承载：按 HTTP 绑定 §5 的口径取 409（条件头才是 412，如 move）
        assert again.status_code == 409
        assert problem(again)["code"] == "DPE_ALREADY_EXISTS"
        stale = await _hook(
            r,
            f"{HOOKS}/self-write",
            {
                "file_uri": URI,
                "document": {"file_type": "org", "pages": []},
                "base_hash": ABSENT,
            },
        )
        assert (stale.status_code, problem(stale)["code"]) == (409, "DPE_PRECONDITION_FAILED")
        both = await _hook(
            r, f"{HOOKS}/self-write", {**base, "base_hash": empty_hash, "if_absent": True}
        )
        assert both.status_code == 400
        unknown = await _hook(r, f"{HOOKS}/self-write", {**base, "nope": 1, "force": True})
        assert unknown.status_code == 400
        no_document = await _hook(r, f"{HOOKS}/self-write", {"file_uri": URI, "force": True})
        assert no_document.status_code == 400
        undeclared = await _hook(
            r, f"{HOOKS}/self-write", {**base, "force": True, "contract": "dpe9"}
        )
        assert problem(undeclared)["code"] == "DPE_CONTRACT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# expire-session 与 clock/advance
# ---------------------------------------------------------------------------


async def test_expire_session_hook() -> None:
    engine, _clock, hooks = _hooked()
    element = {"category": "NarrativeText", "text": "hello"}
    raw = json.dumps(element).encode()
    hash_ = dpe_hash.object_hash(element, "element", C)
    async with remote(engine, hooks=hooks) as r:
        sid = await _negotiate(r)
        other = await r.raw(
            "POST",
            "negotiate",
            content=json.dumps(
                {"file_uri": "test://docs/other", "document": {"file_type": "md", "pages": []}}
            ).encode(),
        )
        other_sid = other.json()["staging_session"]["id"]
        expired = await _hook(r, f"{HOOKS}/expire-session", {"session_id": sid})
        assert (expired.status_code, expired.json()) == (200, {"expired": True})
        assert (await _hook(r, f"{HOOKS}/expire-session", {"session_id": sid})).json() == {
            "expired": False
        }
        use = await r.raw("PUT", f"staging/{sid}/objects/{hash_}", content=raw)
        assert use.status_code == 410
        assert problem(use)["code"] == "DPE_SESSION_EXPIRED"
        # 其它会话不受影响
        assert (
            await r.raw("PUT", f"staging/{other_sid}/objects/{hash_}", content=raw)
        ).status_code == 201


async def test_clock_advance_hook_renews_and_expires() -> None:
    engine, _clock, hooks = _hooked(staging_ttl_seconds=3600)
    element = {"category": "NarrativeText", "text": "hello"}
    page = {"elements": [dpe_hash.object_hash(element, "element", C)]}
    page_raw = json.dumps(page).encode()
    page_hash = dpe_hash.object_hash(page, "page", C)
    async with remote(engine, hooks=hooks) as r:
        payload = {"file_uri": URI, "document": {"file_type": "md", "pages": [page_hash]}}
        negotiated = await r.raw("POST", "negotiate", content=json.dumps(payload).encode())
        sid = negotiated.json()["staging_session"]["id"]
        target = f"staging/{sid}/pages/{page_hash}"
        before = await r.raw("HEAD", target)
        expires = before.headers["dpe-session-expires"]
        assert (before.status_code, before.headers["dpe-upload-offset"]) == (200, "0")
        advanced = await _hook(r, f"{HOOKS}/clock/advance", {"advance_seconds": 60})
        assert advanced.status_code == 200 and advanced.json()["now"].endswith("Z")
        # 断点查询不续期：过期时间原样返回
        again = await r.raw("HEAD", target)
        assert again.headers["dpe-session-expires"] == expires
        # 成功的上传续期到「此刻 + TTL」：拨钟 60 秒后，过期时间后移约 60 秒（秒级取整）
        uploaded = await r.raw("PUT", target, content=page_raw)
        assert uploaded.status_code == 201
        delta = _seconds(uploaded.headers["dpe-session-expires"]) - _seconds(expires)
        assert 59 <= delta <= 61
        # 拨过 TTL 之后会话过期（过期时间向上取整到秒，实际可用时长不少于 TTL：多拨 1 秒）
        await _hook(r, f"{HOOKS}/clock/advance", {"advance_seconds": 3601})
        gone = await r.raw("HEAD", target)
        assert gone.status_code == 410
        bad = await _hook(r, f"{HOOKS}/clock/advance", {"advance_seconds": 0})
        assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 错误注入（#123）：429 / 503 与 Retry-After 的黑盒构造
# ---------------------------------------------------------------------------

PROBE_HEADER = "DPE-Conformance-Probe"


async def _armed(r: Remote, payload: dict[str, Any]) -> httpx.Response:
    response = await _hook(r, f"{HOOKS}/inject-error", payload)
    assert response.status_code == 200, response.text
    assert response.json()["armed"] is True
    return response


async def test_inject_error_hook_returns_429_in_full_form() -> None:
    """注入后，带探针头的请求得到 429 + problem 全形 + ``Retry-After``；
    不带探针头与探针不同的请求不受影响；计数用完后恢复正常。"""
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        await _armed(
            r,
            {"code": "DPE_RATE_LIMITED", "probe": "p-429", "count": 2, "retry_after": 5},
        )
        # 不带探针头：照常走引擎（capabilities 200）
        untouched = await r.raw("GET", "capabilities", contract=None)
        assert untouched.status_code == 200
        # 探针不同：同样不受影响
        other = await r.raw("GET", "capabilities", contract=None, headers={PROBE_HEADER: "p-other"})
        assert other.status_code == 200
        # 命中探针的两次请求：429 全形（documents 之外的路径不带 Vary）
        for _ in range(2):
            response = await r.raw(
                "GET", "capabilities", contract=None, headers={PROBE_HEADER: "p-429"}
            )
            body = problem(response)
            assert (response.status_code, body["code"]) == (429, "DPE_RATE_LIMITED")
            assert body["retryable"] is True
            assert response.headers["retry-after"] == "5"
            assert "vary" not in response.headers
        # 计数耗尽：恢复
        again = await r.raw("GET", "capabilities", contract=None, headers={PROBE_HEADER: "p-429"})
        assert again.status_code == 200


async def test_inject_error_hook_503_defaults_retry_after_to_one() -> None:
    """503 注入不给 ``retry_after``：服务端给缺省值 ``1``（HTTP 绑定 §5）。"""
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        await _armed(r, {"code": "DPE_UNAVAILABLE", "probe": "p-503"})
        response = await r.raw(
            "HEAD", TARGET, headers={PROBE_HEADER: "p-503"}
        )  # HEAD：错误只在码头
        assert response.status_code == 503
        assert response.headers["dpe-error-code"] == "DPE_UNAVAILABLE"
        assert response.headers["retry-after"] == "1"


async def test_inject_error_hook_intercepts_before_engine_and_vary_follows_path() -> None:
    """拦截在路由之后、引擎之前：不存在的文档也得到注入的错误（而非 404）；
    ``documents`` 路径的注入响应照常带 ``Vary``（HTTP §3.1），钩子通道与未知路径不受影响。"""
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        await _armed(r, {"code": "DPE_RATE_LIMITED", "probe": "p-doc", "count": 3})
        response = await r.raw(
            "GET",
            "documents?uri=test%3A%2F%2Fdocs%2Fabsent",
            headers={PROBE_HEADER: "p-doc"},
        )
        body = problem(response)
        assert (response.status_code, body["code"]) == (429, "DPE_RATE_LIMITED")
        assert response.headers["vary"] == "DPE-Hash-Contract"
        # 未知路径与钩子通道不受注入影响（路由先于拦截）
        missing = await r.raw("GET", "nope", headers={PROBE_HEADER: "p-doc"})
        assert missing.status_code == 404
        hook_call = await _hook(
            r, f"{HOOKS}/inject-error", {"code": "DPE_UNAVAILABLE", "probe": "p-doc"}
        )
        assert hook_call.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "DPE_VALIDATION", "probe": "p"},  # 非可重试码
        {"code": "DPE_RATE_LIMITED"},  # 缺 probe
        {"code": "DPE_RATE_LIMITED", "probe": ""},  # 空 probe
        {"code": "DPE_RATE_LIMITED", "probe": "   "},  # 纯空白 probe（头值读取时会 strip）
        {"code": "DPE_RATE_LIMITED", "probe": " p "},  # 带首尾空白的 probe
        {"code": "DPE_RATE_LIMITED", "probe": "p", "count": 0},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "count": -1},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "count": "2"},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "count": True},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "retry_after": -1},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "retry_after": "5"},
        {"code": "DPE_RATE_LIMITED", "probe": "p", "extra": 1},
    ],
    ids=[
        "non-retryable-code",
        "missing-probe",
        "empty-probe",
        "blank-probe",
        "padded-probe",
        "count-zero",
        "count-negative",
        "count-string",
        "count-bool",
        "retry-after-negative",
        "retry-after-string",
        "unknown-member",
    ],
)
async def test_inject_error_hook_validation(payload: dict[str, Any]) -> None:
    """钩子载荷按封闭成员与取值校验：违例一律 400 + ``DPE_VALIDATION``。"""
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        response = await _hook(r, f"{HOOKS}/inject-error", payload)
        assert response.status_code == 400, response.text
        assert problem(response)["code"] == "DPE_VALIDATION"


async def test_inject_error_hook_rearming_replaces_the_previous_injection() -> None:
    """同一探针重复注入以最后一次为准（count 覆盖，不累加）。"""
    engine, _clock, hooks = _hooked()
    async with remote(engine, hooks=hooks) as r:
        await _armed(r, {"code": "DPE_RATE_LIMITED", "probe": "p", "count": 5})
        await _armed(r, {"code": "DPE_UNAVAILABLE", "probe": "p", "count": 1})
        first = await r.raw("GET", "capabilities", contract=None, headers={PROBE_HEADER: "p"})
        assert first.status_code == 503
        second = await r.raw("GET", "capabilities", contract=None, headers={PROBE_HEADER: "p"})
        assert second.status_code == 200


async def test_inject_error_hook_is_ignored_when_hooks_are_off() -> None:
    """钩子未启用：探针头被完全忽略，请求照常走引擎（行为与不带该头一致）。"""
    engine = make_engine()
    async with remote(engine) as r:  # 无 hooks
        with_probe = await r.raw("GET", "capabilities", contract=None, headers={PROBE_HEADER: "p"})
        without = await r.raw("GET", "capabilities", contract=None)
        assert with_probe.status_code == without.status_code == 200


# ---------------------------------------------------------------------------
# 顺带受益（#123）：传输层的可重试错误重发对真参考服务端端到端


class _ProbeTransport(httpx.AsyncBaseTransport):
    """给经过的每个请求加探针头：让真实参考服务端的错误注入命中 SDK 的传输层。"""

    def __init__(self, inner: httpx.AsyncBaseTransport, probe: str) -> None:
        self._inner = inner
        self._probe = probe

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers[PROBE_HEADER] = self._probe
        return await self._inner.handle_async_request(request)


async def test_injected_rate_limit_is_retried_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#13 传输层「可重试错误按 ``Retry-After`` 等待后原样重发」对真参考服务端端到端：
    注入一次 429 + ``Retry-After: 2``，客户端等待 2 秒（测试替换等待函数）后重发成功。"""
    from dpe_sdk.transport import AsyncDpeClient

    engine, _clock, hooks = _hooked()
    waits: list[float] = []

    async def fake_asleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("dpe_sdk.transport._asleep", fake_asleep)
    app = create_app(engine, prefix=PREFIX, hooks=hooks)
    async with AsyncDpeClient(
        REMOTE, transport=_ProbeTransport(httpx.ASGITransport(app=app), "e2e")
    ) as client:
        assert (await client.capabilities()).protocol == "dpe/1"  # 能力协商先完成
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            armed = await http.post(
                f"{ORIGIN}{HOOKS}/inject-error",
                json={
                    "code": "DPE_RATE_LIMITED",
                    "probe": "e2e",
                    "count": 1,
                    "retry_after": 2,
                },
            )
            assert armed.status_code == 200, armed.text
        # 这次 head 的第一次尝试被注入 429 挡下：传输层按 Retry-After 等待后重发，
        # 重发命中引擎（文档不存在 → None）
        assert await client.head(URI) is None
    assert waits == [2.0], "传输层应按注入的 Retry-After 等待恰好 2 秒（本次替换了等待函数）"
