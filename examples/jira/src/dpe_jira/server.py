"""线协议服务端（connector 契约 §6.2–§6.8）：sans-IO，一行请求进、一行响应出。

方法：``initialize``、``scan``（按批拉取）、``read_blob``（按偏移读附件字节）、``shutdown``；
``cancel`` 是通知。请求同步处理（同 markdown-dir）。

**read_blob**（§6.7）：句柄是附件 id（附件内容按 id 不可变，天然是快照），``size`` 取附件元数据。
本轮发出过的句柄才登记（文档整篇或首段发出时），新的轮首 ``scan`` 或 ``shutdown`` 时全部失效
（``-32002``）。读取用 HTTP Range；服务端忽略 Range 时整份下载到临时文件再按偏移取，临时文件随
句柄一起删除。返回的字节总数必须等于声明的 ``size``：对不上即 ``-32003``（``retryable:
false``），不截断、不补齐。读取是同步的：服务端忽略 Range 时首次读取要整份下载，期间处理不了
``cancel``。

**一轮**（``supports_cursor: true``，§6.8）：轮首用增强搜索把范围内全部工单的 id、key、updated
枚举完（按 nextPageToken 翻页，按 id 升序）。**枚举是完整的才往下走**：任何一页失败、分页字段
矛盾或 token 重复，本轮在产出任何条目之前以 ``-32003`` 结束——截断的枚举会被当成大批删除，
因此结构上不允许它产出 delete / move。项目集合（配置给出的，或工单所属的；
``include_project_docs: false`` 时为空）也在轮首逐个取回，取不到同样整轮失败。之后：

- **全量轮**（没有游标或游标解释不了）：每个工单取全文与全部评论，映射为 document 条目
  （``mapping``）；结束批 ``incremental: false``；
- **增量轮**（与游标里的 id → [key, updated] 比较）：上一轮有、本轮没有的工单 → ``delete``
  （排在最前，按 id 升序）；其余按 id 升序：新工单或 updated 变了 → document；key 变了
  （换项目）→ ``move`` 旧 URI → 新 URI，紧跟新 URI 的 document（不论内容是否变化，§6.5）；
  都没变 → 跳过。项目排在工单之后：项目没有 updated，取回组件与版本后按映射出的内容摘要
  判断（游标里记 id → [key, 摘要]），变了才产出，改 key 同样表达为 ``move``，消失即 ``delete``。
  结束批 ``incremental: true``；
- **取全文失败**（枚举后被删、内容无法映射）→ 条目级 error；移动的目标取不到时连 ``move`` 也
  不发，只发目标 URI 的 error（不先改名再建不出内容）；
- **装不下的文档**：取全文时就按单条预算判断能否发出（带文档对象的首段头部装得下、每页都能
  单独成段，并为后续批次留余量；移动条目本身也要装得下），不能就当作取全文失败
  （``content_invalid``），同样不发 ``move``；
- **新游标**（``cursor``）：工单的新记录 [key, updated]（项目为 [key, 摘要]）在它的文档**最后一段
  发出时**才写入；
  跳过的工单沿用旧记录；任何失败（含发出途中改判为 error）沿用旧记录（新工单不记）；已删除
  的不记——新游标不越过没成功产出的变化（§6.8）。
- **换凭证账号**：游标的数据源摘要含 ``/myself`` 返回的账号 id，账号一变旧游标即解释不了，
  回退全量轮（不表达删除）——换成权限更小的账号不会把它看不到的工单当作删除。账号 id 是每轮的
  前置依赖：握手时没取到，每轮开头再取，取不到本轮以 ``-32003`` 失败（宁可不跑，也不用一个
  区分不了账号的摘要）；
- **游标与本轮自相矛盾**（两条记录指向同一个 key，或本轮某工单的 key 在上一轮属于另一个
  工单——Jira 不复用 key，正常不会发生）：不冒险，本轮按全量轮处理。结束批放不下游标时，
  本批以 ``next`` 结束、下一批单独给出游标；游标本身超过单条预算则给 ``null``（运行器保留
  旧游标，下一轮从旧位置重扫，§6.8）。

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

import atexit
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dpe_hash import RemoteUrlError, check_remote_url, jcs

from dpe_jira import PLUGIN_NAME, __version__
from dpe_jira.client import (
    JiraClient,
    JiraError,
    NotFound,
    RateLimited,
    SpoolFailed,
    Unavailable,
    UnexpectedResponse,
)
from dpe_jira.cursor import Marks, decode_cursor, encode_cursor, is_issue_id, source_digest
from dpe_jira.mapping import (
    ISSUE_FIELDS,
    MappingError,
    blob_handles,
    issue_document,
    project_document,
)

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


def project_uri(prefix: str, key: str) -> str:
    """实例前缀 + ``project/<项目 key>``（编码规则同 ``issue_uri``）。"""
    return f"{prefix}project/{_encode_key(key)}"


def _content_digest(item: dict[str, Any]) -> str:
    """项目文档的内容摘要（不含 ``file_uri``：改 key 不改内容），用来判断项目是否变化。"""
    body = {"document": item["document"], "pages": item["pages"]}
    return hashlib.sha256(jcs(body).encode("utf-8")).hexdigest()[:32]


def _contradicts(previous: dict[_Ref, tuple[str, str]], now: list[tuple[str, str, str]]) -> bool:
    """游标与本轮自相矛盾：同类里两条记录指向同一个 key，或本轮某个 key 上一轮属于另一个 id。"""
    owners: dict[tuple[str, str], str] = {}
    for (kind, entry_id), (key, _) in previous.items():
        if owners.setdefault((kind, key), entry_id) != entry_id:
            return True
    return any(owners.get((kind, key), entry_id) != entry_id for kind, entry_id, key in now)


def issue_uri(prefix: str, key: str) -> str:
    """实例前缀 + ``issue/<工单 key>``（key 按 RFC 3986 编码；正常的 key 本身就是安全字符）。"""
    return f"{prefix}issue/{_encode_key(key)}"


def _encode_key(key: str) -> str:
    return "".join(
        ch if ch in _KEEP else "".join(f"%{b:02X}" for b in ch.encode("utf-8")) for ch in key
    )


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
    """搜索得到的一个工单：数字 id、key 与 updated（原样字符串，只比较相等）。"""

    id: str
    key: str
    updated: str


@dataclass(frozen=True)
class _Project:
    """轮首取到的一个项目：数字 id、key 与项目本身的响应（名称、描述）。"""

    id: str
    key: str
    raw: dict[str, Any]


#: 新游标记录的键：（"issue" | "project", id）——工单与项目的 id 各成一个空间
_Ref = tuple[str, str]


@dataclass(frozen=True)
class _Step:
    """一轮中的一步：删除一篇消失的文档（``delete_uri``），取一个工单的全文（``issue``），或取
    一个项目的组件与版本（``project``）；``moved_from`` 为移动前的 key。"""

    issue: _Issue | None = None
    project: _Project | None = None
    moved_from: str | None = None
    delete_uri: str | None = None


@dataclass
class _Out:
    """待发出的条目；``mark`` 是它完整发出后要写进新游标的记录（工单与项目的 document 带）。"""

    item: dict[str, Any]
    ref: _Ref | None = None
    mark: tuple[str, str] | None = None
    #: 文档里的 blob 句柄 → size：条目（整篇或首段）发出时才登记给 ``read_blob``
    handles: dict[str, int] = field(default_factory=dict)


@dataclass
class _Round:
    number: int
    steps: list[_Step]
    incremental: bool
    #: 上一轮的记录（增量轮）；取全文失败时沿用
    previous: dict[_Ref, tuple[str, str]]
    #: 新游标的记录：先放跳过的工单与项目，文档完整发出时补上
    marks: dict[_Ref, tuple[str, str]]
    position: int = 0
    #: 已产出、尚未发出的条目（本批装不下的留到下一批，不重取）
    queue: list[_Out] = field(default_factory=list)
    #: 未发完的分段文档：(file_uri, 剩余页)，以及末段发出后要写入的记录
    pending: tuple[str, list[dict[str, Any]]] | None = None
    pending_out: _Out | None = None
    #: 结束批算出的游标（放不下而多发一批时复用）
    cursor: str | None = None
    issued: int = 0

    def commit(self, out: _Out) -> None:
        if out.ref is not None and out.mark is not None:
            self.marks[out.ref] = out.mark

    def revert(self, ref: _Ref | None) -> None:
        """这篇文档本轮没有成功产出：新游标沿用上一轮的记录（新的不记）。"""
        if ref is None:
            return
        if ref in self.previous:
            self.marks[ref] = self.previous[ref]
        else:
            self.marks.pop(ref, None)

    def table(self, kind: str) -> Marks:
        """新游标里一张表（工单或项目）的记录。"""
        return {entry_id: mark for (k, entry_id), mark in self.marks.items() if k == kind}

    def exhausted(self) -> bool:
        return self.pending is None and not self.queue and self.position >= len(self.steps)


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
        self._config: dict[str, Any] = {}
        self._projects: list[str] | None = None  # 配置给出的项目；None 即按工单所属项目
        self._include_projects = True
        self._account: str | None = None
        self._source = ""
        self._round: _Round | None = None
        self._rounds = 0
        #: 本轮产出过的 blob 句柄（附件 id）→ 声明的 size
        self._handles: dict[str, int] = {}
        #: 服务端忽略 Range 时整份下载的附件：句柄 → 临时文件
        self._spooled: dict[str, str] = {}
        self._spool_dir: str | None = None

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
            self._drop_handles()
            return _line({"jsonrpc": "2.0", "id": id_, "result": {}})
        if method == "scan":
            return self._scan(id_, params)
        if method == "read_blob":
            return self._read_blob(id_, params)
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
                "capabilities": {"supports_cursor": True},
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
        account: str | None = None
        # 各自探测：一项暂时失败不让另一项的配置错误漏过；暂时性失败不是配置问题，留给 scan 报告
        try:
            account = _account_id(client.myself(attempts=1))
        except (Unavailable, RateLimited):
            pass
        except JiraError as exc:
            return f"站点、凭证或范围不被 Jira 接受：{exc}"
        try:
            client.search(jql, ["key"], None, max_results=1, attempts=1)
        except (Unavailable, RateLimited):
            pass
        except JiraError as exc:
            return f"站点、凭证或范围不被 Jira 接受：{exc}"
        self.prefix = prefix
        self._client = client
        self._jql = jql
        self._config = dict(config)
        self._projects = sorted(set(projects)) if projects is not None else None
        self._include_projects = include
        self._account = account  # 探测暂时失败为 None：每轮开头再取（取不到本轮失败）
        return None

    def _source_digest(self) -> str:
        """游标的数据源摘要：前缀 + 配置 + 凭证对应的账号 id。

        账号 id 是标识而非凭证值。纳入它是为了换账号时旧游标作废、回退全量轮（全量轮不表达
        删除）——否则换成权限更小的账号后，看不到的工单会在增量轮里被当作删除。
        """
        if self._account is None:
            assert self._client is not None
            try:
                self._account = _account_id(self._client.myself())
            except JiraError as exc:
                raise type(exc)(f"确认凭证账号失败：{exc}") from None
        return source_digest(self.prefix, self._config, self._account)

    # ------------------------------------------------------------------ read_blob

    def _read_blob(self, id_: Any, params: dict[str, Any]) -> str:
        handle, offset, limit = (
            params.get("handle"),
            params.get("offset", 0),
            params.get("max_bytes"),
        )
        if (
            not isinstance(handle, str)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return self._error(id_, -32602, "read_blob 参数非法")
        size = self._handles.get(handle)
        if size is None:
            return self._error(id_, -32002, "未知或已失效的句柄")
        # base64 膨胀 4/3：单条响应不超过 max_message_bytes
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _size(id_)
        if budget < 256:
            return self._error(id_, -32602, "单条预算过小，无法产出")
        room = budget * 3 // 4
        want = min(limit, room, size - offset)
        if want <= 0:  # offset 到达或超过末尾：空块 + eof，不发请求
            return self._result(id_, {"data": "", "eof": True})
        try:
            chunk = self._read_range(handle, size, offset, want)
        except JiraError as exc:
            return self._error(id_, -32003, f"读取附件失败：{exc}", {"retryable": exc.retryable})
        return self._result(
            id_,
            {"data": base64.b64encode(chunk).decode("ascii"), "eof": offset + len(chunk) >= size},
        )

    def _read_range(self, handle: str, size: int, offset: int, want: int) -> bytes:
        """读附件 ``[offset, offset + want)`` 中开头的一段（至少 1 字节）；长度对不上即抛异常。"""
        assert self._client is not None
        spooled = self._spooled.get(handle)
        if spooled is not None:
            try:
                return _read_file(spooled, offset, want)
            except FileNotFoundError:
                del self._spooled[handle]  # 轮内被系统清理掉：重新下载
        paths: list[str] = []

        def spool() -> str:
            paths.append(os.path.join(self._spool(), f"{handle}.bin"))
            return paths[-1]

        try:
            fetched = self._client.attachment_range(handle, offset, want, spool, size)
        except JiraError:
            for path in paths:  # 整份下载中途失败：不留半截文件
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
            raise
        if fetched.partial:
            if fetched.total != size:
                raise _size_mismatch()
            if fetched.first != offset:
                raise UnexpectedResponse("附件 Range 响应的起点不符")
            return fetched.data
        [path] = paths
        if fetched.written != size:
            os.unlink(path)
            raise _size_mismatch()
        self._spooled[handle] = path
        return _read_file(path, offset, want)

    def _spool(self) -> str:
        """临时目录（第一次整份下载时才创建）。"""
        if self._spool_dir is None:
            try:
                self._spool_dir = tempfile.mkdtemp(prefix="dpe-jira-")
            except OSError as exc:
                raise SpoolFailed(f"无法创建临时目录（{type(exc).__name__}）") from None
            atexit.register(shutil.rmtree, self._spool_dir, True)
        return self._spool_dir

    def _drop_handles(self) -> None:
        """句柄失效：清空登记，清空临时目录（含没登记上的半截文件）。"""
        self._handles.clear()
        self._spooled.clear()
        if self._spool_dir is not None:
            for name in os.listdir(self._spool_dir):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self._spool_dir, name))

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
            self._drop_handles()  # 上一轮的句柄到此失效（§6.7）
            try:
                self._round = self._open_round(cursor)
            except JiraError as exc:
                self._round = None
                return self._error(id_, -32003, f"枚举失败：{exc}", {"retryable": exc.retryable})
        round_ = self._round
        assert round_ is not None
        max_items = params.get("max_items")
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items <= 0:
            max_items = _BATCH_MAX_ITEMS
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _size(id_)
        if budget < 256:
            return self._error(id_, -32602, "单条预算过小，无法产出")
        try:
            items, used = self._next_batch(round_, min(max_items, _BATCH_MAX_ITEMS), budget)
        except _RoundAborted as exc:
            self._round = None
            return self._error(id_, -32003, str(exc), {"retryable": exc.retryable})
        if not round_.exhausted():
            round_.issued += 1
            return self._result(id_, {"items": items, "next": self._token(round_)})
        # 结束批：给出新游标（§6.8）。信封预留只覆盖 "cursor":null，游标串的字节另计
        if round_.cursor is None:
            round_.cursor = encode_cursor(
                self._source, round_.table("issue"), round_.table("project")
            )
        cursor_value: str | None = round_.cursor
        if used + _size(cursor_value) > budget:
            if items:
                # 本批放不下游标：以 next 结束本批，下一批单独给出空 items 与游标
                round_.issued += 1
                return self._result(id_, {"items": items, "next": self._token(round_)})
            # 游标本身超过单条预算：本轮不提供游标（不发超限消息，§6.5）。运行器保留已缓存的
            # 游标，下一轮从旧位置重扫（§6.8）——范围不缩小就会一直如此，需要调大单条上限
            cursor_value = None
        self._round = None
        return self._result(
            id_, {"items": items, "cursor": cursor_value, "incremental": round_.incremental}
        )

    def _open_round(self, cursor: str | None) -> _Round:
        self._source = self._source_digest()
        current = self._enumerate()
        projects = self._fetch_projects(current)
        decoded = None if cursor is None else decode_cursor(cursor, self._source)
        self._rounds += 1
        full = [_Step(issue=issue) for issue in current] + [
            _Step(project=project) for project in projects
        ]
        if decoded is None:  # 全量轮：没有游标，或游标解释不了（§6.8 回退，不报错）
            return _Round(self._rounds, full, incremental=False, previous={}, marks={})
        previous: dict[_Ref, tuple[str, str]] = {
            **{("issue", k): v for k, v in decoded[0].items()},
            **{("project", k): v for k, v in decoded[1].items()},
        }
        now: list[tuple[str, str, str]] = [("issue", i.id, i.key) for i in current]
        now += [("project", p.id, p.key) for p in projects]
        if _contradicts(previous, now):
            # 游标与本轮自相矛盾（见模块说明）：不冒险删除，按全量轮处理
            return _Round(self._rounds, full, incremental=False, previous={}, marks={})
        alive = {(kind, entry_id) for kind, entry_id, _ in now}
        steps = [
            _Step(delete_uri=self._uri(kind, previous[(kind, entry_id)][0]))
            for kind in ("issue", "project")
            for entry_id in sorted((i for k, i in previous if k == kind), key=int)
            if (kind, entry_id) not in alive
        ]
        marks: dict[_Ref, tuple[str, str]] = {}
        for issue in current:
            old = previous.get(("issue", issue.id))
            if old is None or (old[0] == issue.key and old[1] != issue.updated):
                steps.append(_Step(issue=issue))
            elif old[0] != issue.key:
                steps.append(_Step(issue=issue, moved_from=old[0]))
            else:
                marks[("issue", issue.id)] = old  # 没变：跳过
        for project in projects:  # 项目没有 updated：取数后按内容摘要判断（``_run_step``）
            old = previous.get(("project", project.id))
            moved = old[0] if old is not None and old[0] != project.key else None
            steps.append(_Step(project=project, moved_from=moved))
        return _Round(self._rounds, steps, incremental=True, previous=previous, marks=marks)

    def _uri(self, kind: str, key: str) -> str:
        return issue_uri(self.prefix, key) if kind == "issue" else project_uri(self.prefix, key)

    def _fetch_projects(self, issues: list[_Issue]) -> list[_Project]:
        """本轮的项目集合（配置给出的，或工单所属的），按 key 排序取回项目本身。

        这一步属于枚举：取不到就整轮失败、不产出任何条目——项目集合不完整时同样不能删除。
        """
        if not self._include_projects:
            return []
        assert self._client is not None
        keys = (
            self._projects
            if self._projects is not None
            else sorted({issue.key.rsplit("-", 1)[0] for issue in issues if "-" in issue.key})
        )
        projects: list[_Project] = []
        seen: set[str] = set()
        for key in keys:
            try:
                raw = self._client.project(key)
            except NotFound:
                if self._projects is not None:  # 配置里的项目读不到：改配置之前重试无用
                    raise NotFound(f"配置的项目 {key} 不存在或不可读") from None
                raise Unavailable(f"项目 {key} 在枚举期间不可读") from None  # 竞态：可重试
            project_id = raw.get("id") if isinstance(raw, dict) else None
            actual = raw.get("key") if isinstance(raw, dict) else None
            if not isinstance(project_id, str) or not is_issue_id(project_id):
                raise UnexpectedResponse("项目响应缺少数字 id")
            if not isinstance(actual, str) or not actual:
                raise UnexpectedResponse("项目响应缺少 key")
            assert isinstance(raw, dict)
            if project_id not in seen:  # 旧 key 会被 Jira 重定向到同一个项目：按 id 去重
                seen.add(project_id)
                projects.append(_Project(project_id, actual, raw))
        return projects

    def _enumerate(self) -> list[_Issue]:
        """枚举范围内全部工单（按 id 升序）；不完整就抛异常，绝不返回截断的结果。"""
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
                fields = issue.get("fields") if isinstance(issue, dict) else None
                updated = fields.get("updated") if isinstance(fields, dict) else None
                if (
                    not isinstance(issue, dict)
                    or not isinstance(issue.get("id"), str)
                    or not is_issue_id(issue["id"])
                    or not isinstance(issue.get("key"), str)
                    or not issue["key"]
                    or not isinstance(updated, str)
                    or not updated
                ):
                    raise _bad_page()
                if issue["id"] not in seen:  # 翻页期间工单变化可能导致重复，按 id 去重
                    seen.add(issue["id"])
                    steps.append(_Issue(issue["id"], issue["key"], updated))
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
        return steps

    # ------------------------------------------------------------------ 批次

    def _next_batch(
        self, round_: _Round, max_items: int, budget: int
    ) -> tuple[list[dict[str, Any]], int]:
        """装一批，返回（条目, 已用字节）。"""
        items: list[dict[str, Any]] = []
        total = 0
        while len(items) < max_items:
            room = budget - total - (1 if items else 0)
            if round_.pending is not None:
                assert round_.pending_out is not None
                payload = self._next_segment(round_, room)
                if payload is None:
                    if items:
                        break
                    # 取全文时已按预算筛过，正常到不了这里；万一到了，如实上报并沿用旧记录
                    uri = round_.pending[0]
                    round_.pending = None
                    round_.revert(round_.pending_out.ref)
                    round_.pending_out = None
                    payload = _error_item(uri, "content_invalid", "分段无法在单条预算内继续", False)
                elif round_.pending is None:  # 末段：文档完整发出
                    round_.commit(round_.pending_out)
                    round_.pending_out = None
            else:
                if not round_.queue:
                    if round_.position >= len(round_.steps):
                        break
                    step = round_.steps[round_.position]
                    round_.queue = self._run_step(round_, step, budget)
                    round_.position += 1
                    continue
                out = round_.queue[0]
                payload = self._fit(round_, out.item, room, at_head=not items)
                if payload is None:
                    break  # 留到下一批（已取得的条目留在队列里，不重取）
                round_.queue.pop(0)
                if payload is out.item:
                    round_.commit(out)
                    self._handles.update(out.handles)
                elif round_.pending is not None:
                    round_.pending_out = out  # 首段：末段发出时再写入记录
                    self._handles.update(out.handles)
                else:
                    round_.revert(out.ref)  # 改判为 error：没发出，句柄不登记
            items.append(payload)
            total += _size(payload) + (1 if len(items) > 1 else 0)
        return items, total

    def _run_step(self, round_: _Round, step: _Step, budget: int) -> list[_Out]:
        """执行一步，返回它产出的待发条目。新游标的记录在文档完整发出时才写入。"""
        if step.delete_uri is not None:
            return [_Out({"kind": "delete", "file_uri": step.delete_uri})]
        if step.project is not None:
            return self._project_step(round_, step, budget)
        issue = step.issue
        assert issue is not None
        item = self._issue_item(issue)
        ref: _Ref = ("issue", issue.id)
        return self._emit(round_, ref, (issue.key, issue.updated), item, step.moved_from, budget)

    def _project_step(self, round_: _Round, step: _Step, budget: int) -> list[_Out]:
        project = step.project
        assert project is not None and self._client is not None
        ref: _Ref = ("project", project.id)
        uri = project_uri(self.prefix, project.key)
        try:
            components = self._client.components(project.key)
            versions = self._client.versions(project.key)
        except NotFound:
            item = _error_item(uri, "source_unavailable", "项目在枚举之后不可读", True)
            round_.revert(ref)
            return [_Out(item)]
        except JiraError as exc:
            raise _RoundAborted(f"读取项目失败：{exc}", exc.retryable) from None
        try:
            item = project_document(uri, project.raw, components, versions)
        except MappingError as exc:
            round_.revert(ref)
            return [_Out(_error_item(uri, "content_invalid", f"项目内容无法映射：{exc}", False))]
        digest = _content_digest(item)
        old = round_.previous.get(ref)
        if step.moved_from is None and old is not None and old[1] == digest:
            round_.marks[ref] = old  # 内容没变：跳过
            return []
        return self._emit(round_, ref, (project.key, digest), item, step.moved_from, budget)

    def _emit(
        self,
        round_: _Round,
        ref: _Ref,
        mark: tuple[str, str],
        item: dict[str, Any],
        moved_from: str | None,
        budget: int,
    ) -> list[_Out]:
        """把取到的条目（document 或 error）排进待发队列，必要时在前面加 move。"""
        step_kind = ref[0]
        move: dict[str, Any] | None = None
        if moved_from is not None:
            move = {
                "kind": "move",
                "from_uri": self._uri(step_kind, moved_from),
                "to_uri": item["file_uri"],
            }
        limit = budget - 2 * _SEGMENT_SLACK
        if item["kind"] == "document" and not self._expressible(item, limit):
            item = _error_item(
                item["file_uri"], "content_invalid", "文档超过单条消息预算，无法分段", False
            )
        elif move is not None and _size(move) > limit:
            item = _error_item(
                item["file_uri"], "content_invalid", "移动条目超过单条消息预算", False
            )
        if item["kind"] != "document":  # 失败：沿用旧记录，下一轮重新尝试（含移动）
            round_.revert(ref)
            return [_Out(item)]
        document = _Out(item, ref, mark, blob_handles(item))
        return [document] if move is None else [_Out(move), document]

    @staticmethod
    def _expressible(item: dict[str, Any], limit: int) -> bool:
        """文档能否在单条预算内发出：带文档对象的首段头部装得下，且每页都能单独成段。

        ``limit`` 已扣两份余量：后续批次的预算会因请求 id 变长略小，而 ``_fit`` 分段时自己还要
        扣一份——这样通过这里的文档到了批首一定装得下或分得开，``move`` 不会落单。
        """
        head = _size({**item, "pages": [], "continued": True})
        base = _size(
            {"kind": "document", "file_uri": item["file_uri"], "pages": [], "continued": True}
        )
        return head <= limit and all(base + _size(page) <= limit for page in item["pages"])

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
        if _size(first) > room - _SEGMENT_SLACK:
            return _error_item(uri, "content_invalid", "文档头部超过单条消息预算", False)
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


def _account_id(me: Any) -> str:
    """``/myself`` 响应里的账号 id；没有即响应不符协议。"""
    account = me.get("accountId") if isinstance(me, dict) else None
    if not isinstance(account, str) or not account:
        raise UnexpectedResponse("/myself 响应缺少 accountId")
    return account


def _read_file(path: str, offset: int, want: int) -> bytes:
    """从临时文件按偏移读；文件不见了原样抛 ``FileNotFoundError``（调用方重新下载），其余 I/O
    错误归为 ``SpoolFailed``。"""
    try:
        with open(path, "rb") as source:
            source.seek(offset)
            return source.read(want)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SpoolFailed(f"读临时文件失败（{type(exc).__name__}）") from None


def _size_mismatch() -> JiraError:
    return UnexpectedResponse("附件字节长度与元数据里的 size 不符")


def _bad_page() -> JiraError:
    return UnexpectedResponse("搜索响应结构不符")
