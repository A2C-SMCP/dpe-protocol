"""基准测试用的模拟 Jira：在测试用的 ``FakeJira``（``tests/fake_jira.py``）上加计数、限流与
故障注入。

- **计数**：请求数、响应字节数（JSON 编码后的长度，附件为字节长度）、429 次数；``snapshot`` 取一份
  当前计数，前后相减即一轮的开销；
- **限流**（W10）：``rate`` 给出时按令牌桶限流（每秒补 ``rate`` 个，桶容量同 ``rate``），没有令牌即
  429 + ``Retry-After: 1``——与 Jira Cloud 按请求速率限流的形态一致；
- **故障注入**（W9）：``fail_ids`` 里的工单取全文 / 评论时返回 500（暂时性失败）；
- **按 id 直接取工单**：``FakeJira._issue`` 线性扫描，2 万个工单时每次取全文都要扫一遍，这里按 id
  建索引（插件按 id 取全文），key 查询回落到原实现。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from fake_jira import FakeJira

__all__ = ["BenchJira", "JiraCounts", "LazyBlobs"]


@dataclass(frozen=True)
class JiraCounts:
    requests: int = 0
    response_bytes: int = 0
    throttled: int = 0

    def __sub__(self, other: JiraCounts) -> JiraCounts:
        return JiraCounts(
            self.requests - other.requests,
            self.response_bytes - other.response_bytes,
            self.throttled - other.throttled,
        )


class LazyBlobs(dict[str, bytes]):
    """附件 id → 字节，按需生成（不常驻内存）：``sizes`` 记大小，``render(id, size)`` 产出字节。

    继承 ``dict`` 只为满足 ``FakeJira.blobs`` 的类型；读取一律走 ``sizes``，不在 dict 本体存字节。
    """

    def __init__(self, render: Callable[[str, int], bytes]) -> None:
        super().__init__()
        self.sizes: dict[str, int] = {}
        self._render = render

    def __getitem__(self, key: str) -> bytes:
        return self._render(key, self.sizes[key])

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self.sizes else default

    def __contains__(self, key: object) -> bool:
        return key in self.sizes

    def __iter__(self) -> Iterator[str]:
        return iter(self.sizes)

    def __len__(self) -> int:
        return len(self.sizes)


@dataclass
class BenchJira(FakeJira):
    rate: float | None = None
    fail_ids: set[str] = field(default_factory=set)
    _counts: JiraCounts = JiraCounts()
    _tokens: float = 0.0
    _refilled: float = 0.0
    _count_lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> JiraCounts:
        with self._count_lock:
            return self._counts

    def set_rate(self, rate: float | None) -> None:
        with self._count_lock:
            self.rate = rate
            self._tokens = rate or 0.0
            self._refilled = time.monotonic()

    def handle(
        self, method: str, raw_path: str, headers: Any, body: bytes
    ) -> tuple[int, Any, dict[str, str]]:
        if self._take_token():
            status, payload, extra = 429, {"errorMessages": ["rate limited"]}, {"Retry-After": "1"}
        elif self._injected_failure(raw_path):
            status, payload, extra = 500, {"errorMessages": ["injected failure"]}, {}
        else:
            status, payload, extra = super().handle(method, raw_path, headers, body)
        size = len(payload) if isinstance(payload, bytes) else len(json.dumps(payload).encode())
        with self._count_lock:
            self._counts = JiraCounts(
                self._counts.requests + 1,
                self._counts.response_bytes + size,
                self._counts.throttled + (status == 429),
            )
        return status, payload, extra

    def _take_token(self) -> bool:
        """限流时没有令牌返回 True（本请求 429）；不限流恒为 False。"""
        with self._count_lock:
            if self.rate is None:
                return False
            now = time.monotonic()
            self._tokens = min(self.rate, self._tokens + (now - self._refilled) * self.rate)
            self._refilled = now
            if self._tokens < 1:
                return True
            self._tokens -= 1
            return False

    def _injected_failure(self, raw_path: str) -> bool:
        if not self.fail_ids:
            return False
        path = raw_path.split("?", 1)[0]
        for issue_id in self.fail_ids:
            stem = f"/rest/api/3/issue/{issue_id}"
            if path in (stem, f"{stem}/comment"):
                return True
        return False

    def _issue(self, id_or_key: str) -> tuple[int, Any, dict[str, str]]:
        issue = self.issues.get(id_or_key)
        if issue is not None:
            if issue["key"] in self.gone:
                return 404, {"errorMessages": ["Issue does not exist"]}, {}
            return 200, issue, {}
        return super()._issue(id_or_key)
