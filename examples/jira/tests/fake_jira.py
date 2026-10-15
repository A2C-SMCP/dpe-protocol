"""模拟 Jira Cloud 服务（只用标准库 http.server，跑在独立线程里）。

覆盖插件用到的端点：``/rest/api/3/myself``、增强搜索 ``POST /rest/api/3/search/jql``
（nextPageToken 分页、按 id 升序）、工单全文 ``GET /rest/api/3/issue/{id 或 key}``、评论分页
``GET /rest/api/3/issue/{id}/comment``。凭证不对时一律 401。

故障注入：``throttle[路径前缀] = n`` 让命中的前 n 个请求返回 429（``Retry-After`` 取
``retry_after``，``None`` 即不带该头），``throttle_forever`` 让命中的请求始终 429；``gone`` 里的
key 取全文时返回 404（模拟枚举之后被删）；``redirect[路径] = URL`` 让该路径返回 302；
``search_override`` 替换搜索响应（模拟不规范的分页字段）；``reject_search`` 让搜索返回 400
（JQL 不合法或项目不存在）；``not_json`` 里的路径返回 200 的 HTML（站点不是 Jira API）；
``server_error`` 里的路径返回 500；``gone_endpoints`` 里的路径返回 404（端点被移除）；
``search_fail_from`` 让起始位置不小于它的搜索页返回 500（前面的页正常，模拟部分页失败）。

源的变化用 ``touch``（改 updated）、``rekey``（换 key，模拟换项目）、``remove``（删除）。

附件：``attach`` 给工单加附件（元数据进工单的 ``attachment`` 字段，字节存在 ``blobs``）。
下载端点 ``/rest/api/3/attachment/content/{id}`` 在设了 ``media_url`` 时 303 到模拟媒体服务
（``FakeMedia``，另一个端口，即另一个源），否则直接在本站返回字节；两处都按 ``Range`` 返回 206，
``honor_range`` 为假时忽略 Range、整份返回 200。
数据由用例用 ``add_issue`` 构造；``requests`` 记下每个请求的（方法, 路径）。
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

EMAIL = "bot@example.com"
TOKEN = "s3cr3t-token-value"


def adf(*paragraphs: str) -> dict[str, Any]:
    """最简 ADF：每个字符串一段。"""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            for text in paragraphs
        ],
    }


@dataclass
class FakeJira:
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)  # id → 工单
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # id → 评论
    search_page_size: int = 100
    comment_page_size: int = 100
    throttle: dict[str, int] = field(default_factory=dict)
    throttle_forever: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)
    retry_after: str | None = "0"
    redirect: dict[str, str] = field(default_factory=dict)
    search_override: list[dict[str, Any]] | None = None
    token: str = TOKEN  # 站点当前接受的 API token（改掉即模拟凭证被吊销）
    account_id: str | None = "bot"  # /myself 返回的账号 id（改掉即换了账号，None 即不返回）
    reject_search: bool = False
    not_json: set[str] = field(default_factory=set)
    server_error: set[str] = field(default_factory=set)
    gone_endpoints: set[str] = field(default_factory=set)
    search_fail_from: int | None = None
    blobs: dict[str, bytes] = field(default_factory=dict)  # 附件 id → 字节
    media_url: str | None = None
    honor_range: bool = True
    auth_seen: list[tuple[str, bool]] = field(default_factory=list)  # (路径, 带了 Authorization)
    requests: list[tuple[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_issue(
        self,
        issue_id: int,
        key: str,
        *,
        summary: str = "",
        description: Any = None,
        updated: str = "2026-10-01T08:00:00.000+0000",
        created: str = "2026-09-01T08:00:00.000+0000",
        comments: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> None:
        self.issues[str(issue_id)] = {
            "id": str(issue_id),
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "created": created,
                "updated": updated,
                **fields,
            },
        }
        self.comments[str(issue_id)] = list(comments or [])

    def touch(self, issue_id: int, updated: str, **fields: Any) -> None:
        """改一个工单：updated 换新值，并更新给出的字段。"""
        self.issues[str(issue_id)]["fields"].update(updated=updated, **fields)

    def rekey(self, issue_id: int, key: str, updated: str | None = None) -> None:
        """换 key（工单被移到另一个项目）；``updated`` 给出时一并更新。"""
        issue = self.issues[str(issue_id)]
        issue["key"] = key
        if updated is not None:
            issue["fields"]["updated"] = updated

    def attach(
        self,
        issue_id: int,
        attachment_id: int,
        filename: str,
        data: bytes,
        *,
        size: int | None = None,
        mime: str = "application/octet-stream",
    ) -> None:
        """加一个附件；``size`` 默认取字节长度（给出别的值即模拟元数据与内容不符）。"""
        self.blobs[str(attachment_id)] = data
        fields = self.issues[str(issue_id)]["fields"]
        fields.setdefault("attachment", []).append(
            {
                "id": str(attachment_id),
                "filename": filename,
                "mimeType": mime,
                "size": len(data) if size is None else size,
                "content": f"/rest/api/3/attachment/content/{attachment_id}",
            }
        )

    def remove(self, issue_id: int) -> None:
        del self.issues[str(issue_id)]
        del self.comments[str(issue_id)]

    # ------------------------------------------------------------------ 路由

    def handle(
        self, method: str, raw_path: str, headers: Any, body: bytes
    ) -> tuple[int, Any, dict[str, str]]:
        parts = urlsplit(raw_path)
        path, query = parts.path, parse_qs(parts.query)
        limited = {} if self.retry_after is None else {"Retry-After": self.retry_after}
        with self.lock:
            self.requests.append((method, path))
            self.auth_seen.append((path, headers.get("Authorization") is not None))
            if path in self.redirect:
                return 302, {}, {"Location": self.redirect[path]}
            if path in self.not_json:
                return 200, b"<html>not jira</html>", {}
            if path in self.server_error:
                return 500, {"errorMessages": ["boom"]}, {}
            if path in self.gone_endpoints:
                return 404, {"errorMessages": ["no such endpoint"]}, {}
            for prefix in sorted(self.throttle_forever):
                if path.startswith(prefix):
                    return 429, {"errorMessages": ["rate limited"]}, limited
            for prefix, left in self.throttle.items():
                if path.startswith(prefix) and left > 0:
                    self.throttle[prefix] = left - 1
                    return 429, {"errorMessages": ["rate limited"]}, limited
        expected = "Basic " + base64.b64encode(f"{EMAIL}:{self.token}".encode()).decode()
        if headers.get("Authorization") != expected:
            return 401, {"errorMessages": ["unauthorized"]}, {}
        if method == "GET" and path == "/rest/api/3/myself":
            me = {"displayName": "Bot"}
            if self.account_id is not None:
                me["accountId"] = self.account_id
            return 200, me, {}
        if method == "POST" and path == "/rest/api/3/search/jql":
            if self.reject_search:
                return 400, {"errorMessages": ["The value 'ABX' does not exist"]}, {}
            return self._search(json.loads(body))
        if method == "GET" and path.startswith("/rest/api/3/attachment/content/"):
            attachment_id = path.removeprefix("/rest/api/3/attachment/content/")
            if attachment_id not in self.blobs:
                return 404, {"errorMessages": ["no attachment"]}, {}
            if self.media_url is not None:
                target = f"{self.media_url}/file/{attachment_id}?token=signed"
                return 303, {}, {"Location": target}
            return ranged(self.blobs[attachment_id], headers, self.honor_range)
        if method == "GET" and path.startswith("/rest/api/3/issue/"):
            rest = unquote(path.removeprefix("/rest/api/3/issue/"))
            if rest.endswith("/comment"):
                return self._comments(rest.removesuffix("/comment"), query)
            return self._issue(rest)
        return 404, {"errorMessages": ["no route"]}, {}

    def _ordered(self) -> list[dict[str, Any]]:
        return [self.issues[i] for i in sorted(self.issues, key=int)]

    def _search(self, body: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
        if self.search_override is not None:
            index = int(body.get("nextPageToken") or 0)
            return 200, self.search_override[min(index, len(self.search_override) - 1)], {}
        start = int(body.get("nextPageToken") or 0)
        if self.search_fail_from is not None and start >= self.search_fail_from:
            return 500, {"errorMessages": ["search backend unavailable"]}, {}
        ordered = self._ordered()
        page = ordered[start : start + self.search_page_size]
        result: dict[str, Any] = {
            "issues": [
                {"id": i["id"], "key": i["key"], "fields": {"updated": i["fields"]["updated"]}}
                for i in page
            ],
            "isLast": start + len(page) >= len(ordered),
        }
        if not result["isLast"]:
            result["nextPageToken"] = str(start + len(page))
        return 200, result, {}

    def _issue(self, id_or_key: str) -> tuple[int, Any, dict[str, str]]:
        for issue in self.issues.values():
            if id_or_key in (issue["id"], issue["key"]) and issue["key"] not in self.gone:
                return 200, issue, {}
        return 404, {"errorMessages": ["Issue does not exist"]}, {}

    def _comments(
        self, issue_id: str, query: dict[str, list[str]]
    ) -> tuple[int, Any, dict[str, str]]:
        if issue_id not in self.comments:
            return 404, {"errorMessages": ["Issue does not exist"]}, {}
        start = int(query.get("startAt", ["0"])[0])
        everything = self.comments[issue_id]
        # 故意不按 created 排序返回：插件必须自己排
        page = list(reversed(everything))[start : start + self.comment_page_size]
        return 200, {"comments": page, "startAt": start, "total": len(everything)}, {}


def ranged(data: bytes, headers: Any, honor: bool) -> tuple[int, Any, dict[str, str]]:
    """按请求头里的 ``Range: bytes=a-b`` 返回 206；不认 Range 时整份返回 200。"""
    match = re.fullmatch(r"bytes=([0-9]+)-([0-9]+)", headers.get("Range") or "")
    if not honor or match is None:
        return 200, data, {}
    first, last = int(match.group(1)), min(int(match.group(2)), len(data) - 1)
    if first >= len(data):
        return 416, b"", {"Content-Range": f"bytes */{len(data)}"}
    return 206, data[first : last + 1], {"Content-Range": f"bytes {first}-{last}/{len(data)}"}


@dataclass
class FakeMedia:
    """模拟媒体服务：按附件 id 返回 ``source.blobs`` 里的字节；记下每个请求是否带了凭证。"""

    source: FakeJira
    honor_range: bool = True
    requests: list[tuple[str, bool]] = field(default_factory=list)  # (路径, 带了 Authorization)
    #: 给出时替代正常响应（构造不规范的 Range 响应、跳转链等）：(路径, 请求头) → 响应
    responder: Callable[[str, Any], tuple[int, Any, dict[str, str]]] | None = None

    def handle(
        self, method: str, raw_path: str, headers: Any, body: bytes
    ) -> tuple[int, Any, dict[str, str]]:
        path = urlsplit(raw_path).path
        self.requests.append((path, headers.get("Authorization") is not None))
        if self.responder is not None:
            return self.responder(path, headers)
        data = self.source.blobs.get(path.removeprefix("/file/"))
        if method != "GET" or data is None:
            return 404, b"", {}
        return ranged(data, headers, self.honor_range)


@contextmanager
def serving(fake: FakeJira | FakeMedia) -> Iterator[str]:
    """在回环地址的随机端口上运行模拟服务，产出站点地址（``http://127.0.0.1:<端口>``）。"""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload, extra = fake.handle(method, self.path, self.headers, body)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # http.server 的命名约定
            self._reply("GET")

        def do_POST(self) -> None:
            self._reply("POST")

        def log_message(self, format: str, *args: Any) -> None:
            return  # 测试输出保持干净

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
