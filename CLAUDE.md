# CLAUDE.md

DPE（Document / Page / Element）**标准协议**的权威仓库：规范、hash 契约、一致性向量、服务端跑分器、connector 契约、两份 SDK 都在这里。类比 Git 之于 GitHub / GitLab / CNB：任何服务端都能实现 DPE，**TFRS 端点只是第一个实现**。

**当前依据**：`docs/plan/v1-plan.md`（CTO 对 Issue #1 的评审定稿稿）。行为与计划冲突时以计划为准；对计划的异议走 Issue #1 回复（须带章节号）。**当前阶段：M1 规范草案待评审**。初版 SDK 在 `archive/initial-sdk` 分支，M1 定稿后按规范改造（M2）。

## 结构

- `spec/core.md`：抽象模型与操作语义（与传输无关）
- `spec/hash-contract-1.md`：hash 契约 1（`dpe1:`）
- `spec/bindings/http.md`：HTTP 绑定（v1 唯一规范性绑定）
- `spec/connector-contract.md`：中立 connector 契约（独立规范；运行边界见 Issue #2）
- `vectors/`：一致性向量（规范的一部分）
- `scripts/gen_vectors.py`：向量生成器 = hash 契约的**规范参考实现**（仅标准库）
- `conformance/`：黑盒 HTTP 跑分器（M2）
- `sdk/python/`、`sdk/rust/`：两份独立 SDK（M2）

## 命令

```bash
make vectors          # 重新生成向量（仅在规范变更时）
make check-vectors    # CI：校验已提交向量与生成器一致
```

## 不可破坏的约束

- **规范先行**：要改 hash 规则，必须先改规范、升契约版本，再改生成器重新生成向量。任何实现（含内核、TFRS）的现有行为都不能倒逼规范。
- **向量由本仓库产出**：生成器是 `scripts/gen_vectors.py`（不 import 任何 SDK / 内核代码），不得手改 `vectors/`。SDK 与各服务端实现只消费向量；SDK 测试失败而向量一致时，错的是 SDK。
- **依赖方向：内核 → Python SDK（#3 S1），SDK 运行时不依赖内核**。内核不保留自有 hash 实现，直接 import Python SDK 的 hash 核心；因此 M2 的 Python SDK 必须：hash 核心可单独安装（或主包零重依赖）、纯 Python（3.11 / 3.12）、导出契约常量（category 表与 hash_parts、metadata 保留键集合），PyPI 发布是内核 K2 的前置门禁。
- **规范与 SDK 中不出现任何服务端私有概念**：Robot、`vnd.tfrs`、`X-TFRS-Robot-Id`、tenant、TFRS 错误信封、JWT/scope 细节都不能进。SDK 只接受 remote URL + 可插拔 `CredentialProvider`。attributes 键（如 `tfrs/creator_id`）只能作为「某服务端声明」的示例出现。
- **字段按来源分类**（源提供进 hash / 寻址坐标与访问方式 / 治理属性 / 服务端衍生不投递，契约 1 §2，#3 S2 取代计划 §6 的三分类）：新增任何字段必须显式可归类；未归类即违规。不进 hash 的字段变更不得被静默丢弃。服务端写入的字段不得进入内容身份；源字段不得带会自行变化的默认值。
- **hash 值始终带契约前缀**（`dpe1:`），不暴露接受裸 hash 的公共 API；结构化值一律 RFC 8785 JCS，时间戳一律 RFC 3339 UTC。
- **写操作默认拒绝无 CAS 前置条件**（`DPE_PRECONDITION_REQUIRED`）；本地状态只作缓存，缓存丢失也必须正确，**绝不自动 force**。
- **两个 SDK 行为对等**（M2 起）：改一个 SDK 的协议行为必须同步另一个并补齐双方测试；两份实现出现分歧说明规范需要补正——修规范，而不是互相对齐。
- **偏离判据见 plan §13**：出现任一（伪变更、可见中间态、静默覆盖、契约升级引发重学、内核新增 diff 接口等）即为跑偏。

## 约定

- 文档与注释用中文，标识符用英文；关键词 MUST / SHOULD 按 RFC 2119。
- 规范草案中的待定项集中写在各文档的「待评审决策点」小节，并在 plan §16 有登记的注明。
- M2 起：Python ≥ 3.11（pydantic v2、sans-IO 核心、sync + async）；Rust ≥ 1.80（serde、sans-IO 核心不绑定异步运行时，默认 reqwest/tokio 适配）；`mypy --strict` / ruff / `cargo clippy -D warnings` / `cargo fmt` 必须通过。
