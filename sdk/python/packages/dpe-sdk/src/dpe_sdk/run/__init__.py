"""dpe-run 运行器的组成部分（connector 契约）：插件清单、实例定义、凭证与插件环境、线协议编解码与
插件进程宿主。

- ``dpe_sdk.run.manifest``：清单与实例配置校验（§4.1–§4.2）；
- ``dpe_sdk.run.definition``：实例定义（§4.4，独立运行器规范档）；
- ``dpe_sdk.run.secrets``：凭证来源引用、插件环境构造与脱敏（§4.3）；
- ``dpe_sdk.run.jsonrpc``：NDJSON 分帧的 JSON-RPC 2.0 编解码（§6.2–§6.3，sans-IO）；
- ``dpe_sdk.run.host``：插件进程宿主（§6.1–§6.3、§6.7 的取消，asyncio）；
- ``dpe_sdk.run.errors``：失败分层与报告字段（§7.1、§7.4）。
"""

from dpe_sdk.run.definition import (
    InstanceDefinition,
    check_against_manifest,
    load_definition,
    resolve_command,
)
from dpe_sdk.run.errors import (
    InstanceFailure,
    ProtocolFailure,
    RoundFailure,
    RpcError,
    RunFailure,
)
from dpe_sdk.run.host import PluginHost, RequestTimedOut
from dpe_sdk.run.jsonrpc import PROTOCOL_VERSION, InitializeResult
from dpe_sdk.run.manifest import Manifest, load_manifest, validate_config
from dpe_sdk.run.secrets import PluginEnvironment, Redactor, build_plugin_env

__all__ = [
    "PROTOCOL_VERSION",
    "InitializeResult",
    "InstanceDefinition",
    "InstanceFailure",
    "Manifest",
    "PluginEnvironment",
    "PluginHost",
    "ProtocolFailure",
    "Redactor",
    "RequestTimedOut",
    "RoundFailure",
    "RpcError",
    "RunFailure",
    "build_plugin_env",
    "check_against_manifest",
    "load_definition",
    "load_manifest",
    "resolve_command",
    "validate_config",
]
