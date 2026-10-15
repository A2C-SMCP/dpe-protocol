"""Jira Cloud REST v3 的最小客户端：只用标准库 urllib，Basic 认证。

- **凭证只发给站点本身**：API 调用不跟随重定向（本不该重定向）；附件下载会被 303 到媒体服务，
  手动跟随（至多 5 跳、不接受 https → http 降级）；只有与站点同源、且这条跳转链还没离开过站点
  的请求才带 ``Authorization``——外部源不能指挥插件带着凭证去取站点上的其他路径。
  任何错误消息都不带凭证，也不带重定向目标（媒体服务的地址里有签名）；
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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from dpe_hash import retry_after_seconds

__all__ = [
    "AuthError",
    "Fetched",
    "JiraClient",
    "JiraError",
    "NotFound",
    "RateLimited",
    "Rejected",
    "SpoolFailed",
    "Unavailable",
    "UnexpectedResponse",
]

#: 单个请求的超时（秒）
_TIMEOUT = 30.0
#: 限流时的最多尝试次数（含首次）与单次等待上限（秒）：超过上限的提示不等，直接放弃
_MAX_ATTEMPTS = 4
_MAX_WAIT = 60.0
#: 附件下载跟随重定向的最多跳数
_MAX_REDIRECTS = 5
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
#: 整份下载写入临时文件时每次读取的字节数
_COPY_CHUNK = 1 << 16
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})")

_T = TypeVar("_T")


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


class SpoolFailed(JiraError):
    """写临时文件失败（磁盘满、目录不可写等）：本机的问题，不是网络失败，原样重试多半无效。"""

    retryable = False


class UnexpectedResponse(JiraError):
    """协议不符：3xx、其余 4xx、响应体不是 JSON 或结构不符。重试不会成功。"""

    retryable = False


def _origin(value: str) -> tuple[str, str, int | None]:
    """（scheme, 主机, 端口）；端口不合法时抛 ``ValueError``。"""
    parts = urllib.parse.urlsplit(value)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    if not parts.hostname:
        raise ValueError("缺少主机名")
    return scheme, parts.hostname.lower(), port


@dataclass(frozen=True)
class Fetched:
    """一次附件读取的结果。

    ``partial`` 为真：服务端按 Range 返回了 ``data``，它从 ``first`` 开始，``total`` 是
    ``Content-Range`` 声明的总长；为假：服务端忽略了 Range、返回整份内容，已写入调用方给的
    文件，共 ``written`` 字节（超过 ``limit`` 时提前停止，``written`` 为 ``limit + 1``）。
    """

    partial: bool
    first: int = 0
    total: int = 0
    data: bytes = b""
    written: int = 0


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

    def project(self, key: str, *, attempts: int = _MAX_ATTEMPTS) -> Any:
        return self._request(
            "GET", f"/rest/api/3/project/{urllib.parse.quote(key, safe='')}", attempts=attempts
        )

    def components(self, key: str) -> Any:
        return self._request(
            "GET", f"/rest/api/3/project/{urllib.parse.quote(key, safe='')}/components"
        )

    def versions(self, key: str) -> Any:
        return self._request(
            "GET", f"/rest/api/3/project/{urllib.parse.quote(key, safe='')}/versions"
        )

    def attachment_range(
        self,
        attachment_id: str,
        offset: int,
        length: int,
        spool: Callable[[], str],
        limit: int,
    ) -> Fetched:
        """按 Range 读附件 ``[offset, offset + length)``；服务端忽略 Range 时把整份内容写进
        ``spool()`` 给出的文件（只在这时才调用；至多 ``limit + 1`` 字节，够判断长度是否超出）。"""
        path = f"/rest/api/3/attachment/content/{urllib.parse.quote(attachment_id, safe='')}"
        what = f"GET {path}"

        def consume(response: Any) -> Fetched:
            if response.status == 206:
                match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range") or "")
                if match is None:
                    raise UnexpectedResponse(f"{what}：Content-Range 不合法")
                first, last, total = (int(group) for group in match.groups())
                data = response.read(length + 1)
                if last < first or len(data) != last - first + 1 or len(data) > length:
                    raise UnexpectedResponse(f"{what}：Range 响应的长度与 Content-Range 不符")
                return Fetched(partial=True, first=first, total=total, data=data)
            if response.status != 200:
                raise UnexpectedResponse(f"{what}：HTTP {response.status}")
            written = 0
            try:
                sink = open(spool(), "wb")  # noqa: SIM115 —— 写失败与读失败要分开归类，见下
            except OSError as exc:
                raise SpoolFailed(f"{what}：无法创建临时文件（{type(exc).__name__}）") from None
            with sink:
                while written <= limit:
                    chunk = response.read(min(_COPY_CHUNK, limit + 1 - written))
                    if not chunk:
                        break
                    try:
                        sink.write(chunk)
                    except OSError as exc:
                        raise SpoolFailed(
                            f"{what}：写临时文件失败（{type(exc).__name__}）"
                        ) from None
                    written += len(chunk)
            return Fetched(partial=False, written=written)

        return self._exchange(
            "GET",
            self._site + path,
            what,
            consume,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
            follow=True,
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
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        raw: bytes = self._exchange(
            method,
            url,
            what,
            lambda response: response.read(),
            data=data,
            headers=headers,
            attempts=attempts,
        )
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise UnexpectedResponse(f"{what}：响应体不是 JSON") from None

    def _same_origin(self, url: str) -> bool:
        """``url`` 与站点同源（scheme、主机、端口都相同）。"""
        return _origin(url) == _origin(self._site)

    def _exchange(
        self,
        method: str,
        url: str,
        what: str,
        consume: Callable[[Any], _T],
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        attempts: int = _MAX_ATTEMPTS,
        follow: bool = False,
    ) -> _T:
        """发一个请求，在响应打开期间交给 ``consume``；限流按 Retry-After 退避，错误分类见模块说明。

        ``follow`` 为真时手动跟随重定向（见模块说明）；``what`` 只含方法与站点内路径，用于消息。
        """
        delay = 1.0
        attempt = 1
        hops = 0
        left_site = False  # 跳转链离开过站点：此后一律不带凭证
        start = (url, method, data)
        while True:
            request = urllib.request.Request(url, data=data, method=method)
            if not left_site and self._same_origin(url):
                request.add_header("Authorization", self._authorization)
            for name, value in (headers or {}).items():
                request.add_header(name, value)
            try:
                with self._opener.open(request, timeout=_TIMEOUT) as response:
                    return consume(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location")
                retry_after = exc.headers.get("Retry-After")
                exc.close()
                if follow and status in _REDIRECTS and location:
                    hops += 1
                    if hops > _MAX_REDIRECTS:
                        raise UnexpectedResponse(
                            f"{what}：重定向超过 {_MAX_REDIRECTS} 跳"
                        ) from None
                    try:
                        target = urllib.parse.urljoin(url, location)
                        _origin(target)  # 端口等不合法即抛 ValueError
                    except ValueError:
                        raise UnexpectedResponse(f"{what}：重定向地址不合法") from None
                    scheme = urllib.parse.urlsplit(target).scheme.lower()
                    if scheme not in ("http", "https") or (
                        urllib.parse.urlsplit(url).scheme.lower() == "https" and scheme != "https"
                    ):
                        raise UnexpectedResponse(f"{what}：拒绝重定向到非 https 地址") from None
                    left_site = left_site or not self._same_origin(target)
                    url, method, data = target, "GET", None
                    continue
                if left_site and status in (401, 403, 404):
                    # 离开站点后（媒体服务的签名地址）：多半是签名过期，与 Jira 凭证无关，可重试
                    raise Unavailable(f"{what}：跳转目标返回 HTTP {status}") from None
                if status in (401, 403):
                    raise AuthError(f"{what}：HTTP {status}（凭证错误或无权限）") from None
                if status == 404:
                    raise NotFound(f"{what}：HTTP 404") from None
                if status in (429, 503):
                    wait = retry_after_seconds(retry_after)
                    if wait is None:
                        wait = delay
                        delay *= 2
                    if attempt >= attempts or wait > _MAX_WAIT:
                        raise RateLimited(
                            f"{what}：HTTP {status}，按 Retry-After 退避 {attempt} 次后仍未成功"
                        ) from None
                    self._sleep(wait)
                    attempt += 1
                    # 从原始地址重新开始：等待期间跳转目标的签名可能已过期
                    url, method, data = start
                    hops, left_site = 0, False
                    continue
                if status == 400:
                    raise Rejected(f"{what}：HTTP 400（请求被拒）") from None
                if status >= 500 or status in (408, 425):  # 含请求超时、过早：暂时性
                    raise Unavailable(f"{what}：HTTP {status}") from None
                raise UnexpectedResponse(f"{what}：HTTP {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                raise Unavailable(f"{what}：网络失败（{type(reason).__name__}）") from None
