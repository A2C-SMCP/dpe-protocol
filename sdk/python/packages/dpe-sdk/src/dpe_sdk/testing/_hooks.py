"""一致性测试钩子（#45）：让外部进程（conformance 跑分器、Rust 集成测试）驱动参考服务端。

钩子**全部在启动配置中设定，不进入 DPE 协议面**：capabilities 与全部 DPE 端点不变；只有在
``create_app(..., hooks=HooksConfig(...))`` 显式启用时才挂在一个保留路径前缀下，默认关闭。
钩子词汇是语义的（``self-write``、``expire-session``），路径前缀只是配置值——跑分器按「执行命令
或调用 HTTP 接口」的配置驱动，不认识也不该认识这个前缀。参考服务端另提供 ``clock/advance``
（拨钟），供 Rust 集成测试与「续期 = 此刻 + TTL」的精确断言用；它不是跑分器的钩子契约。

每个钩子和它在引擎里的对应物：

- ``self-write`` → ``Engine.server_write``：服务端自身写入（core §2.4 同级写入），同一求值顺序、
  同样受 CAS 与报文校验约束，只绕过对外授权；
- ``expire-session`` → ``Engine.expire_session``：让一个暂存会话立即过期（不必等 ≥ 1 小时的 TTL）；
- ``clock/advance`` → ``AdjustableClock.advance``：把时钟向前拨，制造「闲置超过 TTL」与可精确
  断言的续期（``staging_ttl_seconds`` 保持规范要求的 ≥ 3600，不去改小它）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["AdjustableClock", "HooksConfig"]


class AdjustableClock:
    """可拨动的时钟：真实 UTC 时间加一个可累积的偏移，线程安全。

    拨钟之后时钟仍按真实时间流动（不是冻结时钟），因此「闲置超过 TTL 的会话过期」与
    「成功操作把过期时间推到此刻 + TTL」都能精确断言。把它作为 ``EngineConfig.clock`` 注入引擎，
    并把**同一个实例**放进 ``HooksConfig``，``clock/advance`` 才拨得动引擎用的时钟
    （``create_app`` 会校验是同一个实例）。
    """

    def __init__(self) -> None:
        self._offset = timedelta()
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            offset = self._offset
        return datetime.now(UTC) + offset

    def advance(self, seconds: float) -> None:
        """把时钟向前拨 ``seconds`` 秒（不可倒拨）。"""
        if seconds <= 0:
            raise ValueError(f"advance 只能向前拨正整数秒，实际为 {seconds!r}")
        with self._lock:
            self._offset += timedelta(seconds=seconds)


@dataclass(frozen=True)
class HooksConfig:
    """钩子通道的启动配置。

    - ``path``：保留路径前缀（如 ``/__hooks__``），须以 ``/`` 开头、不带尾部 ``/``、只含无需
      百分号编码的 path 字符，且与 DPE remote 的 ``prefix`` 互不遮蔽（``create_app`` 校验）；
    - ``clock``：必须是注入引擎的**同一个** ``AdjustableClock`` 实例（``EngineConfig.clock``）。
    """

    path: str
    clock: AdjustableClock
