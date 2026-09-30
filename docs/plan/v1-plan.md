# DPE Protocol v1 计划

> 状态：评审定稿稿（CTO 评审 Issue #1 后形成）｜ 日期：2026-09-29
> 取代：Issue #1《dpe-protocol 初版设计评审》中与本文冲突的部分
> 关联：Issue #2《租户自定义 connector 的安全运行方案》

---

## 0. 目标与定位

外部内容进入知识库时**只传变更，不再全量重传**。

DPE 是**标准协议，不是 TFRS 的私有接口**。就像 Git 之于 GitHub、GitLab、CNB：任何服务端都能实现它，任何开发者都能在自己的系统里用 SDK 接入。**TFRS 端点只是第一个实现**。本版本同时覆盖协议规划和第一版实现。

**分层原则**：协议只负责「把文档正确、增量、可靠地投递到一个远端」。产品相关的决策放在两个地方：
- **Connector**：接哪些数据源、何时推送、删除哪个文档。
- **服务端实现**：鉴权、租户、ACL、学习、召回。

```
┌─ Connector（中立契约，§11）─────────────────────────────┐
│ 飞书 / COS / Jira / 用户自研 …   产出变更与 Document       │
└──────────────────────┬─────────────────────────────────┘
                       │ Document / 删除意图 / 移动意图
┌──────────────────────▼─ 运行器 + DPE SDK（§10）─────────┐
│ hash · 协商 · 暂存 · CAS · 重试 · 本地缓存 · 凭证          │
└──────────────────────┬─────────────────────────────────┘
                       │ DPE Core 语义 ← HTTP 绑定（v1 唯一规范性绑定）
┌──────────────────────▼─ 任意 DPE 服务端（TFRS 为第一个）──┐
│ 鉴权/授权 · 重算 hash · 原子切换 · 写库 · 学习（私有）       │
└────────────────────────────────────────────────────────┘
```

## 1. 规范归属与治理

- **本仓库 `A2C-SMCP/dpe-protocol` 是唯一权威**，负责：
  - 核心协议（Core）
  - HTTP 绑定
  - Hash 契约
  - 一致性向量
  - 服务端一致性跑分器
  - Connector 契约
- 内核与 TFRS 都只是**按规范实现的一方**，在各自的 CI 里跑向量和一致性跑分器。
- **要改 hash 规则，必须先改规范、升契约版本**。任何实现的现有行为都不能倒逼规范。
- 定稿后发布到 `doc.turingfocus.cn` 下的独立 path，具体发布策略在撰写过程中制定。**规范正文中的所有标识都不带产品或品牌名**，包括 capability 名、媒体类型、错误码、属性命名空间示例之外的文字。
- TFRobotServer `docs/protocol/dpe/` 下现有的 `push-protocol-v1.md` 和 `hash-contract-v1.md` 草案，由本仓库的规范**取代**。TFRS 那边改为引用本仓库，只保留「TFRS 实现说明」（scope 名称、部署、Robot 映射等）。

## 2. 规范文档结构（本仓库）

```
spec/
  core.md               # 抽象模型、操作、状态机、错误码（与传输无关）
  hash-contract-1.md    # hash 契约 1（dpe1:）
  bindings/
    http.md             # v1 唯一规范性绑定
  connector-contract.md # 中立 connector 契约（独立规范，不属于 Core）
vectors/                # 一致性向量（规范的一部分）
conformance/            # 黑盒 HTTP 跑分器
sdk/python/  sdk/rust/  # 两份独立实现
```

**版本编号**：对外从 **DPE v1 / hash 契约 1** 起步。TFRS 的 `dpe-push/1` 草案从未实现过，也没有外部客户端，所以不需要兼容。内核中已存的旧 hash 属于**内部 legacy**，由内核离线迁移（§12），线上不需要兼容。投递协议版本和 hash 契约版本是**两个独立的轴**。

---

## 3. 核心模型

> **修订注记**：骨架形态与暂存会话绑定已被 Issue #4 修订——元素 entry 为 `{hash, occurrence?}`，页按 `number` 升序阅读，会话只绑定 `(file_uri, 调用者身份)`，另有投递状态摘要 `state_hash`；Skeleton 中的 doc title 已被 #3 S6 删除。以 [spec/core.md](../../spec/core.md) §1–§3 为准。

| 概念 | 定义 |
| --- | --- |
| **Remote** | 一个文档空间的 URL，由服务端定义，协议不关心它的内部结构。对 TFRS 来说，一个 remote 就是一个 robot。所有端点都相对于 remote URL，不使用域名根路径下的 `.well-known`。 |
| **file_uri** | **文档身份**。任意绝对 URI（RFC 3986），规范化后比较，**在同一个 remote 内唯一**。scheme 由 connector 决定（`feishu://…`、`s3://…`），协议不规定格式，也不引入 tenant 概念。 |
| **revision** | 服务端在每次成功写入时分配的不透明版本令牌，类似 ETag。内容或属性的任何变更都会产生新的 revision，**CAS 只用它**。 |
| **doc_hash** | 纯内容 hash（§6），只用来判断内容有没有变，**不再承担并发控制**，也不包含 file_uri。 |
| **Skeleton（骨架）** | 文档的 hash 树：doc title，加上 pages[{number, title, elements[content_hash]}]。数组顺序就是阅读顺序。 |
| **Content object** | 元素内容，按 `content_hash` 寻址。同一篇文档内容相同的元素共用同一个对象，位置只记录在骨架里。 |
| **Blob** | 二进制内容（图片等），按 `sha256:<64hex>` 寻址，元素里只放引用。 |
| **Attributes** | 正交属性：`{"<ns>/<key>": JSON}`。协议不定义任何具体键，服务端在 capabilities 里声明自己支持哪些键。 |
| **Staging session** | 暂存会话，由 negotiate 开启，绑定到 `(file_uri, base_revision, 调用者身份)`，有过期时间，暂存期间对外不可见。 |

## 4. 核心操作

| 操作 | 语义 | 前置条件 | 写？ |
| --- | --- | --- | --- |
| `capabilities` | 返回协议版本、接受的 hash 契约版本、限额（max_payload、staging_ttl、blob 上限）、content_encodings、支持的 attributes 键、可选 capability | — | 否 |
| `head` / `batch_head` | 按 URI 返回 `{revision, doc_hash}`，文档不存在时返回 null。批量版只读，不涉及原子性 | — | 否 |
| `get_skeleton` | 返回骨架和 attributes，不带内容原文 | — | 否 |
| `list` | 按 URI 前缀分页列出 `{file_uri, revision}` | — | 否 |
| `negotiate` | 提交骨架，返回 `missing_content_hashes`、`missing_blobs` 和一个 `staging_session` | — | 否（只会开启会话） |
| `upload` | 往暂存会话里上传内容对象或 blob。幂等，服务端会校验 hash；blob 支持分块和断点续传 | 会话有效 | 暂存 |
| `commit` | 提交骨架和 attributes（可以内联内容，也可以引用暂存会话）。**原子切换**：要么完整生效，要么没有任何变化 | 见 §5 | 是 |
| `delete` | 按 URI 删除整篇文档 | 见 §5 | 是 |
| `move` | `from_uri → to_uri` 原子改名，学习产物原样保留 | 源 URI 满足 CAS，目标 URI 不存在 | 是 |

- **快路径**：小文档可以跳过 negotiate，直接内联完整内容 commit，一次往返完成。大量小文档的场景，由 `batch_head` 筛选需要推送的文档，再配合 HTTP/2 并发。**写操作始终是单文档原子的**，协议不提供批量 commit，也不提供集合级的清单同步。
- **分批投递**：超过单次请求上限的内容，分批 `upload` 到暂存会话，最后一次 `commit` 原子切换。**中途失败不影响召回**，暂存内容过期后回收。这替换了 Issue #1 的 D4 和 TFRS 草案 §10.3 的页前缀方案。
- 单个元素超过上限时，唯一的情况就是超大文本（二进制已经走 blob），返回 `DPE_PAYLOAD_TOO_LARGE`。

### commit 响应

```json
{ "status": "created|updated|unchanged",
  "revision": "…", "doc_hash": "dpe1:…",
  "delta": { "added": 3, "removed": 1, "retained": 120 } }
```

- **delta 是规范定义的量**：取新旧骨架 `content_hash` **多重集**的差，服务端 **MUST** 如实回报。
- 一致性跑分器会校验 delta 和 `get_skeleton` 读回的结果。
- 内核是否真的做到了没有重学，由内核自己的测试来保证。协议只保证**不制造伪变更**。
- 骨架和 attributes 都没变时，返回 `unchanged`，revision 不变。

## 5. 写入冲突（CAS）

> **修订注记**：重试恢复规则已被 Issue #4 B1 扩展——比较 `state_hash`（覆盖全部投递字段），并对 commit / delete / move 分别给出判定；幂等去重提升为 MUST。以 [spec/core.md](../../spec/core.md) §5 为准。

| 场景 | 请求 | 结果 |
| --- | --- | --- |
| 新建 | `if_absent` | 文档已存在时返回 `DPE_ALREADY_EXISTS` |
| 更新 | `base_revision = R` | 与当前 revision 不一致时返回 `DPE_REVISION_CONFLICT`；文档已被删除时返回 `DPE_NOT_FOUND` |
| 没有前置条件 | 两者都不带 | **默认拒绝**（`DPE_PRECONDITION_REQUIRED`） |
| 强制覆盖 | `force: true` | 无条件覆盖。服务端可以对 force 要求额外权限，返回 403 |

- **删除后的复活防护**：基于旧 revision 的提交在文档被删之后只会得到 `DPE_NOT_FOUND`，由上层决定要不要以 `if_absent` 重新创建。**服务端不需要保存墓碑**。
- **重试恢复规则**（写入 SDK）：commit 响应丢失后重试，如果收到 `REVISION_CONFLICT`，而 `head` 显示当前 `doc_hash` 和 attributes 与本次提交相同，就视为本次提交已经成功。此外，commit、delete、move 都带 `Idempotency-Key`。
- 前缀授权（哪个调用者能写哪些 URI）**由服务端实现决定**，协议只定义错误语义（403 `DPE_FORBIDDEN`）。

## 6. 字段三分类

> **修订注记**：本节已被 Issue #3 S2 取代（按「字段由谁产生」分类，源提供的 metadata 与 `file_type` 进 hash，正交属性只剩治理属性），并经 Issue #4 细化保留键分组。以 [spec/hash-contract-1.md](../../spec/hash-contract-1.md) §2 与 [spec/core.md](../../spec/core.md) §2.4 为准；下文保留作为历史。

规范为每个字段**显式归类**，存在未归类的字段即视为违规。

| 分类 | 含义 | 字段 |
| --- | --- | --- |
| **内容身份** | 进 hash，改动会触发重新学习 | 元素中按 category 规定的内容字段（§7）、页 title、页内元素顺序、文档 title |
| **正交属性** | 随报文投递，独立写库，**不重新学习**，受 revision CAS 保护 | `file_type`、`doc_metadata`（整体）、`page_metadata`、`attributes`（ACL 等，例如 `tfrs/creator_id`、`tfrs/group_id`）；元素中不属于内容的 metadata |
| **衍生字段** | 由服务端计算，**不投递** | `keywords`、各类 id、`seq_in_page`、所有 hash 字段 |

- `doc_metadata` 整体移出 hash 之后，「`created_at` 缺省取当前时间」导致永远判定不了 `unchanged` 的问题**随之消失**（Issue #1 第 2 条）。
- **未声明的 attributes 键 MUST 拒绝**（`DPE_ATTRIBUTE_UNSUPPORTED`），不能静默丢弃。
- 客户端提交的衍生字段一律拒绝，沿用 TFRS 草案的 Rule 0。

## 7. Hash 契约 1

> **修订注记**：doc_hash 的范围已被 Issue #3 修订（含 `file_type` 与经过滤的 `doc_metadata`，无 doc title），Image 身份只认 blob（#3 S4）。以 [spec/hash-contract-1.md](../../spec/hash-contract-1.md) 为准。

- **格式**：`dpe1:<64 hex>`，即完整 sha256，不截断，前缀标明契约版本。blob 使用 `sha256:<64hex>`，与 OCI 和 Git SHA-256 的惯例一致。
- **拼接**：每段前面加 4 字节大端长度前缀，再计算 sha256。null 视为空串。沿用内核的做法，并写成与语言无关的规范文本。
- **结构化值**：只在必要处使用，统一采用 **RFC 8785 JCS**；时间戳统一为 RFC 3339 UTC 字符串。
- **category 是封闭枚举**，每个契约版本都完整列出 category 表和各自的 hash_parts，遇到未知 category 直接拒绝。新增 category 需要升契约的次版本，并通过 capabilities 协商。
  - `Image`：text + 图片引用（`blob:sha256:…` 或 `url`）+ mime。**`path` 通道移出协议**，它是服务端本地路径。
  - `Table` / `Formula`：text + `text_as_html`
  - 其余 category：text
- `page_hash` = page title + 元素 content_hash 序列。
- `doc_hash` = doc title + 按 `number` 排序的 page_hash 序列。**不包含 file_uri、file_type、doc_metadata**。
- **元素身份**就是 content_hash，不包含位置。这与内核现状一致：内核也允许重复元素，用 LCS 对齐来配对。
- **向量**：覆盖每个 category、重复元素、跨页移动、Unicode、空值、JCS 边界、以及契约升级演练（同一份内容同时给出 `dpe1` 和一个假想 `dpe2` 的值）。

## 8. 契约演进

- 契约版本在能力发现阶段就完成协商，hash 值本身也带着版本标识。
- 升级时，**服务端用已存内容离线重算新 hash，元素身份保持不变**；过渡期同时接受新旧版本。
- 契约升级 **MUST NOT** 引起未变内容的重推或重学。

## 9. HTTP 绑定（v1 唯一规范性绑定）

> **修订注记**：commit 已按 Issue #4 B5 改为 `PUT {remote}/documents?uri=`（条件头作用于文档资源），move 的前置条件放在请求体中。以 [spec/bindings/http.md](../../spec/bindings/http.md) 为准。

- **路径**都相对于 remote：`GET {remote}/capabilities`、`POST {remote}/negotiate`、`PUT {remote}/staging/{sid}/objects/{hash}`、`PUT {remote}/staging/{sid}/blobs/{sha256}`（分块）、`POST {remote}/commit`、`POST {remote}/heads`、`GET {remote}/documents?uri=…`、`GET {remote}/documents?prefix=…&cursor=…`、`DELETE {remote}/documents?uri=…`、`POST {remote}/move`。具体路径由 `http.md` 定稿。
- **CAS 映射到 HTTP 原生语义**：
  - revision 对应 `ETag`
  - `base_revision` 对应 `If-Match`
  - `if_absent` 对应 `If-None-Match: *`
  - 冲突返回 `412`
  - 缺少前置条件返回 `428`
- **媒体类型**使用中性命名（不带 `tfrs`）。版本通过媒体类型参数或 capabilities 协商。
- **鉴权**：协议不定义鉴权，交给传输层处理。
  - 使用标准 `Authorization` 头，未认证返回 `401` 加 `WWW-Authenticate`，无权限返回 `403`。
  - 用 PAT、OAuth、JWT 还是 mTLS，由服务端决定。TFRS 用自己的 RS256 JWT 和 `dpe:push` scope，这属于 TFRS 的实现说明，**不写进规范**。
- **错误**：采用 RFC 9457 `application/problem+json`。
  - `type` 是规范中稳定的 URI。
  - 扩展成员有 `code`（例如 `DPE_REVISION_CONFLICT`）和 `retryable`。
  - 429 和 503 都带 `Retry-After`。
- **压缩**：请求使用 `Content-Encoding: gzip`，并规定 `max_payload_bytes` **按压缩前计算**（Issue #1 第 7 条）。
- **不暴露服务端的处理进度**，commit 成功就是协议的终点。

## 10. SDK

- **两份独立实现**（Python ≥ 3.11，Rust ≥ 1.80），共享同一套向量。两份实现出现分歧，说明规范需要补正。
- 每个 SDK 内部拆成 **sans-IO 核心**（hash、协议状态机，不做任何 I/O）和**传输适配层**。
  - Python 同时提供 sync 和 async 接口。
  - Rust 核心不绑定异步运行时，默认提供 reqwest/tokio 适配。
- **SDK 里不封装任何服务端端点**：只接受 remote URL（类似 `git remote`），加一个可插拔的 `CredentialProvider`（类似 git credential helper）。
- **SDK 运行时不依赖内核**，按规范独立实现 hash，用向量逐字节校验。
- **本地状态**（StatefulPusher）**只作缓存**，**按 remote 隔离**。缓存丢失或过期时仍然必须正确：revision 不匹配就重新协商，**绝不自动 force**。源侧 fingerprint 只存在本地缓存里。
- 提供 `testing` 模块：内存版的**参考服务端**，命名中性，不叫 Robot。

## 11. Connector 契约（独立中立规范，不属于 Core）

- 类比 git remote-helper：核心投递协议完全不知道 connector 的存在。同一个 connector 既可以由用户自己部署（SDK 自带独立运行器 `dpe-run`），也可以托管在 TFRS/TFRSUC 的宿主里运行，**宿主只是这份契约的另一个运行器**。
- **运行器负责推送，插件只负责产出**：
  - connector 产出变更、Document、删除意图和移动意图。
  - 协商、暂存、CAS、状态缓存和 DPE 凭证全部由运行器掌握。
  - 插件**拿不到 robot 凭证**。
- **内容源实例绑定 URI 前缀**：创建实例时由宿主分配前缀，运行器拒绝越界的 Document，服务端的前缀授权再兜底一次。
- connector 用 JSON Schema 声明自己的配置，宿主据此渲染配置表单；数据源凭证由宿主的密钥存储注入。
- 官方 connector 以插件形式提供（先从飞书、COS 开始），用户选择启用；用户也可以自研、自己部署，投递到对应 robot 的 remote 即可。
- **运行边界（进程边界 + 语言无关的线协议？）待定**，见 Issue #2。v1 **不开放**租户自定义 connector 在我们的环境中运行，但要确保将来开放时只需要扩展，不需要重构。

## 12. 第一个实现：TFRS 与内核的改造清单

| # | 改造 | 说明 |
| --- | --- | --- |
| K1 | 去掉「hash 策略 URI 不同就全量重建」 | `unified_memory.py:2288` 目前按字符串比较策略 URI。改为离线重算新 hash，保持元素身份和学习产物 |
| K2 | 迁移到 hash 契约 1 | `content_hash` 列扩宽（目前最长 32），加上前缀；doc_hash 移除 file_uri、file_type、doc_metadata；JCS |
| K3 | revision 列 + CAS | 每次写入（包括只改属性）都生成新的 revision |
| K4 | attributes 存储 | `creator_id` / `group_id` 映射到 `tfrs/*` 命名空间 |
| K5 | 按 URI 删除和 move | 现有的删除按 `doc_id` 进行（`base.py:1334`）；move 只改 `file_uri` |
| K6 | blob 存储 | 图片按 sha256 存储，替换 `{robot_id}/` 路径拼接 |
| K7 | 按 category 分派 | 服务端重建文档时，按 category 实例化成 Image、Table、Formula（Issue #1 第 1 条） |
| K8 | 暂存区 | 存储介质由 TFRS 决定，需要支持 TTL 回收，并按文档隔离 |
| S1 | DPE 端点 | 实现 Core 和 HTTP 绑定，在 CI 中通过跑分器 |
| S2 | 存量同步重构 | 飞书和 COS 的 Celery 同步（`add_dpe_to_robot`）改为基于 SDK 和运行器，所有写入都经过同一套协议语义 |
| S3 | TFRS 实现说明 | scope `dpe:push`、remote URL 与 robot 的映射、前缀授权策略 |

**约束**：内核**不新增 diff 接口**。一致性只由内核的 upsert 保证，增量推送与全量推送收敛后的结果必须完全一致。学习的去重归内核负责，协议不实现去重。

## 13. 判据（出现任一即偏离目标）

1. 协议或 SDK 中出现了某个服务端实现的私有概念；或者 connector 必须调用服务端的私有 API 才能完成投递、删除或移动。
2. SDK 运行时依赖内核，而不是按规范独立实现 hash、用向量逐字节校验。
3. 协议制造了伪变更：元素先删后建，或者 hash 与规范不一致，导致退化为全量。
4. 分批投递期间，召回能看到中间态。
5. 静默覆盖了他人写入的内容。
6. 存在未归类的字段，或者字段改了却被静默丢弃（包括未声明的 attributes 键）。
7. 契约升级引起未变内容的重推或重学。
8. 内核新增了 diff 接口。

## 14. 里程碑与验收

| 里程碑 | 内容 | 验收 |
| --- | --- | --- |
| **M1 规范** | core、http 绑定、hash-contract-1、connector 契约大纲、向量；评审定稿 | 规范评审通过；向量同时由两个 SDK 原型校验通过 |
| **M2 SDK + 跑分器** | 同事按规范改造 Python 和 Rust SDK（初版代码先推到分支供参考）；实现黑盒跑分器；参考服务端通过跑分器 | 两个 SDK 通过全部向量；参考服务端跑分全部通过 |
| **M3 TFRS + 内核** | K1–K8、S1、S3 | TFRS 端点在 CI 中跑分全部通过 |
| **M4 E2E** | S2；飞书 connector 走运行器 | 在 test 集群上，以飞书为真实数据源，端到端跑通增量推送、删除和移动，结果与全量推送收敛后一致 |

## 15. 对 Issue #1 的处置

| 项 | 处置 |
| --- | --- |
| 定位 | **改**：本仓库从「TFRS 规范的客户端实现」改为**规范权威** |
| D1 以 Document 为边界 | 保留。connector 契约作为独立规范另行定义（§11） |
| D2 只暴露文档级 API | 保留写操作单文档原子；新增 `batch_head`、`list`、`move`、`delete` |
| D3 独立实现 hash | 保留，向量改由规范产出 |
| D4 按缺失内容分片 | **替换**为暂存加原子切换（§4） |
| D5 向量移交内核 | **反转**：向量归本仓库，内核在自己的 CI 中消费 |
| D6 Python + Rust | 保留，两份独立实现，采用 sans-IO 结构 |
| 第 1 条 category 分派 | 服务端 MUST 分派（K7） |
| 第 2 条 created_at | 随 doc_metadata 移出 hash 而消失 |
| 第 3 条 `markdown` / `md` | file_type 改为正交属性，枚举值在规范中写明 |
| 第 4 条 keywords / page_metadata / creator_id / group_id | 按 §6 三分类处理：keywords 属于衍生字段；page_metadata 属于正交属性；creator_id / group_id 进 attributes |
| 第 5 条删除 | 纳入 v1：按 URI 删除，带 CAS |
| 第 6 条 file_uri scheme | 不做保留；任意绝对 URI，在 remote 内唯一 |
| 第 7 条 max_payload | 按压缩前计算 |
| 第 8 条 §10.3 | 废弃 |
| 第 9 条向量托管 | 见 D5 |
| Q5 tenant 分配 | 不再需要：tenant 属于服务端概念，由 remote 表达 |
| Q6 metadata 不参与 hash | 采纳，并显式归入正交属性，保证改了能落库 |

## 16. 未决项

- Issue #2：connector 的运行边界与租户插件沙箱（由 TFRSOperator、TFRS、TFRSUC 回复）。
- HTTP 绑定的具体路径和媒体类型命名（在 M1 撰写 `http.md` 时定稿）。
- 规范在 `doc.turingfocus.cn` 上的发布 path 和版本化发布流程。
- `Idempotency-Key` 的去重窗口，以及暂存会话 TTL 的规范下限。
