"""线协议服务端（connector 契约 §6.2–§6.4）：JSON-RPC 2.0 over stdio、NDJSON 分帧。

本模块与进程/流无关（sans-IO）：``Serve.handle(line)`` 吃一行（不含行尾）返回一行（不含行尾），
I/O 在 ``cli`` 里。协议要点：

- **握手先行**：``initialize`` 必须是第一条消息，此前任何其他请求一律 ``-32600``；身份自报
  （``plugin.name`` / ``plugin.version``）由运行器与清单核对；
- **全量枚举**：``capabilities.supports_cursor = false``——本包不产游标，结束批的 ``cursor`` 为
  ``null``、``incremental`` 为 ``false``（契约 §6.8：无游标即每轮全量）；
- **续批**：``next`` 是指向本次轮文件列表的偏移；未知或过期的 token 以 ``-32602`` 拒绝，运行器
  会以原 cursor 重开（§6.4）；
- **产出**：``scan`` 的 ``items`` 是 §6.5 的产出项；单条响应不超过 ``max_message_bytes``，批次
  条目数按序列化后的大小自行控制；
- **通知**：``cancel`` 是通知（无响应），本连接器在批次处理结束后应答 ``-32001``；v1 只有运行器
  发起请求，插件不主动发消息（§6.1）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from dpe_git_connector import PLUGIN_NAME, __version__
from dpe_git_connector.files import file_type_of
from dpe_git_connector.gitrepo import FileEntry, GitError, GitRepo
from dpe_git_connector.mapping import DocumentMapper, Limits

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


@dataclass
class _Round:
    """一轮的枚举状态：文件列表（按路径字节序）与下一批的偏移。"""

    entries: list[FileEntry]
    position: int = 0


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
        self._repo: GitRepo | None = None
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
            # 收到 cancel 时对应请求必然已应答；记录只为「不做错事」——见 _cancelled 的说明。
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
        repo_path = config.get("repo")
        ref = config.get("ref", "HEAD")
        if not isinstance(repo_path, str) or not repo_path:
            return self._handshake_error(id_, -32005, "配置缺少 repo")
        if not isinstance(ref, str) or not ref:
            return self._handshake_error(id_, -32005, "配置的 ref 必须是非空字符串")
        try:
            _check_prefix(uri_prefix)
        except ValueError as exc:
            return self._handshake_error(id_, -32005, f"实例前缀不可用：{exc}")
        repo = GitRepo(repo_path, ref)
        try:
            repo.open()
        except GitError as exc:
            # schema 无法表达的语义约束在 initialize 拒绝（契约 §4.2、§6.3）
            return self._handshake_error(id_, -32005, f"仓库或 ref 不可用：{exc}")

        self.initialized = True
        self.plugin_name = PLUGIN_NAME
        self.plugin_version = __version__
        self.uri_prefix = uri_prefix
        self.max_message_bytes = max_bytes
        self._repo = repo
        self._mapper = DocumentMapper(uri_prefix, _limits(params.get("remote_limits")))
        _log.info(
            "initialize：repo=%s ref=%s commit=%s prefix=%s",
            repo.repo,
            repo.ref,
            repo.commit,
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

    def _handshake_error(self, id_: Any, code: int, message: str) -> str:
        """initialize 失败的应答：置致命标记，此后不再接受任何请求（§6.3、§7.1）。"""
        self.fatal = True
        return _error(id_, code, message)

    # ------------------------------------------------------------------ scan

    def _scan(self, id_: Any, params: dict[str, Any]) -> str:
        assert self._repo is not None and self._mapper is not None
        if "next" in params and params["next"] is not None:
            if "cursor" in params:
                return _error(id_, -32602, "scan 不得同时带 cursor 与 next")
            token = params["next"]
            if not isinstance(token, str) or not _DECIMAL.fullmatch(token):
                return _error(id_, -32602, "未知的 next")
            if self._round is None or int(token) > len(self._round.entries):
                # 未知或过期的 token：运行器以原 cursor 重开（§6.4）
                return _error(id_, -32602, "未知或已过期的 next")
            self._round.position = int(token)
        else:
            cursor = params.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                return _error(id_, -32602, "cursor 必须是字符串或 null")
            if cursor is not None:
                # 本插件不支持游标（capabilities.supports_cursor = false）：如实回退为全量轮
                _log.warning("收到游标 %r 但本插件不支持游标，按全量枚举处理", cursor)
            try:
                # 轮首重新解析 ref（§6.3 只对「内容身份变化」强制重启插件，跨轮重启是 MAY）：
                # 进程被宿主跨轮复用时，分支前进必须能反映到下一轮，否则永远枚举旧提交
                self._repo.resolve()
                entries = self._repo.list_blobs()
            except GitError as exc:
                return _error(id_, -32003, str(exc), {"retryable": True})
            self._round = _Round(entries)
            self._cancel_last = None  # 跨轮残留的 cancel id 只会误伤未来的请求
        max_items = _optional_positive_int(params.get("max_items")) or _BATCH_MAX_ITEMS
        items = self._next_batch(max_items, id_)
        result: dict[str, Any] = {"items": items}
        if self._round is not None and self._round.position < len(self._round.entries):
            result["next"] = str(self._round.position)
        else:
            # 结束批：无游标（§6.8），且本轮不是基于游标的增量枚举（§6.4）
            result["cursor"] = None
            result["incremental"] = False
            self._round = None
        return _result(id_, result)

    def _next_batch(self, max_items: int, id_: Any) -> list[dict[str, Any]]:
        """取下一批产出项：条目数与**累计紧凑编码字节（含条目间逗号）**都不超预算（至少一条）。

        预算 = 消息上限 − ``_ENVELOPE_RESERVE`` − 响应里回显的 ``id`` 的编码长度：预留覆盖
        JSON-RPC 信封（``jsonrpc`` / ``result`` / ``items`` 键）、结束字段（``cursor`` /
        ``incremental``）与行尾 LF，id 长度另扣（它由运行器给出，可能很长）。批次内各
        条目之间的逗号在记账时就地计入（每条非首条 +1），否则接近条目数上限的批次会把响应顶出
        上限（实测：1000 条、总计 171000 字节、上限 172050 时，漏记逗号与信封会超出 29 字节）。
        """
        assert self._repo is not None and self._mapper is not None and self._round is not None
        round_ = self._round
        # 信封预留之外再扣掉响应里回显的 id 自身的编码长度：id 由运行器给出，可能很长
        budget = self.max_message_bytes - _ENVELOPE_RESERVE - _encoded_size(id_)
        items: list[dict[str, Any]] = []
        total = 0
        while round_.position < len(round_.entries) and len(items) < max_items:
            entry = round_.entries[round_.position]
            file_type = file_type_of(entry.path)
            if file_type is None:
                # 映射表外的文件不产出（README「映射规则」）；跳过不是错误，也不扣住游标
                _log.debug("跳过未支持的类型：%r", entry.path)
                round_.position += 1
                continue
            item = self._mapper.document(self._repo, entry, file_type)
            payload = item.payload()
            round_.position += 1
            size = _encoded_size(payload) + (1 if items else 0)  # 非首条：多一个分隔逗号
            if items and total + size > budget:
                round_.position -= 1  # 装不下：留到下一批
                break
            if not items and size > budget:
                # §6.5：无法在限制内表达的内容 MUST 以条目级 error 上报，MUST NOT 发送超限消息
                _log.warning(
                    "条目 %r 紧凑编码后 %d 字节，超过单条预算 %d（消息上限 %d），"
                    "按 content_invalid 上报",
                    entry.path,
                    size,
                    budget,
                    self.max_message_bytes,
                )
                payload = self._mapper.oversize_error(entry, size, budget, self.max_message_bytes)
                size = _encoded_size(payload)
            items.append(payload)
            total += size
        return items


#: 前缀结尾必须是的 URI 分隔字符：码点前缀匹配不识别段边界（契约 §6.5），前缀不带分隔符
#: 时 ``prefix + path`` 会改写 authority 或上一个段（``git://docs`` + ``a.md`` → ``git://docsa.md``）
_PREFIX_END = frozenset("/?#")


def _check_prefix(prefix: str) -> None:
    """校验实例前缀的拼接前提：非空、以 URI 分隔字符结尾（契约 §6.3 只保证它是规范化不动点）。"""
    if not prefix or prefix[-1] not in _PREFIX_END:
        raise ValueError("实例前缀必须以 URI 分隔字符（/ ? #）结尾，才能与仓库内路径拼接")


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
