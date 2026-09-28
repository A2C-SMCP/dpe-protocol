"""dpe-protocol：DPE 投递的协议层，把上层产出的 DPE 文档通过 dpe-push/1 增量投递到 TFRobot。

本包以 **Document** 为边界：飞书、网盘、三方系统等对接，以及何时拉取、拉取哪些，都由上层负责。

分层（自底向上）：

- :mod:`dpe_protocol.schema`     DPE 三层数据模型（Document / DocPage / DocElement）
- :mod:`dpe_protocol.hashing`    hash-contract-v1 三层内容寻址 hash
- :mod:`dpe_protocol.push`       dpe-push/1 客户端（发现 → 协商 → 投递，含自动分片）
- :mod:`dpe_protocol.uri`        ``dpe://{tenant}/{path}`` 命名空间
- :mod:`dpe_protocol.validation` 文档级校验
- :mod:`dpe_protocol.pusher`     有状态推送器（自动 base_doc_hash、fingerprint 跳过）
- :mod:`dpe_protocol.testing`    内存版 Robot 服务端与文档一致性检查
"""

from dpe_protocol.push import DPEPushClient, PushResult
from dpe_protocol.push.models import MEDIA_TYPE, PROTOCOL_VERSION
from dpe_protocol.pusher import StatefulPusher
from dpe_protocol.schema import DocElement, DocMetadata, DocPage, Document, ElementCategory, ElementMetadata, FileType
from dpe_protocol.state import JsonFileStateStore, MemoryStateStore, SyncRecord, SyncStateStore
from dpe_protocol.uri import DPE_SCHEME, make_dpe_uri
from dpe_protocol.validation import check_document

__version__ = "0.1.0-dev"

__all__ = [
    "DPE_SCHEME",
    "MEDIA_TYPE",
    "PROTOCOL_VERSION",
    "DPEPushClient",
    "DocElement",
    "DocMetadata",
    "DocPage",
    "Document",
    "ElementCategory",
    "ElementMetadata",
    "FileType",
    "JsonFileStateStore",
    "MemoryStateStore",
    "PushResult",
    "StatefulPusher",
    "SyncRecord",
    "SyncStateStore",
    "check_document",
    "make_dpe_uri",
]
