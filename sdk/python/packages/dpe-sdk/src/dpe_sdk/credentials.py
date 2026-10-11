"""凭证注入（HTTP 绑定 §6）：SDK 通过可插拔的 ``CredentialProvider`` 取出 ``Authorization``
头的值，不感知鉴权方案——方案与凭证形式完全归 provider 的实现方（运行器、connector 的使用者与
测试），SDK 侧只有「一个头部值」。头字段与错误映射保持中性：不出现具体产品名与私有头。

provider 在**每个请求发送前**调用一次（同一客户端可据此轮换凭证）；传输层自行重发的请求
（网络失败后的重连、按 ``Retry-After`` 等待后的重试）同样算一次发送，会重新取凭证——轮换中的
短时凭证在长时间等待后仍可用。返回值是 ``Authorization`` 头的完整值（含方案名，如
``"Bearer <token>"``），``None`` 表示不带凭证。实现必须快速、非阻塞：同步客户端的调用发生在
调用线程，异步客户端的调用发生在事件循环内；需要在网络处刷新凭证的实现应自行预取或缓存。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["CredentialProvider", "StaticCredentialProvider"]


class CredentialProvider(Protocol):
    """按请求给出 ``Authorization`` 头的值（HTTP 绑定 §6）。"""

    def authorization(self) -> str | None:
        """``Authorization`` 头的完整值（含方案名）；``None`` 表示本次请求不带凭证。"""
        ...


@dataclass(frozen=True)
class StaticCredentialProvider:
    """固定凭证的简单实现（运行器从环境注入、脚本与测试使用）。"""

    value: str

    def authorization(self) -> str | None:
        return self.value
