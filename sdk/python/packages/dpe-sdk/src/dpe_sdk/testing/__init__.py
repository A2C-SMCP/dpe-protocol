"""dpe_sdk.testing：内存版参考服务端（#16），供两个 SDK 的集成测试、conformance 自测与第三方对照。

``Engine`` 是不含传输的核心引擎：按规范的求值顺序实现 core 语义（含暂存会话与分块上传，
#43），错误以 ``dpe_sdk.errors`` 抛出，结果为 ``dpe_sdk.wire`` 的响应模型或本模块的暂存结果
类型。时钟与会话 id 生成器可注入（``EngineConfig``）。命名保持中性，不出现任何服务端私有概念。
"""

from dpe_sdk.testing._engine import (
    AllowAll,
    Authorizer,
    BaseHash,
    DedupScope,
    Engine,
    EngineConfig,
    IfAbsent,
    Precondition,
)
from dpe_sdk.testing._staging import (
    UploadChunk,
    UploadOffsetError,
    UploadResult,
    UploadSnapshot,
    rfc3339_utc,
)

__all__ = [
    "AllowAll",
    "Authorizer",
    "BaseHash",
    "DedupScope",
    "Engine",
    "EngineConfig",
    "IfAbsent",
    "Precondition",
    "UploadChunk",
    "UploadOffsetError",
    "UploadResult",
    "UploadSnapshot",
    "rfc3339_utc",
]
