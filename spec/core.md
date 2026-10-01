# DPE Core v1（抽象模型与操作语义）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md)（尤其 §0.1 北极星原则），经 Issue #3、Issue #4 修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

DPE（Document / Page / Element）是把任意格式文档的**内容面**——面向 LLM 阅读的内容——**正确、增量、可靠**地投递到一个远端的标准协议。本文定义与传输无关的核心语义；v1 唯一的规范性传输绑定是 HTTP（[bindings/http.md](bindings/http.md)）；内容身份的计算见 [hash-contract-1.md](hash-contract-1.md)。

DPE 只表达内容（plan §0.1 P1）：不承载编辑、治理（鉴权、ACL、租户）、学习与召回的状态。这些属于服务端实现或 connector 的职责，本文 MUST NOT 出现任何服务端实现的私有概念。

## 1. 概念模型

| 概念 | 定义 |
| --- | --- |
| **Remote** | 一个文档空间的地址，由服务端定义，协议不关心其内部结构。所有操作都相对于一个 remote。 |
| **file_uri** | 文档身份。任意绝对 URI（RFC 3986），在同一 remote 内唯一。scheme 由上游决定，协议不规定格式，不引入租户概念。比较前按 RFC 3986 §6.2.2 做语法规范化（scheme/host 小写、百分号编码大写并解码 unreserved 字符）；除此之外 MUST NOT 做任何语义规范化。 |
| **doc_hash** | 文档的内容身份（契约 1 §5）。DPE 中文档的全部状态都进 doc_hash，因此它同时是文档的**版本令牌**：判断是否变化、CAS 前置条件（§5）都只用它（plan §0.1 P4）。作用域是单个文档，remote 不存在全局版本。 |
| **Skeleton** | 文档的 hash 树：`{doc_metadata, pages: [{number, title, page_metadata, elements: ["dpe1:…"]}]}`，元素 entry 是内容对象的 `content_hash`。页的阅读顺序是 `number` 升序；页内元素的阅读顺序是 `elements` 数组顺序。服务端凭骨架加 `file_type` 即可重算并校验 page_hash 与 doc_hash；content_hash 需要内容对象才能重算。 |
| **Content object** | 元素内容，按 `content_hash` 寻址，恰为 content_hash 的完整原像（§2.3）。内容完全相同的元素共用同一对象。 |
| **Blob** | 二进制内容（图片等），按 `sha256:<64hex>` 寻址，由内容对象引用（`image_blob`），不直接出现在骨架中。blob 的存储方式由服务端实现决定。 |
| **Staging session** | 暂存会话。由 negotiate 开启，绑定 `(file_uri, 调用者身份)`，有过期时间；暂存内容对读接口和召回**不可见**（§3.4）。 |

## 2. 数据模型

### 2.1 Document

| 字段 | 说明 |
| --- | --- |
| `file_uri` | 身份，见 §1。不进 hash |
| `file_type` | 封闭枚举（§2.5），进 doc_hash |
| `pages[]` | 页数组，`number` 文档内唯一，MUST 按 `number` 严格升序排列 |
| `doc_metadata` | JSON 对象，进 doc_hash |

契约 1 没有 doc title（#3 S6）。

### 2.2 Page

| 字段 | 说明 |
| --- | --- |
| `number` | 整数，文档内唯一，取值范围见 §2.6（安全整数）。页的阅读顺序就是 `number` 升序；`pages` 数组 MUST 按 `number` 严格升序排列，否则 `DPE_VALIDATION`（数组顺序不承载独立语义） |
| `title` | 可为 null |
| `elements[]` | 元素 `content_hash` 数组；数组顺序即页内阅读顺序 |
| `page_metadata` | JSON 对象，进 page_hash |

### 2.3 Element（内容对象）

内容对象恰为 content_hash 的完整原像，只能包含其 category 规定的字段：

| 字段 | 说明 |
| --- | --- |
| `category` | 封闭枚举，见契约 1 §4.1 |
| `text` | 文本内容，可为 null |
| `text_as_html` | 仅 `Table` / `Formula` |
| `image_blob` / `image_mime_type` | 仅 `Image`；blob 为字节的引用（契约 1 §4.2） |
| `metadata` | JSON 对象，进 content_hash。版面坐标、图片 url 等源提供的信息都放在这里 |

- 内容对象中出现其 category 未规定的字段（如 NarrativeText 带 `text_as_html`）MUST 拒绝（`DPE_VALIDATION`）——否则同一 content_hash 会对应不同字节，服务端去重时静默丢掉其一。
- 元素身份不含其在骨架中的位置：位置由骨架表达（page_hash / doc_hash 体现），content_hash 只为传输去重（plan §0.1 P3，类比 Git 的 blob 与 tree）。

### 2.4 字段原则

**源即内容**（plan §0.1 P2）：DPE 的每个字段都由源提供，除身份 `file_uri` 外全部进 hash，没有例外、没有过滤。每个字段 MUST 显式属于本文数据模型；未定义的字段 MUST 拒绝（`DPE_VALIDATION`）。

- **服务端衍生物不进 DPE**：keywords、各类服务端 id、抽取过程写回的字段（如全局指代字典）等由服务端计算的数据，MUST NOT 写入任何 DPE 字段（含 metadata）。它们属于服务端自己的存储，读接口也 MUST NOT 把它们作为 DPE 字段返回。
- **源字段不得带会自行变化的默认值**（如 `created_at` 缺省取当前时间）：值由源提供，源给不出就留空。
- **易变字段不应进入 metadata**：不随内容变化、却会频繁变化的源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）SHOULD NOT 放进任何 metadata——它们一旦进 hash，每次同步都会制造内容变化。
- **不重复骨架已表达的信息**：metadata SHOULD NOT 重复骨架已表达的位置信息（如元素 metadata 中的页码、页内序号）。这不改变"进 hash"的规则，只是代价说明：这类键会让插入一页时后续元素的 content_hash 连锁变化，内容对象全部重传，退化为全量传输。
- **治理不属于 DPE**：访问控制等治理属性不是内容，协议不承载；服务端依据调用者身份或自身配置决定（plan §0.1 P1）。
- **所有写入同级，只经 commit**（plan §0.1 P5）：DPE 字段只能经 commit 写入；服务端实现 MUST NOT 提供绕过 commit 修改 DPE 字段的途径。服务端自己修改内容（包括作为某个 URI 的来源，如用户直接上传的文件）同样经 commit、同样受 CAS 约束。来源与服务端的写入同权同级，后写覆盖前写。

### 2.5 file_type

封闭枚举，由源提供，进 doc_hash。取值：

`bmp` `csv` `doc` `docx` `eml` `epub` `heic` `html` `jpg` `json` `md` `msg` `ndjson` `odt` `org` `pdf` `png` `ppt` `pptx` `rst` `rtf` `tiff` `tsv` `txt` `wav` `xls` `xlsx` `xml` `zip` `java_repo` `python_repo` `javascript_repo` `typescript_repo` `unk` `empty` `tfchat` `jira_project` `jira_issue`

注意是 `md` 而不是 `markdown`。未知取值 MUST 拒绝（`DPE_VALIDATION`）。SDK MUST 以常量导出本枚举（同 `vectors/manifest.json` 的 `file_types`）。

### 2.6 数值

整数字面量（不含小数点与指数）的绝对值 MUST NOT 超过 2^53−1（与 JCS 一致，契约 1 §3.3），否则 `DPE_VALIDATION`。该范围统一适用于页 `number` 与 metadata 中的整数（#6 F4）——否则不同语言的实现会在此分歧（任意精度整数 / i64 / 丢失精度的 double）。超大 id 请用字符串。metadata 中带小数点或指数的数字按 IEEE-754 double 处理。

### 2.7 内容等价

hash 定义了"同一内容"：两份输入的 doc_hash 相等，即为同一内容。契约 1 下的等价关系恰好包括：metadata 中值为 null 的键与缺省等价；段级 null 与空串等价（如页 title 为 null 与 `""`）；JSON 数字按 JCS 规范化（如 `1.0` 与 `1`）。等价关系之外的任何差异都是内容变化。服务端读回时 MAY 返回等价类中的任一表示，SHOULD 返回最近一次写入的原样表示。协议不做 Unicode 或 URL 规范化，这类差异都是内容变化。

## 3. 操作

只读操作无前置条件；写操作（commit / delete / move）受 §5 CAS 约束，且 MUST 是**单文档原子**的。协议不提供批量 commit，也不提供集合级清单同步。

**授权先于一切**（#6 F3）：所有写操作 MUST 先完成授权判定，再做任何依赖文档状态的判定或响应；move 对源与目标 URI 都要判定，任一侧无写授权即 `DPE_FORBIDDEN`。因此对无写授权的 URI，delete / move / commit 无论文档是否存在、内容是否相同，一律得到 `DPE_FORBIDDEN`（探测面见 §9）。

| 操作 | 语义 | 写？ |
| --- | --- | --- |
| `capabilities` | 返回协议版本、接受的 hash 契约版本、限额（`max_payload_bytes`、`staging_ttl`、blob 上限与分块参数）、`dedup_scope`（§3.3）、`content_encodings`、可选能力 | 否 |
| `head` / `batch_head` | 按 URI 返回 `{doc_hash}`；不存在返回 null。批量版只读，不涉及原子性 | 否 |
| `get_skeleton` | 返回 `doc_hash`、`file_type` 与骨架（含 doc / page metadata），不含内容对象。返回值 MUST 与最近一次写入的值**内容等价**（§2.7）——据此重算的 doc_hash 必须等于返回值 | 否 |
| `list` | 按 file_uri 前缀（码点前缀匹配规范化后的 URI）分页返回 `{file_uri, doc_hash}` | 否 |
| `negotiate` | 提交骨架，返回 `missing_content_hashes`、`missing_blobs` 与一个 `staging_session`；骨架超限时可不带骨架只开会话（§3.2 大骨架） | 否（只开会话） |
| `upload` | 向暂存会话上传内容对象、blob 或**页骨架片段**（#6 F2）。幂等；内容对象 MUST 校验字段（§2.3）并重算 hash，不符返回 `DPE_HASH_MISMATCH`；blob 支持分块与断点续传；页片段按 `number` 幂等替换，响应给出该页的 `missing_*` | 暂存 |
| `commit` | 提交 `file_type` + 骨架（内容可内联或引用暂存会话）。**原子切换**：要么完整生效，要么没有任何变化 | 是 |
| `delete` | 按 URI 删除整篇文档 | 是 |
| `move` | `from_uri → to_uri` 原子改名，学习产物原样保留。前置：源满足 CAS，目标不存在（否则 `DPE_ALREADY_EXISTS`） | 是 |

### 3.1 能力协商

- 客户端 MUST 使用服务端声明接受的 hash 契约版本计算 hash；不支持时 MUST 失败（`DPE_CONTRACT_UNSUPPORTED`），MUST NOT 静默降级。
- 服务端收到未声明支持的契约版本的 hash 值 MUST 拒绝。
- **每个请求声明所用契约**（HTTP 绑定 §3.1）。读接口（head / batch_head / get_skeleton / list）MUST 按请求声明的契约返回 hash 值；服务端同时支持多个契约时，MUST 能对每个受支持契约给出已存内容的 hash（契约 1 §6 原位重算）。这样契约升级期间，仍用旧契约的客户端比对的是同一契约下的值，不会把未变内容误判为变化。

### 3.2 快路径与分批投递

- **快路径**：小文档 MAY 跳过 negotiate，直接内联完整内容 commit，一次往返完成。
- **大量小文档**：用 `batch_head` 比对 doc_hash 筛选需要推送的文档（本地算出的 doc_hash 与服务端返回值不同即推送），再并发快路径提交。筛选不依赖任何本地缓存，缓存丢失时结果不变。DPE 的全部字段都进 doc_hash，因此任何字段的变化都会被这一步发现。
- **大文档**：分批 `upload` 到暂存会话，最后一次 `commit` 原子切换。中途失败不影响召回，暂存内容过期回收。
- **大骨架**（#6 F2）：骨架本身超过 `max_payload_bytes` 时（每个元素 entry 约 72 字节，8 MiB 限额下约 11 万元素即超限），negotiate 与 commit 都无法内联携带。路径：negotiate 不带骨架只开会话 → 逐页 `upload` 页骨架片段（每页的响应即该页的 `missing_*`，等价于分页的 negotiate）→ 上传缺失内容 → 以**会话骨架**形式 commit（§3.3）。因此对任意页数的文档协议都能完成 commit；**单页片段**超过 `max_payload_bytes` 时返回 `DPE_PAYLOAD_TOO_LARGE`——页是分批的最小单位，单页超限（约 11 万元素一页）如何拆分是上游的内容决策，与超大文本同理。
- 单个内容对象超过 `max_payload_bytes` 时（只可能是超大文本；二进制走 blob），返回 `DPE_PAYLOAD_TOO_LARGE`，协议不提供文本切分，切分是上游的内容决策。

### 3.3 commit 语义

请求：`file_type`、骨架、CAS 前置条件（§5）、内联内容对象和/或 `staging_session` 引用。骨架二选一（同时出现或都缺 → `DPE_VALIDATION`）：

- **内联骨架**：请求体直接携带骨架（含 doc / page metadata）；
- **会话骨架**（#6 F2，大骨架路径）：请求体携带 `doc_metadata` 与**本次提交的目标 `doc_hash`**，页片段取自引用的暂存会话（§3.4）。目标 doc_hash 由客户端按同一契约算出——客户端本来就为 negotiate 算过全部 hash。

服务端 MUST 按以下顺序求值，前一步失败即返回，后续步骤不执行（每个错误码只属于唯一一步）：

1. **授权**（§3，先于一切）：调用者对该 URI 的写授权；带 `force` 时还包括 force 权限 → `DPE_FORBIDDEN`。
2. **报文校验**（§2）→ `DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` / `DPE_CONTRACT_UNSUPPORTED`。内联内容对象在这一步完成**字段校验与 content_hash 重算**（Rule 0：不信任客户端提交的 hash；#6 F5）——重算只依赖请求体本身：字段不合法 → `DPE_VALIDATION`；重算出的 hash 未被内联骨架引用 → `DPE_VALIDATION`（多余对象）。
3. **前置条件存在性**：既无 `base_hash`、`if_absent` 也无 `force` → `DPE_PRECONDITION_REQUIRED`（即使内容未变也拒绝，保持"写操作必须带前置条件"的形式要求）。
4. **unchanged**：内联骨架时由骨架与 `file_type` 算出本次提交的 doc_hash（不需要内容对象）；会话骨架时直接取请求声明的目标 doc_hash。当前 doc_hash 已等于它时返回 `unchanged`，**不论前置条件是否满足**（§5.2），MUST NOT 产生任何写入或学习动作，也不检查、不消费所引用的暂存会话，不处理内联对象（它们已通过第 2 步校验，但不入库）。会话骨架的 commit 因此在会话被消费后原样重试仍得到 `unchanged`。
5. **前置条件求值**（§5.1）→ `DPE_PRECONDITION_FAILED` / `DPE_ALREADY_EXISTS` / `DPE_NOT_FOUND`。
6. **会话与可得性**：引用的暂存会话须有效（§3.4，否则 `DPE_SESSION_EXPIRED`；会话属于调用者但 file_uri 不符的 `DPE_VALIDATION` 也在这一步判定）。会话骨架时，由会话中的页片段（按 `number` 升序）与请求体的 `doc_metadata`、`file_type` 装配骨架并重算 doc_hash，与声明的目标 doc_hash 不符 → `DPE_HASH_MISMATCH`。骨架引用的每个 `content_hash`，以及这些内容对象引用的每个 blob，MUST 能在「去重范围内的已存内容 ∪ 暂存会话 ∪ 内联」中找到，否则 `DPE_MISSING_CONTENT`，本次 commit 无任何效果。
  - **去重范围**由 capabilities 的 `dedup_scope` 声明：`document`（仅该文档当前状态引用的内容，所有实现 MUST 支持的最小范围）或 `writable`（调用者有写授权的全部文档）。范围 MUST NOT 超出调用者的写授权范围；negotiate 的 `missing_*` 与 commit 的可得性判定 MUST 使用同一范围（§9）。
- 服务端 MUST 按 category 实例化元素，MUST NOT 全部按纯文本处理。

响应：

```json
{ "status": "created|updated|unchanged",
  "doc_hash": "dpe1:…",
  "delta": { "added": 3, "removed": 1, "retained": 120 } }
```

**delta 是规范定义的量**：取新旧骨架元素 `content_hash` **多重集**之差——`added` = 新∖旧，`removed` = 旧∖新，`retained` = 交集大小（均按多重集计）。服务端 MUST 如实回报；`created` 时旧骨架为空集，`unchanged` 时 `added = removed = 0`。一致性跑分器校验 delta 与 `get_skeleton` 读回的结果。

**变化即重学**（plan §0.1 P3）：

- 任何层级的 hash 变化都是该层内容的变化：content_hash 变化是元素内容变了，page_hash 变化是页的内容（页号、title、page_metadata 或元素序列）变了，doc_hash 变化是文档的内容（file_type、doc_metadata 或页序列）变了。服务端 SHOULD 刷新依赖变化层级的衍生物；刷新的效率由服务端自己解决，协议不为规避重学做任何设计。
- 顺序属于内容：页序列、页内元素序列的变化会改变 page_hash / doc_hash，即使 delta 全为 retained。例：对调"他们离婚了"与"A 与 C 再婚了"两句，delta 为 0，但页的内容已变，基于上下文的衍生物需要刷新。
- **delta 衡量的是传输量**：`retained` 只代表该内容对象不需要重传，不代表依赖它的衍生物仍然有效。

协议只保证**不制造伪变更**（内容未变的元素不会出现在 added / removed 中）；服务端是否高效地完成了重学，由其自身测试保证。

### 3.4 暂存会话

```
negotiate ──▶ open ──upload*──▶ open ──commit 成功──▶ 已消费
                │                    │  commit 失败 → 仍为 open
                └──── TTL 过期 ──────┴──▶ 过期回收（对外从不可见）
```

- 会话绑定 `(file_uri, 调用者身份)`，按文档隔离；**不绑定任何前置条件**。CAS 只在 commit 时按该次 commit 的前置条件裁决（§5），因此各种前置条件下的会话校验完全相同：会话存在、未过期、未消费，且 file_uri 与调用者身份相符。
- 引用的会话已过期、已消费、不存在或不属于该调用者时，upload / commit 一律返回 `DPE_SESSION_EXPIRED`（不区分原因，避免泄露他人会话是否存在）；会话属于该调用者但 file_uri 不符时返回 `DPE_VALIDATION`。
- **失败的 commit（任何错误）MUST NOT 消费会话**。并发写入导致 `DPE_PRECONDITION_FAILED` 时，客户端重新读取后，以新的前置条件引用**同一会话**重新 commit，已上传内容无需重传。
- 会话过期后重开 negotiate：已传内容是否仍可复用由服务端决定，通过 `missing_*` 如实反映。
- 会话内可暂存三种东西：内容对象、blob、**页骨架片段**（#6 F2）。页片段按 `number` 寻址、幂等替换（重传同页覆盖前值）；片段未被最终的会话骨架 commit 使用时随会话回收。
- upload 幂等：同一 hash 重复上传 MUST 成功且无副作用。upload 的一切可观察结果（"新写入 / 重复"的区分、blob 断点续传的已收字节数、页片段的 `missing_*`）MUST 只按**本会话内**已收到的内容与去重范围判定，与服务端其他位置是否已存该内容无关（§9；页片段的 `missing_*` 按 `dedup_scope ∪ 本会话` 计算，与 negotiate 一致）。
- commit 以 `created` / `updated` 成功后会话即消费完毕；`unchanged` 不消费会话（会话随 TTL 回收）。未引用的暂存对象随会话回收。

## 4. 删除与移动

- `delete` 受 CAS 约束（§5），成功后该 URI 上的 `head` 返回 null。
- **复活防护**：基于旧 doc_hash 的 commit 在文档被删后只会得到 `DPE_NOT_FOUND`，由上层决定是否以 `if_absent` 重建。服务端不需要保存墓碑。
- `move` 成功后目标 URI 的 doc_hash 等于移动前源文档的 doc_hash（file_uri 不进 hash），源 URI 视同被删。学习产物 MUST 原样保留（不重新学习）。move MUST 带 `base_hash`，不支持 force。

## 5. 写入冲突（CAS）与重试

### 5.1 前置条件

| 场景 | 前置条件 | 结果 |
| --- | --- | --- |
| 新建 | `if_absent` | 已存在 → `DPE_ALREADY_EXISTS` |
| 更新 / 删除 / 移动 | `base_hash = H`（读到的 doc_hash） | 当前 doc_hash 不等于 H → `DPE_PRECONDITION_FAILED`；文档已删 → `DPE_NOT_FOUND` |
| 无前置条件 | — | **默认拒绝**：`DPE_PRECONDITION_REQUIRED` |
| 强制覆盖 | `force: true`（仅 commit） | 无条件覆盖。服务端 MAY 对 force 要求额外权限（`DPE_FORBIDDEN`） |

- **ABA 无害**：文档从 A 变为 B 又变回 A 后，基于 A 的写入成立。协议不承载历史，当前状态就是写入者读到的状态，这次写入不会丢失任何人的内容。删除后以相同内容重建的文档同理：基于旧 A 的写入会作用于新建的文档，协议对学习产物的延续不作承诺。
- **契约升级期**：服务端同时接受多个契约时，MUST 把当前内容在任一受支持契约下的 doc_hash 都视为与 `base_hash` 匹配（契约 1 §6）。
- 前缀授权（哪个调用者能写哪些 URI）由服务端实现决定，协议只定义错误语义（`DPE_FORBIDDEN`）。move 要求调用者对源与目标 URI 都有写授权。

### 5.2 幂等由内容保证

写入的结果状态就是它提交的内容，因此协议不需要幂等键：

- **commit**：当前 doc_hash 已等于提交内容时一律返回 `unchanged`（§3.3）。响应丢失后原样重试，首次已生效则得到 `unchanged`，未生效则正常执行。
- **move**：求值顺序为：
  1. **授权**（§3，先于一切）：调用者对源与目标 URI 的写授权 → `DPE_FORBIDDEN`。
  2. 报文校验（含 `base_hash` 存在性 → `DPE_PRECONDITION_REQUIRED`）。
  3. 源不存在时：若目标存在且其 doc_hash 等于 `base_hash`，返回成功（与首次成功的响应相同）；否则 `DPE_NOT_FOUND`。
  4. 源存在时：doc_hash 不等于 `base_hash` → `DPE_PRECONDITION_FAILED`；目标已存在 → `DPE_ALREADY_EXISTS`；否则执行移动。

  第 3 步是"目标状态已达成即成功"：服务端不区分重试与首次请求，因此若源恰好已被他人删除、而目标恰好是另一份内容相同的文档，也返回成功——此时协议只保证状态（源不存在、目标内容为 H），不保证目标的学习产物来自源。首次 move 成功后目标又被他人改写或删除的，重试得到 `DPE_NOT_FOUND`，SDK 如实上报，由上层重新读取后决定。
- **delete**：求值顺序为：授权（§3，先于一切）→ 报文校验（含前置条件存在性）→ 前置条件求值（§5.1）→ 执行。响应丢失后重试得到 `DPE_NOT_FOUND` 时，SDK MUST 视为成功（目标状态"不存在"已达成）。
- **force commit**：重放 force 会覆盖首次提交之后他人的写入。响应丢失后 SDK MUST NOT 自动重放：先 `head`，doc_hash 等于提交内容即成功，否则上报，由上层决定是否重新发起。

重试后得到 `DPE_PRECONDITION_FAILED`，说明首次写入之后（或之前）有他人写入。按同级写入规则（§2.4），SDK 如实上报，由上层重新读取后决定；SDK 绝不自动改用 force。commit 恢复为 `unchanged` 时，首次写入的 delta 不可得，这是预期行为。

## 6. 错误码

`retryable` 只表示"**原样重试**同一请求可能成功"。需要改变请求才能恢复的错误一律为 `否`，恢复动作见"恢复"列；SDK 的通用重试逻辑只依据 `retryable`。

| code | retryable | 语义 | 恢复 |
| --- | --- | --- | --- |
| `DPE_VALIDATION` | 否 | 报文不合法：未定义字段、内容对象多余字段、页未按 number 升序、未知 file_type、整数越界等 | 修正报文 |
| `DPE_CONTRACT_UNSUPPORTED` | 否 | hash 契约版本不被支持 | 按 capabilities 换契约，或失败 |
| `DPE_CATEGORY_UNKNOWN` | 否 | category 不在契约的封闭枚举内 | 修正报文 |
| `DPE_PRECONDITION_REQUIRED` | 否 | 写操作缺少 CAS 前置条件 | 补前置条件 |
| `DPE_PRECONDITION_FAILED` | 否 | 当前 doc_hash 与 `base_hash` 不符 | 重新读取后由上层决定（§5.2） |
| `DPE_ALREADY_EXISTS` | 否 | `if_absent` 冲突 / move 目标已存在 | 交上层决定 |
| `DPE_NOT_FOUND` | 否 | 文档不存在（含已删除） | delete 重试视为成功（§5.2）；commit 时交上层决定是否以 `if_absent` 重建（§4） |
| `DPE_SESSION_EXPIRED` | 否 | 暂存会话失效：过期、已消费、不存在或不属于调用者（§3.4） | 重新 negotiate，上传 `missing_*` 后重新 commit |
| `DPE_HASH_MISMATCH` | 否 | 上传内容与声明的 hash 不符；会话骨架 commit 装配重算的 doc_hash 与声明的目标值不符（§3.3 第 6 步）。内联对象没有声明 hash（由服务端重算得出），不产生此错误 | 修正内容或 hash |
| `DPE_MISSING_CONTENT` | 否 | commit 引用了去重范围内不可得的 content_hash / blob | 上传缺失内容后重新 commit |
| `DPE_PAYLOAD_TOO_LARGE` | 否 | 单请求或单对象超过 `max_payload_bytes`（按压缩前计算） | 改走暂存分批；单对象超限由上游切分 |
| `DPE_FORBIDDEN` | 否 | 无权限（含前缀授权、force 权限） | — |
| `DPE_RATE_LIMITED` | 是 | 限流（配合 Retry-After） | 按 Retry-After 原样重试 |
| `DPE_UNAVAILABLE` | 是 | 服务端暂时不可用（配合 Retry-After） | 按 Retry-After 原样重试 |

## 7. 一致性要求（判据）

任何实现 MUST 满足（plan §13 摘录，作为规范性验收条款）：

1. 增量推送、删除、move 收敛后的结果与全量推送完全一致；
2. 不制造伪变更：内容未变的元素 MUST NOT 出现在 delta 的 added/removed 中；
3. 分批投递期间，读接口与召回 MUST NOT 看到中间态；
4. 不静默覆盖他人写入：默认拒绝无前置条件的写；同级写入之间的后写覆盖经 CAS 发生，不属于静默覆盖；
5. 字段变更不被静默丢弃：DPE 的全部字段都进 doc_hash，内容等价（§2.7）之外的任何变化都会被发现并落库；
6. 契约升级不引起未变内容的重推或重学。

## 8. 待评审决策点

1. `list` 的前缀匹配定义为规范化后 URI 的码点前缀（§3），是否需要按 path 段边界匹配待评审。
2. `move` 后旧 URI 是否在 `list` / `head` 留下任何痕迹：本文选择完全不留（与 delete 一致）。
3. `dedup_scope` 的取值（§3.3）：本文给出 `document` / `writable` 两档，是否需要更细的服务端自定义范围待评审。
4. **不定义幂等键**（§5.2，#4 评审中维护者确认）：幂等由内容保证，原 plan §16 中「幂等键去重窗口」一项随之撤销。
5. **治理属性移出 DPE**（§2.4，#4 评审中维护者确认）：将来若需要把数据源的文档权限同步到远端，以不进内容核心的扩展方式引入。
6. **file_type 枚举中的 `tfchat` / `jira_project` / `jira_issue`**（§2.5，#4 C1 维护者确认）：视为开放标准的格式名保留，属 plan §1 命名规则的登记例外；新增枚举值仍不得带产品或品牌名。
7. hash 相关的决策点集中在契约 1 §7。

## 9. 安全考虑

- **跨文档探测**：按内容寻址的去重天然是一个存在性预言机。若 negotiate 的 `missing_*` 或 commit 的可得性判定覆盖整个 remote，一个只被授权写某个 URI 前缀的调用者，可以通过提交任意 hash 探测其他文档里是否存在某段内容或某个 blob，绕过前缀授权；更进一步，commit 引用一个自己从未上传的 hash 若能成功，就等于凭 hash "认领"了别人的内容。因此去重范围 MUST NOT 超出调用者的写授权范围，negotiate 与 commit MUST 使用同一范围（§3.3），范围外的内容一律视为缺失。
- **上传侧信道**：upload 的"新写入 / 重复"状态与断点续传偏移只按本会话判定（§3.4），否则上传一个猜测的内容对象即可从响应得知它是否存在于别处。
- **会话隔离**：会话绑定调用者身份；引用他人会话与引用不存在的会话返回同一错误（§3.4），不泄露会话是否存在。
- **unchanged 不是探测口**：§3.3 的 unchanged 判定只比较目标文档自身的 doc_hash，调用者对该 URI 有写授权才会走到这一步。
- **delete / move 不是探测口**（#6 F3）：授权先于一切状态判定（§3）。若先判存在性再判授权，`DPE_NOT_FOUND` / `DPE_FORBIDDEN` 的差异会泄露文档是否存在；move 的"目标已达成"判定（§5.2 第 3 步）若先于授权执行，无目标写权限的调用者可借 `base_hash` 探测目标是否存在、内容是否为某值。
- **凭证边界**：数据源凭证与 remote 凭证分属 connector 与运行器（connector 契约 §0），协议不提供由服务端代为抓取 url 的通道（契约 1 §4.2）。
