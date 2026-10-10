"""线协议服务端（connector 契约 §6.2–§6.4）：JSON-RPC 2.0 over stdio、NDJSON 分帧。

本模块与进程/流无关（sans-IO）：``Serve.handle(line)`` 吃一行（不含行尾）返回一行（不含行尾），
I/O 在 ``cli`` 里。协议要点：

- **握手先行**：``initialize`` 必须是第一条消息，此前任何其他请求一律 ``-32600``；身份自报
  （``plugin.name`` / ``plugin.version``）由运行器与清单核对；
- **全量枚举**：``capabilities.supports_cursor = false``——本包不产游标，结束批的 ``cursor`` 为
  ``null``、``incremental`` 为 ``false``（契约 §6.8：无游标即每轮全量）；
- **续批**：``next`` 是不透明的批次 token（§6.4），只有**最近回出的那个值**合法——收到
  token 即意味着上一批也已收到，直接续批；未知与过期（回退、重复、陌生）一律 ``-32602``，
  运行器中止本轮并以原 cursor 重开（恢复路径是稳定重跑整轮，§6.8）；
- **产出**：``scan`` 的 ``items`` 是 §6.5 的产出项；单条响应不超过 ``max_message_bytes``，
  一篇装不下的文档在**页边界**分段（同 ``file_uri`` 的各段连续出现：首段带 ``document``、
  续段只带 ``file_uri`` 与页，非末段 ``continued: true``）；
- **通知**：``cancel`` 是通知（无响应），本连接器在批次处理结束后应答 ``-32001``；v1 只有运行器
  发起请求，插件不主动发消息（§6.1）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dpe_git_connector import PLUGIN_NAME, __version__
from dpe_git_connector.gitrepo import GitError, GitRepo, RepoDir, discover_repos, looks_like_repo
from dpe_git_connector.mapping import DocumentMapper, Item, Limits, RepoSpec

__all__ = ["PROTOCOL_VERSION", "PROTOCOL_VERSIONS", "Serve"]

#: 本插件支持的线协议版本（§6.2）；与 DPE 协议版本、hash 契约版本相互独立
PROTOCOL_VERSIONS: tuple[str, ...] = ("dpe-connector/1",)
PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

#: 批次条目数的上界：超过它的批次即便再小也切分，避免超大文档挤占单条消息的处理时长
_BATCH_MAX_ITEMS = 1000

#: batch token 的合法形态：十进制数字（``str.isdigit`` 会放行全角/上标数字，**不能**用它）
_DECIMAL = re.compile(r"[0-9]+")

#: 错误消息里回显未知方法名的长度上限（消息不得因超长方法名而越限）
_METHOD_ECHO_LIMIT = 64

#: 响应信封与 result 包装的预留字节（``{"jsonrpc":"2.0","id":N,"result":{"items":[…]}}``
#: 加上行尾 LF 的实际开销是几十字节；留 1 KiB 使非法 id 长度也不至于挤爆上限）
_ENVELOPE_RESERVE = 1024


def _encoded_size(value: Any) -> int:
    """值的紧凑编码字节数（与运行时 ``_encode`` 的口径一致，比默认 json.dumps 少空格）。"""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _greedy_fit(sizes: list[int], room: int) -> int:
    """从前到后装页：返回装下的页数。``pages`` 数组内各页之间的逗号逐页计入（第 2 页起 +1）。"""
    count = 0
    total = 0
    for size in sizes:
        if total + size + (1 if count else 0) > room:
            break
        total += size + (1 if count else 0)
        count += 1
    return count


#: 服务端日志（stderr，见 cli）：stdout 只走协议
_log = logging.getLogger("dpe_git_connector")


def _encode(response: dict[str, Any]) -> str:
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _result(id_: Any, result: dict[str, Any]) -> str:
    return _encode({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_: Any, code: int, message: str, data: dict[str, Any] | None = None) -> str:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _encode({"jsonrpc": "2.0", "id": id_, "error": error})


@dataclass(frozen=True)
class _Source:
    """initialize 解析出的数据源：单仓库（``repo``）或仓库目录（``root``）+ 仓储规则。"""

    repo_path: str | None
    root_path: str | None
    spec: RepoSpec


@dataclass
class _Pending:
    """一篇尚未发完的分段文档（§6.5）：``file_uri``、剩余页与各页的编码尺寸。"""

    file_uri: str
    pages: list[dict[str, Any]]
    sizes: list[int]
    position: int = 0


@dataclass
class _Round:
    """一轮的枚举状态：仓库列表（按身份字节序）与批次位置。"""

    entries: list[RepoDir]
    #: 下一个待枚举的条目下标
    position: int = 0
    #: 未发完的分段文档（同一时刻至多一个，§6.5）
    pending: _Pending | None = None
    #: 已回出的 ``next`` 计数：token 不透明（§6.4），回出的值即批次序号；
    #: 只有最近回出的那个值合法（收到它意味着上一批也已收到），其余一律过期。
    issued: int = 0

    def next_token(self) -> str:
        self.issued += 1
        return str(self.issued)

    def exhausted(self) -> bool:
        return self.pending is None and self.position >= len(self.entries)


class Serve:
    """请求处理（sans-IO）。一行进、一行出；``shutdown`` 由调用方据此结束进程。"""

    def __init__(self) -> None:
        self.initialized = False
        self.shutdown_requested = False
        #: 握手失败后置位：此后一切请求一律回错、不产出内容（见 cli 的说明）
        self.fatal = False
        self.plugin_name = ""
        self.plugin_version = ""
        self.uri_prefix = ""
        self.max_message_bytes = 0
        self._source: _Source | None = None
        self._mapper: DocumentMapper | None = None
        self._round: _Round | None = None
        #: 最近一次被 cancel 指向的请求 id。本插件的处理是同步的（收到 cancel 时对应请求
        #: 必然已应答），因此**不能**真的中断在途工作；记下它只为对「同一 id 的请求不会再
        #: 出现」的情形如实回 -32001。只认最近一次：JSON-RPC 允许 id 复用，保留集合会让
        #: 之后的合法请求被误判为已取消（见 README 的已知限制）。
        self._cancel_last: int | None = None

    # ------------------------------------------------------------------ 入口

    def handle(self, line: str) -> str | None:
        """处理一行请求，返回一行响应；无法解析或为通知时返回 ``None``。"""
        if self.initialized and len(line.encode("utf-8")) + 1 > self.max_message_bytes:
            # §6.2：插件收到超限请求 MUST 以 -32600 拒绝并尽力丢弃该行（上限含行尾 LF）
            return _error(None, -32600, "请求超过 max_message_bytes")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # 分帧错误由运行器按协议级处理（§6.2），这里以 JSON-RPC 的标准码如实应答
            return _error(None, -32700, "解析错误")
        if not isinstance(message, dict):
            return _error(None, -32600, "请求必须是 JSON 对象")
        if message.get("jsonrpc") != "2.0":
            return _error(message.get("id"), -32600, '请求缺少 "jsonrpc": "2.0"')
        method = message.get("method")
        id_ = message.get("id")
        params = message.get("params", {})
        if method == "cancel":
            # 通知（无 id、无响应）：记下待取消的请求（§6.7）。本插件的处理是同步的，
            # 收到 cancel 时对应请求必然已应答；记录只为「不做错事」——见 _cancel_last 的说明。
            if isinstance(params, dict) and isinstance(params.get("id"), int):
                self._cancel_last = params["id"]
            return None
        if not isinstance(method, str) or not isinstance(params, dict):
            return _error(id_, -32600, "非法请求")
        if method == "shutdown" and self.initialized:
            self.shutdown_requested = True
            return _result(id_, {})
        if not self.initialized:
            if method != "initialize":
                # 握手先行（§6.3）：initialize 成功前不发任何内容
                return _error(id_, -32600, "握手完成前不接受其他请求")
            return self._initialize(id_, params)
        if self._cancel_last is not None and id_ == self._cancel_last:
            self._cancel_last = None
            return _error(id_, -32001, "请求已被取消")
        if method == "scan":
            return self._scan(id_, params)
        return _error(id_, -32601, f"未知方法：{method[:_METHOD_ECHO_LIMIT]}")

    # ------------------------------------------------------------------ initialize

    def _initialize(self, id_: Any, params: dict[str, Any]) -> str:
        versions = params.get("protocol_versions")
        if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
            return self._handshake_error(id_, -32602, "protocol_versions 必须是字符串数组")
        chosen = next((v for v in PROTOCOL_VERSIONS if v in versions), None)
        if chosen is None:
            return self._handshake_error(id_, -32004, "没有共同支持的线协议版本")
        max_bytes = params.get("max_message_bytes")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            return self._handshake_error(id_, -32602, "max_message_bytes 必须是正整数")
        instance = params.get("instance")
        if not isinstance(instance, dict):
            return self._handshake_error(id_, -32602, "instance 必须是对象")
        uri_prefix, config = instance.get("uri_prefix"), instance.get("config")
        if not isinstance(uri_prefix, str) or not isinstance(config, dict):
            return self._handshake_error(
                id_, -32602, "instance.uri_prefix / instance.config 缺失或类型不符"
            )
        try:
            _check_prefix(uri_prefix)
        except ValueError as exc:
            return self._handshake_error(id_, -32005, f"实例前缀不可用：{exc}")
        parsed = _parse_config(config)
        if isinstance(parsed, str):
            # schema 无法表达的语义约束在 initialize 拒绝（契约 §4.2、§6.3）
            return self._handshake_error(id_, -32005, parsed)
        try:
            self._validate_source(parsed)
        except GitError as exc:
            return self._handshake_error(id_, -32005, f"数据源不可用：{exc}")

        self.initialized = True
        self.plugin_name = PLUGIN_NAME
        self.plugin_version = __version__
        self.uri_prefix = uri_prefix
        self.max_message_bytes = max_bytes
        self._source = parsed
        self._mapper = DocumentMapper(uri_prefix, _limits(params.get("remote_limits")))
        _log.info(
            "initialize：repo=%s root=%s branches=%s since=%s prefix=%s",
            parsed.repo_path,
            parsed.root_path,
            ",".join(parsed.spec.branches) or "-",
            parsed.spec.since,
            uri_prefix,
        )
        return _result(
            id_,
            {
                "protocol_version": chosen,
                "plugin": {"name": self.plugin_name, "version": self.plugin_version},
                "capabilities": {"supports_cursor": False},
            },
        )

    def _validate_source(self, source: _Source) -> None:
        """initialize 的 eager 校验：配置问题就地拒绝（``invalid_config``），不等扫描暴露。

        - 单仓库模式：仓库可用、默认分支可确定、``since`` 可解析且是默认分支的祖先——都直接
          影响整篇文档的映射，早失败早诊断；空仓库合法（零页文档）。
        - ``root`` 模式：目录存在且**不是仓库本身**（root 是容器；指向仓库时其子模块目录会被
          误当成独立仓库）。root 下个别仓库不可用不在此判定：扫描时如实产出条目级错误。
        """
        if source.repo_path is not None:
            repo = GitRepo(source.repo_path)
            repo.open()
            if not repo.has_refs():
                if source.spec.since is not None:
                    raise GitError(f"since 无法解析为提交（仓库为空）：{source.spec.since}")
                return
            default_name = repo.default_branch(source.spec.default_branch)
            tip = repo.resolve_branch(default_name)
            if tip is None:
                raise GitError(f"默认分支 {default_name!r} 无法解析为提交")
            if source.spec.since is not None:
                since_sha = repo.resolve_ref(source.spec.since)
                if since_sha is None:
                    raise GitError(f"since 无法解析为提交：{source.spec.since}")
                if not repo.is_ancestor(since_sha, tip):
                    raise GitError(
                        f"since 不是默认分支 {default_name!r} 的祖先：{source.spec.since}"
                    )
            return
        assert source.root_path is not None
        root = Path(source.root_path)
        if not root.is_dir():
            raise GitError(f"root 不是目录：{source.root_path}")
        if looks_like_repo(root):
            raise GitError(
                "root 指向的是仓库本身：root 是容纳多仓库的目录（单仓库请用 repo 配置，"
                "或把 root 指向其父目录）"
            )

    def _handshake_error(self, id_: Any, code: int, message: str) -> str:
        """initialize 失败的应答：置致命标记，此后不再接受任何请求（§6.3、§7.1）。"""
        self.fatal = True
        return _error(id_, code, message)

    # ------------------------------------------------------------------ scan

    def _scan(self, id_: Any, params: dict[str, Any]) -> str:
        assert self._source is not None and self._mapper is not None
        if "next" in params and params["next"] is not None:
            if "cursor" in params:
                return _error(id_, -32602, "scan 不得同时带 cursor 与 next")
            token = params["next"]
            if not isinstance(token, str) or not _DECIMAL.fullmatch(token):
                return _error(id_, -32602, "未知的 next")
            # 只有最近回出的 token 合法（§6.4）：收到 token 即意味着上一批也已收到，直接续批
            # （含在途的分段文档）；其余值（回退、重复、陌生）一律过期拒绝，运行器中止本轮并
            # 以原 cursor 重开——恢复路径是稳定重跑整轮（§6.8），不回退重放。
            if self._round is None or int(token) != self._round.issued:
                return _error(id_, -32602, "未知或已过期的 next")
        else:
            cursor = params.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                return _error(id_, -32602, "cursor 必须是字符串或 null")
            if cursor is not None:
                # 本插件不支持游标（capabilities.supports_cursor = false）：如实回退为全量轮
                _log.warning("收到游标 %r 但本插件不支持游标，按全量枚举处理", cursor)
            try:
                self._round = self._open_round()
            except GitError as exc:
                return _error(id_, -32003, str(exc), {"retryable": True})
            self._cancel_last = None  # 跨轮残留的 cancel id 只会误伤未来的请求
        max_items = _optional_positive_int(params.get("max_items")) or _BATCH_MAX_ITEMS
        items = self._next_batch(max_items, id_)
        result: dict[str, Any] = {"items": items}
        assert self._round is not None
        if not self._round.exhausted():
            result["next"] = self._round.next_token()
        else:
            # 结束批：无游标（§6.8），且本轮不是基于游标的增量枚举（§6.4）
            result["cursor"] = None
            result["incremental"] = False
            self._round = None
        return _result(id_, result)

    def _open_round(self) -> _Round:
        """开始一轮：重新解析数据源（§6.3 只对「内容身份变化」强制重启插件，跨轮重启是 MAY）。

        单仓库模式的标识取仓库目录名（``file_uri`` 的仓库标识）；root 模式重新发现仓库集合
        （新仓库出现即出现新文档、消失即消失——delete / move 意图归后续拆分，见 #66）。
        """
        assert self._source is not None
        if self._source.repo_path is not None:
            name = os.path.basename(os.path.normpath(self._source.repo_path))
            entries = [RepoDir(path=self._source.repo_path, identifier=os.fsencode(name))]
        else:
            assert self._source.root_path is not None
            entries = discover_repos(self._source.root_path)
        return _Round(entries)

    def _next_batch(self, max_items: int, id_: Any) -> list[dict[str, Any]]:
        """取下一批产出项：条目数与**累计紧凑编码字节（含条目间逗号）**都不超预算（至少一条）。

        预算 = 消息上限 − ``_ENVELOPE_RESERVE`` − 响应里回显的 ``id`` 的编码长度：预留覆盖
        JSON-RPC 信封（``jsonrpc`` / ``result`` / ``items`` 键）、结束字段（``cursor`` /
        ``incremental``）与行尾 LF，id 长度另扣（它由运行器给出，可能很长）。批次内各条目
        之间的逗号在记账时就地计入（每条非首条 +1）。

        装不下的条目**不消费**（留到下一批从头枚举），保证批次推进与确定性；装不下的文档在
        **页边界**分段（§6.5），已在途的分段文档优先发完（各段连续）。
        """
        assert self._source is not None and self._mapper is not None and self._round is not None
        round_ = self._round
        # 信封预留之外再扣掉响应里回显的 id 自身的编码长度：id 由运行器给出，可能很长
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _encoded_size(id_)
        items: list[dict[str, Any]] = []
        total = 0
        while len(items) < max_items:
            if round_.pending is not None:
                payload = self._pending_segment(budget - total - (1 if items else 0))
                if payload is None:
                    # 装不下下一页：本批到此为止（pending 保留，下批继续）。批首预算足够时
                    # 不可能发生——分段前已校验每一页都能装进续段信封，故 items 必非空。
                    assert items
                    break
                size = _encoded_size(payload) + (1 if items else 0)
                items.append(payload)
                total += size
                continue
            if round_.position >= len(round_.entries):
                break
            entry = round_.entries[round_.position]
            item = self._mapper.document(GitRepo(entry.path), entry, self._source.spec)
            payload = self._fit_item(item, budget - total - (1 if items else 0), at_head=not items)
            if payload is None:
                assert items  # 批首必能推进一条（见 _fit_item），否则会空批死循环
                break
            size = _encoded_size(payload) + (1 if items else 0)
            items.append(payload)
            total += size
            round_.position += 1
        return items

    def _fit_item(self, item: Item, room: int, at_head: bool) -> dict[str, Any] | None:
        """把一条条目装进本批剩余空间：返回要发出的载荷，或 ``None``（本批不消费该条目）。

        ``at_head`` 表示本批还没有任何条目：此时必须推进——文档不可分段、error 也装不下时
        改以「超限」条目如实上报（§6.5），保证每批至少一条，不出现空批死循环。
        """
        assert self._round is not None
        if item.kind == "document":
            fields = item.fields
            whole = item.payload()
            if _encoded_size(whole) <= room:
                return whole
            first, pending = self._first_segment(item, room)
            if first is not None:
                self._round.pending = pending
                return first
            if not at_head:
                return None
            return self._oversize_document(fields["file_uri"], fields["pages"], room)
        payload = item.payload()
        if _encoded_size(payload) <= room:
            return payload
        if not at_head:
            return None
        # 批首仍装不下（error 条目的字段由本插件构造，正常极小）：以「条目超限」如实上报，
        # 保证推进；message 里报告实际数字，便于诊断
        return self._oversize_entry(item, room)

    def _first_segment(
        self, item: Item, room: int
    ) -> tuple[dict[str, Any] | None, _Pending | None]:
        """整篇装不下时的首段（带 ``document``）与剩余页；本批空间不足时返回 ``(None, None)``。

        可分段的前提（用**续段信封**——页能出现的最小信封——判定）：每一页都能单独装进本批
        剩余空间。某一页自身就装不进单条预算时整篇文档无法表达，由调用方转成条目级 ``error``。
        首段允许 0 页（只带 ``document`` 与 ``continued``）：该文档仍然按段推进，不会有页丢失。
        """
        fields = item.fields
        file_uri = fields["file_uri"]
        document = fields["document"]
        pages: list[dict[str, Any]] = fields["pages"]
        sizes = [_encoded_size(page) for page in pages]
        if not sizes:
            return None, None  # 防御：空文档必然装得下，走不到这里
        cont_base = _encoded_size(
            {"kind": "document", "file_uri": file_uri, "pages": [], "continued": True}
        )
        if cont_base + max(sizes) > room:
            return None, None
        first_base = _encoded_size(
            {
                "kind": "document",
                "file_uri": file_uri,
                "document": document,
                "pages": [],
                "continued": True,
            }
        )
        if first_base > room:
            return None, None
        count = _greedy_fit(sizes, room - first_base)
        first: dict[str, Any] = {
            "kind": "document",
            "file_uri": file_uri,
            "document": document,
            "pages": pages[:count],
            "continued": True,
        }
        pending = _Pending(file_uri=file_uri, pages=pages[count:], sizes=sizes[count:])
        return first, pending

    def _pending_segment(self, room: int) -> dict[str, Any] | None:
        """未完文档的下一段：装得下至少一页则返回该段（末段省略 ``continued``），否则 ``None``。"""
        assert self._round is not None and self._round.pending is not None
        pending = self._round.pending
        base = _encoded_size(
            {"kind": "document", "file_uri": pending.file_uri, "pages": [], "continued": True}
        )
        count = _greedy_fit(pending.sizes[pending.position :], room - base)
        if count == 0:
            return None
        start = pending.position
        pending.position += count
        segment: dict[str, Any] = {
            "kind": "document",
            "file_uri": pending.file_uri,
            "pages": pending.pages[start : start + count],
        }
        if pending.position < len(pending.pages):
            segment["continued"] = True
        else:
            self._round.pending = None
        return segment

    def _oversize_document(
        self, file_uri: str | None, pages: list[dict[str, Any]], room: int
    ) -> dict[str, Any]:
        """整篇不可分段时的条目级 ``content_invalid``：报告最大页与单条预算（§6.5）。"""
        assert self._mapper is not None
        largest = max((_encoded_size(page) for page in pages), default=0)
        message = (
            f"页紧凑编码后最大 {largest} 字节，超过单条预算 {room}"
            f"（消息上限 {self.max_message_bytes}），无法按页分段"
        )
        _log.warning("文档 %s：%s", file_uri, message)
        return self._mapper.error_item(
            file_uri, "content_invalid", message, retryable=False
        ).payload()

    def _oversize_entry(self, item: Item, room: int) -> dict[str, Any]:
        """error 条目本身装不进单条预算时的兜底上报（保证批次推进，§6.5 如实上报）。"""
        assert self._mapper is not None
        file_uri = item.fields.get("file_uri")
        message = f"条目紧凑编码后超过单条预算 {room}（消息上限 {self.max_message_bytes}）"
        _log.warning("条目 %s：%s", file_uri, message)
        return self._mapper.error_item(
            file_uri, "content_invalid", message, retryable=False
        ).payload()


#: 前缀结尾必须是的 URI 分隔字符：码点前缀匹配不识别段边界（契约 §6.5），前缀不带分隔符
#: 时 ``prefix + path`` 会改写 authority 或上一个段（``git://docs`` + ``a.md`` → ``git://docsamd.md``）
_PREFIX_END = frozenset("/?#")


def _check_prefix(prefix: str) -> None:
    """校验实例前缀的拼接前提：非空、以 URI 分隔字符结尾（契约 §6.3 只保证它是规范化不动点）。"""
    if not prefix or prefix[-1] not in _PREFIX_END:
        raise ValueError("实例前缀必须以 URI 分隔字符（/ ? #）结尾，才能与仓库标识拼接")


def _parse_config(config: dict[str, Any]) -> _Source | str:
    """实例配置 → 数据源与仓储规则；不合法返回错误消息（initialize 以 ``invalid_config`` 拒绝）。

    配置字段（清单 ``config_schema`` 是封闭 schema，运行器先校验；这里再防一手类型）：

    - ``repo`` / ``root``：二选一，单仓库路径或容纳多仓库的目录；
    - ``branches``：跟踪的分支名集合（缺省只有默认分支；默认分支本身不产页，由月页表达）；
    - ``default_branch``：显式默认分支名（缺省取仓库的 ``refs/remotes/origin/HEAD``）；
    - ``since``：历史起点提交（仅单仓库模式——一个提交 id 对多仓库没有意义）。
    """
    repo_path = config.get("repo")
    root_path = config.get("root")
    if repo_path is not None and (not isinstance(repo_path, str) or not repo_path):
        return "配置的 repo 必须是非空字符串"
    if root_path is not None and (not isinstance(root_path, str) or not root_path):
        return "配置的 root 必须是非空字符串"
    if (repo_path is None) == (root_path is None):
        return "配置必须且只能给出 repo（单仓库）或 root（多仓库目录）之一"
    branches_raw = config.get("branches", [])
    if not isinstance(branches_raw, list) or not all(
        isinstance(name, str) and name for name in branches_raw
    ):
        return "配置的 branches 必须是非空字符串数组"
    default_branch = config.get("default_branch")
    if default_branch is not None and (not isinstance(default_branch, str) or not default_branch):
        return "配置的 default_branch 必须是非空字符串"
    since = config.get("since")
    if since is not None and (not isinstance(since, str) or not since):
        return "配置的 since 必须是非空字符串"
    if root_path is not None and since is not None:
        return "since 只在单仓库模式（repo）可用：一个提交 id 对 root 下的多个仓库没有意义"
    # 去重保序：重复的分支名只会产出一一对应的页，静默去重不改变语义
    branches = tuple(dict.fromkeys(branches_raw))
    return _Source(
        repo_path=repo_path,
        root_path=root_path,
        spec=RepoSpec(default_branch=default_branch, branches=branches, since=since),
    )


def _limits(raw: Any) -> Limits:
    """``remote_limits`` → 切分守卫用的限额；不认识的结构按缺省（无限额）处理。"""
    if not isinstance(raw, dict):
        return Limits()
    value = raw.get("max_payload_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return Limits(max_payload_bytes=value)
    return Limits()


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None
