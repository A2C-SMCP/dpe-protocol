# DPE Core v1（抽象模型与操作语义）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md)，经 Issue #3（首个实现方输入）、Issue #4（评审）修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

DPE（Document / Page / Element）是把结构化文档**正确、增量、可靠**地投递到一个远端的标准协议。本文定义与传输无关的核心语义；v1 唯一的规范性传输绑定是 HTTP（[bindings/http.md](bindings/http.md)）；内容身份与投递状态摘要的计算见 [hash-contract-1.md](hash-contract-1.md)。

本文 MUST NOT 出现任何服务端实现的私有概念。鉴权、租户、ACL、学习、召回都属于服务端实现或 connector 的职责，不在协议内。

## 1. 概念模型

| 概念 | 定义 |
| --- | --- |
| **Remote** | 一个文档空间的地址，由服务端定义，协议不关心其内部结构。所有操作都相对于一个 remote。 |
| **file_uri** | 文档身份。任意绝对 URI（RFC 3986），在同一 remote 内唯一。scheme 由上游决定，协议不规定格式，不引入租户概念。比较前按 RFC 3986 §6.2.2 做语法规范化（scheme/host 小写、百分号编码大写并解码 unreserved 字符）；除此之外 MUST NOT 做任何语义规范化。 |
| **revision** | 服务端在每次成功写入时分配的不透明版本令牌。投递状态（§2.4 中任何投递字段）的任何变更都产生新 revision；CAS 只用它（§5）。客户端 MUST NOT 解析其内部结构。 |
| **doc_hash** | 纯内容 hash（契约 1 §5），判断内容身份是否变化，不承担并发控制。 |
| **state_hash** | 投递状态摘要（契约 1 §6）：doc_hash 加全部不进内容身份的投递字段（attributes、各位置的 occurrence）。判断"这次投递与服务端现状是否完全一致"，不承担并发控制。 |
| **Skeleton** | 文档的 hash 树：`{doc_metadata, pages: [{number, title, page_metadata, elements: [entry…]}]}`，entry 为 `{"hash": "dpe1:…", "occurrence": {…}?}`（§2.3）。页的阅读顺序是 `number` 升序；页内元素的阅读顺序是 `elements` 数组顺序。服务端凭骨架加 `file_type` 即可重算并校验 page_hash 与 doc_hash；content_hash 需要内容对象才能重算。 |
| **Content object** | 元素内容，按 `content_hash` 寻址，恰为 content_hash 的完整原像（§2.3）。同一文档内相同内容的元素共用同一对象；位置以及随位置变化的字段只记录在骨架 entry 上。 |
| **Blob** | 二进制内容（图片等），按 `sha256:<64hex>` 寻址，由内容对象引用（`image_blob`），不直接出现在骨架中。 |
| **Attributes** | 文档级**治理属性** `{"<ns>/<key>": <JSON 值>}`（访问控制等）。协议不定义任何具体键；服务端在 capabilities 中声明支持的键。 |
| **Staging session** | 暂存会话。由 negotiate 开启，绑定 `(file_uri, 调用者身份)`，有过期时间；暂存内容对读接口和召回**不可见**（§3.4）。 |

## 2. 数据模型

### 2.1 Document

| 字段 | 分类（§2.4） | 说明 |
| --- | --- | --- |
| `file_uri` | 身份 | 见 §1。不进 hash |
| `file_type` | 源提供 | 封闭枚举（§2.6），进 doc_hash |
| `pages[]` | 源提供 | 页数组，`number` 文档内唯一，MUST 按 `number` 严格升序排列 |
| `doc_metadata` | 源提供 | JSON 对象，进 doc_hash（经契约 1 §2.1 处理）；时间戳为 RFC 3339 UTC 字符串，MUST NOT 携带会自行变化的默认值 |
| `attributes` | 治理属性 | §2.5 |

契约 1 没有 doc title（#3 S6）。

### 2.2 Page

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `number` | 源提供（配对身份 + 阅读顺序） | 整数，文档内唯一，上游保证稳定。页的阅读顺序就是 `number` 升序；`pages` 数组 MUST 按 `number` 严格升序排列，否则 `DPE_VALIDATION`（数组顺序不承载独立语义） |
| `title` | 源提供 | 可为 null |
| `elements[]` | 源提供 | 骨架 entry 数组；数组顺序即页内阅读顺序 |
| `page_metadata` | 源提供 | JSON 对象，进 page_hash（经契约 1 §2.1 处理） |

### 2.3 Element

一个元素在线上拆成两部分：**内容对象**（按 content_hash 寻址、可被多个位置共享）与**骨架 entry**（每个位置一份）。

内容对象——恰为 content_hash 的完整原像，只能包含其 category 规定的字段：

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `category` | 源提供 | 封闭枚举，见契约 1 §4.1 |
| `text` | 源提供 | 文本内容，可为 null |
| `text_as_html` | 源提供 | 仅 `Table` / `Formula` |
| `image_blob` / `image_mime_type` | 源提供 | 仅 `Image`；身份只认 blob（契约 1 §4.2） |
| `metadata` | 源提供 | JSON 对象，进 content_hash（经契约 1 §2.1 处理） |

骨架 entry：

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `hash` | 身份引用 | 该位置内容对象的 `content_hash` |
| `occurrence` | 随位投递 | 可选 JSON 对象：该内容**在这个位置**出现时附带、不进 content_hash 的字段。键 MUST 属于契约 1 §2.1 的随位投递键（`coordinates`、`image_url`、`parent_id`、`related_ids`）；`image_url` 仅允许出现在引用 Image 内容对象的 entry 上。其余键 MUST 拒绝（`DPE_VALIDATION`） |

规则：

- 内容对象中出现其 category 未规定的字段（如 NarrativeText 带 `text_as_html`、任何内容对象带 `image_url`）MUST 拒绝（`DPE_VALIDATION`）——否则同一 content_hash 会对应不同字节，服务端去重时静默丢掉其一。
- 任一层 metadata（doc / page / element）中出现契约 1 §2.1 的保留键（随位投递键或不投递键）MUST 拒绝（`DPE_VALIDATION`）。随位投递键只能放在 entry 的 `occurrence` 中。
- 同一内容出现在多个位置时，各位置的 `occurrence` 分别表达、分别落库（向量 `duplicate_occurrence`）。
- 治理属性只在文档级（`attributes`）。

### 2.4 字段分类：按来源

规范性分类规则见契约 1 §2（按"字段由谁产生"分类，#3 S2）。每个字段 MUST 显式可归类，存在未归类字段即视为违规（plan §13-6）。落到协议报文上：

| 分类 | 语义 | 字段 | 线上处理 |
| --- | --- | --- | --- |
| **源提供** | 进 hash，变更即内容变更 | `file_type`；页 `number` / `title` / 元素顺序；内容对象的全部字段；doc / page / element 的 metadata | 投递、落库 |
| **随位投递**（寻址坐标 / 访问方式 / 分区关系） | 不进 hash，进 state_hash | entry `occurrence` 中的 `coordinates`、`image_url`、`parent_id`、`related_ids` | 只能经 entry `occurrence` 投递；出现在 metadata 中即拒绝 |
| **治理属性** | 不进 hash，进 state_hash，受 revision CAS 保护 | `attributes` | 投递、独立落库 |
| **不投递**（骨架位置派生 / keywords / 推送侧本地） | 不进 hash、不投递 | 保留键 `page_number`、`page_name`、`seq_in_page`、`keywords`、`image_path`、`image_base64`、`file_directory` | 出现即拒绝（`DPE_VALIDATION`） |
| **服务端衍生**（非报文字段） | 不进 hash；报文中没有它们的位置 | 服务端分配的各类 id、hash 字段、抽取过程写回的字段 | 不是线上校验规则：服务端 MUST NOT 把它们作为源提供字段对外呈现或计入 hash——读接口返回的 metadata 中不得出现（契约 1 §2.1 私有排除键）；源 metadata 中的任意键（即使名字像 id）一律按源提供处理 |

保留键的封闭集合与分组以契约 1 §2.1 为准，本表只是引用。线上拒绝**只**针对这一封闭集合，两个合规实现对同一份报文的接受 / 拒绝结果必然相同。

- 服务端写入的字段 MUST NOT 进入内容身份（自触发循环）。
- **内容身份只有一条写入通道**（#3）：源提供字段只能经 `commit` 写入；服务端实现 MUST NOT 提供绕过 commit 修改内容身份字段的途径。服务端自己作为某个 URI 的来源时（如用户直接上传的文件），同样经 commit 写入。"来源"指投递该 URI 的一方，不一定是外部 connector。
- 不进 hash 的投递字段（随位投递、治理属性）变更同样 MUST 产生新 revision 并如实落库，MUST NOT 被静默丢弃；发现与推送的路径见 §3.2、§3.3。

### 2.5 Attributes

- 键形如 `<ns>/<key>`，ns 与 key 均为 `[a-z0-9_-]+`；值为任意 JSON 值，但其中**整数字面量**（不含小数点与指数）的绝对值 MUST NOT 超过 2^53−1（与 JCS 一致，契约 1 §3.3；超大 id 请用字符串），否则 `DPE_VALIDATION`；带小数点或指数的数字按 IEEE-754 double 处理。同一约束适用于 metadata 与 occurrence。
- 服务端在 capabilities 里声明支持的键；**未声明的键 MUST 拒绝**（`DPE_ATTRIBUTE_UNSUPPORTED`），不能静默丢弃。
- 值为 null 的键与缺省等价（null 键删除是递归的，嵌套对象中的 null 键同样删除，契约 1 §2.1）；commit 时 attributes 是全量的（缺省视同 `{}`）。等值比较经 state_hash 完成（契约 1 §6）。
- **写入不区分来源**：服务端经自身入口修改 attributes（或任何不进 hash 的投递字段）与 commit 是同权同级的写入——同样产生新 revision、受同一 CAS 约束，后写覆盖前写。协议不引入按键的写入归属。由此的预期行为：来源下一轮推送时，快路径发现 state_hash 不同，会以来源的全量视图覆盖服务端所做的修改（见 §8-5）。内容身份字段不在此列，只能经 commit 写入（§2.4）。

### 2.6 file_type

封闭枚举，由源提供，进 doc_hash（#3：按来源规则不开例外）。取值：

`bmp` `csv` `doc` `docx` `eml` `epub` `heic` `html` `jpg` `json` `md` `msg` `ndjson` `odt` `org` `pdf` `png` `ppt` `pptx` `rst` `rtf` `tiff` `tsv` `txt` `wav` `xls` `xlsx` `xml` `zip` `java_repo` `python_repo` `javascript_repo` `typescript_repo` `unk` `empty` `tfchat` `jira_project` `jira_issue`

注意是 `md` 而不是 `markdown`。未知取值 MUST 拒绝（`DPE_VALIDATION`）。SDK MUST 以常量导出本枚举（同 `vectors/manifest.json` 的 `file_types`）。

## 3. 操作

只读操作无前置条件；写操作（commit / delete / move）受 §5 CAS 约束，且 MUST 是**单文档原子**的。协议不提供批量 commit，也不提供集合级清单同步。

| 操作 | 语义 | 写？ |
| --- | --- | --- |
| `capabilities` | 返回协议版本、接受的 hash 契约版本、限额（`max_payload_bytes`、`staging_ttl`、blob 上限与分块参数）、`idempotency_window_seconds`、`dedup_scope`（§3.3）、`content_encodings`、支持的 attributes 键、可选能力 | 否 |
| `head` / `batch_head` | 按 URI 返回 `{revision, doc_hash, state_hash}`；不存在返回 null。批量版只读，不涉及原子性 | 否 |
| `get_skeleton` | 返回骨架（含 doc / page metadata 与各 entry 的 occurrence）+ `file_type` + `attributes` + `revision` / `doc_hash` / `state_hash`，不含内容对象。返回的 metadata、occurrence 与 attributes MUST 与最近一次写入的值在契约 1 规范化（null 键删除）意义下相等，不含任何服务端私有键——据此重算的 doc_hash / state_hash 必须等于返回值 | 否 |
| `list` | 按 file_uri 前缀（码点前缀匹配规范化后的 URI）分页返回 `{file_uri, revision}` | 否 |
| `negotiate` | 提交骨架，返回 `missing_content_hashes`、`missing_blobs` 与一个 `staging_session` | 否（只开会话） |
| `upload` | 向暂存会话上传内容对象或 blob。幂等；服务端 MUST 校验字段（§2.3）并重算 hash，不符返回 `DPE_HASH_MISMATCH`；blob 支持分块与断点续传 | 暂存 |
| `commit` | 提交骨架 + `file_type` + `attributes`（内容可内联或引用暂存会话）。**原子切换**：要么完整生效，要么没有任何变化 | 是 |
| `delete` | 按 URI 删除整篇文档 | 是 |
| `move` | `from_uri → to_uri` 原子改名，学习产物原样保留。前置：源满足 CAS，目标不存在（否则 `DPE_ALREADY_EXISTS`） | 是 |

### 3.1 能力协商

- 客户端 MUST 使用服务端声明接受的 hash 契约版本计算 hash；不支持时 MUST 失败（`DPE_CONTRACT_UNSUPPORTED`），MUST NOT 静默降级。
- 服务端收到未声明支持的契约版本的 hash 值 MUST 拒绝。

### 3.2 快路径与分批投递

- **快路径**：小文档 MAY 跳过 negotiate，直接内联完整内容 commit，一次往返完成。
- **大量小文档**：用 `batch_head` 比对 **`state_hash`** 筛选需要推送的文档（本地算出的 state_hash 与服务端返回值不同即推送），再并发快路径提交。MUST NOT 只比对 doc_hash 筛选——否则只改随位字段或 attributes 的文档会被漏推。筛选不依赖任何本地缓存，缓存丢失时结果不变。
- **大文档**：分批 `upload` 到暂存会话，最后一次 `commit` 原子切换。中途失败不影响召回，暂存内容过期回收。
- 单个内容对象超过 `max_payload_bytes` 时（只可能是超大文本；二进制走 blob），返回 `DPE_PAYLOAD_TOO_LARGE`，协议不提供文本切分，切分是上游的内容决策。

**只改不进 hash 的字段时的路径**（以 `coordinates` 为例，`image_url`、`parent_id`、`related_ids`、attributes 同理）：本地 state_hash 变化 → `batch_head` 发现与服务端不同 → 快路径 commit（没有新内容对象，无需上传）→ 服务端判定 state_hash 变化，返回 `updated` 与新 revision，delta 全为 retained，doc_hash 不变。

### 3.3 commit 语义

请求：骨架（含 doc / page metadata 与 entry occurrence）、`file_type` 与 `attributes` 全量、CAS 前置条件（§5）、内联内容对象和/或 `staging_session` 引用。

- 骨架引用的每个 `content_hash`，以及这些内容对象引用的每个 blob，MUST 能在「去重范围内的已存内容 ∪ 暂存会话 ∪ 内联」中找到，否则 `DPE_MISSING_CONTENT`，本次 commit 无任何效果。
  - **去重范围**由 capabilities 的 `dedup_scope` 声明：`document`（仅该文档当前 revision 引用的内容，所有实现 MUST 支持的最小范围）或 `writable`（调用者有写授权的全部文档）。范围 MUST NOT 超出调用者的写授权范围；negotiate 的 `missing_*` 与 commit 的可得性判定 MUST 使用同一范围（§9）。
- 服务端 MUST 对每个新内容对象校验字段（§2.3）并重算 hash（Rule 0：不信任客户端提交的 hash）；MUST 自行计算 doc_hash 与 state_hash。
- 服务端 MUST 按 category 实例化元素，MUST NOT 全部按纯文本处理。
- **新旧 state_hash 相等时返回 `unchanged`**，revision 不变，MUST NOT 产生任何写入或学习动作。state_hash 覆盖全部投递字段，因此 unchanged 不会吞掉任何字段变更。

响应：

```json
{ "status": "created|updated|unchanged",
  "revision": "…", "doc_hash": "dpe1:…", "state_hash": "dpe1:…",
  "delta": { "added": 3, "removed": 1, "retained": 120 } }
```

**delta 是规范定义的量**：取新旧骨架 entry `hash` **多重集**之差——`added` = 新∖旧，`removed` = 旧∖新，`retained` = 交集大小（均按多重集计）。服务端 MUST 如实回报；`created` 时旧骨架为空集，`unchanged` 时 `added = removed = 0`。一致性跑分器校验 delta 与 `get_skeleton` 读回的结果。

**hash 的两种职责**（#3）：

- **变更检测**：page_hash、doc_hash、state_hash 只回答"要不要推送、要不要进一步比对"。
- **衍生物刷新**：上下文相关衍生物是否需要刷新，由元素 content_hash 与有序输入（阅读顺序）决定。page_hash、doc_hash 或 state_hash 变化**本身不构成刷新理由**。例：只改页号时 page_hash / doc_hash 变化、必须推送，但所有 content_hash 与顺序都未变，没有衍生物需要刷新；只改 occurrence 或 attributes 时同理。

**delta 衡量的是传输量，不是受影响范围**（#3 S7）：

- 元素身份与位置无关（否则一次插入就连锁变化）；但**顺序属于内容身份**——由 page_hash（元素序列）与 doc_hash（页序列）体现：重排会改变 page_hash / doc_hash，即使 delta 全为 retained。
- `retained` 只代表该内容对象不需要重传，**不代表基于上下文产生的衍生物仍然有效**（例：对调"他们离婚了"与"A 与 C 再婚了"两句，delta 为 0，但指代所指已变）。
- 服务端 SHOULD 刷新"有序输入发生了变化"的上下文相关衍生物；具体机制（如统一变更集）属于服务端内部，不进协议。

协议只保证**不制造伪变更**；服务端有没有真正避免多余的重学，由其自身测试保证。

### 3.4 暂存会话

```
negotiate ──▶ open ──upload*──▶ open ──commit 成功──▶ 已消费
                │                    │  commit 失败 → 仍为 open
                └──── TTL 过期 ──────┴──▶ 过期回收（对外从不可见）
```

- 会话绑定 `(file_uri, 调用者身份)`，按文档隔离；**不绑定 revision 或任何前置条件**。CAS 只在 commit 时按该次 commit 的前置条件裁决（§5），因此 `base_revision`、`if_absent`、`force` 三种前置条件下的会话校验完全相同：会话存在、未过期、未消费，且 file_uri 与调用者身份相符。
- 引用的会话已过期、已消费、不存在或不属于该调用者时，upload / commit 一律返回 `DPE_SESSION_EXPIRED`（不区分原因，避免泄露他人会话是否存在）；会话属于该调用者但 file_uri 不符时返回 `DPE_VALIDATION`。
- **失败的 commit（任何错误）MUST NOT 消费会话**。并发写入（例如他人修改了 attributes）导致 `DPE_REVISION_CONFLICT` 时，客户端重新读取、合并后，以新的前置条件引用**同一会话**重新 commit，已上传内容无需重传。
- 会话过期后重开 negotiate：已传内容是否仍可复用由服务端决定，通过 `missing_*` 如实反映。
- upload 幂等：同一 hash 重复上传 MUST 成功且无副作用。upload 的一切可观察结果（"新写入 / 重复"的区分、blob 断点续传的已收字节数）MUST 只按**本会话内**已收到的内容判定，与服务端其他位置是否已存该内容无关（§9）。
- commit 成功后会话即消费完毕；未引用的暂存对象随会话回收。

## 4. 删除与移动

- `delete` 受 CAS 约束（§5），成功后该 URI 上的 `head` 返回 null。
- **复活防护**：基于旧 revision 的 commit 在文档被删后只会得到 `DPE_NOT_FOUND`，由上层决定是否以 `if_absent` 重建。服务端不需要保存墓碑。
- `move` 保持 revision 语义：成功后目标 URI 上产生新 revision，源 URI 视同被删；目标的 doc_hash 与 state_hash 等于移动前源文档的值。学习产物 MUST 原样保留（不重新学习）。

## 5. 写入冲突（CAS）与重试

| 场景 | 前置条件 | 结果 |
| --- | --- | --- |
| 新建 | `if_absent` | 已存在 → `DPE_ALREADY_EXISTS` |
| 更新 / 删除 / 移动 | `base_revision = R` | 与当前不符 → `DPE_REVISION_CONFLICT`；文档已删 → `DPE_NOT_FOUND`（HTTP 映射见绑定 §3.2） |
| 无前置条件 | — | **默认拒绝**：`DPE_PRECONDITION_REQUIRED` |
| 强制覆盖 | `force: true` | 无条件覆盖。服务端 MAY 对 force 要求额外权限（`DPE_FORBIDDEN`） |

前缀授权（哪个调用者能写哪些 URI）由服务端实现决定，协议只定义错误语义（`DPE_FORBIDDEN`）。

### 5.1 幂等键

- commit / delete / move MUST 携带客户端生成的幂等键（绑定见 HTTP 绑定 §5），缺失 → `DPE_VALIDATION`。每个不同的请求使用新键；重试 MUST 复用同一键并发送**逐字节相同**的请求。
- **"重试"的边界**：重试只指传输失败（未收到响应）或收到 `retryable: 是` 之后的原样重发。收到成功或 `retryable: 否` 的错误之后再次发起（例如补传缺失内容后重新 commit），一律是**新请求**，MUST 使用新键——即使请求字节相同。
- 服务端 MUST 在 capabilities 声明的 `idempotency_window_seconds` 内对同键请求返回首次**已完成**的结果（含原 status、revision 与 delta），不再执行。"已完成"只指成功，或带错误码且 `retryable: 否` 的错误；其余结果（`retryable: 是` 的错误，以及不带错误码的服务端内部错误）MUST NOT 被记录，同键的后续请求照常执行。服务端 MUST 保证未被记录的请求没有产生任何效果：已完成结果的幂等记录 MUST 与该写操作在同一原子单元内持久化。窗口下限见 §8-4。
- 同一键配不同请求 → `DPE_IDEMPOTENCY_MISMATCH`；同一键的首个请求仍在处理中 → `DPE_IDEMPOTENCY_IN_PROGRESS`。

### 5.2 重试恢复判定（写入 SDK 的规范性行为）

幂等窗口之外（如进程崩溃后隔天恢复）重试写操作，可能因首次请求已生效而得到下列错误。SDK MUST 记录每个写请求首次发出的时间，并按下表判定，判定成立即视为该写入已成功；不成立则如实上报错误。判定一律比较 state_hash（覆盖全部投递字段），MUST NOT 只比较 doc_hash。SDK 绝不因重试失败而自动改用 force。

| 操作 | 重试时收到 | 判定为已成功的条件 |
| --- | --- | --- |
| commit（`base_revision`） | `DPE_REVISION_CONFLICT` | `head` 返回的 state_hash 等于本次提交的 state_hash |
| commit（`if_absent`） | `DPE_ALREADY_EXISTS` | 同上 |
| commit（引用 `staging_session`） | `DPE_SESSION_EXPIRED`（首次成功已消费会话） | 同上；不成立时按 §6 重新 negotiate，新请求用新键 |
| commit（`force`） | —（force 不做 CAS） | 窗口外 SDK MUST NOT 原样重放 force 请求（重放会覆盖首次提交之后他人的写入）：先 `head`，state_hash 相等即成功，否则上报，由上层决定是否重新发起 force |
| delete | `DPE_NOT_FOUND` | 恒成立（目标状态"不存在"已达成） |
| move | 源 `DPE_NOT_FOUND`，或目标 `DPE_ALREADY_EXISTS` | `head(from_uri)` 为 null，且 `head(to_uri)` 的 state_hash 等于移动前源文档的 state_hash |

为使 move 可判定，SDK 在发起 move 前 MUST 取得源文档当前的 state_hash（与 `base_revision` 同一次 `head` 读到），并与该请求的幂等键、首次发出时间一起保存到重试记录中。

判定成立时 SDK 以 `head` 读到的 revision 作为本次写入的结果；commit 的 delta 此时不可得，SDK MUST 将其标为未知，MUST NOT 伪造。若他人恰好写入了完全相同的状态，判定同样成立——协议关心的是投递状态已达成，而不是由谁达成。

## 6. 错误码

`retryable` 只表示"**原样重试**同一请求可能成功"。需要改变请求才能恢复的错误一律为 `否`，恢复动作见"恢复"列；SDK 的通用重试逻辑只依据 `retryable`。

| code | retryable | 语义 | 恢复 |
| --- | --- | --- | --- |
| `DPE_VALIDATION` | 否 | 报文不合法：保留键出现在 metadata、内容对象多余字段、未知 occurrence 键、页未按 number 升序、未知 file_type 等 | 修正报文 |
| `DPE_CONTRACT_UNSUPPORTED` | 否 | hash 契约版本不被支持 | 按 capabilities 换契约，或失败 |
| `DPE_CATEGORY_UNKNOWN` | 否 | category 不在契约的封闭枚举内 | 修正报文 |
| `DPE_ATTRIBUTE_UNSUPPORTED` | 否 | attributes 键未在 capabilities 声明 | 修正报文 |
| `DPE_PRECONDITION_REQUIRED` | 否 | 写操作缺少 CAS 前置条件 | 补前置条件 |
| `DPE_REVISION_CONFLICT` | 否 | `base_revision` 与当前不符 | 按 §5.2 判定；不成立则重新读取、合并后重新提交 |
| `DPE_ALREADY_EXISTS` | 否 | `if_absent` 冲突 / move 目标已存在 | 按 §5.2 判定；不成立交上层决定 |
| `DPE_NOT_FOUND` | 否 | 文档不存在（含已删除） | 按 §5.2 判定；commit 时交上层决定是否以 `if_absent` 重建（§4） |
| `DPE_SESSION_EXPIRED` | 否 | 暂存会话失效：过期、已消费、不存在或不属于调用者（§3.4） | 重试时先按 §5.2 判定；否则重新 negotiate，上传 `missing_*` 后重新 commit |
| `DPE_HASH_MISMATCH` | 否 | 上传内容与声明的 hash 不符 | 修正内容或 hash |
| `DPE_MISSING_CONTENT` | 否 | commit 引用了去重范围内不可得的 content_hash / blob | 上传缺失内容后重新 commit |
| `DPE_PAYLOAD_TOO_LARGE` | 否 | 单请求或单对象超过 `max_payload_bytes`（按压缩前计算） | 改走暂存分批；单对象超限由上游切分 |
| `DPE_FORBIDDEN` | 否 | 无权限（含前缀授权、force 权限） | — |
| `DPE_IDEMPOTENCY_MISMATCH` | 否 | 同一幂等键配了不同请求 | 客户端缺陷：新请求用新键 |
| `DPE_IDEMPOTENCY_IN_PROGRESS` | 是 | 同键的首个请求仍在处理 | 退避后原样重试 |
| `DPE_RATE_LIMITED` | 是 | 限流（配合 Retry-After） | 按 Retry-After 原样重试 |
| `DPE_UNAVAILABLE` | 是 | 服务端暂时不可用（配合 Retry-After） | 按 Retry-After 原样重试 |

## 7. 一致性要求（判据）

任何实现 MUST 满足（plan §13 摘录，作为规范性验收条款）：

1. 增量推送、删除、move 收敛后的结果与全量推送完全一致；
2. 不制造伪变更：内容未变的元素 MUST NOT 出现在 delta 的 added/removed 中；
3. 分批投递期间，读接口与召回 MUST NOT 看到中间态；
4. 不静默覆盖他人写入（默认拒绝无前置条件的写）；
5. 字段变更不被静默丢弃（包括不进 hash 的投递字段，§2.4）；同级写入之间的后写覆盖（§2.5）经 CAS 与新 revision 发生，不属于静默覆盖；
6. 契约升级不引起未变内容的重推或重学。

## 8. 待评审决策点

1. `list` 的前缀匹配定义为规范化后 URI 的码点前缀（§3），是否需要按 path 段边界匹配待评审。
2. `move` 后旧 URI 是否在 `list` / `head` 留下任何痕迹：本文选择完全不留（与 delete 一致）。
3. `dedup_scope` 的取值（§3.3）：本文给出 `document` / `writable` 两档，是否需要更细的服务端自定义范围待评审。
4. 幂等去重窗口下限（§5.1，plan §16 登记）：草案取 `idempotency_window_seconds ≥ 86400`。
5. **同级写入、后写覆盖**（§2.5，#4 评审中维护者已确认接受）：不进 hash 的投递字段被服务端修改后，会在来源下一轮推送时被覆盖。协议有意不引入按键的写入归属，以保持模型简单；若将来确有"服务端管理、来源不得覆盖"的字段需求，再以扩展方式引入。
6. hash 相关的决策点集中在契约 1 §8。

## 9. 安全考虑

- **跨文档探测**：按内容寻址的去重天然是一个存在性预言机。若 negotiate 的 `missing_*` 或 commit 的可得性判定覆盖整个 remote，一个只被授权写某个 URI 前缀的调用者，可以通过提交任意 hash 探测其他文档里是否存在某段内容或某个 blob，绕过前缀授权；更进一步，commit 引用一个自己从未上传的 hash 若能成功，就等于凭 hash "认领"了别人的内容。因此去重范围 MUST NOT 超出调用者的写授权范围，negotiate 与 commit MUST 使用同一范围（§3.3），范围外的内容一律视为缺失。
- **上传侧信道**：upload 的"新写入 / 重复"状态与断点续传偏移只按本会话判定（§3.4），否则上传一个猜测的内容对象即可从响应得知它是否存在于别处。
- **会话隔离**：会话绑定调用者身份；引用他人会话与引用不存在的会话返回同一错误（§3.4），不泄露会话是否存在。
- **凭证边界**：数据源凭证与 remote 凭证分属 connector 与运行器（connector 契约 §0），协议不提供由服务端代为抓取 url 的通道（契约 1 §4.2）。
