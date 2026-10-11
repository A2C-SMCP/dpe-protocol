"""北极星判据「规范与 SDK 中不出现任何服务端私有概念」的自动化守卫（#13 验收）。

扫描 ``dpe_sdk`` 包的全部源码（含注释与文档字符串）：SDK 是标准协议的中立实现，任何一行的
产品名、租户概念、私有头或私有的鉴权词汇，都说明抽象层被某个服务端实现渗透了。词表刻意保持
最小且可判定——通用 HTTP / 认证术语（``Authorization``、Bearer、ASGI 的 ``scope``、PAT 等）
不在此列，规范本身就用它们描述「方案由服务端决定」的部分。
"""

from __future__ import annotations

import re
from pathlib import Path

import dpe_sdk

#: 不能出现在 SDK 源码里的服务端私有概念（大小写不敏感）
FORBIDDEN = ("robot", "tenant", "tfrs", "vnd.tfrs", "x-tfrs", "jwt", "oauth")


def test_no_server_private_concepts_in_sources() -> None:
    pattern = re.compile("|".join(re.escape(term) for term in FORBIDDEN), re.IGNORECASE)
    root = Path(dpe_sdk.__file__).resolve().parent
    offenders = [
        f"{path.relative_to(root)}:{number}: {line.strip()}"
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == [], "SDK 源码出现服务端私有概念：\n" + "\n".join(offenders)
