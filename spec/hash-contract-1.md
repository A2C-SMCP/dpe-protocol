# DPE Hash 契约 1（`dpe1`）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §7–§8，经 Issue #3（首个实现方输入）、Issue #4（评审）修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

Hash 契约定义**内容身份**与**投递状态摘要**的计算方式：给定一篇文档，任何合规实现都必须逐字节算出相同的 `content_hash` / `page_hash` / `doc_hash` / `state_hash`。契约版本与投递协议版本是两个独立的轴；本文是契约 **1**，值前缀为 `dpe1:`。

**hash 只由契约版本和 category 决定**：不存在按文件类型或其他条件选择的 hash 策略（#3 S9）。

一致性向量（[vectors/](../vectors/)）是本契约的组成部分：实现 MUST 通过全部向量，向量与本文冲突时视为规范缺陷，按治理流程修订（见 CLAUDE.md）。

## 1. 值格式

| 类型 | 格式 | 说明 |
| --- | --- | --- |
| `content_hash` / `page_hash` / `doc_hash` / `state_hash` | `dpe1:<64 lowercase hex>` | 完整 SHA-256，不截断，前缀标明契约版本 |
| blob 引用 | `sha256:<64 lowercase hex>` | 对 blob 原始字节的 SHA-256，与 OCI digest 惯例一致 |

- hex MUST 为小写。带前缀的完整字符串才是 hash 值；比较、存储、传输 MUST 携带前缀。
- 实现 MUST 拒绝无前缀或前缀未知的 hash 值（`DPE_CONTRACT_UNSUPPORTED`）。

## 2. 字段分类：按来源

字段按**由谁产生**分类（#3 S2，取代 v1 计划 §6 的三分类），每个字段 MUST 显式可归入一类：

| 类别 | 判定 | 进 hash | 投递 |
| --- | --- | --- | --- |
| **源提供** | 正文、category、`file_type`、页 number / title、doc / page / element 的 metadata——凡由 connector 或数据源给出的 | **进**（content / page / doc_hash） | 是 |
| **随位投递** | 该内容在某个位置出现时附带的字段：寻址坐标（版面坐标）、访问方式（图片 url；标识不等于访问方式）、分区关系（解析器生成的 parent / related id） | 不进内容 hash，进 state_hash | 是，只经骨架 entry 的 `occurrence`（core.md §2.3） |
| **治理属性** | 访问控制等（creator / group 类） | 不进内容 hash，进 state_hash | 是，走 attributes + revision CAS（core.md §2.5） |
| **不投递** | §2.1 封闭集合中的不投递键：可由骨架位置派生的字段（页码、页名、页内序号）、`keywords`、推送侧本地信息（本地路径、目录、未转成 blob 的 base64） | 不进 | **否**（出现即拒绝） |
| **服务端衍生**（非报文字段） | 服务端计算或写回的其余字段（各类 id、hash 字段、抽取过程写回的字段、服务端私有键） | 不进 | 报文中没有它们的位置；不是线上校验规则，约束落在服务端：不得作为源字段对外呈现或计入 hash（§2.1 私有排除键） |

规范性规则：

- **服务端写入的字段 MUST NOT 进入内容身份**（否则形成"写回 → hash 变 → 重算 → 再写回"的自触发循环）。其逆否也成立：内容身份字段只能经 commit 写入（core.md §2.4）。
- **源提供的字段 MUST NOT 携带会自行变化的默认值**（如 `created_at` 缺省取当前时间）：值由源提供，源给不出就留空。
- **易变字段不应进入 metadata**：不随内容变化而变化、却会频繁变化的源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）SHOULD NOT 放进任何 metadata——它们一旦进 hash，每次同步都会让元素被判为变化并重新学习。真正随内容一起变化的时间（如源文档的修改时间）不在此列。
- 相对顺序（页按 number 升序的序列、页内元素序列）**属于内容身份**，由 page_hash / doc_hash 的序列结构体现；版面坐标等绝对位置不属于。

### 2.1 保留键与 metadata 的 hash 输入

**保留键集合**是规范的一部分（封闭集合），SDK MUST 以常量导出（同 `vectors/manifest.json` 的 `occurrence_keys` / `undelivered_keys` / `reserved_metadata_keys`），实现 MUST NOT 自行维护副本。契约 1 的保留键分两组，每个键只属于一组：

```
随位投递键（occurrence_keys）：coordinates, image_url, parent_id, related_ids
不投递键（undelivered_keys）：  page_number, page_name, seq_in_page, keywords,
                               image_path, image_base64, file_directory
保留键全集 = 两组的并集
```

两组在**线上报文**中的处理是唯一的（core.md §2.3）：随位投递键只能出现在骨架 entry 的 `occurrence` 里，出现在任何 metadata 中 MUST 拒绝；不投递键出现在任何位置 MUST 拒绝。线上不存在"过滤后接收"。

**服务端私有排除键**：服务端实现 MAY 在重算已存数据时额外排除一组私有键，但其中 MUST 只包含**服务端自己写入的键**，MUST NOT 排除任何源提供的键——否则同一份骨架在不同服务端会算出不同的 hash。私有键 MAY 与源 metadata 存在同一存储结构中，但 MUST NOT 出现在任何读接口返回的 metadata 中（core.md §3 `get_skeleton`），也不进任何 hash；若源提供了与某私有键同名的键，以源为准：该键按源提供进 hash 并原样读回，服务端 MUST 把自己的数据另行存放，MUST NOT 覆盖源值。一致性跑分器据此检查：服务端回报的 doc_hash 必须等于按本契约对推送内容算出的值。

doc / page / element 各有一个 metadata JSON 对象（源提供，键值自由）。进入 hash 前依次做两步：

1. **过滤保留键**（顶层键名匹配保留键全集）。线上报文按上文不会携带保留键；这一步让内部把随位字段与 metadata 存在一处的实现（例如服务端存储模型、connector 透传的解析器元素字典）直接对原始 metadata 算出同一个 hash，而无需先拆分数据结构（向量 `metadata_reserved_and_null`）。
2. **递归删除值为 null 的键**（#3 S5）：对象中值为 null 的键 MUST 在 JCS 之前删除，即**缺省 ≡ null**。否则实现方给模型新增一个可选字段，所有存量 hash 都会变化，等于一次隐性契约升级。数组元素不受此规则影响（`[null]` 原样保留）。

处理后的对象经 RFC 8785 JCS（§3.3）序列化为 UTF-8 字节，记作 `meta(m)`。metadata 缺省视同空对象：`meta(null) = meta({}) = "{}"` 的字节。

### 2.2 汇总

```
element:  category、按 §4 category 规定的内容字段、metadata（经 §2.1）
page:     number、title、元素序列、metadata（经 §2.1）
document: file_type、页序列（number 升序）、doc_metadata（经 §2.1）
state:    doc_hash、attributes、各位置的 occurrence（§6）
```

`file_uri` 不参与任何 hash。契约 1 **没有 doc title**（#3 S6：不为多数实现给不出的悬空字段留位；确有需要由后续契约引入）。

## 3. 基本原语

### 3.1 长度前缀拼接

```
seg(b)      = len(b) 的 4 字节大端无符号整数 || b        （b 为字节串，len < 2^32）
concat(P…)  = seg(P1) || seg(P2) || … || seg(Pn)
H(P…)       = SHA-256(concat(P…))
```

长度前缀消除 `["ab","c"]` 与 `["a","bc"]` 的歧义。空段（`len=0`）也 MUST 写出前缀 `00 00 00 00`。**段级 null 按空串处理**（§3.2），与 §2.1 的"metadata 对象内 null 键删除"是两条不同层面的规则。

### 3.2 文本段

```
text(s) = s 的 UTF-8 编码；s 为 null 时取空字节串
```

null 与空串产出相同字节。实现 MUST NOT 对 null 做隐式字符串化（如 `"None"` / `"null"`）。

**hash 的是投递的字节，不是语义等价物**（#3 S5）：字符串按收到的原样进入 hash，协议不做 Unicode 规范化（NFC/NFD）、URL 规范化 / percent-encode 等任何改写。实现侧的序列化框架若会自动改写（如 URL 类型自动编码），MUST 在进 hash 前还原为原始输入。

### 3.3 结构化值（RFC 8785 JCS）

metadata 进入 hash（§2.1），JCS 是契约 1 的**必需**组成部分。凡 JSON 结构化值进入 hash 或做规范化比较之处，MUST 使用 [RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785) 序列化后的 UTF-8 字节。要点：

- 对象键按 UTF-16 码元序排序；
- 数字按 ECMAScript `Number::toString` 序列化（整数不带小数点，指数不带前导零）；
- **整数字面量**（不含小数点与指数）绝对值超过 2^53−1 时 MUST 拒绝；带小数点或指数的数字按 IEEE-754 double 解析后序列化（向量 `jcs_numbers`）；
- 时间戳一律为 RFC 3339 UTC 字符串（`Z` 结尾），作为字符串参与 JCS，不做时区换算之外的改写。

JCS 实现的正确性由 `kind: "jcs"` 向量单独校验。

## 4. `content_hash`

```
content_hash = "dpe1:" + hex( H( text(category), part1, …, partN, meta(metadata) ) )
```

首段恒为 category 的字符串值（#3 S3：以什么语义角色呈现属于内容本身；`Title("Foo")` 与 `NarrativeText("Foo")` 是不同内容，category 改动不得被静默丢弃）。末段恒为元素 metadata（经 §2.1）。

category 是**封闭枚举**；遇到本契约未列出的 category，实现 MUST 拒绝（`DPE_CATEGORY_UNKNOWN`），MUST NOT 退化为 text-only hash。内容对象恰为本节公式的完整原像：出现其 category 未规定的字段 MUST 拒绝（core.md §2.3）。

### 4.1 category 表与内容字段（part1…partN）

| category | 内容字段（按序） |
| --- | --- |
| `Image` | `text(text)`、`text(blob_ref)`、`text(image_mime_type)` |
| `Table`、`Formula` | `text(text)`、`text(text_as_html)` |
| 其余（见下） | `text(text)` |

text-only category 全集：`UncategorizedText`、`CheckBox`、`CompositeElement`、`FigureCaption`、`NarrativeText`、`ListItem`、`Title`、`Address`、`EmailAddress`、`PageBreak`、`TableChunk`、`Header`、`Footer`、`CodeSnippet`、`PageNumber`、`FormKeysValues`、`tfchat`。

`FormKeysValues` 的键值对如由源随元素 metadata 提供，则经 §2.1 自动进入 hash，无需专门的内容字段。

新增 category 需要升契约次版本，并通过 capabilities 协商（§7）。

### 4.2 Image：身份只认 blob

```
blob_ref = "blob:" + blob 引用       （如 "blob:sha256:ab…"）
         | null                      （纯占位图片元素）
```

**图片的内容身份只认 blob hash**（#3 S4）：url 是访问方式，不是身份——同一张图换一个 CDN 地址不是内容变化。规则：

- **字节由持有数据源凭证的一方提供**：只有 url 的图片，由产出方（connector，或直接调用 SDK 的上游）用自己的数据源凭证取回字节交给推送侧；推送侧（SDK / 运行器）只负责计算 `sha256:` 引用并上传 blob。运行器不持有数据源凭证，MUST NOT 自行解引用数据源 url（connector 契约 §0、§1-2）；
- 字节确实无法取得的图片按占位图片处理（`blob_ref` 为 null），url 仍可作为访问方式投递；
- `image_url` 是随位投递字段，只经骨架 entry 的 `occurrence` 投递，MUST NOT 进入 content_hash，进 state_hash（§6）；
- 服务端不负责抓取 url；
- OCR / 抽取出的文字若由服务端生成，属服务端衍生字段，不进 hash（图片型文档不再需要特殊 hash 策略，#3 S9）。

## 5. `page_hash` 与 `doc_hash`

```
page_hash = "dpe1:" + hex( H( ascii(number), text(title), meta(page_metadata), utf8(ch1), …, utf8(chn) ) )
```

- `ascii(number)`：页号的十进制 ASCII 字符串（如 `12`、`-3`），无前导零、无正号。
- `ch1…chn`：页内各骨架 entry 的 `hash`（即元素 `content_hash` 的**完整字符串**，含 `dpe1:` 前缀）按数组顺序的 UTF-8 字节。数组顺序即页内阅读顺序，顺序本身就是内容身份。entry 的 `occurrence` 不进 page_hash。
- 重复元素照常重复出现：元素身份是 `content_hash`，**与位置无关**；同一内容在多个位置共用同一个内容对象。

页号进入 page_hash（#3 已确认）：页号既是页的配对身份，也决定页的阅读顺序；若不进 hash，纯改页号（内容不动）会得到相同 doc_hash，`head` 快路径漏推。页号变化只影响 page_hash / doc_hash，不改变任何 content_hash，因此不会造成"插入一页 → 元素连锁重学"，也不构成刷新衍生物的理由（core.md §3.3"hash 的两种职责"）。

```
doc_hash = "dpe1:" + hex( H( text(file_type), meta(doc_metadata), utf8(ph1), …, utf8(phm) ) )
```

- `file_type`：core.md §2.6 封闭枚举的字符串值（源提供，#3）；未知取值 MUST 拒绝。
- `ph1…phm`：各页 `page_hash` 完整字符串按 `number` **升序**排列的 UTF-8 字节——`number` 升序就是页的阅读顺序（core.md §2.2）。hash 计算对输入数组顺序宽容（向量 `page_array_order_irrelevant`），线上报文则 MUST 按 number 升序排列。
- `number` 在文档内 MUST 唯一。
- 空文档（0 页）合法：`doc_hash = "dpe1:" + hex(H(text(file_type), meta(doc_metadata)))`。空页（0 元素）同理。

## 6. `state_hash`（投递状态摘要）

doc_hash 只覆盖内容身份。不进内容 hash 的投递字段（随位投递、治理属性）变化时，仅比对 doc_hash 会让 `unchanged` 判定、`batch_head` 快路径筛选和重试恢复全部察觉不到（#4 A2）。state_hash 覆盖**全部投递字段**：

```
state_hash = "dpe1:" + hex( H( text("state"), utf8(doc_hash), jcs(strip_nulls(S)) ) )

S = { "attributes":  attributes（缺省视同 {}），
      "occurrences": [ [ occ(e1), …, occ(en) ]  对每一页，页按 number 升序 ] }
occ(e) = 骨架 entry e 的 occurrence（缺省视同 {}）
```

- **输入是线上视图**：S 取自骨架 entry 的 `occurrence` 与文档的 `attributes`。把随位字段与 metadata 存在一处的实现，计算 state_hash 前 MUST 先按随位投递键把它们从元素 metadata 移入 occurrence（与构造线上报文的拆分相同）；MUST NOT 直接用 §2.1 过滤后的 metadata 代替——那样只改 metadata 中的坐标时 state_hash 不变，就是一条静默丢弃路径。
- `strip_nulls`：与 §2.1 第 2 步相同的递归 null 键删除（缺省 ≡ null）；`jcs` 见 §3.3（attributes 与 occurrence 中的数字同样受安全整数范围约束）。
- 首段 `"state"` 是域分隔，保证 state_hash 不会与任何 page_hash / doc_hash 的原像相撞。
- `occurrences` 的外层按页（number 升序）、内层按页内元素位置排列，与骨架一一对应；某位置没有 occurrence 时写 `{}`，保持位置对齐。
- 服务端 MUST 自行计算 state_hash（不信任客户端提交的值）。
- state_hash 只用于变更检测（core.md §3.3），其变化本身不构成刷新任何衍生物的理由。

## 7. 契约演进

- 契约版本在能力发现阶段协商（core.md §3.1）；hash 值自带版本前缀，跨版本的值不可比。
- **升级对应规则**（#3 S8）：服务端按已存的（页, 页内序号）用新契约**逐元素原位重算**，MUST NOT 重新做差异配对；元素身份（学习产物的归属）保持不变。page_hash / doc_hash / state_hash 随之重算。过渡期 MAY 同时接受多个版本。
- 契约升级 MUST NOT 引起未变内容的重推或重学（判据见 plan §13-7）。
- 向量包含契约升级演练：同一份内容同时给出 `dpe1` 与假想 `dpe2` 的期望值，两个契约下的逐页、逐元素结果按位置一一对应，即为对应关系断言（`dpe2` 定义见 vectors/README.md，仅用于测试）。

实现方从既有 hash 规则迁移到本契约的对照，见 [docs/migration/kernel-to-dpe1.md](../docs/migration/kernel-to-dpe1.md)（非规范）。

## 8. 待评审决策点

已关闭：

- ~~页号进 page_hash~~：#3 已确认保留，理由见 §5。
- ~~保留键集合的完整枚举~~：#3 按首个实现方的全部已知 metadata 键普查后补全，并拆为随位投递 / 不投递两组（§2.1）；源提供的 `created_at`、`last_modified`、`category_depth`、`summary` 按来源规则进 hash，不列为保留键。
- ~~`file_type` 的归属~~：#3 确认按来源规则进 doc_hash（§5）。

待评审：

1. **`page_name` 归为"骨架位置派生"**（§2.1）：假定它就是所在页的名称，由页 `title` 表达。若存在与页 title 不同、且需要投递的页名，需改为随位投递键。
2. **`parent_id` / `related_ids` 的值语义**（§2.1）：作为不透明的随位字段投递；其值引用的是解析器生成的元素 id，协议不定义 id 的解析方式，是否需要改为引用骨架位置（页号, 下标）待评审。
