"""dpe-sdk：DPE 协议 Python SDK。

依赖 dpe-hash 计算 hash；承载三层对象的数据模型（``dpe_sdk.models``），以及后续的 sans-IO 协议核心、
传输适配、增量推送、testing 与 dpe-run（#12–#17）。
"""

from importlib.metadata import version

from dpe_sdk.models import Document, DocumentObject, ElementObject, Page, PageObject

__version__ = version("dpe-sdk")

__all__ = [
    "Document",
    "DocumentObject",
    "ElementObject",
    "Page",
    "PageObject",
    "__version__",
]
