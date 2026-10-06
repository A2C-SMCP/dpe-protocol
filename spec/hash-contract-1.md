# DPE Hash 契约 1（`dpe1`）

> 状态：**定稿**（M1，2026-10-06；此后的变更经 Issue 修订并发布新文档版本）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §0.1、§7–§8，经 Issue #3、#4、#6、#30、#31、#60 修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

Hash 契约定义**内容身份**的计算方式：给定一篇文档，任何合规实现都必须逐字节算出相同的 `content_hash` / `page_hash` / `doc_hash`。doc_hash 同时是文档的版本令牌（core.md §1）。契约版本与投递协议版本是两个独立的轴；本文是契约 **1**，值前缀为 `dpe1:`。

契约 1 是一棵三层同构的 tree，与 Git 的对象模型一一对应：元素对象 ↔ blob，页对象与文档对象 ↔ tree。每一层都是「自身内容 + 子对象 hash 的有序列表」，hash 的原像就是该对象本身。

**hash 只由契约版本和对象内容决定**：不存在按文件类型或其他条件选择的 hash 策略（#3 S9）。

一致性向量（[vectors/](../vectors/README.md)）是本契约的组成部分：实现 MUST 通过全部向量，向量与本文冲突时视为规范缺陷，按治理流程修订（见 CLAUDE.md）。

## 1. 值格式

| 类型 | 格式 | 说明 |
| --- | --- | --- |
| `content_hash` / `page_hash` / `doc_hash` | `dpe1:<64 lowercase hex>` | 完整 SHA-256，不截断，前缀标明契约版本 |
| blob 引用 | `sha256:<64 lowercase hex>` | 对 blob 原始字节的 SHA-256，与 OCI digest 惯例一致 |

- hex MUST 为小写。带前缀的完整字符串才是 hash 值；比较、存储、传输 MUST 携带前缀。
- 实现 MUST 拒绝无前缀或前缀未知的 hash 值（`DPE_CONTRACT_UNSUPPORTED`）。

## 2. 输入：源即内容

DPE 的每个字段都由源提供（plan §0.1 P2），除身份 `file_uri` 外**全部**进 hash，没有保留键、没有过滤、没有例外：正文、category、`file_type`、文档 title 与页 title，以及 doc / page / element 三层 metadata 的全部键（含版面坐标、图片 url、源页码标签等）。

规范性规则：

- **服务端衍生物不进 DPE 字段**：服务端计算或写回的数据（keywords、各类服务端 id、抽取过程写回的字段）MUST NOT 写入任何 DPE 字段，因此也永远不会进入 hash（否则形成"写回 → hash 变 → 重算 → 再写回"的自触发循环）。它们存放在服务端自己的存储中（core.md §2.4）。
- **源提供的字段 MUST NOT 携带会自行变化的默认值**（如 `created_at` 缺省取当前时间）：值由源提供，源给不出就留空。
- **易变字段不应进入 metadata**：不随内容变化、却会频繁变化的源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）SHOULD NOT 放进任何 metadata。
- **顺序属于内容，位置不属于对象身份**：页序（文档对象 `pages` 的数组顺序）与页内元素序（页对象 `elements` 的数组顺序）都是内容，由上层对象体现；任何对象的 hash 都不含它自己在上层中的位置。因此插入、删除、移动一页时，只有文档对象变化，其余页对象与元素对象都不变、无需重传——这只为传输去重，不意味着位置变化不是内容变化（plan §0.1 P3）。

## 3. 原语

### 3.1 对象 hash

```
H(obj) = "dpe1:" + hex( SHA-256( utf8( JCS( norm(obj) ) ) ) )
```

这是契约 1 唯一的原语：三层对象都用它计算 hash，没有长度前缀、分段拼接等其他构造。

### 3.2 规范化 `norm`

1. **递归删除值为 null 的键**（#3 S5）：对象中值为 null 的键 MUST 在 JCS 之前删除，即**缺省 ≡ null**。否则实现方给模型新增一个可选字段，所有存量 hash 都会变化，等于一次隐性契约升级。数组元素不受此规则影响（`[null]` 原样保留）。
2. **metadata 字段缺省视同 `{}`**：元素的 `metadata`、页对象的 `page_metadata`、文档对象的 `doc_metadata` 缺省或为 null 时，取 `{}`；即这三个字段在原像中总是出现。

除此之外不做任何改写。空串 `""` 是一个值，与 null（缺省）不等价（向量 `null_vs_empty_distinct`）。

**hash 的是投递的内容，不是语义等价物**（#3 S5）：字符串按收到的原样进入 hash，协议不做 Unicode 规范化（NFC/NFD）、URL 规范化 / percent-encode 等任何改写。实现侧的序列化框架若会自动改写（如 URL 类型自动编码），MUST 在进 hash 前还原为原始输入。

### 3.3 结构化值（RFC 8785 JCS）

对象一律用 [RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785) 序列化为 UTF-8 字节。要点：

- 对象键按 UTF-16 码元序排序；
- 数字按 ECMAScript `Number::toString` 序列化（整数不带小数点，指数不带前导零）；
- **整数字面量**（不含小数点与指数）绝对值超过 2^53−1 时 MUST 拒绝；带小数点或指数的数字按 IEEE-754 double 解析后序列化（向量 `jcs_numbers`）；
- 时间戳一律为 RFC 3339 UTC 字符串（`Z` 结尾），作为字符串参与 JCS，不做时区换算之外的改写。

JCS 实现的正确性由 `kind: "jcs"` 向量单独校验；向量 `preimage_basic` 给出三层对象规范化后的完整原像，供逐字节排查。

### 3.4 层间不会混淆

三层对象都是**封闭 schema**：出现未定义的字段 MUST 拒绝（core.md §2）。三层各有一个必有、且只有它有的键——元素的 `category`、页对象的 `elements`、文档对象的 `pages`——因此不同层的对象不可能有相同的原像，不需要额外的类型标签（作用相当于 Git 的对象头）。多处违例时的校验顺序与错误码见 core.md §2.8。

## 4. `content_hash`（元素对象）

```
content_hash = H(element)
element      = { "category": …, 按 §4.1 该 category 允许的内容字段…, "metadata": {…} }
```

category 进 hash（#3 S3：以什么语义角色呈现属于内容本身；`Title("Foo")` 与 `NarrativeText("Foo")` 是不同内容）。category 是**封闭枚举**；遇到本契约未列出的 category，实现 MUST 拒绝（`DPE_CATEGORY_UNKNOWN`），MUST NOT 退化为 text-only hash。

### 4.1 category 表与内容字段

| category | 允许的内容字段（均可选） |
| --- | --- |
| `Image` | `text`、`blob`、`mime_type` |
| `Table`、`Formula` | `text`、`text_as_html` |
| 其余（见下） | `text` |

text-only category 全集：`UncategorizedText`、`CheckBox`、`CompositeElement`、`FigureCaption`、`NarrativeText`、`ListItem`、`Title`、`Address`、`EmailAddress`、`PageBreak`、`TableChunk`、`Header`、`Footer`、`CodeSnippet`、`PageNumber`、`FormKeysValues`、`tfchat`。其中 `tfchat` 是开放格式名，属 plan §1 命名规则的登记例外（同 core.md §2.5）。

内容字段的值均为字符串（`blob` 为 §1 的 blob 引用）。出现其 category 未允许的字段 MUST 拒绝。

二进制内容一律经通用的 `blob` / `mime_type` 携带，不为每种媒体另设字段名（类比 OCI 描述符的 `digest` + `mediaType`）。本表决定哪些 category 可以携带 blob；以后新增二进制类别（如音频、视频）只需增加 category 与表行，字段名与 hash 规则都不变。`FormKeysValues` 的键值对如由源随元素 metadata 提供，则自然进入 hash，无需专门的内容字段。

新增 category 需要升契约次版本，并通过 capabilities 协商（§6）。本表以机器可读形式随向量发布（`vectors/manifest.json` 的 `category_content_fields`），SDK MUST 以常量导出，并与之一致。

### 4.2 blob 与 Image

- 字节由 blob 承载，`blob` 是字节的引用（`"sha256:…"`），`mime_type` 是字节的媒体类型（如 `image/png`），二者直接作为元素对象的字段进 hash；图片 url、版面坐标等源信息放在元素 metadata 中，与其他源字段一样进 hash（§2）。同一张图换一个 url 即内容变化（向量 `image_url_is_content`）。
- **SDK 上传辅助**（非规范性，登记为 M2 SDK 需求）：SDK 应提供辅助函数——给它本地路径或字节，它计算 `sha256:` 引用、上传 blob 并把元素替换为引用。字节只经 `blob` 携带，这是正向定义；协议不对 metadata 中的路径类字符串做任何识别或拒收。
- **字节由持有数据源凭证的一方提供**：需要数据源凭证才能取得的图片，由产出方（connector，或直接调用 SDK 的上游）用自己的凭证取回字节；推送侧（SDK / 运行器）只负责计算 `sha256:` 引用并上传 blob。运行器不持有数据源凭证，MUST NOT 自行解引用数据源 url（connector 契约 §0、§1-2）。字节确实无法取得时，`blob` 缺省。
- 服务端不负责抓取 url；OCR / 抽取出的文字若由服务端生成，属服务端衍生物，不进 DPE（图片型文档不再需要特殊 hash 策略，#3 S9）。

## 5. `page_hash` 与 `doc_hash`（tree 对象）

```
page_hash = H(page)         page     = { "title"?: …, "page_metadata": {…}, "elements": [content_hash…] }
doc_hash  = H(document)     document = { "file_type": …, "title"?: …, "doc_metadata": {…}, "pages": [page_hash…] }
```

- `elements` / `pages` 是子对象 hash 完整字符串（含 `dpe1:` 前缀）的数组，**数组顺序即阅读顺序**。子对象 hash 的契约 MUST 与本对象相同：前缀缺失或为不受支持的契约时拒绝（`DPE_CONTRACT_UNSUPPORTED`，§1）；前缀为受支持、但与本对象不同的契约时（契约混用）拒绝（`DPE_VALIDATION`）。重复出现照常重复：内容完全相同的元素共用同一个元素对象，内容完全相同的页共用同一个页对象（向量 `duplicate_pages`）。
- 页没有页号字段：页的位置就是它在 `pages` 中的下标。源文件自带的页码标签（印刷页码、PDF PageLabels，如 `iv`）是源内容，放进 `page_metadata`。由位置算出的序号 SHOULD NOT 写进 `page_metadata`——它会让插入一页后，后续每一页的页对象都变化，页层重新出现连锁重传。
- 文档对象与页对象的 `title` 为字符串，可缺省（缺省与 null 等价，`""` 是独立的值）；`file_type` 为 core.md §2.5 封闭枚举的字符串值，未知取值 MUST 拒绝。
- 空文档（0 页）与空页（0 元素）均合法：`pages` / `elements` 为 `[]`。

## 6. 契约演进

- 契约版本在能力发现阶段协商（core.md §3.1）；hash 值自带版本前缀，跨版本的值不可比。
- **升级对应规则**（#3 S8）：服务端按已存的（页序号, 页内序号）用新契约**逐对象原位重算**，MUST NOT 重新做差异配对；元素身份（学习产物的归属）保持不变。
- **升级期的版本令牌**：doc_hash 同时是 CAS 令牌（core.md §5.1）。过渡期内，服务端 MUST 把同一内容在任一受支持契约下的 doc_hash 都视为该文档的当前版本（`If-Match` 按所带值的契约前缀比较、`unchanged` 按请求声明的契约判定），读接口按请求声明的契约返回值（core.md §3.1）；契约升级本身 MUST NOT 让任何写入者的前置条件失效，也 MUST NOT 让快路径筛选把未变内容判为变化。
- 契约升级 MUST NOT 引起未变内容的重推或重学（判据见 plan §13-7）。
- 向量包含契约升级演练：同一份内容同时给出 `dpe1` 与假想 `dpe2` 的期望值，两个契约下的逐页、逐元素结果按位置一一对应，即为对应关系断言（`dpe2` 定义见 vectors/README.md，仅用于测试）。

实现方从既有 hash 规则迁移到本契约的对照，见 [docs/migration/kernel-to-dpe1.md](../docs/migration/kernel-to-dpe1.md)（非规范）。
