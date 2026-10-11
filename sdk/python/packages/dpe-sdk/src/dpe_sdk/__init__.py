"""dpe-sdk：DPE 协议 Python SDK。

依赖 dpe-hash 计算 hash；承载三层对象的数据模型（``dpe_sdk.models``）、协议错误
（``dpe_sdk.errors``）、响应报文（``dpe_sdk.wire``）、sans-IO 协议核心（``dpe_sdk.protocol``）、
媒体来源适配（``dpe_sdk.media``：路径 / 流 / ``read_blob`` 回调 → ``BlobSource``）、httpx
传输适配与凭证注入（``dpe_sdk.transport`` / ``dpe_sdk.credentials``，不在顶层导入——按需
import，不拉入 httpx）、增量推送（``dpe_sdk.pusher``，同样不在顶层导入）、内存版参考服务端
（``dpe_sdk.testing``）与 dpe-run 运行器的组成部分（``dpe_sdk.run``，同样不在顶层导入）。
"""

from importlib.metadata import version

from dpe_sdk import errors, media, protocol, wire
from dpe_sdk.errors import DpeError
from dpe_sdk.models import Document, DocumentObject, ElementObject, Page, PageObject

__version__ = version("dpe-sdk")

__all__ = [
    "Document",
    "DocumentObject",
    "DpeError",
    "ElementObject",
    "Page",
    "PageObject",
    "__version__",
    "errors",
    "media",
    "protocol",
    "wire",
]
