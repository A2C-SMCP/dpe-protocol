"""线协议服务端（connector 契约 §6.2–§6.8）：sans-IO，一行请求进、一行响应出。

方法：``initialize``、``scan``（按批拉取）、``read_blob``（按偏移读快照字节）、``shutdown``；
``cancel`` 是通知。本示例**同步**处理请求：收到 ``cancel`` 时对应请求已经应答，因此不满足
§8.1-7「处理长请求期间继续处理 cancel」——单个请求的耗时随目录与文件大小增长（见 README）。

**一轮**（``_open_round``）：发现文件，按游标排好步骤——move → delete → 现存文件 → 读不了的
目录。增量轮里内容摘要与上一轮相同的文件不产出（每轮都读内容，见 ``source``）。

**删除与改名**（显式条目，§6.5）：上一轮有、本轮发现不到的文件 → ``delete``；与本轮新出现的
文件内容摘要相同且一一对应 → ``move``，随后紧跟目标的 ``document``（#178；多对多无法确定
配对，按 delete + 新建）。**发现不完整时不产出 delete / move**（读不了的目录，或读不出的
新文件）：被藏起来的可能正是改名目标；此时消失文件的记录原样带入新游标，摘要与某个消失
文件相同的新文件先以 error 推迟，推迟本身也报一条 error——这样的轮必有 error 条目，运行器
不推进游标（理由见 ``_open_round``）。一轮里对每个新文件的判断固定在轮首，同一轮内不重读。

**新游标不越过未产出的变更**（§6.8）：文件读失败、装不下转成 error、分段中途放弃时，保留它
上一轮的记录（没有就不记）。

**单条消息上限**（§6.2、§6.5）：批次按编码字节累计；一篇装不下就在页边界分段（``continued``），
单页都装不下即整篇 ``content_invalid``；结束批放不下游标时单独多发一批。
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from dpe_markdown_dir import PLUGIN_NAME, __version__
from dpe_markdown_dir.cursor import decode_cursor, encode_cursor, source_digest
from dpe_markdown_dir.source import (
    Built,
    ContentError,
    Found,
    SourceError,
    build_document,
    discover,
)

__all__ = ["PROTOCOL_VERSION", "Serve"]

PROTOCOL_VERSION = "dpe-connector/1"
#: 信封（jsonrpc / id / result / items 键、结束字段与行尾 LF）的预留字节；id 长度另扣
_ENVELOPE_RESERVE = 1024
_BATCH_MAX_ITEMS = 1000
#: 单批里跳过（未变）的文件数上限：单个 scan 请求的工作量有界（§6.4 允许空批）
_BATCH_MAX_SKIPPED = 256
#: ``next`` token：``<轮号>.<批号>``——轮号让上一轮的过期 token 不会碰巧与本轮的相等（§6.4）
_TOKEN = re.compile(r"[0-9]{1,18}\.[0-9]{1,18}")
#: 前缀须以 URI 分隔字符结尾：码点前缀匹配不识别段边界，否则拼接会越界（§6.5）
_PREFIX_END = frozenset("/?#")
#: RFC 3986 unreserved 与路径分隔符照原样进 URI，其余字节百分号编码
_KEEP = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def file_uri(prefix: str, identifier: bytes) -> str:
    """实例前缀 + 相对路径（百分号编码；非 UTF-8 的路径字节同样可表达）。"""
    return prefix + "".join(chr(b) if b in _KEEP else f"%{b:02X}" for b in identifier)


def _error_item(uri: str | None, code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "kind": "error",
        "file_uri": uri,
        "code": code,
        "message": message,
        "retryable": retryable,
    }


@dataclass(frozen=True)
class _Unreadable:
    """发现层读不了的目录（产出条目级 error）。"""

    entry: Found


#: 一轮里的一步：现成的条目（move / delete / error）、一个文件，或读不了的目录
_Step = dict[str, Any] | Found | _Unreadable


@dataclass
class _Round:
    number: int
    #: 按产出顺序的步骤
    steps: list[_Step]
    incremental: bool = False
    #: 现存文件用于比较的上一轮摘要（改名目标取其源的摘要）
    baseline: dict[bytes, str] = field(default_factory=dict)
    #: 新游标的记录：标识 → 摘要
    marks: dict[bytes, str] = field(default_factory=dict)
    #: 轮首读过的新文件：读出的文档，或读失败时的错误条目。一轮里对它的判断固定在轮首——
    #: 走到它那一步时直接复用，不重读（重读可能得到另一个结果，绕过本轮的推迟判定）
    prebuilt: dict[bytes, Built | dict[str, Any]] = field(default_factory=dict)
    #: 本轮 move 的目标：无论内容是否变化，都紧跟着产出目标的 document（#178）
    move_targets: set[bytes] = field(default_factory=set)
    #: 已记入新记录、但发送时还可能转成 error 的文档：file_uri → 标识
    tentative: dict[str, bytes] = field(default_factory=dict)
    position: int = 0
    #: 当前步骤已读出的条目（本批装不下留到下一批时复用，不重读）
    scanned: tuple[int, dict[str, Any] | None] | None = None
    #: 未发完的分段文档：(file_uri, 剩余页)
    pending: tuple[str, list[dict[str, Any]]] | None = None
    issued: int = 0

    def revert(self, uri: str) -> None:
        """这篇文档最终没能产出：恢复上一轮的记录，新游标不越过它（§6.8）。"""
        identifier = self.tentative.pop(uri, None)
        if identifier is None:
            return
        previous = self.baseline.get(identifier)
        if previous is None:
            self.marks.pop(identifier, None)
        else:
            self.marks[identifier] = previous


class Serve:
    def __init__(self) -> None:
        self.initialized = False
        self.shutdown_requested = False
        self.fatal = False
        self.max_message_bytes = 0
        self.prefix = ""
        self.root = ""
        self.include_hidden = False
        self.digest = ""
        self._round: _Round | None = None
        #: 本轮的 blob 快照：句柄 → 字节（下一轮轮首 scan 或 shutdown 时清空，§6.7）
        self._blobs: dict[str, bytes] = {}
        #: 句柄与轮的计数器：整个进程内递增、不随轮重置——旧句柄失效后不会被新的字节「复活」，
        #: 上一轮的 next token 也不会与本轮的相等
        self._handles = 0
        self._rounds = 0

    def _new_handle(self) -> str:
        self._handles += 1
        return str(self._handles)

    def _read_new(self, entry: Found) -> Built | dict[str, Any]:
        """读一个文件：成功给出文档，失败给出条目级 error（不可映射的内容不可重试）。"""
        try:
            return build_document(self.root, entry.path, self._new_handle, self.include_hidden)
        except ContentError as exc:
            return _error_item(
                file_uri(self.prefix, entry.identifier), "content_invalid", str(exc), False
            )
        except SourceError as exc:
            return _error_item(
                file_uri(self.prefix, entry.identifier), "source_unavailable", str(exc), True
            )

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
            self._blobs.clear()
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
        prefix, config = instance.get("uri_prefix"), instance.get("config")
        problem = self._configure(prefix, config)
        if problem is not None:
            self.fatal = True
            return self._error(id_, -32005, problem)
        assert isinstance(prefix, str) and isinstance(config, dict)
        self.digest = source_digest(prefix, config)
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
        """校验并取下配置；不合法返回原因（``-32005`` invalid_config，§6.3）。"""
        if not isinstance(prefix, str) or not prefix or prefix[-1] not in _PREFIX_END:
            return "实例前缀必须以 URI 分隔字符（/ ? #）结尾，才能与相对路径拼接"
        if not isinstance(config, dict) or set(config) - {"root", "include_hidden"}:
            return "配置只认 root 与 include_hidden"
        root, hidden = config.get("root"), config.get("include_hidden", False)
        if not isinstance(root, str) or not os.path.isdir(root):
            return "root 必须是存在的目录"
        if not isinstance(hidden, bool):
            return "include_hidden 必须是布尔值"
        self.prefix, self.root, self.include_hidden = prefix, root, hidden
        return None

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
        data = self._blobs.get(handle)
        if data is None:
            return self._error(id_, -32002, "未知或已失效的句柄")
        # base64 膨胀 4/3：单条响应不超过 max_message_bytes
        room = max(1, (self.max_message_bytes - _ENVELOPE_RESERVE - _size(id_)) * 3 // 4)
        chunk = data[offset : offset + min(limit, room)]
        return self._result(
            id_,
            {
                "data": base64.b64encode(chunk).decode("ascii"),
                "eof": offset + len(chunk) >= len(data),
            },
        )

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
            self._blobs.clear()  # 上一轮的句柄到此失效（§6.7）
            try:
                self._round = self._open_round(cursor)
            except SourceError as exc:
                return self._error(id_, -32003, str(exc), {"retryable": True})
        round_ = self._round
        assert round_ is not None
        max_items = params.get("max_items")
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items <= 0:
            max_items = _BATCH_MAX_ITEMS
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _size(id_)
        if budget < 256:
            return self._error(id_, -32602, "单条预算过小，无法产出")
        items, used = self._next_batch(round_, min(max_items, _BATCH_MAX_ITEMS), budget)
        if round_.pending is not None or round_.position < len(round_.steps):
            round_.issued += 1
            return self._result(id_, {"items": items, "next": self._token(round_)})
        cursor_value: str | None = encode_cursor(self.digest, round_.marks)
        if used + _size(cursor_value) > budget:
            if items:  # 放不下游标：本批以 next 结束，下一批单独给出
                round_.issued += 1
                return self._result(id_, {"items": items, "next": self._token(round_)})
            cursor_value = None  # 游标本身超过单条预算：本轮不提供游标
        self._round = None
        return self._result(
            id_, {"items": items, "cursor": cursor_value, "incremental": round_.incremental}
        )

    def _open_round(self, cursor: str | None) -> _Round:
        found, unreadable = discover(self.root, self.include_hidden)
        tail: list[_Step] = [*found]
        tail += [_Unreadable(entry) for entry in unreadable]
        previous = None if cursor is None else decode_cursor(cursor, self.digest)
        self._rounds += 1
        if previous is None:
            return _Round(self._rounds, tail)
        current = {entry.identifier for entry in found}
        round_ = _Round(
            self._rounds,
            [],
            incremental=True,
            baseline={key: digest for key, digest in previous.items() if key in current},
        )
        gone = sorted(key for key in previous if key not in current)
        if not gone:
            round_.steps = tail
            return round_
        appeared = [entry for entry in found if entry.identifier not in previous]
        digests: dict[bytes, str] = {}
        unknown = 0  # 读不出摘要的新文件数：它们可能正是改名目标
        for entry in appeared:
            outcome = self._read_new(entry)
            round_.prebuilt[entry.identifier] = outcome
            if isinstance(outcome, Built):
                digests[entry.identifier] = outcome.digest
            else:
                unknown += 1
        intents: list[_Step] = []
        held: dict[bytes, dict[str, Any]] = {}
        if unreadable or unknown:
            # 发现不完整（读不了的目录，或读不出的新文件——不分暂时还是内容问题：内容问题也会
            # 消失，比如文件写到一半、或编码被改回去）：被藏起来的可能正是改名目标，推迟
            # delete / move。不推迟会怎样：本轮先 delete 了源，而本轮因 error 不推进游标；
            # 下一轮以旧游标重判，目标读得出了、配成 move，源却已在 remote 上删掉——move
            # 永久失败（§7.2「源已不存在、目标未达成」，#178 落地前）。摘要与某个消失文件
            # 相同的新文件同样先不产出——否则它先被建出来，下一轮配成的 move 会撞上已存在的
            # 目标
            gone_digests = {previous[key] for key in gone}
            for entry in appeared:
                if digests.get(entry.identifier) in gone_digests:
                    round_.prebuilt.pop(entry.identifier, None)
                    held[entry.identifier] = _error_item(
                        file_uri(self.prefix, entry.identifier),
                        "source_unavailable",
                        "可能是改名后的文件，但本轮发现不完整，推迟到发现完整的一轮再判定",
                        True,
                    )
            round_.marks.update({key: previous[key] for key in gone})
            # 推迟本身可观测：报告里看得到删除与改名为什么没有发生
            intents.append(
                _error_item(
                    None,
                    "source_unavailable",
                    f"本轮发现不完整，{len(gone)} 个消失文件的删除或改名推迟到发现完整的一轮",
                    True,
                )
            )
        else:
            intents = self._intents(gone, previous, appeared, digests, round_)
        round_.steps = intents + [
            held[step.identifier] if isinstance(step, Found) and step.identifier in held else step
            for step in tail
        ]
        return round_

    def _intents(
        self,
        gone: list[bytes],
        previous: dict[bytes, str],
        appeared: list[Found],
        digests: dict[bytes, str],
        round_: _Round,
    ) -> list[_Step]:
        sources: dict[str, list[bytes]] = {}
        for key in gone:
            sources.setdefault(previous[key], []).append(key)
        targets: dict[str, list[Found]] = {}
        for entry in appeared:
            digest = digests.get(entry.identifier)
            if digest is not None:  # 读不出摘要的新文件不参与配对
                targets.setdefault(digest, []).append(entry)
        steps: list[_Step] = []
        moved: set[bytes] = set()
        for digest, from_keys in sources.items():
            to_entries = targets.get(digest, [])
            if len(from_keys) == 1 and len(to_entries) == 1:  # 一一对应才是改名
                source, target = from_keys[0], to_entries[0]
                steps.append(
                    {
                        "kind": "move",
                        "from_uri": file_uri(self.prefix, source),
                        "to_uri": file_uri(self.prefix, target.identifier),
                    }
                )
                moved.add(source)
                round_.baseline[target.identifier] = previous[source]
                round_.move_targets.add(target.identifier)
        for key in gone:
            if key not in moved:
                steps.append({"kind": "delete", "file_uri": file_uri(self.prefix, key)})
        return steps

    # ------------------------------------------------------------------ 批次

    def _next_batch(
        self, round_: _Round, max_items: int, budget: int
    ) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        total = skipped = 0
        while len(items) < max_items and skipped < _BATCH_MAX_SKIPPED:
            room = budget - total - (1 if items else 0)
            if round_.pending is not None:
                payload = self._next_segment(round_, room)
                if payload is None:
                    if items:
                        break
                    uri = round_.pending[0]
                    round_.pending = None
                    round_.revert(uri)
                    payload = _error_item(uri, "content_invalid", "分段无法在单条预算内继续", False)
            elif round_.position >= len(round_.steps):
                break
            else:
                item = self._step_item(round_)
                if item is None:
                    round_.position += 1
                    round_.scanned = None
                    skipped += 1
                    continue
                payload = self._fit(round_, item, room, at_head=not items)
                if payload is None:
                    break  # 留到下一批（scanned 保留，不重读）
                if item["kind"] == "document" and payload["kind"] == "error":
                    round_.revert(item["file_uri"])
                round_.position += 1
                round_.scanned = None
            items.append(payload)
            total += _size(payload) + (1 if len(items) > 1 else 0)
        return items, total

    def _step_item(self, round_: _Round) -> dict[str, Any] | None:
        if round_.scanned is not None and round_.scanned[0] == round_.position:
            return round_.scanned[1]
        step = round_.steps[round_.position]
        if isinstance(step, _Unreadable):
            item: dict[str, Any] | None = _error_item(
                file_uri(self.prefix, step.entry.identifier),
                "source_unavailable",
                "目录读取失败（发现层）",
                True,
            )
        elif isinstance(step, Found):
            item = self._file_item(round_, step)
        else:
            item = step
        round_.scanned = (round_.position, item)
        return item

    def _file_item(self, round_: _Round, entry: Found) -> dict[str, Any] | None:
        uri = file_uri(self.prefix, entry.identifier)
        previous = round_.baseline.get(entry.identifier)
        built = round_.prebuilt.pop(entry.identifier, None) or self._read_new(entry)
        if not isinstance(built, Built):
            if previous is not None:
                round_.marks[entry.identifier] = previous
            return built
        round_.marks[entry.identifier] = built.digest
        # move 的目标总是紧跟着产出 document（内容未变时运行器判为 unchanged，代价很小）：
        # 万一源已在早先一个未推进游标的轮里被删除，运行器据此把目标建出来，而不是让 move
        # 永久失败（#178）
        if built.digest == previous and entry.identifier not in round_.move_targets:
            return None  # 内容未变（如只是 touch）：不产出
        self._blobs.update(built.blobs)
        round_.tentative[uri] = entry.identifier
        return {
            "kind": "document",
            "file_uri": uri,
            "document": {"file_type": "md"},
            "pages": built.pages,
        }

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
        if any(base + _size(page) > room for page in pages) or not at_head:
            if not at_head:
                return None
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
