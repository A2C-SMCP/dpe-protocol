"""Jira Cloud REST v3 的最小客户端：只用标准库 urllib，Basic 认证。

- **凭证只发给站点本身**：不跟随任何重定向（API 调用本不该重定向；附件下载的跨域重定向留到
  附件一期单独处理），任何错误消息都不带凭证；
- **代理只认环境变量**（``HTTP(S)_PROXY`` / ``NO_PROXY``）：不读操作系统的代理设置，与两个 SDK
  的口径一致（#132）；
- **限流**：429 / 503 按 ``Retry-After`` 等待后重试（RFC 9110 文法，复用
  ``dpe_hash.retry_after_seconds``，不合文法视同没有提示、改用指数退避），有次数与单次等待的
  上限；耗尽即 ``RateLimited``，由调用方按轮级可重试处理（connector §8 / #223 §8）；
- **错误分两类**（同运行器对 remote 的判定口径，connector §7.1）：传输失败、5xx 与限流是
  暂时的（``retryable``）；3xx、其余 4xx、2xx 却不是 JSON 说明站点或请求本身不对，重试不会
  成功（``retryable = False``）。
"""

from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from dpe_hash import retry_after_seconds

__all__ = [
    "AuthError",
    "JiraClient",
    "JiraError",
    "NotFound",
    "RateLimited",
    "Rejected",
    "Unavailable",
    "UnexpectedResponse",
]

#: 单个请求的超时（秒）
_TIMEOUT = 30.0
#: 限流时的最多尝试次数（含首次）与单次等待上限（秒）：超过上限的提示不等，直接放弃
_MAX_ATTEMPTS = 4
_MAX_WAIT = 60.0


class JiraError(Exception):
    """Jira 请求失败；``retryable`` 表示同样的请求稍后重试可能成功。消息不含凭证。"""

    retryable = True


class AuthError(JiraError):
    """401 / 403：凭证错误、过期或无权限。改凭证之前重试不会成功。"""

    retryable = False


class NotFound(JiraError):
    """404：资源不存在。端点 404 说明站点不对，重试不会成功；工单 404（两次请求之间被删）由
    调用方另行判为条目级、可重试的 ``source_unavailable``。"""

    retryable = False


class RateLimited(JiraError):
    """429 / 503 按 Retry-After 退避重试后仍未成功。"""


class Unavailable(JiraError):
    """暂时不可用：网络失败、超时、连接中断、5xx 或 408 / 425。"""


class Rejected(JiraError):
    """400：请求被拒（如 JQL 不合法、项目不存在）。改配置之前重试不会成功。"""

    retryable = False


class UnexpectedResponse(JiraError):
    """协议不符：3xx、其余 4xx、响应体不是 JSON 或结构不符。重试不会成功。"""

    retryable = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：3xx 原样作为错误交回（凭证不会被带到别的主机）。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class JiraClient:
    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._site = site
        credential = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        self._authorization = f"Basic {credential}"
        self._sleep = sleep
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
            _NoRedirect(),
        )

    # ------------------------------------------------------------------ 接口

    def myself(self, *, attempts: int = _MAX_ATTEMPTS) -> Any:
        """当前凭证对应的账号（``initialize`` 用它探测凭证是否可用）。"""
        return self._request("GET", "/rest/api/3/myself", attempts=attempts)

    def search(
        self,
        jql: str,
        fields: list[str],
        page_token: str | None,
        *,
        max_results: int = 100,
        attempts: int = _MAX_ATTEMPTS,
    ) -> Any:
        """增强搜索（``POST /rest/api/3/search/jql``）的一页；``nextPageToken`` 翻页。"""
        body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if page_token is not None:
            body["nextPageToken"] = page_token
        return self._request("POST", "/rest/api/3/search/jql", body=body, attempts=attempts)

    def issue(self, id_or_key: str, fields: list[str]) -> Any:
        return self._request(
            "GET",
            f"/rest/api/3/issue/{urllib.parse.quote(id_or_key, safe='')}",
            {"fields": ",".join(fields)},
        )

    def comments(self, issue_id: str, start_at: int) -> Any:
        return self._request(
            "GET",
            f"/rest/api/3/issue/{urllib.parse.quote(issue_id, safe='')}/comment",
            {"startAt": str(start_at), "maxResults": "100", "orderBy": "created"},
        )

    # ------------------------------------------------------------------ 传输

    def _request(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        *,
        body: Any = None,
        attempts: int = _MAX_ATTEMPTS,
    ) -> Any:
        url = self._site + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        what = f"{method} {path}"
        delay = 1.0
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", self._authorization)
            request.add_header("Accept", "application/json")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with self._opener.open(request, timeout=_TIMEOUT) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status in (401, 403):
                    raise AuthError(f"{what}：HTTP {status}（凭证错误或无权限）") from None
                if status == 404:
                    raise NotFound(f"{what}：HTTP 404") from None
                if status in (429, 503):
                    wait = retry_after_seconds(exc.headers.get("Retry-After"))
                    if wait is None:
                        wait = delay
                        delay *= 2
                    if attempt == attempts or wait > _MAX_WAIT:
                        raise RateLimited(
                            f"{what}：HTTP {status}，按 Retry-After 退避 {attempt} 次后仍未成功"
                        ) from None
                    self._sleep(wait)
                    continue
                if status == 400:
                    raise Rejected(f"{what}：HTTP 400（请求被拒）") from None
                if status >= 500 or status in (408, 425):  # 含请求超时、过早：暂时性
                    raise Unavailable(f"{what}：HTTP {status}") from None
                raise UnexpectedResponse(f"{what}：HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                raise Unavailable(f"{what}：网络失败（{type(reason).__name__}）") from None
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                raise UnexpectedResponse(f"{what}：响应体不是 JSON") from None
        raise AssertionError("不可达")  # pragma: no cover
