# dpe-protocol

DPE（Document / Page / Element）：把结构化文档**正确、增量、可靠**地投递到一个远端的**标准协议**。本仓库是规范的唯一权威——就像 Git 之于 GitHub / GitLab / CNB，任何服务端都能实现 DPE，任何开发者都能用 SDK 接入自己的系统；TFRS 端点只是第一个实现。

**当前状态**：M1 规范草案（见 [docs/plan/v1-plan.md](docs/plan/v1-plan.md)，即 CTO 对 #1 的评审结论）。初版 SDK 代码保留在 `archive/initial-sdk` 分支供参考，M1 定稿后按规范改造（M2）。

## 仓库结构

```
docs/plan/v1-plan.md      # v1 计划（评审定稿稿，当前工作的依据）
spec/
  core.md                 # 核心协议：抽象模型、操作、CAS、错误（与传输无关）
  hash-contract-1.md      # hash 契约 1（dpe1:）
  bindings/http.md        # HTTP 绑定（v1 唯一规范性绑定）
  connector-contract.md   # 中立 connector 契约（大纲，独立规范）
vectors/                  # 一致性向量（规范的一部分，由规范产出）
scripts/gen_vectors.py    # 向量生成器 = hash 契约的规范参考实现（仅标准库）
conformance/              # 服务端一致性跑分器（M2 占位）
sdk/python/  sdk/rust/    # 两份独立 SDK 实现（M2）
```

## 命令

```bash
make vectors          # 重新生成向量（仅在规范变更时）
make check-vectors    # 校验已提交向量与生成器一致（CI）
```

## 分层

```
Connector（中立契约）→ 运行器 + SDK（hash·协商·暂存·CAS·重试）→ 任意 DPE 服务端
```

协议只负责投递；接什么源、何时推送是 connector 的事，鉴权、租户、学习、召回是服务端实现的事。规范与 SDK 中不出现任何服务端的私有概念。

## 里程碑

| | 内容 | 状态 |
| --- | --- | --- |
| M1 | 规范（core / http / hash-contract-1 / connector 大纲 / 向量） | **草案，待评审** |
| M2 | 两份 SDK（sans-IO）+ 黑盒跑分器 + 参考服务端 | 未开始 |
| M3 | 第一个服务端实现（TFRS + 内核改造） | 未开始 |
| M4 | 飞书真实数据端到端验收 | 未开始 |
