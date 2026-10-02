"""dpe-sdk：DPE 协议 Python SDK。

依赖 dpe-hash 计算 hash；承载 sans-IO 协议核心、传输适配、增量推送、testing 与 dpe-run（#11–#17）。
本模块当前只是工程骨架。
"""

from importlib.metadata import version

__version__ = version("dpe-sdk")

__all__ = ["__version__"]
