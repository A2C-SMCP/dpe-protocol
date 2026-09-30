# dpe-protocol

DPE（Document / Page / Element）投递链路的**协议层**：以 Document 为边界，把上层产出的 DPE 文档通过 **dpe-push/1** 协议增量投递到 TFRobot 的 Memory。

## 解决什么问题

让飞书、网盘、三方系统等外部内容进入 Robot Memory 时**只传变更、不再全量重传**，并把与内核逐字节一致的 hash、协商、分片、重试等协议细节统一封装，接入方只需交出一份 Document。

## 分层

### 在整体链路中的位置

```
┌──────────── 上层：connector（不在本项目）────────────┐
│ 飞书 / 网盘 / Jira / 三方系统 ...                      │
│ 鉴权 · 拉取 · 解析为 DPE · 何时推送、推送哪些              │
└─────────────────────────┬───────────────────────────┘
                          │ Document（+ 可选的源侧 fingerprint）
┌─────────────────────────▼──── 本项目：dpe-protocol ───┐
│ 保证文档被正确、增量、可靠地投递                          │
└─────────────────────────┬───────────────────────────┘
                          │ dpe-push/1（HTTP）
┌─────────────────────────▼──── TFRobotServer / 内核 ───┐
│ 协商 · 重算 hash · 写入 Memory（索引 / 学习）            │
└──────────────────────────────────────────────────────┘
```

本项目不定义 connector 的形态：上层可以定时扫描、由 webhook 触发，或者用自己的框架，只要最终交给本项目一份完整的 Document 即可。

### 本项目内部分层

自下而上，Python 与 Rust 两个 SDK 一一对应：

| 层 | 模块 | 作用 |
| --- | --- | --- |
| 测试工具 | `testing` | 内存版 Robot 服务端；文档一致性检查 `check_documents`，供上层在 CI 中使用 |
| 有状态推送 | `pusher` · `state` | `StatefulPusher`：自动携带 `base_doc_hash`，根据 fingerprint 判断能否跳过 |
| 文档约束 | `validation` · `uri` | `check_document` 静态校验；`make_dpe_uri` 生成 `dpe://{tenant}/{path}` |
| 协议客户端 | `push` | dpe-push/1：能力发现、协商、增量提交、自动分片、幂等、重试 |
| 内容寻址 | `hashing` | hash-contract-v1 三层 hash（与内核逐字节一致） |
| 数据模型 | `schema` | Document / DocPage / DocElement |

依赖只自下而上。`schema` 与 `hashing` 不依赖网络，可以单独使用。完整设计见 [docs/design.md](docs/design.md)。

## 快速上手

```python
from dpe_protocol import DPEPushClient, JsonFileStateStore, StatefulPusher, make_dpe_uri

async with DPEPushClient("https://robot.example.com", token="<JWT with dpe:push>") as client:
    pusher = StatefulPusher(client, JsonFileStateStore(".dpe-state/feishu.json"))
    for item in await feishu.list_changed():                  # 上层逻辑
        uri = make_dpe_uri("feishu-acme", f"wiki/{item.token}")
        if await pusher.needs_push(uri, item.revision):       # 内容未变则跳过拉取
            await pusher.push(await feishu.build_document(item), fingerprint=item.revision)
```

以上为示意代码（`feishu` 是虚构的上层对象），可直接运行的示例见 [`python/examples/`](python/examples/) 与 [`rust/examples/`](rust/examples/)。Rust 用法和完整说明见各 SDK 的 README。

## SDK

两个 SDK 都是 TFRobotServer `docs/protocol/dpe/push-protocol-v1.md` 与 `hash-contract-v1.md` 的符合性实现：

| SDK | 目录 | 包名 | 状态 |
| --- | --- | --- | --- |
| Python | [`python/`](python/) | `dpe-protocol`（`import dpe_protocol`） | 0.1.0-dev |
| Rust | [`rust/`](rust/) | `dpe-protocol`（`use dpe_protocol`） | 0.1.0-dev |

## 跨语言一致性

三层 hash 必须与 TFRobot 内核**逐字节**一致。为此：

- [`vectors/v1/`](vectors/v1/) 中的一致性向量**由内核生成**（[`scripts/gen_vectors.py`](scripts/gen_vectors.py)），不由任何 SDK 生成；`manifest.json` 的 `provenance` 记录生成时的内核版本
- 每个 SDK 的测试都读取同一份向量逐字节比对，覆盖 Unicode 键序、浮点 repr、控制字符转义、datetime isoformat、URL 规范化等跨语言易错点
- 运行和测试都不依赖内核，只有重新生成向量时需要内核环境
- 向量目前临时托管在本项目，计划移交内核仓库，由内核 CI 生成并发布（见 [design.md §7.1](docs/design.md)）

## 目录结构

```
docs/design.md          基础设计（分层、与上层的契约、一致性检查、分片、待对齐问题）
vectors/v1/             内核生成的 hash 一致性向量（所有 SDK 共享，临时托管）
scripts/gen_vectors.py  向量生成脚本（需内核环境，待移交内核仓库）
python/                 Python SDK（uv）
rust/                   Rust SDK（cargo）
```

## 开发

```bash
make test          # 两个 SDK 的全部测试
make lint          # ruff + mypy --strict / cargo fmt + clippy -D warnings
make format        # ruff format / cargo fmt
make vectors TFROBOT_PYTHON=<装有 tfrobot 的 python>   # 用内核重新生成向量
```

## License

MIT
