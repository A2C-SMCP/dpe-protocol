"""Invoke 任务定义 - 文档构建与多版本部署。

多版本由 mike 管理：每个文档版本是 gh-pages 分支下的一个目录，别名（latest / dev）是指向版本目录的符号链接，
站点根的 index.html 重定向到默认别名 latest。

使用方式:
    inv docs.serve                    # 本地预览当前工作区
    inv docs.serve-versioned          # 预览 gh-pages 上的多版本站点
    inv docs.build                    # mike 构建当前版本到本地 gh-pages
    inv docs.deploy                   # 构建 + 推送 gh-pages + 上传到服务器（默认 mode=upload）
    inv docs.deploy --mode=git        # 服务器侧 git fetch + reset 同步 gh-pages（需服务器已 clone）
    inv docs.list                     # 列出已发布的文档版本
    inv docs.clean                    # 清理构建产物
    inv docs.server-setup             # 查看服务器初始化步骤
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
import urllib.request

from invoke import task

from .config import DeployConfig
from .version_utils import get_project_version, is_dev_version

config = DeployConfig.from_env()

DOCS_URL = "https://doc.turingfocus.cn/dpe/"
GH_PAGES = "gh-pages"
DEPLOY_MODES = ("upload", "git")


def _resolve(version: str | None, alias: str | None) -> tuple[str, str]:
    """确定目标版本与别名：已发布版本默认占 latest，开发版本默认只占 dev，避免未发布内容顶替 latest。"""
    target_version = version or get_project_version()
    if alias is None:
        alias = "dev" if is_dev_version(target_version) else "latest"
    return target_version, alias


def _has_alias(c, alias: str) -> bool:
    result = c.run(f"mike list --json --branch {GH_PAGES}", warn=True, hide=True)
    if not result.ok or not result.stdout.strip():
        return False
    return any(alias in v.get("aliases", []) for v in json.loads(result.stdout))


def sync_gh_pages(c) -> None:
    """同步远程 gh-pages 到本地，避免多人发布时推送 non-fast-forward。"""
    print("🔄 同步远程 gh-pages 分支...")
    result = c.run(f"git ls-remote --heads origin {GH_PAGES}", warn=True, hide=True)
    if not result.stdout.strip():
        print("   远程 gh-pages 分支不存在，跳过同步（首次部署）")
        return
    c.run(f"git fetch origin {GH_PAGES}:{GH_PAGES}", warn=False)
    print("   ✅ 同步完成")


@task
def build(c, version=None, alias=None):
    """用 mike 构建文档到本地 gh-pages 分支。

    Args:
        version: 版本号，默认取 pyproject.toml（如 '0.1.0'）
        alias: 版本别名，默认已发布版本为 'latest'、开发版本为 'dev'；设为空字符串禁用别名
    """
    target_version, alias = _resolve(version, alias)
    print(f"🔨 构建文档 (version={target_version}, alias={alias or '-'})")

    cmd = ["mike", "deploy", target_version]
    if alias:
        cmd += [alias, "--update-aliases"]
    c.run(shlex.join(cmd), warn=False)

    # 站点根重定向到 latest；首个正式版发布前 latest 不存在，此时不设默认版本
    if _has_alias(c, "latest"):
        c.run("mike set-default latest", warn=False)
    print("✅ 文档构建完成")


@task
def serve(c):
    """启动本地开发服务器（当前工作区，单版本）。"""
    print("🚀 启动 MkDocs 开发服务器 (http://127.0.0.1:8000)")
    c.run("mkdocs serve", pty=True)


@task
def serve_versioned(c):
    """启动多版本文档预览服务器（读取本地 gh-pages）。"""
    print("🚀 启动 mike 多版本服务器 (http://127.0.0.1:8000)")
    c.run("mike serve", pty=True)


@task(name="list")
def list_versions(c):
    """列出本地 gh-pages 上已发布的文档版本与别名。"""
    c.run("mike list")


@task
def deploy(c, version=None, alias=None, push=True, mode="upload"):
    """构建并部署文档到 doc.turingfocus.cn。

    工作流程:
        1. 同步远程 gh-pages 分支
        2. mike 构建指定版本到本地 gh-pages
        3. 推送 gh-pages 到 GitHub（多版本历史的权威源）
        4. 更新服务器（按 mode）

    Args:
        version: 版本号，默认取 pyproject.toml
        alias: 版本别名，默认已发布版本为 'latest'、开发版本为 'dev'
        push: 是否推送 gh-pages 到 GitHub（默认 True；mode=git 时必须推送）
        mode: 'upload'（默认）本地打包 gh-pages → SFTP 上传 → 远端解压覆盖，不依赖服务器访问 GitHub；
              'git' 服务器执行 git fetch + reset --hard origin/gh-pages，需服务器已 clone gh-pages
    """
    if mode not in DEPLOY_MODES:
        print(f"❌ 未知 mode: {mode}（仅支持 {' / '.join(DEPLOY_MODES)}）")
        sys.exit(1)
    if mode == "git" and not push:
        print("❌ mode=git 依赖服务器从 GitHub 拉取，不能与 --no-push 同用")
        sys.exit(1)

    errors = config.validate()
    if errors:
        print("❌ 配置错误:")
        for error in errors:
            print(f"   - {error}")
        sys.exit(1)

    target_version, alias = _resolve(version, alias)
    print(f"🚀 部署文档 (version={target_version}, alias={alias or '-'}, mode={mode})")

    sync_gh_pages(c)
    build(c, version=target_version, alias=alias)

    if push:
        print("📤 推送 gh-pages 到 GitHub...")
        c.run(f"git push origin {GH_PAGES}", warn=False)
    else:
        print("⚠️  跳过 gh-pages 推送 (--no-push)")

    print(f"🔄 更新服务器 (mode={mode})...")
    if mode == "upload":
        upload_server(c)
    else:
        update_server()

    notify_wecom(
        f"✅ DPE 协议文档部署成功\n"
        f"版本: {target_version}\n"
        f"别名: {alias or '-'}\n"
        f"模式: {mode}\n"
        f"地址: {DOCS_URL}"
    )
    print(f"✅ 部署完成: {DOCS_URL}")


@task(name="upload-server")
def upload_server_task(c):
    """单独执行 mode=upload 的服务器更新（需本地 gh-pages 已构建）。"""
    upload_server(c)


@task(name="update-server")
def update_server_task(c):
    """单独执行 mode=git 的服务器更新。"""
    update_server()


@task
def server_setup(c):
    """显示服务器初始化步骤（首次部署前在服务器上执行）。"""
    path = config.server.deploy_path
    print("🖥️  服务器初始化步骤：")
    print()
    print("1. 创建部署目录（mode=upload 会自动 mkdir -p，可跳过）：")
    print(f"   mkdir -p {path}")
    print()
    print("   如需使用 mode=git，改为 clone gh-pages：")
    print(f"   git clone -b {GH_PAGES} https://github.com/A2C-SMCP/dpe-protocol.git {path}")
    print(f"   git -C {path} config core.fileMode false")
    print()
    print("2. 在 Nginx 配置 (/etc/nginx/conf.d/doc.turingfocus.cn.conf) 中添加：")
    print("   location /dpe/ {")
    print(f"       alias {path.rstrip('/')}/;")
    print("       index index.html;")
    print("       try_files $uri $uri/ =404;")
    print("   }")
    print()
    print("3. 在门户首页 (/var/www/doc.turingfocus.cn/index.html) 添加 DPE 文档入口")
    print()
    print("4. 重载 Nginx：nginx -t && systemctl reload nginx")


@task
def clean(c):
    """清理本地构建产物。"""
    c.run("rm -rf site/", warn=False)
    print("✅ 清理完成")


def _connect_ssh():
    """打开 SSH 连接；未配置凭证时返回 None。"""
    import paramiko

    if not (config.server.password or config.server.key_filename):
        return None
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        config.server.host,
        port=config.server.port,
        username=config.server.user,
        password=config.server.password,
        key_filename=config.server.key_filename,
    )
    return ssh


def _exec_remote(ssh, cmd: str) -> str:
    """在 SSH 会话上执行命令，失败时打印输出并退出。"""
    _, stdout, stderr = ssh.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()
    if exit_code != 0:
        print(f"❌ 远端命令失败 (exit {exit_code}):\n   命令: {cmd}")
        if out:
            print(f"   STDOUT:\n{out}")
        if err:
            print(f"   STDERR:\n{err}")
        sys.exit(1)
    return out


def _open_ssh_or_exit():
    try:
        ssh = _connect_ssh()
    except Exception as e:
        print(f"❌ SSH 连接失败: {e}")
        sys.exit(1)
    if ssh is None:
        print("❌ 未配置 SSH 密码或密钥文件")
        sys.exit(1)
    return ssh


def update_server() -> None:
    """mode=git：服务器侧 fetch + reset --hard 对齐 origin/gh-pages。"""
    ssh = _open_ssh_or_exit()
    path = shlex.quote(config.server.deploy_path)
    try:
        out = _exec_remote(
            ssh,
            f"cd {path} && git config core.fileMode false && "
            f"git fetch origin {GH_PAGES} && git reset --hard origin/{GH_PAGES}",
        )
        print(f"✅ 服务器更新成功 (mode=git):\n{out}")
    finally:
        ssh.close()


def upload_server(c) -> None:
    """mode=upload：本地打包 gh-pages → SFTP 上传 → 远端解压覆盖。

    tar 只覆盖同名文件、不删除多余文件；各版本目录由 mike 管理，旧版本保留即为多版本站点的预期。
    服务器 deploy_path 若是 git checkout，.git 保持不动，下次走 mode=git 时 reset --hard 会重新对齐。
    """
    deploy_path = config.server.deploy_path.rstrip("/")
    q_path = shlex.quote(deploy_path)
    nginx_user = shlex.quote(config.server.nginx_user)
    timestamp = int(time.time())
    worktree = tempfile.mkdtemp(prefix="dpe-gh-pages-")
    local_tar = os.path.join(tempfile.gettempdir(), f"dpe-docs-{timestamp}.tar.gz")
    remote_tar = f"/tmp/dpe-docs-{timestamp}.tar.gz"

    ssh = _open_ssh_or_exit()
    try:
        print(f"🔄 检出 gh-pages worktree: {worktree}")
        c.run(f"git worktree add --force {worktree} {GH_PAGES}", warn=False, hide=True)

        # COPYFILE_DISABLE / --no-xattrs：避免 macOS tar 写入 ._* 与扩展属性
        print(f"📦 本地打包: {local_tar}")
        c.run(
            f"COPYFILE_DISABLE=1 tar --no-xattrs --exclude='./.git' -czf {local_tar} -C {worktree} .",
            warn=False,
        )

        print(f"📤 SFTP 上传到 {config.server.host}:{remote_tar}")
        sftp = ssh.open_sftp()
        try:
            sftp.put(local_tar, remote_tar)
        finally:
            sftp.close()

        out = _exec_remote(
            ssh,
            f"mkdir -p {q_path} && cd {q_path} && "
            f"tar --no-same-owner -xzf {remote_tar} && "
            f"chown -R {nginx_user}:{nginx_user} {q_path} && "
            f"find {q_path} -path {q_path}/.git -prune -o -type d -print0 | xargs -0 -r chmod 755 && "
            f"find {q_path} -path {q_path}/.git -prune -o -type f -print0 | xargs -0 -r chmod 644 && "
            f"rm -f {remote_tar} && ls {q_path}",
        )
        print(f"✅ 服务器更新成功 (mode=upload)，部署目录内容:\n{out}")
    finally:
        c.run(f"git worktree remove --force {worktree}", warn=True, hide=True)
        try:
            os.unlink(local_tar)
        except FileNotFoundError:
            pass
        ssh.close()


def notify_wecom(message: str) -> None:
    """发送企业微信通知（未配置 WECOM_WEBHOOK_URL 时跳过）。"""
    if not config.wecom_webhook_url:
        return
    body = json.dumps({"msgtype": "text", "text": {"content": message}}).encode()
    req = urllib.request.Request(
        config.wecom_webhook_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        print(f"⚠️  企业微信通知失败: {e}")
