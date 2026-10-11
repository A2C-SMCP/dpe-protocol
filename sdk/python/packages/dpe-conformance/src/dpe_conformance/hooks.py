"""服务端钩子的客户端：按中性配置把一次钩子调用驱动成 HTTP 请求或命令。

钩子是测试侧信道，**不进入 DPE 协议面**：跑分器按「HTTP 或命令」的入口配置驱动，不假定
服务端如何实现。载荷与成功判据（包 README「服务端钩子契约」）：

- ``self-write``（core §2.4 同级写入）：载荷是「commit 请求体成员 + file_uri + 前置条件」，
  成员名全为 DPE 协议词汇（``document`` / ``pages`` / ``objects`` / ``force`` /
  ``base_hash`` / ``if_absent`` / ``contract``）；
- ``expire-session``：载荷 ``{"session_id": …}``，成功响应体给 ``{"expired": bool}``；
- ``clock-advance``：载荷 ``{"advance_seconds": N}``（正整数）——把服务端时钟推进 N 秒，
  **会话 TTL 按推进后的时间判定**；成功响应体给 ``{"now": "<RFC 3339>"}``；
- ``inject-error``（#123）：载荷 ``{"code", "probe", "count"?, "retry_after"?}``——让带请求头
  ``DPE-Conformance-Probe: <probe>`` 的请求接下来 ``count`` 次（缺省 1）直接得到该错误
  （``code`` 只接受 ``DPE_RATE_LIMITED`` / ``DPE_UNAVAILABLE``）；``retry_after`` 缺省时由服务端
  给缺省值（``1``，HTTP 绑定 §5）。成功响应体给 ``{"armed": true, "count": N}``。

命令入口从 stdin 收一行 JSON 载荷，退出码 0 即成功（stdout 若为 JSON 对象则作为响应体）；
HTTP 入口以 ``POST`` 发送同一载荷，2xx 即成功。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from dpe_conformance.config import HookEndpoint

#: 错误注入（#123）的探针请求头：探针值随载荷给出，服务端只拦截带同一头值的请求
PROBE_HEADER = "DPE-Conformance-Probe"

#: 单次钩子调用的缺省超时（秒）
DEFAULT_HOOK_TIMEOUT = 30.0


@dataclass(frozen=True)
class HookResult:
    """一次钩子调用的结果：``ok`` 为成功判据的结论；``detail`` 供失败断言的可读消息。"""

    ok: bool
    detail: str
    body: Any | None = None


class HookDriver:
    """一个钩子入口的调用器；``client`` 由调用方提供（套件复用 target 的客户端）。"""

    def __init__(
        self,
        endpoint: HookEndpoint,
        client: httpx.Client,
        *,
        timeout: float = DEFAULT_HOOK_TIMEOUT,
    ) -> None:
        self._endpoint = endpoint
        self._client = client
        self._timeout = timeout

    def call(self, payload: Mapping[str, Any]) -> HookResult:
        """驱动一次钩子调用。"""
        if self._endpoint.http is not None:
            return self._call_http(payload)
        assert self._endpoint.command is not None
        return self._call_command(payload)

    def _call_http(self, payload: Mapping[str, Any]) -> HookResult:
        assert self._endpoint.http is not None
        try:
            response = self._client.post(
                self._endpoint.http, json=dict(payload), timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return HookResult(False, f"HTTP 钩子调用失败：{exc}")
        if not 200 <= response.status_code < 300:
            return HookResult(False, f"HTTP 钩子返回 {response.status_code}：{response.text[:200]}")
        return HookResult(True, f"HTTP {response.status_code}", _maybe_json(response))

    def _call_command(self, payload: Mapping[str, Any]) -> HookResult:
        assert self._endpoint.command is not None
        try:
            completed = subprocess.run(
                list(self._endpoint.command),
                input=json.dumps(dict(payload), ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HookResult(False, f"钩子命令执行失败：{exc}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return HookResult(False, f"钩子命令退出码 {completed.returncode}：{detail[:200]}")
        return HookResult(True, "命令退出码 0", _maybe_json_text(completed.stdout))


def self_write_payload(
    *,
    file_uri: str,
    document: Mapping[str, Any],
    pages: list[Mapping[str, Any]] | None = None,
    objects: list[Mapping[str, Any]] | None = None,
    base_hash: str | None = None,
    if_absent: bool = False,
    force: bool = False,
    contract: str | None = None,
) -> dict[str, Any]:
    """组装 ``self-write`` 载荷（成员名与 commit 请求体一致，均见 HTTP 绑定 §4.8）。

    成员组合的合法性交由服务端裁决（如 ``base_hash`` 与 ``if_absent`` 并存会被拒绝）——
    跑分器要能用它构造非法组合，因此这里不做前置校验。
    """
    payload: dict[str, Any] = {"file_uri": file_uri, "document": dict(document)}
    if pages:
        payload["pages"] = [dict(page) for page in pages]
    if objects:
        payload["objects"] = [dict(obj) for obj in objects]
    if base_hash is not None:
        payload["base_hash"] = base_hash
    if if_absent:
        payload["if_absent"] = True
    if force:
        payload["force"] = True
    if contract is not None:
        payload["contract"] = contract
    return payload


def _maybe_json(response: httpx.Response) -> Any | None:
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _maybe_json_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def inject_error_payload(
    *,
    code: str,
    probe: str,
    count: int | None = None,
    retry_after: float | None = None,
) -> dict[str, Any]:
    """组装 ``inject-error`` 载荷（#123）：``probe`` 是本次检查一次性生成的随机值，服务端据此
    只拦截带同一 ``DPE-Conformance-Probe`` 请求头的请求——不绑定身份、不携带凭证，两种入口同构。

    ``count`` 缺省交给服务端（1）；``retry_after`` 不给时验证服务端自己的缺省值。
    """
    payload: dict[str, Any] = {"code": code, "probe": probe}
    if count is not None:
        payload["count"] = count
    if retry_after is not None:
        payload["retry_after"] = retry_after
    return payload
