"""外部源 ``file_uri`` 的保留命名空间：``dpe://{tenant}/{path}``。

``file_uri`` 在 Robot Memory 中是 UNIQUE 键，外部源必须与 Robot 内部源（feishu:// / cos 等）隔离。
push-protocol-v1 将 ``dpe://{tenant}/{path}`` 列为候选方案（待决策项 #2），本 SDK 先按此实现。
"""

import re
from urllib.parse import quote

from pydantic import AnyUrl

DPE_SCHEME = "dpe"

_TENANT_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def make_dpe_uri(tenant: str, path: str) -> str:
    """构造规范化的 ``dpe://`` URI。

    Args:
        tenant: 租户 / 数据源命名空间，小写字母数字、``-``、``.``（如 ``acme-crm``、``com.acme.wiki``）。
        path: 源内路径，以 ``/`` 分隔，各段自动做百分号编码。

    返回值已经过与内核相同的 ``AnyUrl`` 规范化，可直接用作 ``Document.file_uri``，
    其字符串形式与进入 ``doc_hash`` 的形式一致。
    """
    if not _TENANT_RE.match(tenant):
        raise ValueError(f"invalid dpe tenant {tenant!r}: use lowercase letters, digits, '-' and '.'")
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        raise ValueError("dpe uri path must not be empty")
    encoded = "/".join(quote(s, safe="") for s in segments)
    return str(AnyUrl(f"{DPE_SCHEME}://{tenant}/{encoded}"))
