"""线协议服务端（connector 契约 §6.2–§6.4）：JSON-RPC 2.0 over stdio、NDJSON 分帧。

本模块与进程/流无关（sans-IO）：``Serve.handle(line)`` 吃一行（不含行尾）返回一行（不含行尾），
I/O 在 ``cli`` 里。协议要点：

- **握手先行**：``initialize`` 必须是第一条消息，此前任何其他请求一律 ``-32600``；身份自报
  （``plugin.name`` / ``plugin.version``）由运行器与清单核对；
- **增量**（#66）：``capabilities.supports_cursor = true``。结束批给出游标（``cursor`` 模块），
  下一轮据此只产出状态有变化的仓库，并把消失的仓库表达为 ``delete`` / ``move``（见
  ``_open_round``）；游标解释不了时回退全量轮、``incremental: false``（§6.8）；
- **续批**：``next`` 是不透明的批次 token（§6.4），只有**最近回出的那个值**合法——收到
  token 即意味着上一批也已收到，直接续批；未知与过期（回退、重复、陌生）一律 ``-32602``，
  运行器中止本轮并以原 cursor 重开（恢复路径是稳定重跑整轮，§6.8）；
- **产出**：``scan`` 的 ``items`` 是 §6.5 的产出项；单条响应不超过 ``max_message_bytes``，
  一篇装不下的文档在**页边界**分段（同 ``file_uri`` 的各段连续出现：首段带 ``document``、
  续段只带 ``file_uri`` 与页，非末段 ``continued: true``）。单条预算连最小条目（error）都
  装不下（``max_message_bytes`` 过小或请求 id 过长）时以 ``-32602`` 应答（本轮失败，不发
  超限消息——§6.5 MUST NOT）；
- **通知**：``cancel`` 是通知（无响应），本连接器在批次处理结束后应答 ``-32001``；v1 只有运行器
  发起请求，插件不主动发消息（§6.1）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dpe_git_connector import PLUGIN_NAME, __version__
from dpe_git_connector.cursor import decode_cursor, encode_cursor, source_digest
from dpe_git_connector.gitrepo import GitError, GitRepo, RepoDir, discover_repos, looks_like_repo
from dpe_git_connector.mapping import DocumentMapper, Item, Limits, RepoMark, RepoSpec

__all__ = ["PROTOCOL_VERSION", "PROTOCOL_VERSIONS", "Serve"]

#: 本插件支持的线协议版本（§6.2）；与 DPE 协议版本、hash 契约版本相互独立
PROTOCOL_VERSIONS: tuple[str, ...] = ("dpe-connector/1",)
PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

#: 批次条目数的上界：超过它的批次即便再小也切分，避免超大文档挤占单条消息的处理时长
_BATCH_MAX_ITEMS = 1000

#: 单批里跳过（状态未变）的仓库数上限：跳过不产出条目，不受 max_items 约束；到达上限即以
#: 空 items + next 结束本批（§6.4 允许空批），单个 scan 请求的工作量因此有界——仓库很多而
#: 大多未变时，不让一个请求跑完整轮而撞上运行器的 scan 超时
_BATCH_MAX_SKIPPED = 256

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


def _budget_message(max_message_bytes: int, budget: int) -> str:
    """单条预算过小的错误消息：报实测口径（上限 − 信封预留 − id 长度 = 可用字节）。"""
    return (
        f"单条预算过小（消息上限 {max_message_bytes} − 信封预留 − id 长度 = {budget} 字节）："
        "连最小的错误条目都装不下，无法产出"
    )


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
    #: 数据源摘要（实例前缀 + 配置）：游标只在同一数据源下有效（``cursor``）
    digest: str = ""


@dataclass
class _Pending:
    """一篇尚未发完的分段文档（§6.5）：``file_uri``、剩余页与各页的编码尺寸。"""

    file_uri: str
    pages: list[dict[str, Any]]
    sizes: list[int]
    position: int = 0


@dataclass(frozen=True)
class _Step:
    """一轮里的一步：现成的条目（delete / move / error）、一个仓库，或读不了的子树。"""

    item: Item | None = None
    repo: RepoDir | None = None
    unreadable: RepoDir | None = None


@dataclass
class _Round:
    """一轮的枚举状态：按产出顺序排好的步骤、批次位置与新游标的记录。

    步骤顺序：move → delete → 现存仓库（按身份字节序）→ 读不了的子树。move 排在目标仓库的
    document 之前（同一 URI 的 document 在 move 之后生效，§6.4 按序生效）。move 的目标不论内容
    是否变化都紧跟着产出 document（§6.5，#178）。
    """

    steps: list[_Step]
    #: 本轮是否基于运行器给出的游标增量枚举（§6.4 ``incremental``）
    incremental: bool = False
    #: 每个现存仓库用于比较的上一轮记录（改名的目标取其源的记录）；全量轮为空
    baseline: dict[bytes, RepoMark] = field(default_factory=dict)
    #: 本轮 move 的目标仓库：不论内容是否变化都产出 document（§6.5，#178）——万一源已在早先
    #: 一个未推进游标的轮里被本实例删除，运行器据此把目标建出来，而不是让 move 永久失败；
    #: 内容未变时运行器判为 unchanged、不发写，代价很小
    move_targets: set[bytes] = field(default_factory=set)
    #: 新游标的记录：随步骤推进逐个写入（见 ``Serve._source_item``）
    marks: dict[bytes, RepoMark] = field(default_factory=dict)
    #: 已记入新记录、但条目还可能在发送时转成 error 的文档：file_uri → 仓库标识
    tentative: dict[str, bytes] = field(default_factory=dict)
    #: 下一个待处理的步骤下标
    position: int = 0
    #: 当前步骤已扫出的条目（本批装不下、留到下一批时复用，不重扫——同一轮内一个仓库
    #: 只扫一次，产出与记录基于同一次扫描）；推进到下一步时清空
    scanned: tuple[int, Item | None] | None = None
    #: 未发完的分段文档（同一时刻至多一个，§6.5）
    pending: _Pending | None = None
    #: 已回出的 ``next`` 计数：token 不透明（§6.4），回出的值即批次序号；
    #: 只有最近回出的那个值合法（收到它意味着上一批也已收到），其余一律过期。
    issued: int = 0

    def total_sources(self) -> int:
        return len(self.steps)

    def revert(self, file_uri: str) -> None:
        """一篇已记入新记录的文档最终没能产出（转成了 error）：恢复上一轮的记录。

        新游标 MUST NOT 越过本轮未能成功产出的变更（§6.8）：有旧记录就留旧记录（下一轮
        仍与旧状态比较、重新产出），没有就不记（下一轮当作新仓库）。
        """
        identifier = self.tentative.pop(file_uri, None)
        if identifier is None:
            return
        previous = self.baseline.get(identifier)
        if previous is None:
            self.marks.pop(identifier, None)
        else:
            self.marks[identifier] = previous

    def next_token(self) -> str:
        self.issued += 1
        return str(self.issued)

    def exhausted(self) -> bool:
        return self.pending is None and self.position >= self.total_sources()


class _BudgetTooSmall(Exception):
    """单条预算连最小条目（error）都装不下：本轮无法产出，不发超限消息（§6.5 MUST NOT）。

    触发条件是 ``max_message_bytes`` 过小或请求 ``id`` 过长（响应必须回显 id，预算被它吃掉），
    属运行器配置问题：``scan`` 以 JSON-RPC error 应答，运行器中止本轮、退避重试（§6.4）。
    """


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
            return self._sized_error(message.get("id"), -32600, '请求缺少 "jsonrpc": "2.0"')
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
            return self._sized_error(id_, -32600, "非法请求")
        if method == "shutdown" and self.initialized:
            self.shutdown_requested = True
            return _result(id_, {})
        if not self.initialized:
            if method != "initialize":
                # 握手先行（§6.3）：initialize 成功前不发任何内容
                return self._sized_error(id_, -32600, "握手完成前不接受其他请求")
            return self._initialize(id_, params)
        if self._cancel_last is not None and id_ == self._cancel_last:
            self._cancel_last = None
            return self._sized_error(id_, -32001, "请求已被取消")
        if method == "scan":
            return self._scan(id_, params)
        return self._sized_error(id_, -32601, f"未知方法：{method[:_METHOD_ECHO_LIMIT]}")

    def _sized_error(
        self, id_: Any, code: int, message: str, data: dict[str, Any] | None = None
    ) -> str:
        """错误应答：整行（含 id 与行尾 LF）≤ ``max_message_bytes``，装不下就只留错误码。

        响应必须回显 id，而 id 由运行器给出、可能很长：消息正文是唯一可裁的部分。只带
        ``code`` 的最小错误对象对**请求本身合规的输入**都装得下（请求还含 method 与 params，
        比它更长；转义只会让请求更大），因此任何路径都不会发出超限消息（§6.5 MUST NOT）。
        裁剪会一并去掉 ``message`` 与 ``data``（如 -32003 的 ``retryable``）——只发生在
        id 逼近上限的窄窗口，信息损失可接受。上限未知时（尚未解析出 ``max_message_bytes``）
        不校验；一旦已知，握手失败的应答同样受校验。
        """
        line = _error(id_, code, message, data)
        if self.max_message_bytes <= 0 or len(line.encode("utf-8")) + 1 <= self.max_message_bytes:
            return line
        return _encode({"jsonrpc": "2.0", "id": id_, "error": {"code": code}})

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
        # 尽早记录：此后（含握手失败的应答）的错误响应都按整行上限校验
        self.max_message_bytes = max_bytes
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
        limits = _limits(params.get("remote_limits"))
        self._source = _Source(
            parsed.repo_path,
            parsed.root_path,
            parsed.spec,
            source_digest(uri_prefix, config, limits.max_payload_bytes),
        )
        self._mapper = DocumentMapper(uri_prefix, limits)
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
                "capabilities": {"supports_cursor": True},
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
        return self._sized_error(id_, code, message)

    # ------------------------------------------------------------------ scan

    def _scan(self, id_: Any, params: dict[str, Any]) -> str:
        assert self._source is not None and self._mapper is not None
        if "next" in params and params["next"] is not None:
            if "cursor" in params:
                return self._sized_error(id_, -32602, "scan 不得同时带 cursor 与 next")
            token = params["next"]
            if not isinstance(token, str) or not _DECIMAL.fullmatch(token):
                return self._sized_error(id_, -32602, "未知的 next")
            # 只有最近回出的 token 合法（§6.4）：收到 token 即意味着上一批也已收到，直接续批
            # （含在途的分段文档）；其余值（回退、重复、陌生）一律过期拒绝，运行器中止本轮并
            # 以原 cursor 重开——恢复路径是稳定重跑整轮（§6.8），不回退重放。
            if self._round is None or int(token) != self._round.issued:
                return self._sized_error(id_, -32602, "未知或已过期的 next")
        else:
            cursor = params.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                return self._sized_error(id_, -32602, "cursor 必须是字符串或 null")
            try:
                self._round = self._open_round(cursor)
            except GitError as exc:
                return self._sized_error(id_, -32003, str(exc), {"retryable": True})
            self._cancel_last = None  # 跨轮残留的 cancel id 只会误伤未来的请求
        max_items = _optional_positive_int(params.get("max_items")) or _BATCH_MAX_ITEMS
        try:
            items, used = self._next_batch(max_items, id_)
        except _BudgetTooSmall as exc:
            # 单条预算连最小条目都装不下：本轮失败，如实应答（§6.4：运行器中止本轮、退避重试）。
            # 轮状态保持原样：运行器若以同一 token 重试本批，结果确定不变（无半程副作用）。
            return self._sized_error(id_, -32602, str(exc))
        result: dict[str, Any] = {"items": items}
        round_ = self._round
        assert round_ is not None
        if not round_.exhausted():
            result["next"] = round_.next_token()
            return _result(id_, result)
        # 结束批：给出新游标（§6.8）。信封预留只覆盖 "cursor":null，游标串的字节另计
        cursor_value: str | None = encode_cursor(self._source.digest, round_.marks)
        if used + _encoded_size(cursor_value) > self._budget(id_):
            if items:
                # 本批放不下游标：以 next 结束本批，下一批单独给出空 items 与游标
                result["next"] = round_.next_token()
                return _result(id_, result)
            # 游标本身超过单条预算：本轮不提供游标（下一轮全量），不发超限消息（§6.5）
            _log.warning("游标超过单条消息预算，本轮不提供游标（下一轮为全量枚举）")
            cursor_value = None
        result["cursor"] = cursor_value
        result["incremental"] = round_.incremental
        self._round = None
        return _result(id_, result)

    def _budget(self, id_: Any) -> int:
        """单条响应的条目预算：消息上限 − 信封预留 − 回显 id 的编码长度。"""
        return self.max_message_bytes - _ENVELOPE_RESERVE - _encoded_size(id_)

    def _open_round(self, cursor: str | None) -> _Round:
        """开始一轮：重新发现数据源，按游标排好本轮的步骤（§6.3 跨轮重启是 MAY，不依赖进程内状态）。

        单仓库模式的标识取仓库目录名；root 模式重新发现仓库集合。

        **增量轮**（游标可解释）：现存仓库与上一轮的记录比较，状态指纹相同即跳过（``_source_item``）；
        **消失的仓库**（上一轮有、本轮发现不到）表达为显式意图（§6.5：删除与移动 MUST 是显式
        条目）——只在 root 模式：

        - 消失的仓库与本轮新出现的仓库**移动键相同且一一对应**（根提交集合 + 默认分支名，#96）
          → ``move``；同键多对多（如同一仓库的多份克隆同时改名）无法确定配对 → 按 delete + 新建；
        - 其余消失的仓库 → ``delete``。

        **发现不完整时不产出 delete / move**（fail-closed）：存在读不了的子树，或某个新出现的
        仓库算不出移动键。被藏起来或算不出键的仓库可能正是移动目标；此时删了源，下一轮的
        move 就永久失败。这类轮必然带 error 条目，运行器不推进游标（§6.8）；消失仓库的记录
        原样带入新游标，新出现、且移动键与某个消失仓库相同的仓库也先不产出（以条目级 error
        如实上报）——否则它先被建出来，下一轮配成的 move 会撞上已存在的目标。
        """
        assert self._source is not None and self._mapper is not None
        if self._source.repo_path is not None:
            name = os.path.basename(os.path.normpath(self._source.repo_path))
            entries = [RepoDir(path=self._source.repo_path, identifier=os.fsencode(name))]
            unreadable: list[RepoDir] = []
        else:
            assert self._source.root_path is not None
            entries, unreadable = discover_repos(self._source.root_path)
        previous = None if cursor is None else decode_cursor(cursor, self._source.digest)
        if cursor is not None and previous is None:
            _log.warning("游标无法解释（格式、版本或数据源不符），本轮回退为全量枚举")
        tail = [_Step(repo=entry) for entry in entries]
        tail += [_Step(unreadable=entry) for entry in unreadable]
        if previous is None:
            return _Round(tail)

        current = {entry.identifier for entry in entries}
        baseline = {key: mark for key, mark in previous.items() if key in current}
        gone = sorted(key for key in previous if key not in current)
        round_ = _Round([], incremental=True, baseline=baseline)
        if not gone:
            round_.steps = tail
            return round_
        if self._source.repo_path is not None:
            # 单仓库模式的身份只随配置变化（游标随之失效）：正常走不到这里。防御性地保留记录，
            # 并以条目级 error 如实上报——不静默（§6.5），本轮不会成为推进游标的成功轮
            round_.marks.update({key: previous[key] for key in gone})
            round_.steps = [self._unexpressible(gone), *tail]
            return round_

        appeared = [entry for entry in entries if entry.identifier not in previous]
        keys: dict[bytes, str | None] = {}
        held: dict[bytes, Item] = {}
        for entry in appeared:
            try:
                keys[entry.identifier] = self._mapper.move_key_of(
                    GitRepo(entry.path), self._source.spec
                )
            except Exception as exc:
                file_uri = self._mapper.file_uri(entry)
                held[entry.identifier] = (
                    file_uri
                    if isinstance(file_uri, Item)
                    else self._mapper.repo_error(file_uri, exc)
                )
        intents: list[_Step] = []
        if unreadable or held:
            gone_keys = {previous[key].move_key for key in gone} - {None}
            for entry in appeared:
                if entry.identifier not in held and keys.get(entry.identifier) in gone_keys:
                    held[entry.identifier] = self._deferred_move_target(entry)
            round_.marks.update({key: previous[key] for key in gone})
        else:
            intents = self._intents(gone, previous, appeared, keys, round_)
        round_.steps = intents + [
            _Step(item=held[step.repo.identifier])
            if step.repo is not None and step.repo.identifier in held
            else step
            for step in tail
        ]
        return round_

    def _intents(
        self,
        gone: list[bytes],
        previous: dict[bytes, RepoMark],
        appeared: list[RepoDir],
        keys: dict[bytes, str | None],
        round_: _Round,
    ) -> list[_Step]:
        """发现完整时的 move 与 delete 步骤；配对成功的目标以源的记录作比较基准。"""
        assert self._mapper is not None
        sources: dict[str, list[bytes]] = {}
        for key in gone:
            source_key = previous[key].move_key
            if source_key is not None:
                sources.setdefault(source_key, []).append(key)
        targets: dict[str, list[RepoDir]] = {}
        for entry in appeared:
            target_key = keys.get(entry.identifier)
            if target_key is not None:
                targets.setdefault(target_key, []).append(entry)
        moves: list[_Step] = []
        moved: set[bytes] = set()
        for move_key, from_keys in sources.items():
            to_entries = targets.get(move_key, [])
            if len(from_keys) != 1 or len(to_entries) != 1:
                continue  # 配对不唯一：按 delete + 新建
            from_key, target = from_keys[0], to_entries[0]
            from_uri = self._intent_uri(from_key)
            to_uri = self._mapper.file_uri(target)
            if from_uri is None or isinstance(to_uri, Item):
                continue
            moves.append(_Step(item=Item("move", {"from_uri": from_uri, "to_uri": to_uri})))
            moved.add(from_key)
            # 目标以源的记录作比较基准（记录随之沿用），且总是紧跟着产出 document（#178）
            round_.baseline[target.identifier] = previous[from_key]
            round_.move_targets.add(target.identifier)
        deletes: list[_Step] = []
        for key in gone:
            if key in moved:
                continue
            file_uri = self._intent_uri(key)
            if file_uri is None:
                # 表达不了：留着记录、如实上报，不越过这个变更（§6.5 不静默、§6.8）
                round_.marks[key] = previous[key]
                deletes.append(self._unexpressible([key]))
                continue
            deletes.append(_Step(item=Item("delete", {"file_uri": file_uri})))
        return moves + deletes

    def _unexpressible(self, identifiers: list[bytes]) -> _Step:
        """消失的仓库无法表达为 delete（防御性，正常走不到）：条目级 error 如实上报。"""
        assert self._mapper is not None
        return _Step(
            item=self._mapper.error_item(
                None,
                "internal",
                f"{len(identifiers)} 个消失的仓库无法表达为 delete（标识构造不出 file_uri）",
                retryable=False,
            )
        )

    def _intent_uri(self, identifier: bytes) -> str | None:
        """上一轮记录里的仓库标识 → file_uri（它在上一轮已成功映射过，正常总能构造）。"""
        assert self._mapper is not None
        file_uri = self._mapper.file_uri(RepoDir(path="", identifier=identifier))
        return None if isinstance(file_uri, Item) else file_uri

    def _deferred_move_target(self, entry: RepoDir) -> Item:
        """发现不完整时，可能是移动目标的新仓库先不产出：条目级 error（可重试）如实上报。"""
        assert self._mapper is not None
        file_uri = self._mapper.file_uri(entry)
        if isinstance(file_uri, Item):
            return file_uri
        return self._mapper.error_item(
            file_uri,
            "source_unavailable",
            "可能是改名后的仓库，但本轮发现不完整（有读不了的子树或算不出移动键的仓库），"
            "推迟到发现完整的一轮再判定是否为移动",
            retryable=True,
        )

    def _next_batch(self, max_items: int, id_: Any) -> tuple[list[dict[str, Any]], int]:
        """取下一批产出项：条目数与**累计紧凑编码字节（含条目间逗号）**都不超预算（至少一条）。

        预算 = 消息上限 − ``_ENVELOPE_RESERVE`` − 响应里回显的 ``id`` 的编码长度：预留覆盖
        JSON-RPC 信封（``jsonrpc`` / ``result`` / ``items`` 键）、结束字段（``cursor`` /
        ``incremental``）与行尾 LF，id 长度另扣（它由运行器给出，可能很长）。批次内各条目
        之间的逗号在记账时就地计入（每条非首条 +1）。

        装不下的条目**不消费**（留到下一批从头枚举），保证批次推进与确定性；装不下的文档在
        **页边界**分段（§6.5），已在途的分段文档优先发完（各段连续）。未变的仓库不产出条目，
        直接推进。返回（条目, 已用字节）——结束批据此判断能否放下游标。
        """
        assert self._source is not None and self._mapper is not None and self._round is not None
        round_ = self._round
        # 信封预留之外再扣掉响应里回显的 id 自身的编码长度：id 由运行器给出，可能很长
        budget = self._budget(id_)
        items: list[dict[str, Any]] = []
        total = 0
        skipped = 0
        while len(items) < max_items and skipped < _BATCH_MAX_SKIPPED:
            if round_.pending is not None:
                payload = self._pending_segment(budget - total - (1 if items else 0))
                if payload is None:
                    if items:
                        break  # 本批装不下下一页：留到下批（pending 保留）
                    # 批首仍装不下：续批的预算比创建分段时缩水了（id 变长等，§6.4 只保证 id
                    # 会回显）。该文档整篇失败（已发出的分段未闭合），如实上报并放弃，批次推进。
                    payload = self._unfinishable_pending(budget - total)
                    size = _encoded_size(payload)
                    if size > budget:
                        # 载荷本身装不下：不放弃 pending（重试同 token 时状态确定不变），
                        # 也不发超限消息——本轮以 -32602 失败（§6.5 MUST NOT）
                        raise _BudgetTooSmall(_budget_message(self.max_message_bytes, budget))
                    round_.revert(round_.pending.file_uri)  # 未能产出：新游标不越过它
                    self._round.pending = None  # 已决定放弃，剩余页不再产出
                    items.append(payload)
                    total += size
                    continue
                size = _encoded_size(payload) + (1 if items else 0)
                items.append(payload)
                total += size
                continue
            if round_.position >= round_.total_sources():
                break
            item = self._source_item(round_)
            if item is None:
                round_.position += 1  # 状态未变的仓库：不产出（记录已沿用）
                round_.scanned = None
                skipped += 1
                continue
            payload = self._fit_item(item, budget - total - (1 if items else 0), at_head=not items)
            if payload is None:
                assert items  # 批首必能推进一条（见 _fit_item），否则会空批死循环
                break
            if item.kind == "document" and payload["kind"] == "error":
                round_.revert(item.fields["file_uri"])  # 装不下而转成 error：不越过它
            size = _encoded_size(payload) + (1 if items else 0)
            items.append(payload)
            total += size
            round_.position += 1
            round_.scanned = None
        return items, total

    def _source_item(self, round_: _Round) -> Item | None:
        """当前步骤的产出项；状态未变的仓库返回 ``None``（不产出）。

        仓库步骤同时写入新游标的记录：未变沿用旧记录；产出 document 记新记录（发送时若转成
        error 由 ``_Round.revert`` 恢复）；产出 error 保留旧记录——新游标不越过未产出的变更。
        """
        assert self._source is not None and self._mapper is not None
        step = round_.steps[round_.position]
        if step.item is not None:
            return step.item
        if step.unreadable is not None:
            return self._mapper.unreadable_directory(step.unreadable)
        entry = step.repo
        assert entry is not None
        if round_.scanned is not None and round_.scanned[0] == round_.position:
            return round_.scanned[1]  # 上一批装不下而留下的同一次扫描结果
        previous = round_.baseline.get(entry.identifier)
        always = entry.identifier in round_.move_targets  # move 的目标总是产出（#178）
        item, mark = self._mapper.scan(
            GitRepo(entry.path),
            entry,
            self._source.spec,
            unchanged_if=None if previous is None or always else previous.fingerprint,
        )
        if mark is not None:
            assert item is not None
            round_.marks[entry.identifier] = mark
            round_.tentative[item.fields["file_uri"]] = entry.identifier
        elif previous is not None:
            round_.marks[entry.identifier] = previous
        else:
            round_.marks.pop(entry.identifier, None)
        round_.scanned = (round_.position, item)
        return item

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
            fallback = self._oversize_document(fields["file_uri"], fields["pages"], room)
            if _encoded_size(fallback) > room:
                # 连最小的 error 条目都装不下：不发超限消息（§6.5 MUST NOT）
                raise _BudgetTooSmall(_budget_message(self.max_message_bytes, room))
            return fallback
        payload = item.payload()
        if _encoded_size(payload) <= room:
            return payload
        if not at_head:
            return None
        # 批首仍装不下（error 条目的字段由本插件构造，正常极小）：以「条目超限」如实上报，
        # 保证推进；message 里报告实际数字，便于诊断
        fallback = self._oversize_entry(item, room)
        if _encoded_size(fallback) > room:
            raise _BudgetTooSmall(_budget_message(self.max_message_bytes, room))
        return fallback

    def _unfinishable_pending(self, room: int) -> dict[str, Any]:
        """在途分段文档的下一页在批首也装不下（分批 token 的 id 变长使预算缩水）。

        该文档整篇失败（已发出的分段未闭合，§6.5），如实产出条目级 ``error``；由调用方在
        确认装得下之后才放弃剩余页（``pending`` 的清空不放这里——载荷装不下要整轮失败，
        重试同 token 时状态必须不变）。批次继续推进（后续条目不因此被扣住）。
        """
        assert self._round is not None and self._round.pending is not None
        assert self._mapper is not None
        pending = self._round.pending
        message = (
            f"分段无法在单条预算内继续（剩余 {len(pending.pages) - pending.position} 页，"
            f"可用 {room} 字节）：该文档整篇失败（已发出的分段未闭合）"
        )
        _log.warning("文档 %s：%s", pending.file_uri, message)
        return self._mapper.error_item(
            pending.file_uri, "content_invalid", message, retryable=False
        ).payload()

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
