"""remote URL 的文法（HTTP 绑定 §1）：判定实现在 ``dpe_hash.remote_url``，此处重新导出。

传输层与 connector 运行器经本模块调用；跑分器只依赖 dpe-hash，直接用同一个函数（#181）。
"""

from dpe_hash.remote_url import RemoteUrlError, check_remote_url

__all__ = ["RemoteUrlError", "check_remote_url"]
