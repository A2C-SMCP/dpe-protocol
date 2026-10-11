"""dpe-conformance：DPE 服务端一致性跑分器。

黑盒对任意 remote 运行协议检查（检查面见仓库 `conformance/README.md`）：套件以 pytest
实现、按章节组织在 ``dpe_conformance.checks`` 下；``dpe-conformance`` CLI 按中性配置
（身份与服务端钩子，见 ``dpe_conformance.config``）驱动套件，输出 JUnit XML 与「已执行 /
跳过」计数，供 CI 断言执行数不低于基线（防止因 skip 而假绿）。

跑分器本体只依赖 dpe-hash（期望 hash）与 HTTP；不 import 任何服务端实现——参考服务端
（``dpe_sdk.testing``）只在本包的测试里作为自测对手，以独立进程或 ASGI 方式连接。
"""

from importlib.metadata import version

__version__ = version("dpe-conformance")

__all__ = ["__version__"]
