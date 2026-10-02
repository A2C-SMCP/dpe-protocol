"""dpe-hash：DPE hash 契约的独立 hash 核心。

零运行时依赖、纯 Python；内核与 dpe-sdk 都直接依赖本包，不另行维护 hash 实现。
hash 核心与契约常量见 #7，本模块当前只是工程骨架。
"""

from importlib.metadata import version

__version__ = version("dpe-hash")

__all__ = ["__version__"]
