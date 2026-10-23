"""CI 的「外部模式」本地变体（#68 决策 8）：起需凭证 + HTTPS 的参考服务端，跑全部 scenarios。

流程：本地参考服务端带 ``--require-auth --grant <auth> <root>`` 与调小的
``--max-payload-bytes`` 启动（参考服务端公布地址的那一行就是就绪信号），前面加 TLS 终结器；
再以外部模式的环境变量启动 ``pytest -m scenarios``。没有 shell 轮询，退出码原样透传。
真实 remote 的运行不进常规 CI。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from e2e_harness import (
    ENV_CA_FILE,
    ENV_CREDENTIAL_ENV,
    ENV_REMOTE_URL,
    ENV_TIMEOUT,
    ENV_URI_PREFIX,
    ServerOptions,
    reference_server,
)

#: 本地变体使用的根前缀：参考服务端的写授权覆盖它，每次运行在其下追加唯一后缀
ROOT = "git://ci-e2e/"
#: 保存 Authorization 头原值的环境变量名（值随运行随机生成，不写入日志）
AUTH_ENV = "DPE_E2E_AUTHORIZATION"
#: 调小单请求上限，让暂存路径用例只需构造几百 KiB 的仓库
MAX_PAYLOAD = 256 * 1024


def main() -> int:
    authorization = f"Bearer {secrets.token_urlsafe(16)}"
    options = ServerOptions(
        root=ROOT,
        authorization=authorization,
        max_payload_bytes=MAX_PAYLOAD,
        tls=True,
        credential_env=AUTH_ENV,
    )
    with reference_server(options) as server:
        env = {
            **os.environ,
            ENV_REMOTE_URL: server.url,
            ENV_CREDENTIAL_ENV: AUTH_ENV,
            AUTH_ENV: authorization,
            ENV_URI_PREFIX: server.root,
            ENV_TIMEOUT: "180",
        }
        if server.ca_file is not None:
            env[ENV_CA_FILE] = str(server.ca_file)
        project = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "scenarios", "-v"],
            cwd=project,
            env=env,
            check=False,
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
