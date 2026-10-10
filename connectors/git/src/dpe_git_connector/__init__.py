"""官方 Git connector（connector 契约 §6 的 dpe-run 插件）。

把 Git 仓库的**提交历史**映射为 DPE 文档（一篇文档 = 一个仓库：默认分支按月分页、其他
跟踪分支各一页、一次提交 = 一个元素），以全量枚举的方式经 stdio 的 JSON-RPC 提供给运行器。
插件只产出源内容：不计算 hash、不接触 DPE remote 与凭证、不感知任何服务端实现的概念。

包版本从安装元数据读取，且必须与随分发附带的 ``dpe-connector.json`` 一致——运行器在
``initialize`` 之后核对二者（契约 §4.1），不一致即实例级 ``plugin_mismatch``（不可自愈）。
两处的版本由 ``bump-my-version`` 原子更新（见 pyproject.toml），并由测试与打包断言看住
（``tests/test_e2e.py``）。清单本身随 wheel 分发（``dpe_git_connector/dpe-connector.json``）。
"""

from importlib.metadata import version

__all__ = ["PLUGIN_NAME", "__version__"]

#: 插件身份名：必须与清单的 ``name`` 相同（契约 §4.1）
PLUGIN_NAME = "git-connector"

__version__ = version("dpe-git-connector")
