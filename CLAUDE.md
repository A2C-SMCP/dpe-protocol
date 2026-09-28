# CLAUDE.md

DPE（Document / Page / Element）投递的**协议层**多语言 SDK：以 Document 为边界，把上层产出的 DPE 文档通过 dpe-push/1 增量投递到 TFRobot。

**分层边界**：飞书、网盘、三方系统等具体对接（鉴权、API、解析、调度、编排）属于上层 connector，**不在本项目**。本项目**不定义 connector 的形态**（没有 Connector 接口、没有 scan/fetch 循环），对外只提供以 Document 为单位的 API：`push`、`StatefulPusher`、`check_document`、`check_documents`。不要在库里加入 connector 抽象或具体系统的对接；示例只放 `examples/`，写成普通脚本。

- `python/`：Python SDK（uv，≥3.11）
- `rust/`：Rust SDK（cargo，≥1.80）
- `vectors/v1/`：内核生成、两个 SDK 共享的 hash 一致性向量（临时托管，待移交内核仓库，见 design.md §7.1）
- `docs/design.md`：基础设计

**Current Version**: 0.1.0-dev

## 权威规范（不在本仓库）

- `TFRobotServer/docs/protocol/dpe/push-protocol-v1.md`：投递协议
- `TFRobotServer/docs/protocol/dpe/hash-contract-v1.md`：hash wire 契约
- 内核模型：`tfrobot.schema.document`（`base.py` / `meta.py` / `hasher.py` / `elements.py`）

本仓库是符合性实现。行为与规范冲突时以规范为准；规范与内核冲突时记录到 `docs/design.md` §8。

## 命令

```bash
make test | make lint | make format      # 同时覆盖 python/ 与 rust/
make test-python | make test-rust
make vectors TFROBOT_PYTHON=/path/to/TFRobotServer/.venv/bin/python
```

## 不可破坏的约束

- **hash 期望值只来自内核**：不得手改 `vectors/`，也不得用任何 SDK 生成向量。改了 hashing 却让向量失败时，错的是 SDK。重新生成时保留 `manifest.json` 的 `provenance`（内核 / pydantic 版本）。
- **SDK 不得依赖内核代码**：`tfrobot` 只允许出现在 `scripts/gen_vectors.py` 中，SDK 运行与测试都不能 import 它。
- **两个 SDK 行为对等**：修改一个 SDK 的协议行为（模型字段、hash、wire 格式、重试、Source 契约）时，必须同步修改另一个，并补齐双方测试。新增 hash 相关边界时，优先加内核向量，而不是只写单语言单测。
- **`DocMetadata` 字段集 = 内核 `TFDocMetadata` 字段集**：它整体进入 `doc_hash`。内核增删字段时，两个 SDK 同步修改，并重新生成向量。
- **Rust 的 `json_stable` / datetime 不能换成 serde 默认实现**：必须与 Python `json.dumps` / `isoformat()` 逐字节一致（浮点 repr、转义、`+00:00`）。
- **源端模型不含 hash / 主键字段**：hash 只存在于 `Manifest` 和 `DocumentHashes`，并且始终携带其 strategy，不暴露接受裸 hash 的公共 API。
- **manifest 的 `doc_metadata` 发送全量字段（包括 null）**：否则内核会用 `now()` 填充 `created_at`。
- **每个不同的 commit 请求体使用新的 `Idempotency-Key`**。
- **文档约束（`file_uri` / `page.number` 稳定、`doc_metadata` 确定、完整文档）由上层保证，下层用 `check_documents` 校验**：新增约束时同步加一条检查规则。

## 约定

- Python：pydantic v2，httpx 异步；`mypy --strict` 与 ruff 必须通过
- Rust：serde，tokio，HTTP 层走 `Transport` trait（默认 reqwest）；`cargo clippy -D warnings` 与 `cargo fmt` 必须通过
- 文档与注释用中文，标识符用英文
- `examples/` 下的示例脚本必须通过文档一致性检查
