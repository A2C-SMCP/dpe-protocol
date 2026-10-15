"""线协议服务端（connector 契约 §6.2–§6.8）：sans-IO，一行请求进、一行响应出。

方法：``initialize``、``scan``（按批拉取）、``shutdown``；``cancel`` 是通知。本期没有 blob，
``read_blob`` 一律以未知句柄（``-32002``）拒绝。请求同步处理（同 markdown-dir）。

**一轮**（本期为全量枚举，``supports_cursor: false``）：轮首用增强搜索枚举范围内全部工单（只取
key 与 updated，按 nextPageToken 翻页，按 id 升序），之后每一步取一个工单的全文与全部评论，
映射为 document 条目（``mapping``）。

**错误归属**：

- 轮首搜索或轮中读取失败 → ``scan`` 以 ``-32003`` 应答，运行器中止本轮（§6.4）；
  ``data.retryable`` 的取值见下文；
- initialize 探测凭证（``/myself``）并用一次 ``maxResults: 1`` 的搜索检查范围（JQL 合法、
  项目存在）；凭证被拒、范围被拒（400）、站点不是 Jira REST API（3xx、404、非 JSON）都判为
  ``-32005``（实例级、不自动重试，§6.3、§7.1）。探测时的网络失败、5xx 与限流不是配置问题：
  探测不重试，握手照常完成，由首个 ``scan`` 以可重试的 ``-32003`` 报告；
- ``-32003`` 的 ``data.retryable`` 取失败的性质：暂时性失败为 ``true``；凭证失效、请求被拒、
  响应不符协议为 ``false``，免得运行器对永久失败空转（§6.8）；
- 单个工单 404（两次请求之间被删）→ 条目级 ``source_unavailable``（可重试，下一轮自然不再出现）；
  内容无法映射 → 条目级 ``content_invalid``（不可重试）。

**单条消息上限**（§6.2、§6.5）：同 markdown-dir——批次按编码字节累计；一篇装不下就在页边界
分段（``continued``），单页都装不下即整篇 ``content_invalid``。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from dpe_hash import RemoteUrlError, check_remote_url

from dpe_jira import PLUGIN_NAME, __version__
from dpe_jira.client import (
    JiraClient,
    JiraError,
    NotFound,
    RateLimited,
    Unavailable,
    UnexpectedResponse,
)
from dpe_jira.mapping import ISSUE_FIELDS, MappingError, issue_document

__all__ = ["PROTOCOL_VERSION", "Serve"]

PROTOCOL_VERSION = "dpe-connector/1"
#: 信封（jsonrpc / id / result / items 键、结束字段与行尾 LF）的预留字节；id 长度另扣
_ENVELOPE_RESERVE = 1024
#: 开始分段时为后续批次预留的余量：请求 id 变长会让后续批的预算变小，单页须在扣掉余量后
#: 仍装得下，已开始的分段才不会中途断掉（§6.5 同一文档的各段连续出现）
_SEGMENT_SLACK = 64
_BATCH_MAX_ITEMS = 1000
#: ``next`` token：``<轮号>.<批号>``——轮号让上一轮的过期 token 不会碰巧与本轮的相等（§6.4）
_TOKEN = re.compile(r"[0-9]{1,18}\.[0-9]{1,18}")
#: 前缀须以 URI 分隔字符结尾：码点前缀匹配不识别段边界，否则拼接会越界（§6.5）
_PREFIX_END = frozenset("/?#")
#: Jira 项目 key（与清单 config_schema 的 pattern 一致）
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
#: 工单 key 中照原样进 URI 的字符（RFC 3986 unreserved），其余百分号编码
_KEEP = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_CONFIG_KEYS = frozenset({"site", "projects", "jql", "include_project_docs"})


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def issue_uri(prefix: str, key: str) -> str:
    """实例前缀 + ``issue/<工单 key>``（key 按 RFC 3986 编码；正常的 key 本身就是安全字符）。"""
    encoded = "".join(
        ch if ch in _KEEP else "".join(f"%{b:02X}" for b in ch.encode("utf-8")) for ch in key
    )
    return f"{prefix}issue/{encoded}"


def _error_item(uri: str | None, code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "kind": "error",
        "file_uri": uri,
        "code": code,
        "message": message,
        "retryable": retryable,
    }


class _RoundAborted(Exception):
    """本轮无法继续（读取失败或响应不符）：``scan`` 以 ``-32003`` 应答，带失败的可重试性。"""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class _Issue:
    """搜索得到的一个工单：数字 id 与 key。"""

    id: str
    key: str


@dataclass
class _Round:
    number: int
    steps: list[_Issue]
    position: int = 0
    #: 当前步骤已取得的条目（本批装不下留到下一批时复用，不重取）
    scanned: tuple[int, dict[str, Any]] | None = None
    #: 未发完的分段文档：(file_uri, 剩余页)
    pending: tuple[str, list[dict[str, Any]]] | None = None
    issued: int = 0


class Serve:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.initialized = False
        self.shutdown_requested = False
        self.fatal = False
        self.max_message_bytes = 0
        self.prefix = ""
        self._environ = os.environ if environ is None else environ
        self._client: JiraClient | None = None
        self._jql = ""
        self._round: _Round | None = None
        self._rounds = 0

    def _token(self, round_: _Round) -> str:
        return f"{round_.number}.{round_.issued}"

    # ------------------------------------------------------------------ 入口

    def handle(self, line: str) -> str | None:
        if self.initialized and len(line.encode("utf-8")) + 1 > self.max_message_bytes:
            return self._error(None, -32600, "请求超过 max_message_bytes")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return self._error(None, -32700, "解析错误")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "非法请求")
        method, id_, params = message.get("method"), message.get("id"), message.get("params", {})
        if method == "cancel":
            return None  # 通知：同步处理，对应请求已应答
        if not isinstance(method, str) or not isinstance(params, dict):
            return self._error(id_, -32600, "非法请求")
        if not self.initialized:
            if method != "initialize":
                return self._error(id_, -32600, "握手完成前不接受其他请求")
            return self._initialize(id_, params)
        if method == "shutdown":
            self.shutdown_requested = True
            return _line({"jsonrpc": "2.0", "id": id_, "result": {}})
        if method == "scan":
            return self._scan(id_, params)
        if method == "read_blob":
            return self._error(id_, -32002, "未知或已失效的句柄")
        return self._error(id_, -32601, "未知方法")

    def _error(self, id_: Any, code: int, message: str, data: Any = None) -> str:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        text = _line({"jsonrpc": "2.0", "id": id_, "error": error})
        if self.max_message_bytes and len(text.encode("utf-8")) + 1 > self.max_message_bytes:
            text = _line({"jsonrpc": "2.0", "id": id_, "error": {"code": code}})  # 只留错误码
        return text

    def _result(self, id_: Any, result: dict[str, Any]) -> str:
        return _line({"jsonrpc": "2.0", "id": id_, "result": result})

    # ------------------------------------------------------------------ initialize

    def _initialize(self, id_: Any, params: dict[str, Any]) -> str:
        versions = params.get("protocol_versions")
        max_bytes = params.get("max_message_bytes")
        instance = params.get("instance")
        if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
            self.fatal = True
            return self._error(id_, -32004, "没有共同支持的线协议版本")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            self.fatal = True
            return self._error(id_, -32602, "max_message_bytes 必须是正整数")
        self.max_message_bytes = max_bytes
        if not isinstance(instance, dict):
            self.fatal = True
            return self._error(id_, -32602, "instance 必须是对象")
        problem = self._configure(instance.get("uri_prefix"), instance.get("config"))
        if problem is not None:
            self.fatal = True
            return self._error(id_, -32005, problem)
        self.initialized = True
        return self._result(
            id_,
            {
                "protocol_version": PROTOCOL_VERSION,
                "plugin": {"name": PLUGIN_NAME, "version": __version__},
                "capabilities": {"supports_cursor": False},
            },
        )

    def _configure(self, prefix: Any, config: Any) -> str | None:
        """校验配置与凭证并探测一次站点；不合法返回原因（``-32005`` invalid_config，§6.3）。

        原因里不回显配置值与凭证值（site 可能误带 userinfo，§4.3「不外泄」）。
        """
        if not isinstance(prefix, str) or not prefix or prefix[-1] not in _PREFIX_END:
            return "实例前缀必须以 URI 分隔字符（/ ? #）结尾，才能与 issue/<key> 拼接"
        if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
            return "配置只认 site、projects、jql 与 include_project_docs"
        site = config.get("site")
        if not isinstance(site, str):
            return "site 必须是字符串"
        try:
            check_remote_url(site)
        except RemoteUrlError as exc:
            return f"site 不是规范的站点地址：{exc}"
        projects, jql = config.get("projects"), config.get("jql")
        if (projects is None) == (jql is None):
            return "projects 与 jql 必须且只能给出一个"
        if projects is not None:
            if (
                not isinstance(projects, list)
                or not projects
                or not all(isinstance(p, str) and _PROJECT_KEY.fullmatch(p) for p in projects)
            ):
                return "projects 必须是非空的项目 key 列表（大写字母开头，只含大写字母、数字与 _）"
            scope = "project in ({})".format(", ".join(f'"{p}"' for p in projects))
        else:
            if not isinstance(jql, str) or not jql.strip():
                return "jql 必须是非空字符串"
            if _ORDER_BY.search(jql):
                return "jql 不得包含 ORDER BY（排序由插件决定）"
            scope = jql
        include = config.get("include_project_docs", True)
        if not isinstance(include, bool):
            return "include_project_docs 必须是布尔值"
        email = self._environ.get("JIRA_EMAIL", "")
        token = self._environ.get("JIRA_API_TOKEN", "")
        if not email or not token:
            return "缺少凭证 JIRA_EMAIL 或 JIRA_API_TOKEN（应由运行器按清单注入）"
        client = JiraClient(site, email, token)
        jql = f"({scope}) ORDER BY id ASC"
        probes: tuple[Callable[[], Any], ...] = (
            lambda: client.myself(attempts=1),
            lambda: client.search(jql, ["key"], None, max_results=1, attempts=1),
        )
        for probe in probes:  # 各自探测：一项暂时失败不让另一项的配置错误漏过
            try:
                probe()
            except (Unavailable, RateLimited):
                pass  # 暂时性失败不是配置问题，留给 scan 按可重试报告
            except JiraError as exc:
                return f"站点、凭证或范围不被 Jira 接受：{exc}"
        self.prefix = prefix
        self._client = client
        self._jql = jql
        return None

    # ------------------------------------------------------------------ scan

    def _scan(self, id_: Any, params: dict[str, Any]) -> str:
        token = params.get("next")
        if token is not None:
            if "cursor" in params or not isinstance(token, str) or not _TOKEN.fullmatch(token):
                return self._error(id_, -32602, "未知的 next")
            # 只有最近回出的那个 token 合法（收到它即上一批已收到）；其余一律过期（§6.4）
            if self._round is None or token != self._token(self._round):
                return self._error(id_, -32602, "未知或已过期的 next")
        else:
            cursor = params.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                return self._error(id_, -32602, "cursor 必须是字符串或 null")
            # 本期不支持游标（initialize 已声明 supports_cursor: false），给了也按全量处理
            try:
                self._round = self._open_round()
            except JiraError as exc:
                self._round = None
                return self._error(
                    id_, -32003, f"枚举工单失败：{exc}", {"retryable": exc.retryable}
                )
        round_ = self._round
        assert round_ is not None
        max_items = params.get("max_items")
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items <= 0:
            max_items = _BATCH_MAX_ITEMS
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _size(id_)
        if budget < 256:
            return self._error(id_, -32602, "单条预算过小，无法产出")
        try:
            items = self._next_batch(round_, min(max_items, _BATCH_MAX_ITEMS), budget)
        except _RoundAborted as exc:
            self._round = None
            return self._error(id_, -32003, str(exc), {"retryable": exc.retryable})
        if round_.pending is not None or round_.position < len(round_.steps):
            round_.issued += 1
            return self._result(id_, {"items": items, "next": self._token(round_)})
        self._round = None
        return self._result(id_, {"items": items, "cursor": None, "incremental": False})

    def _open_round(self) -> _Round:
        assert self._client is not None
        steps: list[_Issue] = []
        seen: set[str] = set()
        tokens: set[str] = set()
        page_token: str | None = None
        while True:
            page = self._client.search(self._jql, ["key", "updated"], page_token)
            issues = page.get("issues") if isinstance(page, dict) else None
            if not isinstance(issues, list):
                raise _bad_page()
            for issue in issues:
                if (
                    not isinstance(issue, dict)
                    or not isinstance(issue.get("id"), str)
                    or not isinstance(issue.get("key"), str)
                ):
                    raise _bad_page()
                if issue["id"] not in seen:  # 翻页期间工单变化可能导致重复，按 id 去重
                    seen.add(issue["id"])
                    steps.append(_Issue(issue["id"], issue["key"]))
            # 是否还有下一页以 nextPageToken 为准；isLast 若给出必须与之一致。宁可中止本轮也不在
            # 截断的枚举之后发出全量结束批——那会让运行器删掉没列出的文档（§6.8）
            token = page.get("nextPageToken")
            more = isinstance(token, str) and bool(token)
            last = page.get("isLast")
            if last is not None and last is not (not more):
                raise UnexpectedResponse("搜索响应的 isLast 与 nextPageToken 矛盾")
            if not more:
                break
            assert isinstance(token, str)
            if token in tokens:
                raise UnexpectedResponse("搜索响应重复给出同一个 nextPageToken")
            tokens.add(token)
            page_token = token
        self._rounds += 1
        return _Round(self._rounds, steps)

    # ------------------------------------------------------------------ 批次

    def _next_batch(self, round_: _Round, max_items: int, budget: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        total = 0
        while len(items) < max_items:
            room = budget - total - (1 if items else 0)
            if round_.pending is not None:
                payload = self._next_segment(round_, room)
                if payload is None:
                    if items:
                        break
                    uri = round_.pending[0]
                    round_.pending = None
                    payload = _error_item(uri, "content_invalid", "分段无法在单条预算内继续", False)
            elif round_.position >= len(round_.steps):
                break
            else:
                item = self._step_item(round_)
                payload = self._fit(round_, item, room, at_head=not items)
                if payload is None:
                    break  # 留到下一批（scanned 保留，不重取）
                round_.position += 1
                round_.scanned = None
            items.append(payload)
            total += _size(payload) + (1 if len(items) > 1 else 0)
        return items

    def _step_item(self, round_: _Round) -> dict[str, Any]:
        if round_.scanned is not None and round_.scanned[0] == round_.position:
            return round_.scanned[1]
        item = self._issue_item(round_.steps[round_.position])
        round_.scanned = (round_.position, item)
        return item

    def _issue_item(self, ref: _Issue) -> dict[str, Any]:
        """取一个工单的全文与全部评论并映射；不可恢复的失败中止本轮（``_RoundAborted``）。"""
        assert self._client is not None
        uri = issue_uri(self.prefix, ref.key)
        try:
            # 按 id 取（不随移动改变）；URI 用本轮搜索得到的 key
            issue = self._client.issue(ref.id, ISSUE_FIELDS)
            comments = self._all_comments(ref.id)
        except NotFound:
            return _error_item(uri, "source_unavailable", "工单在枚举之后被删除或移走", True)
        except JiraError as exc:
            raise _RoundAborted(f"读取工单失败：{exc}", exc.retryable) from None
        if not isinstance(issue, dict):
            return _error_item(uri, "content_invalid", "工单响应不是对象", False)
        try:
            return issue_document(uri, issue, comments)
        except MappingError as exc:
            return _error_item(uri, "content_invalid", f"工单内容无法映射：{exc}", False)

    def _all_comments(self, issue_id: str) -> list[dict[str, Any]]:
        assert self._client is not None
        comments: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self._client.comments(issue_id, start)
            batch = page.get("comments") if isinstance(page, dict) else None
            total = page.get("total") if isinstance(page, dict) else None
            if not isinstance(batch, list) or not isinstance(total, int):
                raise _RoundAborted("评论响应结构不符", retryable=False)
            if not all(isinstance(comment, dict) for comment in batch):
                raise _RoundAborted("评论响应结构不符", retryable=False)  # 不静默丢评论
            comments.extend(batch)
            start += len(batch)
            if not batch or start >= total:
                return comments

    def _fit(
        self, round_: _Round, item: dict[str, Any], room: int, at_head: bool
    ) -> dict[str, Any] | None:
        if _size(item) <= room:
            return item
        if item["kind"] != "document":
            return (
                None
                if not at_head
                else _error_item(
                    item.get("file_uri"), "content_invalid", "条目超过单条消息预算", False
                )
            )
        uri, pages = item["file_uri"], item["pages"]
        base = _size({"kind": "document", "file_uri": uri, "pages": [], "continued": True})
        if not at_head:
            return None
        if any(base + _size(page) > room - _SEGMENT_SLACK for page in pages):
            return _error_item(uri, "content_invalid", "单页超过单条消息预算，无法分段", False)
        first = {**item, "pages": [], "continued": True}
        count = self._fill(pages, room - _size(first))
        first["pages"] = pages[:count]
        round_.pending = (uri, pages[count:])
        return first

    def _next_segment(self, round_: _Round, room: int) -> dict[str, Any] | None:
        assert round_.pending is not None
        uri, pages = round_.pending
        base = _size({"kind": "document", "file_uri": uri, "pages": [], "continued": True})
        count = self._fill(pages, room - base)
        if count == 0:
            return None
        segment: dict[str, Any] = {"kind": "document", "file_uri": uri, "pages": pages[:count]}
        if count < len(pages):
            segment["continued"] = True
            round_.pending = (uri, pages[count:])
        else:
            round_.pending = None
        return segment

    @staticmethod
    def _fill(pages: list[dict[str, Any]], room: int) -> int:
        """从前往后装页，返回装下的页数（页之间的逗号计入）。"""
        count = used = 0
        for page in pages:
            size = _size(page) + (1 if count else 0)
            if used + size > room:
                break
            used += size
            count += 1
        return count


def _bad_page() -> JiraError:
    return UnexpectedResponse("搜索响应结构不符")
