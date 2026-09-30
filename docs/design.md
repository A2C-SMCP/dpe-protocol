# dpe-protocol 基础设计

> **版本**: 0.1.0-dev · **状态**: 起草中 · **最后更新**: 2026-09

## 1. 定位与分层

dpe-protocol 是 DPE（Document / Page / Element）投递链路中的**协议层**，提供 Python / Rust 两个对等的 SDK。它以 **Document** 为边界：上层交给它一份完整、确定的 DPE 文档，它负责把文档可靠、增量地投递到指定 Robot 的 Memory。

内容从哪里来、怎么解析、何时拉取、拉哪些，都不在本项目范围内。本项目也**不定义 connector 的形态**：上层可以是定时扫描、webhook 驱动，或任何自有框架。

```
┌────────────── 上层：connector（不在本项目）──────────────┐
│ feishu / 网盘 / jira / ...（形态由上层自定）              │
│ · 鉴权、凭证、限流、分页、webhook / 定时触发                  │
│ · 拉取原始内容，解析为 DPE（自行解析或调用共享解析服务）         │
│ · 原生 ID → file_uri，保证页编号稳定、metadata 确定          │
│ · 编排：何时拉取、拉取哪些、失败如何重试                        │
└──────────────────────────┬──────────────────────────────┘
                           │ Document（+ 可选的源侧 fingerprint）
┌──────────────────────────▼──── 下层：dpe-protocol（本项目）─┐
│ schema      DPE 数据模型与校验                              │
│ hashing     hash-contract-v1 三层内容寻址 hash               │
│ push        dpe-push/1 客户端：协商、增量、分片、幂等、重试       │
│ uri         dpe://{tenant}/{path} 命名空间                   │
│ validation  文档级校验 check_document                        │
│ pusher      有状态推送器：自动 base_doc_hash、fingerprint 跳过  │
│ testing     假 Robot 服务端 + 文档一致性检查                   │
└──────────────────────────┬─────────────────────────────────┘
                           ▼  dpe-push/1（HTTP）
┌─────────────── TFRobotServer / TFRobot 内核 ───────────────┐
│ /.well-known/dpe-push · :negotiate · :commit                │
│ 内核 upsert_doc（LCS reconcile / 索引 / 学习）                  │
└────────────────────────────────────────────────────────────┘
```

**划分原则**：与某个具体系统有关的（鉴权、API、原始格式、解析、调度、编排）放上层；决定协议正确性的、与内容来源无关的放下层。

### 1.1 职责边界

| 事项 | 上层 connector | 下层 dpe-protocol |
| --- | --- | --- |
| connector 的形态、配置、装载与调度 | ✅ | — |
| 业务系统鉴权与凭证 | ✅ | — |
| 枚举、拉取原始内容、判断何时推送 | ✅ | — |
| 原始格式 → DPE 解析 | ✅（可共用解析服务） | — |
| `file_uri` 命名、页编号、metadata 的稳定性 | ✅ 负责保证 | 提供 `make_dpe_uri`，并通过文档一致性检查校验 |
| 三层 hash、manifest、协商、幂等、重试 | — | ✅ |
| 大文档分片 | — | ✅（自动） |
| 记住上次投递的 `doc_hash`、源侧 fingerprint | 可直接使用 | ✅ 由 `StatefulPusher` 提供 |
| `dpe:push` token | 负责传入 | ✅ 负责携带 |

### 1.2 权威规范

Robot 端的协议规范、服务端实现与内核 schema 由 TFRobotServer / TFRobot 负责：

- `TFRobotServer/docs/protocol/dpe/push-protocol-v1.md`：投递协议正文
- `TFRobotServer/docs/protocol/dpe/hash-contract-v1.md`：Hash 算法 wire 契约

本项目是这两份规范在源端的**符合性实现**，不另立协议。

### 1.3 与 A2C-SMCP 的关系

A2C-SMCP 在 v0.2 移除了 DPE（见 `a2c-smcp-protocol/CHANGELOG_DPE_REMOVAL.md`），理由是控制面和数据面应当分开：DPE 数据流向是「数据源 → Robot」，体量从 KB 到数百 MB，不适合走 Socket.IO 控制通道。本项目接手的就是这条数据面。

当时的规划是走 OCI Distribution + Merkle。对「投递到 Robot」这一段，本项目改为对齐之后成型的 dpe-push/1：它直接复用内核的三层 hash 和 `upsert_doc`，不需要额外部署 Registry。

## 2. 模块

| 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `schema` | DPE 三层数据模型（源端视角），枚举值与内核逐字对齐；`Document.from_kernel_dump` | — |
| `hashing` | hash-contract-v1：`concat_parts` / `digest` / `json_stable`、按 category 多态的 element hash、策略 URI 与版本分派 | schema |
| `push` | dpe-push/1 客户端：能力发现、策略协商、manifest 构造、negotiate / commit、自动分片、错误映射、重试 | hashing |
| `uri` | `make_dpe_uri`：`dpe://{tenant}/{path}` 命名空间 | — |
| `validation` | `check_document`：文档级静态校验 | schema, push |
| `state` | 每个 `file_uri` 的投递记录（fingerprint + 服务端 doc_hash） | — |
| `pusher` | `StatefulPusher`：`needs_push` / `push` / `forget` | push, state |
| `testing` | `FakeRobotServer`、文档一致性检查 `check_documents` | push, validation |

依赖只自下而上。`schema` / `hashing` 不依赖网络层，可以单独用作 `dpe-core`。

### 2.1 Python 与 Rust 的对应

| 概念 | Python（`python/`） | Rust（`rust/`） |
| --- | --- | --- |
| 数据模型 | pydantic `Document` | serde `Document`（`file_uri: url::Url`，与 pydantic `AnyUrl` 同源于 `url` crate） |
| 稳定 JSON | `json.dumps(sort_keys, ensure_ascii=False)` | 手写 `json_stable`：Python 浮点 repr、小写 `\u00xx` 转义、码点序 |
| 时间 | `datetime` + `isoformat()` | `PyDateTime`：保留墙钟与偏移，逐字节复刻 `isoformat()` |
| HTTP | httpx | `Transport` trait（默认 `ReqwestTransport`） |
| 状态文件 | `JsonFileStateStore` | `JsonFileStateStore`（同一文件格式，可互读） |
| 测试工具 | `dpe_protocol.testing` | `testing` feature |

Rust 的 `json_stable` 和时间序列化**不能**用 serde 默认实现替代。Rust 的默认输出在浮点（`1e16` 对比 Python 的 `1e+16`）、分隔符和 UTC 表示（`Z` 对比 `+00:00`）上都与内核不同，会直接导致 `doc_hash` 分叉。相关边界都已由内核向量覆盖。

## 3. 数据模型要点

源端模型与内核 `tfrobot.schema.document` 的差异都是刻意的：

| 差异 | 原因 |
| --- | --- |
| 不含任何 hash 字段（`content_hash` / `page_hash` / `doc_hash` / `seq_in_page` / `hash_strategy_uri`） | dpe-push/1 §5「第 0 条校验规则」：客户端永远不以文档字段形式提交 hash；hash 只出现在 manifest |
| 不含数据库主键（`doc_id` / `page_id` / `ele_id` / `entrance_*`） | 源端无意义；读取内核格式的产出时用 `Document.from_kernel_dump` 剔除 |
| `DocElement` 是单一类，`category` 为枚举字段 | hash 多态由 hashing 层按 category 分派，避开「dict 构造丢失子类多态」的问题（见 §9-1） |
| `DocMetadata.created_at` 默认 `None`（内核默认 `now()`） | 默认取当前时间会让同一内容每次得到不同 `doc_hash`，增量协商失效 |
| `DocMetadata` 的 datetime 序列化用 `isoformat()` | 复刻内核 `json_encoders`，带时区时为 `+00:00` 而非 pydantic 默认的 `Z` |

`DocMetadata` 的**声明字段集**直接进入 `doc_hash`（内核对 `model_dump(mode="json")` 做 `json_stable`，未赋值字段以 null 出现），所以必须和内核 `TFDocMetadata` 保持完全一致。内核增删字段时，两个 SDK 必须同步修改，并重新生成测试向量。

## 4. 与上层的契约

本项目对上层暴露的是**以 Document 为单位的 API**，不规定上层如何组织：

```python
pusher = StatefulPusher(client, JsonFileStateStore(".dpe-state/feishu.json"))

for item in await feishu.list_changed():                 # 上层逻辑：枚举、调度
    uri = make_dpe_uri("feishu-acme", f"wiki/{item.token}")
    if await pusher.needs_push(uri, item.revision):      # 源侧 fingerprint 未变则跳过拉取
        doc = await feishu.build_document(item)          # 上层逻辑：拉取、解析
        await pusher.push(doc, fingerprint=item.revision)
```

| API | 作用 |
| --- | --- |
| `DPEPushClient.push(doc, base_doc_hash)` | 无状态投递单个文档；调用方自行保存返回的 `doc_hash` |
| `StatefulPusher.needs_push(file_uri, fingerprint)` | fingerprint 与上次成功投递时一致即返回 false，调用方可跳过拉取；传 `None` 时总是返回 true，由 hash 协商兜底 |
| `StatefulPusher.push(doc, fingerprint)` | 投递，并自动带上次的 `base_doc_hash`、成功后记录新状态 |
| `StatefulPusher.forget(file_uri)` | 清除本地状态（不会删除 Robot 中的文档） |
| `check_document(doc)` | 运行时静态校验：`dpe://` 命名空间、禁用 `image_path`、页编号唯一、`file_uri` 与预期一致 |

上层交来的文档必须满足四条约束。协议层无法代为保证，只能校验，而且违反时**不会报错**，只会悄悄退化：

| 约束 | 违反的后果 |
| --- | --- |
| **`file_uri` 稳定且全局唯一**：用 `make_dpe_uri(tenant, path)` 生成 `dpe://{tenant}/{path}`，tenant 统一分配 | 同一文档被当成新文档，Robot 中出现重复；不同 connector 互相覆盖 |
| **`page.number` 稳定**：中部插页不能让后续页整体位移（可用内容指纹派生编号，或维护本地编号映射） | 页配对全部失配，退化为整篇重建与重新学习 |
| **`doc_metadata` 确定**：同一内容多次产出相同的 metadata，不要填当前时间或 mtime | 每次都被视为变更 |
| **每次都是完整文档** | 缺席的页和元素会被服务端当作删除 |

### 4.1 示例

`examples/` 下有两个示例脚本（Python 与 Rust 各一份），演示上层如何调用本项目。它们是普通脚本，不继承任何接口，也不属于库的 API：

| 示例 | 形态 |
| --- | --- |
| `push_markdown_dir` | 遍历本地 Markdown 目录，自行解析为 Document，用 `StatefulPusher` 增量投递 |
| `push_dpe_json` | 读取上游已产出的内核格式 DPE JSON，经 `from_kernel_dump` 后投递 |

两个示例本身都通过文档一致性检查（Python 的测试覆盖了这一点）。

## 5. 文档一致性检查

上层怎么组织 connector 由它自己决定，所以一致性检查也以文档为单位：上层对**同一份未修改的源数据产出两次文档**，交给 `check_documents` 即可。建议在上层 CI 中运行：

```python
first, second = await build_doc(item), await build_doc(item)          # Python: dpe_protocol.testing
(await check_documents(first, second, expected_file_uri=uri)).raise_for_issues()
```

```rust
let options = CheckOptions { expected_file_uri: Some(&uri), ..Default::default() };   // Rust: testing feature
check_documents(&first, &second, options).await.assert_ok();
```

| 规则 | 检查方式 |
| --- | --- |
| `static` | 对第一份文档跑 `check_document` |
| `file_uri` | 文档的 `file_uri` 与预期一致，且两次产出相同 |
| `deterministic` | 两次产出的 `doc_hash` 相同（专抓「metadata 写了当前时间」这类问题） |
| `push_roundtrip` | 经 `FakeRobotServer` 投递第一份，再投递第二份，必须返回 `unchanged` |

`file_uri` 全局唯一需要跨文档判断，`page.number` 稳定需要构造「中部插页」的源数据变更才能检验，这两项无法以单个文档为单位通用化，由上层在自己的测试中覆盖。

## 6. 投递流程

```
上层                  StatefulPusher / DPEPushClient                  Robot
  │ needs_push(uri, fp)     │                                          │
  │ ◀── false → 跳过拉取      │                                          │
  │ 拉取 + 解析 → Document   │                                          │
  │ ─── push(doc, fp) ────▶ │ GET /.well-known/dpe-push (TTL 缓存)       │
  │                         │ 选 hash 策略（SDK ∩ allowlist）             │
  │                         │ compute_hashes → Manifest(base_doc_hash)   │
  │                         │ POST :negotiate ────────────────────────▶ │
  │                         │ ◀──── unchanged / gap / new ───────────── │
  │                         │ unchanged → 结束                            │
  │                         │ 缺口超过上限 → 分片（§6.1）                   │
  │                         │ POST :commit (manifest + 缺口内容) ───────▶ │
  │                         │ ◀── 409 INSUFFICIENT → 补一轮              │
  │                         │ ◀── created / updated / … + doc_hash       │
  │                         │ state.put(doc_hash, fp)                    │
  │ ◀── PushResult ──────── │                                          │
```

关键行为：

- **两级短路**：fingerprint 未变时上层可以不拉取（`needs_push`）；拉取之后如果协商返回 `unchanged`，不进入 commit。
- **只传缺口**：`new` 传全部内容，`gap` 只传 `missing_content_hashes`；内容相同的 element 共享同一个 hash，只传一次。
- **快速路径**：`skip_negotiate` 直接 commit，服务端返回 409 时按其给出的缺口清单补传一轮。
- **幂等**：每个 commit 请求体使用新的 `Idempotency-Key`（请求体不同时如果复用 key，会被服务端回放成首次响应）。
- **重试**：429 / 503 按 `Retry-After` 退避，网络层错误按指数退避；两类请求都可以安全重试（negotiate 只读，commit 带幂等键）。
- **本地拦截**：`image_path`、协议版本不支持、无可用 hash 策略、单个 element 超出上限，这些情况在发请求之前直接报错。
- **权威值**：commit 返回的 `doc_hash` 是服务端重算的权威值，写入 state，下次作为 `base_doc_hash` 使用。

### 6.1 大文档分片

commit 请求体超过能力文档中的 `max_payload_bytes` 时，客户端自动做文档层分片，上层无感。`PushResult.shards` 记录实际提交次数。

push-protocol-v1 §10.3 给出的是**页前缀**方案（依次提交前 100 页、前 200 页……）。这个方案只适合首次投递：对已有文档做更新时，前缀会截掉尾部页面，服务端按「缺席即删除」把它们删掉，后续分片再重新加回来，造成重复传输和重复 LLM 学习。

本项目改为按**缺失内容**切批：

1. 按文档顺序把缺失的内容贪心切成若干批，每批加上 manifest 不超过上限。
2. 第 i 片提交的是**完整文档结构**（全部页），但每页只保留「服务端已有 + 前 i 批」的 element，顺序不变。
3. 最后一片就是完整文档。

这样每一片都是合法的完整 manifest，与协议语义一致；而且最终文档中仍存在的 element 从不被删除，首次投递和更新都适用。和前缀方案一样，中间态会真实落库，对召回可见。

单个 element 本身就超过上限时，无法在文档层拆分，直接报 `DPE_PAYLOAD_TOO_LARGE`，需要等协议 v2 的内容暂存（content staging）。

## 7. 一致性保证

**测试向量由内核生成，不由任何 SDK 生成**。`scripts/gen_vectors.py` 在装有 `tfrobot` 的环境中运行，用内核的 `DefaultHashStrategy` 计算期望值，写入 `vectors/v1/`。两个 SDK 都跑这同一份向量，它们只是被测对象。

向量覆盖 hash-contract-v1 §7.2 列出的全部类别：空值归一化、图片三通道、表格 / 公式 HTML、页内顺序、Unicode、`doc_metadata` 键序、页编号、边界情况和负例对。此外还覆盖了几类跨语言易错点：`file_uri` 规范化、各种 datetime 形态（`Z`、非 UTC 偏移、微秒、naive）、浮点 repr 边界、控制字符与引号转义、非身份 metadata。

内核升级 `algo_version` 时的流程：先生成向量（向量先红），两个 SDK 在 `hashing` 中新增分派分支，再把新策略加入支持列表。

**运行与测试不依赖内核**：SDK 只依赖常规库；向量是提交在仓库里的 JSON 文件，跑测试不需要 `tfrobot`。只有重新生成向量时需要内核环境。本项目依赖的是内核的**算法定义**（hash-contract-v1）与**标准答案**（向量），而不是内核的代码。

每次生成时，`vectors/v1/manifest.json` 的 `provenance` 会记录生成环境：`tfrobot` / `pydantic` / `pydantic-core` 版本与生成时间。当前向量对应 tfrobot 0.9.4a4、pydantic 2.11.10。内核或 pydantic 升级后，应重新生成并比对。两个 SDK 的测试都会检查这些字段存在。

### 7.1 向量的归属：临时托管，待移交内核

现状是向量和生成脚本都放在本项目中，这是**临时安排**，目的是方便起步。按 hash-contract-v1 §7「规范仓库发布测试向量，所有实现跑同一套向量」，向量的所有者应当是规范方。

| 问题 | 现状（本项目托管） | 移交后（内核托管） |
| --- | --- | --- |
| 内核改了算法或 `TFDocMetadata` 字段 | 内核测试照样通过，本项目向量悄悄过期，只能靠人记得重新生成 | 向量随内核变更一起更新，本项目拉取新版本后测试立即变红 |
| 内核无意中改动算法（重构时未升 `algo_version`） | 无人发现 | 内核自己的 CI 跑向量，当场拦截 |
| 向量的权威性 | 下游替上游维护标准答案 | 规范方负责产出 |

目标形态：

```
TFRobot 内核仓库（向量所有者）
  ├─ gen_vectors.py + vectors/v1/*.json
  └─ CI：内核自身先跑向量；算法变更时向量随之更新，按版本发布（包或 release 附件）
          │
          ▼
dpe-protocol（消费方）：按指定版本拉取向量 → Python / Rust 测试比对
```

移交步骤：

1. 与内核团队对齐归属，参见 §9.1-9。
2. 把 `scripts/gen_vectors.py` 整体搬到内核仓库。它只依赖 `tfrobot` 与标准库，不依赖本项目任何代码，可以直接使用。
3. 内核 CI 增加两个任务：用内核跑向量（防回归），以及算法变更时重新生成并发布。
4. 本项目改为按版本拉取向量，删除生成脚本；`provenance` 字段保留，用来核对拉取的版本。

移交完成前，向量继续由本项目托管，否则两个 SDK 就没有校验依据了。

## 8. 版本

| 维度 | 载体 | 本项目对应 |
| --- | --- | --- |
| 协议版本 | `application/vnd.tfrs.dpe.v1+json` | `PROTOCOL_VERSION = "1"` |
| 算法策略版本 | `hash_strategy_uri` | 支持的策略列表（当前仅 `default?algo_version=v1`） |
| SDK 版本 | `python/pyproject.toml` / `rust/Cargo.toml` | SemVer，`-dev` 表示未发布；两个 SDK 的 MAJOR.MINOR 保持一致 |

以 Document 为单位的 API（`push` / `StatefulPusher` / `check_document` / `check_documents`）是本项目对上层的契约，变更同样遵循 SemVer：不兼容变更要升 MAJOR（0.x 阶段升 MINOR）。

## 9. 待对齐问题

### 9.1 与 TFRobotServer / TFRobot

| # | 问题 | 影响 | 建议 |
| --- | --- | --- | --- |
| 1 | 服务端重建文档时，元素需要按 `category` 分派为具体类型，Image / Table / Formula 的 hash 规则才会生效 | 如果未分派，服务端重算的 content_hash 与 hash-contract-v1 不一致，图片 / 表格 / 公式元素会被拒绝（`DPE_CONTENT_HASH_MISMATCH`） | 服务端实现时按 category 分派元素，并用 `vectors/v1` 校验 |
| 2 | 内核 `doc_metadata.created_at` 缺省时取当前时间 | 服务端重建 metadata 时如果丢弃显式 null，每次 doc_hash 都不同，协商永远不会返回 `unchanged` | 服务端原样使用 manifest 中的 `doc_metadata`（SDK 已发送全量字段，包括 null） |
| 3 | 规范示例中 `file_type: "markdown"` | 内核枚举值是 `"md"`，没有 `"markdown"` | 修正 push-protocol-v1 §3 与 hash-contract-v1 §5.1 的示例 |
| 4 | manifest 不含 doc / page `keywords`、`page_metadata`、`creator_id` / `group_id` | 这些字段无法经 dpe-push/1 投递 | 确认是否需要，需要的话作为非 hash 字段加入 manifest |
| 5 | 协议没有删除语义 | 上层能发现源端删除，但无法同步到 Robot（`StatefulPusher.forget` 只清本地状态） | 推动增加 `:delete`，下层再暴露给上层 |
| 6 | `file_uri` 保留 scheme 待决策 | SDK 暂按 `dpe://{tenant}/{path}` 实现 | 跟随服务端的决策 |
| 7 | `max_payload_bytes` 未说明按压缩前还是压缩后计算 | SDK 保守地按压缩前计算（分片也按此计算） | 在规范中明确 |
| 8 | §10.3 的页前缀分片不适用于更新 | 更新时尾部页面会先删后建 | 建议规范改为推荐「按缺失内容分片」（§6.1） |
| 9 | hash 一致性向量由下游（本项目）托管 | 内核变更时向量可能悄悄过期；内核自身也没有向量回归保护 | 向量与生成脚本移交内核仓库，由内核 CI 生成、校验并发布（§7.1） |

### 9.2 与上层 connector

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | `dpe://` 的 tenant 由谁分配 | 维护一份 tenant 登记表，每个 connector 使用独立的 tenant，不得共用 |
| 2 | 多个 connector 都要解析 PDF / Office | 共用一个解析服务，归上层 |
| 3 | 两类凭证容易混淆 | 业务系统凭证由 connector 管理；`dpe:push` token 通过 SDK 客户端传入，部署文档中分开说明 |
| 4 | 元素 hash 只包含 `text`、图片通道（`image_url` / `image_base64` / `image_path` 三选一）与 `image_mime_type`、表格 / 公式的 `text_as_html`；元素的其他 metadata、keywords 只改动时不会被识别为变更 | 确认业务是否依赖这些字段的更新；如果依赖，需要推动 hash-contract 扩展，而不是在 SDK 中单方面修改 hash 规则 |

## 10. 路线图

- [x] 数据模型、hash v1、内核向量
- [x] dpe-push/1 客户端（发现 / 协商 / 投递 / 重试 / 幂等）
- [x] 以 Document 为边界的 API：`check_document`、`StatefulPusher` 与状态存储
- [x] 文档一致性检查 `check_documents`
- [x] 大文档自动分片（按缺失内容）
- [x] Rust SDK（与 Python 对等，共享内核向量）
- [ ] 对接真实 TFRobotServer（等待服务端实现 dpe-push/1）
- [ ] 删除同步（依赖协议扩展 `:delete`）
- [ ] 协议 v2 内容暂存（单个超大 element）
- [ ] 一致性向量移交内核仓库（§7.1）
