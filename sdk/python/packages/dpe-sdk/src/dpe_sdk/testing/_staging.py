"""参考服务端的暂存会话（core §3.4、HTTP 绑定 §4.5–§4.7）。

会话绑定 ``(file_uri, 调用者身份)``，内容对读接口不可见；成功的 negotiate / upload（含分块
上传的中间块）续期，只读的断点查询不续期；commit 成功消费会话，任何失败不消费（消费与判定
时机在 ``_engine`` 的求值顺序里）。**暂存对象以「上传声明的契约」的 hash 直接索引**：同一
会话内混用契约时，未按该契约上传的对象视为缺失——方向安全（客户端按缺失清单补传后收敛），
conformance 无跨契约暂存场景。

会话状态与对象存储共用引擎的同一把锁，本模块不自带锁。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from dpe_sdk.errors import SessionExpiredError, ValidationError
from dpe_sdk.wire import ElementUploadMissing, PageUploadMissing

__all__ = [
    "InvalidChunk",
    "PartialUpload",
    "Session",
    "SessionStore",
    "StagedElement",
    "StagedPage",
    "UploadChunk",
    "UploadOffsetError",
    "UploadOutcome",
    "UploadResult",
    "UploadSnapshot",
    "rfc3339_utc",
]


def default_clock() -> datetime:
    """默认时钟：当前 UTC 时间。"""
    return datetime.now(UTC)


def default_session_id() -> str:
    """默认会话 id 生成器（不透明字符串，HTTP 绑定 §4.5 示例为 ``st-…``）。"""
    return "st-" + secrets.token_hex(16)


def rfc3339_utc(value: datetime) -> str:
    """RFC 3339 UTC 时间戳（协议的时间戳口径，秒精度）。"""
    if value.tzinfo is None:
        raise ValueError("时间戳必须是带时区的 datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class UploadChunk:
    """分块请求的范围（HTTP：``Content-Range: bytes {from}-{to}/{total}``，含首尾）。"""

    from_byte: int
    to_byte: int
    total: int


@dataclass(frozen=True)
class InvalidChunk:
    """绑定层无法解析的分块参数（HTTP：``Content-Range`` 语法非法，或分块请求带
    ``Content-Encoding``）。

    与合法但不一致的分块参数同属 HTTP 绑定 §4.7 第 4 步（``DPE_VALIDATION``）：会话判定与
    「本会话内已完成 → 200」先于它，已完成的 hash 重复上传不因它失败（core §3.4）。元素对象
    不分块、没有「已完成」阶梯：带分块参数时在传输层上限与契约之后即判定，先于 I-JSON（#83）。
    """

    reason: str


UploadOutcome = Literal["created", "duplicate", "partial"]


@dataclass(frozen=True)
class UploadResult:
    """一次 upload 的结果（HTTP 绑定 §4.6、§4.7 的映射由传输层完成）。

    - ``outcome``：``created`` → 201、``duplicate`` → 200、``partial`` → 202；
    - ``expires_at``：续期后的过期时间（``DPE-Session-Expires`` 头）；
    - ``offset``：``partial`` 时的本会话已收字节数（``DPE-Upload-Offset`` 头）；
    - ``missing``：完成时的下一层缺失清单（响应体）；blob 完成没有下一层，为 ``None``（空对象）。
    """

    outcome: UploadOutcome
    expires_at: str
    offset: int | None = None
    missing: PageUploadMissing | ElementUploadMissing | None = None


@dataclass(frozen=True)
class UploadSnapshot:
    """断点查询的结果（HTTP：HEAD + ``DPE-Upload-Offset`` / ``DPE-Session-Expires``，不续期）。"""

    #: 无进度为 0，进行中为已收字节数，已完成为该对象已收的总字节数
    offset: int
    expires_at: str


class UploadOffsetError(ValidationError):
    """分块上传的 ``from`` 与本会话已收字节数不符（``DPE_VALIDATION``）。

    ``offset`` 是本会话已收字节数：HTTP 绑定 §4.7 要求该错误响应 MUST 带
    ``DPE-Upload-Offset``，客户端据此重新同步、无需再查询。
    """

    def __init__(self, message: str = "", path: str = "", *, offset: int) -> None:
        super().__init__(message, path)
        self.offset = offset


@dataclass
class StagedPage:
    """暂存的页对象：``body`` 的子引用为**上传声明契约**下的 hash。"""

    body: dict[str, Any]
    contract: str
    #: 已收字节数（整段上传与分块为实际接收的字节；negotiate 附带的页没有单独字节，
    #: 取其规范序列化（JCS）的 UTF-8 长度）
    size: int


@dataclass
class StagedElement:
    """暂存的元素对象：``hashes`` 是全部受支持契约下的 hash（元素无子引用，总能全算）。

    元素对象不分块、也没有按对象查询的接口，因此不记录字节数（``completed_size`` 只覆盖
    页与 blob）。
    """

    body: dict[str, Any]
    hashes: dict[str, str]


@dataclass
class PartialUpload:
    """分块中间态：已收字节与首块声明的总量（页对象或 blob，由目标 hash 前缀区分）。"""

    data: bytearray
    total: int


@dataclass
class Session:
    id: str
    #: 规范化后的 file_uri
    uri: str
    caller: str
    expires_at: datetime
    pages: dict[str, StagedPage] = field(default_factory=dict)
    elements: dict[str, StagedElement] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    partials: dict[str, PartialUpload] = field(default_factory=dict)

    def completed_size(self, target: str) -> int | None:
        """目标（页或 blob）在本会话已完成的字节数；未完成返回 ``None``。"""
        page = self.pages.get(target)
        if page is not None:
            return page.size
        blob = self.blobs.get(target)
        return None if blob is None else len(blob)


class SessionStore:
    """会话表：开启、取用（封闭清单校验）、续期、消费与惰性回收。

    ``get`` 不区分不可用的原因（过期、已消费、不存在、调用者或 file_uri 不符），一律
    ``DPE_SESSION_EXPIRED``（core §3.4，避免泄露他人会话是否存在）。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        ttl_seconds: int,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, Session] = {}

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock 必须返回带时区的 datetime")
        return value

    def open(self, uri: str, caller: str) -> Session:
        """开会话并续期计时（core §3.4：从最近一次成功操作起算）。"""
        self._sweep()
        session = Session(id=self._id_factory(), uri=uri, caller=caller, expires_at=self.now())
        self.renew(session)
        self._sessions[session.id] = session
        return session

    def renew(self, session: Session) -> None:
        """把过期时间推到「此刻 + staging_ttl」（成功的 negotiate / upload 含中间块）。"""
        session.expires_at = self.now() + self._ttl

    def get(self, caller: str, session_id: str, uri: str | None = None) -> Session:
        """取可用会话；``uri`` 给出时还要求会话的 file_uri 相符（commit；upload 不传）。"""
        self._sweep()
        session = self._sessions.get(session_id)
        if session is None or session.caller != caller:
            raise SessionExpiredError("暂存会话不可用")
        if uri is not None and session.uri != uri:
            raise SessionExpiredError("暂存会话不可用")
        if self.now() >= session.expires_at:
            del self._sessions[session_id]
            raise SessionExpiredError("暂存会话不可用")
        return session

    def consume(self, session: Session) -> None:
        """commit 成功后消费会话；未引用的暂存对象随之回收。"""
        self._sessions.pop(session.id, None)

    def expire(self, session_id: str) -> bool:
        """让会话立即过期（一致性测试钩子）：返回它此前是否存在且可用。

        只是把过期时间置为此刻——之后的取用仍走 ``get`` 的真实 TTL 比较（core §3.4），得到与闲置
        超过 TTL 相同的 ``DPE_SESSION_EXPIRED``；只影响这一个会话，其余会话照常续期与过期。
        """
        self._sweep()
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.expires_at = self.now()
        return True

    def _sweep(self) -> None:
        now = self.now()
        for session_id in [s.id for s in self._sessions.values() if now >= s.expires_at]:
            del self._sessions[session_id]
