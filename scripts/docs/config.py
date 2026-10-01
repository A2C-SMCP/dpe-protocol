"""文档部署配置，从环境变量加载。"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DEPLOY_PATH = "/var/www/doc.turingfocus.cn/dpe"
# 安全门控：deploy_path 必须包含此标识，防止配置错误覆盖其他站点
DEPLOY_PATH_MARKER = "dpe"


@dataclass
class DocServerConfig:
    """文档服务器配置。"""

    host: str
    port: int = 22
    user: str = "root"
    password: str | None = None
    key_filename: str | None = None
    deploy_path: str = DEFAULT_DEPLOY_PATH
    nginx_user: str = "nginx"  # 用于 chown，nginx worker user


@dataclass
class DeployConfig:
    """部署配置总入口。"""

    server: DocServerConfig
    wecom_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "DeployConfig":
        """从环境变量加载配置。

        环境变量:
            DOCS_SERVER_HOST: 服务器地址（必需）
            DOCS_SERVER_PORT: SSH 端口（默认 22）
            DOCS_SERVER_USER: SSH 用户名（默认 root）
            DOCS_SERVER_PASSWORD: SSH 密码（与 KEY_FILE 二选一）
            DOCS_SERVER_KEY_FILE: SSH 私钥路径（与 PASSWORD 二选一）
            DPE_DOCS_DEPLOY_PATH: 部署路径（默认 /var/www/doc.turingfocus.cn/dpe）
            DOCS_NGINX_USER: nginx worker 用户（默认 nginx）
            WECOM_WEBHOOK_URL: 企业微信机器人 Webhook（可选）

        部署路径使用 DPE_ 前缀的独立变量：DOCS_SERVER_* 与其他文档站共用，
        而部署路径若沿用通用的 DOCS_DEPLOY_PATH，会把本站覆盖到别的站点目录。
        """
        server = DocServerConfig(
            host=os.getenv("DOCS_SERVER_HOST", ""),
            port=int(os.getenv("DOCS_SERVER_PORT", "22")),
            user=os.getenv("DOCS_SERVER_USER", "root"),
            password=os.getenv("DOCS_SERVER_PASSWORD") or None,
            key_filename=os.getenv("DOCS_SERVER_KEY_FILE") or None,
            deploy_path=os.getenv("DPE_DOCS_DEPLOY_PATH", DEFAULT_DEPLOY_PATH),
            nginx_user=os.getenv("DOCS_NGINX_USER", "nginx"),
        )
        return cls(server=server, wecom_webhook_url=os.getenv("WECOM_WEBHOOK_URL") or None)

    def validate(self) -> list[str]:
        """返回配置错误列表，空列表表示通过。"""
        errors = []
        if not self.server.host:
            errors.append("DOCS_SERVER_HOST 未设置")
        if not self.server.password and not self.server.key_filename:
            errors.append("DOCS_SERVER_PASSWORD 或 DOCS_SERVER_KEY_FILE 至少需要设置一个")
        if DEPLOY_PATH_MARKER not in os.path.basename(self.server.deploy_path.rstrip("/")):
            errors.append(
                f"部署路径末级目录缺少 '{DEPLOY_PATH_MARKER}' 标识，拒绝部署: {self.server.deploy_path}"
            )
        return errors
