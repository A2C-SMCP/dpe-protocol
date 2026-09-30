# DPE Core v1（抽象模型与操作语义）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md)，经 Issue #3（内核输入）修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

DPE（Document / Page / Element）是把结构化文档**正确、增量、可靠**地投递到一个远端的标准协议。本文定义与传输无关的核心语义；v1 唯一的规范性传输绑定是 HTTP（[bindings/http.md](bindings/http.md)）；内容身份的计算见 [hash-contract-1.md](hash-contract-1.md)。

本文 MUST NOT 出现任何服务端实现的私有概念。鉴权、租户、ACL、学习、召回都属于服务端实现或 connector 的职责，不在协议内。

## 1. 概念模型

| 概念 | 定义 |
| --- | --- |
| **Remote** | 一个文档空间的地址，由服务端定义，协议不关心其内部结构。所有操作都相对于一个 remote。 |
| **file_uri** | 文档身份。任意绝对 URI（RFC 3986），在同一 remote 内唯一。scheme 由上游决定，协议不规定格式，不引入租户概念。比较前按 RFC 3986 §6.2.2 做语法规范化（scheme/host 小写、百分号编码大写并解码 unreserved 字符）；除此之外 MUST NOT 做任何语义规范化。 |
| **revision** | 服务端在每次成功写入时分配的不透明版本令牌。内容**或任何不进 hash 字段**（attributes、file_type 等）的变更都产生新 revision；CAS 只用它（§5）。客户端 MUST NOT 解析其内部结构。 |
| **doc_hash** | 纯内容 hash（契约 1），只用于判断内容是否变化，不承担并发控制。 |
| **Skeleton** | 文档的 hash 树：`{doc_metadata, pages: [{number, title, page_metadata, elements: ["dpe1:…"]}]}`。数组顺序即阅读顺序。metadata 是内容身份的一部分（进 hash，见契约 1 §2.1），服务端凭骨架即可重算并校验全部三层 hash。 |
| **Content object** | 元素内容，按 `content_hash` 寻址。同一文档内相同内容的元素共用同一对象，位置只记录在骨架里。 |
| **Blob** | 二进制内容（图片等），按 `sha256:<64hex>` 寻址，元素里只放引用。 |
| **Attributes** | 文档级**治理属性** `{"<ns>/<key>": <JSON 值>}`（访问控制等）。协议不定义任何具体键；服务端在 capabilities 中声明支持的键。 |
| **Staging session** | 暂存会话。由 negotiate 开启，绑定 `(file_uri, base_revision, 调用者身份)`，有过期时间；暂存内容对读接口和召回**不可见**。 |

## 2. 数据模型

### 2.1 Document

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `file_uri` | 身份 | 见 §1。不进 hash |
| `pages[]` | 内容身份 | 页数组，`number` 文档内唯一 |
| `doc_metadata` | 内容身份（源提供） | JSON 对象，进 doc_hash（经契约 1 §2.1 过滤）；时间戳为 RFC 3339 UTC 字符串，MUST NOT 携带会自行变化的默认值 |
| `file_type` | 正交属性 | 封闭枚举（§2.6），不进 hash（归属待评审，契约 1 §8-3） |
| `attributes` | 治理属性 | §2.5 |

契约 1 没有 doc title（#3 S6）。

### 2.2 Page

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `number` | 内容身份（配对身份） | 整数，文档内唯一，上游保证稳定 |
| `title` | 内容身份 | 可为 null |
| `elements[]` | 内容身份 | 数组顺序即阅读顺序 |
| `page_metadata` | 内容身份（源提供） | JSON 对象，进 page_hash（经契约 1 §2.1 过滤） |

### 2.3 Element

内容对象（按 `content_hash` 寻址）：

| 字段 | 分类 | 说明 |
| --- | --- | --- |
| `category` | 内容身份 | 封闭枚举，见契约 1 §4.1 |
| `text` | 内容身份 | 文本内容，可为 null |
| `text_as_html` | 内容身份 | 仅 `Table` / `Formula` |
| `image_blob` / `image_mime_type` | 内容身份 | 仅 `Image`；身份只认 blob（契约 1 §4.2） |
| `image_url` | 访问方式 | 仅 `Image`；MAY 投递，不进 hash |
| `metadata` | 内容身份（源提供） | JSON 对象，进 content_hash（经契约 1 §2.1 过滤，寻址坐标键被滤除） |

骨架中的元素 entry 就是 `content_hash` 字符串本身。元素级没有独立的正交属性：源提供的 metadata 属于内容身份，治理属性只在文档级（`attributes`）。

### 2.4 字段分类：按来源

规范性分类规则见契约 1 §2（按"字段由谁产生"分类，#3 S2）。每个字段 MUST 显式可归类，存在未归类字段即视为违规（plan §13-6）。落到协议报文上：

| 分类 | 语义 | 字段 |
| --- | --- | --- |
| **源提供** | 进 hash，变更即内容变更 | 页 `number`（待评审）/ `title` / 元素顺序；元素 `category` 与内容字段；doc / page / element 的 metadata（经保留键过滤） |
| **寻址坐标 / 访问方式** | 投递、落库，不进 hash | metadata 中的保留键（`page_number`、`coordinates` 等）；`image_url` |
| **治理属性** | 投递、独立落库，不进 hash，受 revision CAS 保护 | `attributes`、`file_type`（待评审） |
| **服务端衍生** | **不投递**，提交即拒绝（`DPE_VALIDATION`） | `keywords`、各类 id、`seq_in_page`、所有 hash 字段、抽取写回的字段 |

- 服务端写入的字段 MUST NOT 进入内容身份（自触发循环）。
- 不进 hash 的字段变更同样 MUST 产生新 revision 并如实落库，MUST NOT 被静默丢弃。

### 2.5 Attributes

- 键形如 `<ns>/<key>`，ns 与 key 均为 `[a-z0-9_-]+`；值为任意 JSON 值。
- 服务端在 capabilities 里声明支持的键；**未声明的键 MUST 拒绝**（`DPE_ATTRIBUTE_UNSUPPORTED`），不能静默丢弃。
- 等值比较（用于判定 `unchanged`）按 RFC 8785 JCS 规范化后的字节相等。

### 2.6 file_type

封闭枚举，作为正交属性投递（不进 hash）。取值：

`bmp` `csv` `doc` `docx` `eml` `epub` `heic` `html` `jpg` `json` `md` `msg` `ndjson` `odt` `org` `pdf` `png` `ppt` `pptx` `rst` `rtf` `tiff` `tsv` `txt` `wav` `xls` `xlsx` `xml` `zip` `java_repo` `python_repo` `javascript_repo` `typescript_repo` `unk` `empty` `tfchat` `jira_project` `jira_issue`

注意是 `md` 而不是 `markdown`。未知取值 MUST 拒绝（`DPE_VALIDATION`）。

## 3. 操作

只读操作无前置条件；写操作（commit / delete / move）受 §5 CAS 约束，且 MUST 是**单文档原子**的。协议不提供批量 commit，也不提供集合级清单同步。

| 操作 | 语义 | 写？ |
| --- | --- | --- |
| `capabilities` | 返回协议版本、接受的 hash 契约版本、限额（`max_payload_bytes`、`staging_ttl`、blob 上限与分块参数）、`content_encodings`、支持的 attributes 键、可选能力 | 否 |
| `head` / `batch_head` | 按 URI 返回 `{revision, doc_hash}`；不存在返回 null。批量版只读，不涉及原子性 | 否 |
| `get_skeleton` | 返回骨架（含 doc / page metadata）+ `file_type` + `attributes`，不含内容原文（元素 metadata 在内容对象里） | 否 |
| `list` | 按 file_uri 前缀（码点前缀匹配规范化后的 URI）分页返回 `{file_uri, revision}` | 否 |
| `negotiate` | 提交骨架，返回 `missing_content_hashes`、`missing_blobs` 与一个 `staging_session` | 否（只开会话） |
| `upload` | 向暂存会话上传内容对象或 blob。幂等；服务端 MUST 重算并校验 hash，不符返回 `DPE_HASH_MISMATCH`；blob 支持分块与断点续传 | 暂存 |
| `commit` | 提交骨架 + `file_type` + `attributes`（内容可内联或引用暂存会话）。**原子切换**：要么完整生效，要么没有任何变化 | 是 |
| `delete` | 按 URI 删除整篇文档 | 是 |
| `move` | `from_uri → to_uri` 原子改名，学习产物原样保留。前置：源满足 CAS，目标不存在（否则 `DPE_ALREADY_EXISTS`） | 是 |

### 3.1 能力协商

- 客户端 MUST 使用服务端声明接受的 hash 契约版本计算 hash；不支持时 MUST 失败（`DPE_CONTRACT_UNSUPPORTED`），MUST NOT 静默降级。
- 服务端收到未声明支持的契约版本的 hash 值 MUST 拒绝。

### 3.2 快路径与分批投递

- **快路径**：小文档 MAY 跳过 negotiate，直接内联完整内容 commit，一次往返完成。
- **大量小文档**：用 `batch_head` 比对 `doc_hash` 筛选需要推送的文档，再并发快路径提交。
- **大文档**：分批 `upload` 到暂存会话，最后一次 `commit` 原子切换。中途失败不影响召回，暂存内容过期回收。
- 单个内容对象超过 `max_payload_bytes` 时（只可能是超大文本；二进制走 blob），返回 `DPE_PAYLOAD_TOO_LARGE`，协议不提供文本切分，切分是上游的内容决策。

### 3.3 commit 语义

请求：骨架（含 doc / page metadata）、`file_type` 与 `attributes` 全量、CAS 前置条件（§5）、内联内容对象和/或 `staging_session` 引用。

- 骨架引用的每个 `content_hash` / blob MUST 能在「服务端已存 ∪ 暂存会话 ∪ 内联」中找到，否则 `DPE_MISSING_CONTENT`，本次 commit 无任何效果。
- 服务端 MUST 对每个新内容对象重算 hash（Rule 0：不信任客户端提交的 hash）。
- 服务端 MUST 按 category 实例化元素（K7），MUST NOT 全部按纯文本处理。
- 骨架（doc_hash 相等即证）与 `file_type`、`attributes` 均未变化时返回 `unchanged`，revision 不变，MUST NOT 产生学习动作。

响应：

```json
{ "status": "created|updated|unchanged",
  "revision": "…", "doc_hash": "dpe1:…",
  "delta": { "added": 3, "removed": 1, "retained": 120 } }
```

**delta 是规范定义的量**：取新旧骨架 `content_hash` **多重集**之差——`added` = 新∖旧，`removed` = 旧∖新，`retained` = 交集大小（均按多重集计）。服务端 MUST 如实回报；`created` 时旧骨架为空集，`unchanged` 时 `added = removed = 0`。一致性跑分器校验 delta 与 `get_skeleton` 读回的结果。

**delta 衡量的是传输量，不是受影响范围**（#3 S7）：

- 元素身份与位置无关（否则一次插入就连锁变化）；但**顺序属于内容身份**——由 page_hash（元素序列）与 doc_hash（页序列）体现：重排会改变 page_hash / doc_hash，即使 delta 全为 retained。
- `retained` 只代表该内容对象不需要重传，**不代表基于上下文产生的衍生物仍然有效**（例：对调"他们离婚了"与"A 与 C 再婚了"两句，delta 为 0，但指代所指已变）。
- 服务端 SHOULD 刷新"有序输入发生了变化"的上下文相关衍生物；具体机制（如统一变更集）属于服务端内部，不进协议。

协议只保证**不制造伪变更**；服务端有没有真正避免多余的重学，由其自身测试保证。

### 3.4 暂存会话

```
negotiate ──▶ open ──upload*──▶ open ──commit(引用会话)──▶ 已消费
                │                    │
                └──── TTL 过期 ──────┴──▶ 过期回收（对外从不可见）
```

- 会话绑定 `(file_uri, base_revision, 调用者身份)`，按文档隔离；三者任一不符，引用该会话的 upload / commit MUST 拒绝。
- 会话过期返回 `DPE_SESSION_EXPIRED`（重开 negotiate 即可，已传内容是否复用由服务端决定，通过 `missing_*` 如实反映）。
- upload 幂等：同一 hash 重复上传 MUST 成功且无副作用。
- commit 成功后会话即消费完毕；未引用的暂存对象随会话回收。

## 4. 删除与移动

- `delete` 受 CAS 约束（§5），成功后该 URI 上的 `head` 返回 null。
- **复活防护**：基于旧 revision 的 commit 在文档被删后只会得到 `DPE_NOT_FOUND`，由上层决定是否以 `if_absent` 重建。服务端不需要保存墓碑。
- `move` 保持 revision 语义：成功后目标 URI 上产生新 revision，源 URI 视同被删。学习产物 MUST 原样保留（不重新学习）。

## 5. 写入冲突（CAS）

| 场景 | 前置条件 | 结果 |
| --- | --- | --- |
| 新建 | `if_absent` | 已存在 → `DPE_ALREADY_EXISTS` |
| 更新 / 删除 / 移动 | `base_revision = R` | 与当前不符 → `DPE_REVISION_CONFLICT`；文档已删 → `DPE_NOT_FOUND` |
| 无前置条件 | — | **默认拒绝**：`DPE_PRECONDITION_REQUIRED` |
| 强制覆盖 | `force: true` | 无条件覆盖。服务端 MAY 对 force 要求额外权限（403） |

**重试恢复规则**（写入 SDK 的规范性行为）：commit 响应丢失后重试收到 `DPE_REVISION_CONFLICT` 时，若 `head` / `get_skeleton` 显示当前 `doc_hash` 与 `file_type`、`attributes` 和本次提交完全一致，视为提交已成功。commit / delete / move 均 MUST 携带幂等键（绑定语义见 HTTP 绑定 §5）。

前缀授权（哪个调用者能写哪些 URI）由服务端实现决定，协议只定义错误语义（`DPE_FORBIDDEN`）。

## 6. 错误码

| code | retryable | 语义 |
| --- | --- | --- |
| `DPE_VALIDATION` | 否 | 报文不合法：衍生字段、未知 file_type、blob 与 url 并存等 |
| `DPE_CONTRACT_UNSUPPORTED` | 否 | hash 契约版本不被支持 |
| `DPE_CATEGORY_UNKNOWN` | 否 | category 不在契约的封闭枚举内 |
| `DPE_ATTRIBUTE_UNSUPPORTED` | 否 | attributes 键未在 capabilities 声明 |
| `DPE_PRECONDITION_REQUIRED` | 否 | 写操作缺少 CAS 前置条件 |
| `DPE_REVISION_CONFLICT` | 否* | `base_revision` 与当前不符（*按 §5 重试恢复规则处理） |
| `DPE_ALREADY_EXISTS` | 否 | `if_absent` 冲突 / move 目标已存在 |
| `DPE_NOT_FOUND` | 否 | 文档不存在（含已删除） |
| `DPE_SESSION_EXPIRED` | 是 | 暂存会话过期或不存在（重新 negotiate） |
| `DPE_HASH_MISMATCH` | 否 | 上传内容与声明的 hash 不符 |
| `DPE_MISSING_CONTENT` | 否 | commit 引用了不可得的 content_hash / blob |
| `DPE_PAYLOAD_TOO_LARGE` | 否 | 单请求或单对象超过 `max_payload_bytes`（按压缩前计算） |
| `DPE_FORBIDDEN` | 否 | 无权限（含前缀授权、force 权限） |
| `DPE_UNAVAILABLE` | 是 | 服务端暂时不可用 / 限流（配合 Retry-After） |

## 7. 一致性要求（判据）

任何实现 MUST 满足（plan §13 摘录，作为规范性验收条款）：

1. 增量推送、删除、move 收敛后的结果与全量推送完全一致；
2. 不制造伪变更：内容未变的元素 MUST NOT 出现在 delta 的 added/removed 中；
3. 分批投递期间，读接口与召回 MUST NOT 看到中间态；
4. 不静默覆盖他人写入（默认拒绝无前置条件的写）；
5. 字段变更不被静默丢弃；
6. 契约升级不引起未变内容的重推或重学。

## 8. 待评审决策点

1. `list` 的前缀匹配定义为规范化后 URI 的码点前缀（§3），是否需要按 path 段边界匹配待评审。
2. `move` 后旧 URI 是否在 `list` / `head` 留下任何痕迹：本文选择完全不留（与 delete 一致）。
3. hash 相关的决策点集中在契约 1 §8（页号进 page_hash、保留键集合枚举、`file_type` 归属）。
