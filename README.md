# dpe-protocol

DPE（Document / Page / Element）：把结构化文档**正确、增量、可靠**地投递到一个远端的**标准协议**。本仓库是规范的唯一权威——就像 Git 之于 GitHub / GitLab / CNB，任何服务端都能实现 DPE，任何开发者都能用 SDK 接入自己的系统；TFRS 端点只是第一个实现。

**文档版本**：0.3.0-dev（[doc.turingfocus.cn/dpe](https://doc.turingfocus.cn/dpe/)）｜**当前状态**：M1 规范已定稿（2026-10-06，依据 [docs/plan/v1-plan.md](docs/plan/v1-plan.md)），M2（SDK 与跑分器）进行中。规范的后续变更经 Issue 修订并发布新的文档版本。

## 仓库结构

```
docs/plan/v1-plan.md      # v1 计划（评审定稿稿，当前工作的依据）
spec/
  core.md                 # 核心协议：抽象模型、操作、CAS、错误（与传输无关）
  hash-contract-1.md      # hash 契约 1（dpe1:）
  bindings/http.md        # HTTP 绑定（v1 唯一规范性绑定）
  connector-contract.md   # 中立 connector 契约（独立规范；§6 运行边界已定稿）
website/                  # 文档站点入口（index.md + 指向 spec/ docs/ vectors/ conformance/ 的符号链接）
vectors/                  # 一致性向量（规范的一部分，由规范产出）
scripts/gen_vectors.py    # 向量生成器 = hash 契约的规范参考实现（仅标准库）
conformance/              # 服务端一致性跑分器（M2 占位）
sdk/python/               # Python SDK workspace：dpe-hash（零依赖 hash 核心）+ dpe-sdk（M2）
sdk/rust/                 # Rust SDK workspace：dpe-hash（hash 核心）+ dpe-sdk（M2）
```

## 命令

```bash
make vectors          # 重新生成向量（仅在规范变更时）
make check-vectors    # 校验已提交向量与生成器一致（CI）
```

两个 SDK 的开发、版本与发布见 [sdk/python/README.md](sdk/python/README.md) 与 [sdk/rust/README.md](sdk/rust/README.md)；SDK 版本独立于文档版本，发布标签分别为 `py-vX.Y.Z`（PyPI）与 `rs-vX.Y.Z`（crates.io）。

## 文档站点与版本管理

规范文档以 MkDocs + mike 发布到 <https://doc.turingfocus.cn/dpe/>，每个文档版本独立保留，`latest` 指向最新正式版。

- **文档版本**（`pyproject.toml` 的 `project.version`，单一来源）与**协议版本**（DPE v1）、**hash 契约版本**（契约 1 / `dpe1:`）是三个独立的轴。
- 版本串两态：`X.Y.Z-dev` = 开发中（未发布，部署时只占 `dev` 别名）；`X.Y.Z` = 已发布（占 `latest`）。

```bash
uv sync --group docs                        # 安装文档工具链（mkdocs / mike / invoke / bump-my-version）
uv run inv docs.serve                       # 本地预览

# 发布流程
uv run bump-my-version bump pre_l           # X.Y.Z-dev → X.Y.Z，自动 commit + tag vX.Y.Z
uv run inv docs.deploy                      # mike 构建 → 推送 gh-pages → 上传服务器（默认 mode=upload）
git push origin main --follow-tags
uv run bump-my-version bump patch           # 开下一周期：X.Y.Z → X.Y.(Z+1)-dev（minor / major 同理）
```

部署所需环境变量：`DOCS_SERVER_HOST`、`DOCS_SERVER_PASSWORD`（或 `DOCS_SERVER_KEY_FILE`），可选 `DOCS_SERVER_PORT` / `DOCS_SERVER_USER` / `DPE_DOCS_DEPLOY_PATH`（默认 `/var/www/doc.turingfocus.cn/dpe`）/ `WECOM_WEBHOOK_URL`。首次部署前的服务器配置见 `uv run inv docs.server-setup`。

## 分层

```
Connector（中立契约）→ 运行器 + SDK（hash·协商·暂存·CAS·重试）→ 任意 DPE 服务端
```

协议只负责投递；接什么源、何时推送是 connector 的事，鉴权、租户、学习、召回是服务端实现的事。规范与 SDK 中不出现任何服务端的私有概念。

## 里程碑

| | 内容 | 状态 |
| --- | --- | --- |
| M1 | 规范（core / http / hash-contract-1 / connector 契约 §6 / 向量） | **已定稿**（connector 契约其余章节见 #41） |
| M2 | 两份 SDK（sans-IO）+ 黑盒跑分器 + 参考服务端 | 进行中（`dpe-hash` 已发布到 PyPI） |
| M3 | 第一个服务端实现（TFRS + 内核改造） | 未开始 |
| M4 | 官方 Git connector 真实数据端到端验收 | 未开始 |
