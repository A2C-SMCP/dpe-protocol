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
│ Git（官方）/ 用户自研 …          产出变更与 Document       │
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

## 0.1 北极星原则

> 由 Issue #4 评审中维护者确立，是全部规范的指导思想；规范、SDK 与任何实现与之冲突时，以本节为准。

DPE 的目标是：参考 Git 的设计理念，**极简、极速地表征世界上任何文档的内容面及其变化**。协议只处理阅读，不考虑编辑，因此把任意格式转换为便于 LLM 阅读的 DPE 结构是可行的。

- **P1 内容面**：DPE 只表达文档的内容面。编辑、治理（鉴权、ACL、租户）、抽取衍生物等状态都不属于 DPE。
- **P2 源即内容**：源给出的一切都是内容，全部进 hash，没有例外、没有过滤。范围包括正文、category、file_type、title、页序与元素序，以及三层 metadata 的全部键（含坐标、url、源页码标签）。服务端衍生物（如全局指代字典、keywords）MUST NOT 出现在任何 DPE 字段中，由服务端单独存放。
- **P3 变化即重学**：任何层级的 hash 变化都是该层内容的变化，依赖它的衍生物就应刷新。协议不为规避重学做设计，重学效率由服务端提升。元素 content_hash 不含骨架位置，只为传输去重，不是为了回避重学：类比 Git 中 blob 的 hash 不含路径，但 tree 的 hash 含。
- **P4 hash 即版本**：doc_hash 完整代表 DPE 中的文档状态，也是唯一的版本令牌与 CAS 依据，没有另设的 revision。重复提交同样的内容，结果不变，因此天然幂等。
- **P5 同级写入**：所有写入（含服务端自身的修改）都经 commit、经 CAS，同权同级，后写覆盖前写。

## 1. 规范归属与治理

- **本仓库 `A2C-SMCP/dpe-protocol` 是唯一权威**，负责：
  - 核心协议（Core）
  - HTTP 绑定
  - Hash 契约
  - 一致性向量
  - 服务端一致性跑分器
  - Connector 契约
- 内核与 TFRS 都只是**按规范实现的一方**，在各自的 CI 里跑向量和一致性跑分器。
- **要改 hash 规则，必须先改规范；一旦存在落库数据，或者已发布的包、已部署的服务依赖该契约的 hash 值（协议仓自身发布的 SDK 不算），任何 hash 规则变更都必须升契约版本，此前可在原契约内修订（实现与 SDK 同步升次版本）。**任何实现的现有行为都不能倒逼规范。
- 定稿后发布到 `doc.turingfocus.cn` 下的独立 path，具体发布策略在撰写过程中制定。**规范正文中的所有标识都不带产品或品牌名**，包括 capability 名、媒体类型、错误码、属性命名空间示例之外的文字。
  - **例外**（Issue #4 C1，维护者确认；#95 修订）：封闭枚举中的 `tfchat`（category 与 file_type）和 `jira_project` / `jira_issue`（file_type）视为开放标准的格式名，予以保留，不属于本条所禁止的品牌标识。例外仅限这三个值。#95 起 `file_type` 改为开放取值 + 推荐登记表（core §2.5），其命名规则为：推荐表移出 `tfchat`（服务端私有概念，不再登记；开放取值下它仍是语法合法的取值），`jira_project` / `jira_issue` 作为已登记例外保留（已有落库数据，改名会改变 doc_hash），`*_repo` 四个取值保留、标注新文档改用 `git_repo`；**新登记**的取值 SHOULD NOT 带产品或品牌名。`category` 仍是封闭枚举，其 `tfchat` 例外不受本 Issue 影响。
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

> **修订注记**：已按 §0.1 与 Issue #4、#6 修订——文档是三层同构 tree（根对象 / 页对象 / 内容对象，各自 `sha256(JCS)`，对应 Git 的 tree / blob）；页没有页号，页序即数组顺序（#6 推翻 #4 B2 的"按 number 升序"）；骨架按页增量传输；**revision 取消，doc_hash 即版本令牌**（§0.1 P4）；Attributes 移出 DPE 核心（§0.1 P1）；会话只绑定 `(file_uri, 调用者身份)`；根对象有可选 `title`，与页对称（撤回 #3 S6）；术语按 DPE 对齐（#30）：根对象改称**文档对象**（Document object，线上字段 `root` → `document`）、内容对象改称**元素对象**（Element object），hash 名称不变。新增校验顺序（#31，core §2.8）：多处违例时错误码跨实现唯一，含 I-JSON 前置、子 hash 逐项判定顺序与违例位置规则，并以拒绝类向量固定。**file_uri 的语法规范化已按 #39 定稿**（core §1.1）：文法写死为 RFC 3986 的 URI 产生式（scheme 必需、fragment 允许，取代含糊的"绝对 URI"）；规范化只做 §6.2.2.1 大小写与 §6.2.2.2 百分号编码（解码表示 unreserved 的三元组，其余三元组 hex 大写；scheme 与 host 中位于三元组之外的 ASCII 字母小写），不做 §6.2.2.3 点段移除、§6.2.3 基于 scheme 的规范化（不补 `/`、不去默认端口）与 §6.2.4 协议规范化（被否决的备选：按 RFC 建议对带 authority 的 URI 去点段——S3 / OSS 的 object key 中点段是字面内容，去段会把两篇不同文档合并成一个身份、commit 时相互静默覆盖，违反 §13"静默覆盖"判据）；不在 URI 字符集的输入（非 ASCII、空格、控制字符、坏百分号三元组）一律 `DPE_VALIDATION`，由上游按 UTF-8 编码（被否决的备选：放行原始非 ASCII——同一文档会出现两个身份）；规范化幂等，服务端以规范化形式作为身份，`get_skeleton` / `list` 返回与 move 的 `Content-Location` MUST 为规范化形式；move 的 `from_uri` 与 `to_uri` 规范化后相等即"目标已存在"→ `DPE_ALREADY_EXISTS`（core §5.2）。唯一实现随 `dpe-hash` 发布（`normalize_file_uri`），配 `kind: "uri"` 一致性向量；connector 的 `instance.uri_prefix` 须为规范化不动点（connector 契约 §6.3）。以 [spec/core.md](../../spec/core.md) §1、§1.1、§5.2 为准；下文保留作为历史。

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

> **修订注记**：本节中的 attributes 与 revision 已按 §0.1 取消——head / list 返回 doc_hash，commit 响应为 `{status, doc_hash, delta}`，doc_hash 未变即 unchanged。negotiate 的写授权与上传的求值顺序已按 #73 补正：negotiate 开会话前 MUST 先完成对 file_uri 的写授权（被否决的备选：不判定、commit 时才 `403`——目标文档不在去重范围时违反 §3.3 的下界）；upload 不重复判定授权，分块上传的会话可用性与「已完成」判定先于分块参数与请求体，重发已收字节得到 `DPE_VALIDATION` 并带 `DPE-Upload-Offset` 供同步（被否决的错误码备选：新增专用错误码——扩张不兼容面；复用 `DPE_PRECONDITION_FAILED`——混淆 doc_hash 的 CAS 语义），客户端 MUST 先断点查询再续传。以 [spec/core.md](../../spec/core.md) §3、§3.4 与 [spec/bindings/http.md](../../spec/bindings/http.md) §4.5–§4.7 为准。

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

> **修订注记**：已按 §0.1 与 Issue #4 修订——前置条件为 `base_hash`（doc_hash）/ `if_absent` / `force`；幂等由内容保证（同内容重复提交返回 unchanged），协议不定义幂等键；本节提到的 attributes 与 revision 已取消。写操作的授权排在只看请求本身的报文校验（I-JSON、契约声明、请求信封、对象）之后、一切依赖文档状态的判定之前（#63）：commit 的 `force` 与 move 的 `from_uri` / `to_uri` 都在请求体中，原先「授权先于 I-JSON 解析」无法严格实现；被否决的备选是只解析授权所需的字段再授权（规则会多出一组特例）。以 [spec/core.md](../../spec/core.md) §5 为准。

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

> **修订注记**：本节已被取代。先由 Issue #3 S2 改为按来源分类，再由 §0.1 P2（源即内容）收敛为：除 `file_uri` 外 DPE 的全部字段都进 hash，没有保留键；治理属性移出 DPE，服务端衍生物不进任何 DPE 字段。以 [spec/hash-contract-1.md](../../spec/hash-contract-1.md) §2 与 [spec/core.md](../../spec/core.md) §2.4 为准；下文保留作为历史。

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

> **修订注记**：hash 结构已按 #6 改为三层同构 tree，原语统一为 `sha256(JCS(对象))`，取消长度前缀拼接与页号；doc_hash 含 `file_type` 与 `doc_metadata`（#3），根对象有可选 `title`（撤回 #3 S6）；metadata 全部进 hash（§0.1 P2）；图片字节以 blob 进 hash，url 作为元素 metadata 进 hash；blob 引用通用化（#60）：元素字段改为 `blob` / `mime_type`，哪些 category 可携带由契约 1 §4.1 表决定。#60 是对契约 1 的**原位修订**，不升契约版本：当时仍在调试期，任何系统都没有落库的 dpe1 数据，唯一使用者是尚未切换的内核，owner 决定越早改越便宜。已发布的 `dpe-hash` 0.1.x 实现的是被取代的契约 1 草案，与 0.2.0 起的实现对 Image 元素算出不同的 `dpe1:` 值，不得混用（内核以 `<0.2.0` 门禁隔离）。此后按 §0.1 的判据：一旦存在落库数据，或者已发布的包、已部署的服务依赖契约 1 的 hash 值（协议仓自身的 SDK 不算），hash 规则变更必须升契约版本。以 [spec/hash-contract-1.md](../../spec/hash-contract-1.md) 为准；下文保留作为历史。

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

> **修订注记**：commit 已按 Issue #4 B5 改为 `PUT {remote}/documents?uri=`；ETag 即 doc_hash（§0.1 P4），move 的前置条件放在请求体中；错误码示例中的 `DPE_REVISION_CONFLICT` 已改名为 `DPE_PRECONDITION_FAILED`；每个请求以 `DPE-Hash-Contract` 头声明契约。以 [spec/bindings/http.md](../../spec/bindings/http.md) 为准。

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

> **修订注记**：「revision 不匹配就重新协商」改为「doc_hash 不匹配（`DPE_PRECONDITION_FAILED`）就重新读取」（§0.1 P4）。暂存路径的分层与编排已按 #49 定稿（会话协商定案）：新增 `deliver` 组合操作——自动选快路径或暂存路径，只处理 `DPE_MISSING_CONTENT` 补传（同一会话）与 `DPE_SESSION_EXPIRED` 重开两类恢复，其余错误（含 CAS 三类）原样抛出、**绝不自动 force**；`commit` 保持单次协议操作的语义，只加可选 `session` 参数（被否决的备选：commit 自动升级为「内联超限即走暂存」的全流程——投递策略混进协议原语，`DPE_PAYLOAD_TOO_LARGE` 等错误码不再如实反映服务端结论；且 blob 永远不能内联，「快路径还是暂存」还取决于 blob 是否已在服务端）。**blob 字节不进 sans-IO 核心**：核心只描述「谁的 `[offset, offset+len)` 区间」（`Request.body` 的延迟区间），字节由传输适配层取出（被否决的备选：核心直接读源——异步驱动会阻塞事件循环、违背 sans-IO 分层；仅接受 bytes——#15 的「内存不随文件大小线性增长」无法达标）。会话只作缓存，过期只信服务端信号（410 与断点查询的 `DPE-Session-Expires`），**不做本地时钟预检**（被否决的备选：本地预检——时钟偏差会提前放弃有效会话、整会话重传），失败异常带当前会话句柄供上层按 core §3.4 引用同一会话重提。以 [spec/core.md](../../spec/core.md) §3.2–§3.4 与 [spec/bindings/http.md](../../spec/bindings/http.md) §4.5–§4.7 为准。

- **两份独立实现**（Python ≥ 3.11，Rust ≥ 1.80），共享同一套向量。两份实现出现分歧，说明规范需要补正。
- 每个 SDK 内部拆成 **sans-IO 核心**（hash、协议状态机，不做任何 I/O）和**传输适配层**。
  - Python 同时提供 sync 和 async 接口。
  - Rust 核心不绑定异步运行时，默认提供 reqwest/tokio 适配。
- **SDK 里不封装任何服务端端点**：只接受 remote URL（类似 `git remote`），加一个可插拔的 `CredentialProvider`（类似 git credential helper）。
- **SDK 运行时不依赖内核**，按规范独立实现 hash，用向量逐字节校验。
- **本地状态**（StatefulPusher）**只作缓存**，**按 remote 隔离**。缓存丢失或过期时仍然必须正确：revision 不匹配就重新协商，**绝不自动 force**。源侧 fingerprint 只存在本地缓存里。
- 提供 `testing` 模块：内存版的**参考服务端**，命名中性，不叫 Robot。

## 11. Connector 契约（独立中立规范，不属于 Core）

> **修订注记**：按 Issue #2 关闭结论调整——平台不引入 connector 运行环境（不托管在 TFRS/TFRSUC），connector 由用户自行开发、自行部署；将来确有需要，托管运行作为独立产品另立。v1 只交付**官方 Git connector**（本仓 `connectors/git/`），TFRS 现有的飞书、COS 同步保持不变、不重构为 connector。下文原有的「宿主」表述保留为契约角色，不代表本平台提供宿主。运行边界（进程边界与语言无关线协议）已随 Issue #34 定稿，见 [spec/connector-contract.md](../../spec/connector-contract.md) §6；下文末条「随 `dpe-run`（M2）定稿」不再适用。其余章节（术语、产出模型、前缀绑定、配置与凭证、生命周期、错误与可观测、一致性）随 Issue #41 定稿：配置以插件随附的静态清单 `dpe-connector.json` 声明；下文「数据源凭证由宿主的密钥存储注入」落实为「清单声明凭证名，运行器解析来源引用后以环境变量注入插件，插件环境为最小白名单」（契约 §4.3）；调度不属于契约，独立运行器每次调用至多一轮（契约 §5.2）。`config_schema` 的正则子集经 Issue #80 补齐（契约 §4.1.1）：`pattern` / `patternProperties` 以 RFC 9485（I-Regexp）为底稿钉死可移植子集与结构上界，越界即 `manifest_invalid`，配 `config_schema_patterns` 一致性向量；被否决的备选：本地方言 + SHOULD（等于承认同一配置的校验结论可因实现而异）、仅要求 ECMA-262 与 RE2 双引擎可编译（可编译 ≠ 同结论：`^\w+$` 对 Unicode 输入、`a$` 对末尾换行两引擎结论即不同）。该修订同时收紧已发布 SDK 的接受集与匹配语义，按不兼容变更口径文档与两个 SDK 同步升次版本（0.3.0）。

- 类比 git remote-helper：核心投递协议完全不知道 connector 的存在。同一个 connector 既可以由用户自己部署（SDK 自带独立运行器 `dpe-run`），也可以托管在某个宿主里运行，**宿主只是这份契约的另一个运行器**。
- **运行器负责推送，插件只负责产出**：
  - connector 产出变更、Document、删除意图和移动意图。
  - 协商、暂存、CAS、状态缓存和 DPE 凭证全部由运行器掌握。
  - 插件**拿不到 robot 凭证**。
- **内容源实例绑定 URI 前缀**：创建实例时由宿主分配前缀，运行器拒绝越界的 Document，服务端的前缀授权再兜底一次。
- connector 用 JSON Schema 声明自己的配置，宿主据此渲染配置表单；数据源凭证由宿主的密钥存储注入。
- 官方 connector 以插件形式提供，v1 只交付 Git connector（以 Git 仓库为真实数据源，位于本仓 `connectors/git/`，依赖 SDK 与 `dpe-run`）；用户也可以自研、自己部署，投递到对应的 remote 即可。
- **运行边界**：Issue #2 已关闭，平台不运行租户 connector。插件与运行器之间的进程边界与线协议随 `dpe-run`（M2）定稿；设计须保证将来出现托管产品时只需要扩展，不需要重构。

## 12. 第一个实现：TFRS 与内核的改造清单

> **修订注记**：按 §0.1 调整——K2 中 doc_hash **包含** file_type 与 doc_metadata（与下表原文相反）；K3 取消 revision 列，CAS 直接比较存储的 doc_hash；K4 的 attributes 存储改为服务端自行决定 ACL，不经 DPE。内核待办见 [docs/migration/kernel-to-dpe1.md](../migration/kernel-to-dpe1.md)。

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
| S2 | ~~存量同步重构~~ | **已撤销**（Issue #2）：飞书和 COS 的现有同步保持不变，不重构为 connector |
| S3 | TFRS 实现说明 | scope `dpe:push`、remote URL 与 robot 的映射、前缀授权策略 |

**约束**：内核**不新增 diff 接口**。一致性只由内核的 upsert 保证，增量推送与全量推送收敛后的结果必须完全一致。学习的去重归内核负责，协议不实现去重。

## 13. 判据（出现任一即偏离目标）

> **修订注记**：第 6 条中「未声明的 attributes 键」随 attributes 移出 DPE 而失效；其余判据不变。

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
| **M1 规范** | core、http 绑定、hash-contract-1、connector 契约（§6 运行边界定稿于 #34，其余章节定稿于 #41）、向量；评审定稿 | 规范评审通过；向量同时由两个 SDK 原型校验通过 |
| **M2 SDK + 跑分器** | 同事按规范改造 Python 和 Rust SDK（初版代码先推到分支供参考）；实现黑盒跑分器；参考服务端通过跑分器 | 两个 SDK 通过全部向量；参考服务端跑分全部通过 |
| **M3 TFRS + 内核** | K1–K8、S1、S3 | TFRS 端点在 CI 中跑分全部通过 |
| **M4 E2E** | 官方 Git connector 走运行器 `dpe-run` | 在 test 集群上，以 Git 仓库为真实数据源，对 TFRS 端点端到端跑通增量推送、删除和移动，结果与全量推送收敛后一致 |

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
| 第 3 条 `markdown` / `md` | file_type 进 doc_hash（§0.1 P2；「正交属性」已随 §6 被取代）；取值由封闭枚举改为开放取值 + 推荐登记表（#95，core §2.5） |
| 第 4 条 keywords / page_metadata / creator_id / group_id | 按 §6 三分类处理：keywords 属于衍生字段；page_metadata 属于正交属性；creator_id / group_id 进 attributes |
| 第 5 条删除 | 纳入 v1：按 URI 删除，带 CAS |
| 第 6 条 file_uri scheme | 不做保留；任意绝对 URI，在 remote 内唯一 |
| 第 7 条 max_payload | 按压缩前计算 |
| 第 8 条 §10.3 | 废弃 |
| 第 9 条向量托管 | 见 D5 |
| Q5 tenant 分配 | 不再需要：tenant 属于服务端概念，由 remote 表达 |
| Q6 metadata 不参与 hash | 采纳，并显式归入正交属性，保证改了能落库 |

## 16. 未决项

- ~~Issue #2：connector 的运行边界与租户插件沙箱~~（已关闭：平台不引入 connector 运行环境，见 §11 修订注记）。
- ~~HTTP 绑定的具体路径和媒体类型命名~~（已定稿：路径按 http.md §1；JSON 主体一律 `application/json`，不设自定义媒体类型）。
- ~~规范的发布 path 和版本化发布流程~~（已落地：doc.turingfocus.cn/dpe，mike 多版本 + bump-my-version）。
- ~~暂存会话 TTL 的规范下限~~（已定：从最近一次成功的会话操作起算，下限 1 小时，见 core.md §3.4）。（原「`Idempotency-Key` 的去重窗口」已撤销：幂等由内容保证，协议不定义幂等键，见 §0.1 P4。）

当前无未决项。
