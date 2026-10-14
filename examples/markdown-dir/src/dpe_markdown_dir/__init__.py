"""示例插件：把本地目录里的 Markdown 文件映射为 DPE 文档（connector 契约 §6 的 dpe-run 插件）。

示范「不用 SDK、任何语言都能写 connector」：运行时只用 Python 标准库，经 stdio 的 JSON-RPC
与运行器通信；只产出源内容，不计算 hash（§6.6）、不接触 DPE remote 与凭证。markdown 引用的
本地图片以句柄 blob 产出，由运行器经 ``read_blob`` 分块读取后上传（§6.7）。

不发布，不是官方 connector（官方的是 ``connectors/git``）。
"""

__all__ = ["PLUGIN_NAME", "__version__"]

#: 插件身份：必须与随附清单 ``dpe-connector.json`` 的 ``name`` / ``version`` 相同（§4.1，
#: 运行器在 initialize 之后核对）。不发布、不经安装元数据读取版本，两处直接写死并由测试看住
PLUGIN_NAME = "markdown-dir"
__version__ = "0.1.0"
