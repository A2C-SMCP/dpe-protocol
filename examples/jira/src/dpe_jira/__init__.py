"""示例插件：把 Jira Cloud 工单映射为 DPE 文档（connector 契约 §6 的 dpe-run 插件，#223）。

用来验证 Git connector 覆盖不到的 DPE 能力：大量小文档、数据源凭证、易变内容（状态、经办人
单独成元素）、限流。插件只产出源内容（§6.6），不计算 hash、不接触 DPE remote 与 remote 凭证；
Jira 凭证由运行器以环境变量注入（§4.3），任何输出都不回显。

不发布，不是官方 connector（官方的是 ``connectors/git``）。
"""

__all__ = ["PLUGIN_NAME", "__version__"]

#: 插件身份：必须与随附清单 ``dpe-connector.json`` 的 ``name`` / ``version`` 相同（§4.1，
#: 运行器在 initialize 之后核对）。不发布、不经安装元数据读取版本，两处直接写死并由测试看住
PLUGIN_NAME = "jira"
__version__ = "0.1.0"
