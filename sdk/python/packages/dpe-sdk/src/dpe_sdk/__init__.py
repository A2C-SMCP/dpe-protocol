"""dpe-sdk：DPE 协议 Python SDK。

依赖 dpe-hash 计算 hash；承载三层对象的数据模型（``dpe_sdk.models``）、协议错误
（``dpe_sdk.errors``）、响应报文（``dpe_sdk.wire``）、sans-IO 协议核心（``dpe_sdk.protocol``）
内存版参考服务端（``dpe_sdk.testing``）与 dpe-run 运行器的组成部分（``dpe_sdk.run``，不在顶层
导入），以及后续的传输适配与增量推送（#13–#15）。
"""

from importlib.metadata import version

from dpe_sdk import errors, protocol, wire
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
    "protocol",
    "wire",
]
