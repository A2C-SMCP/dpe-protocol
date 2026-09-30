# DPE Hash 契约 1（`dpe1`）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §7–§8，经 Issue #3（内核输入）修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

Hash 契约定义**内容身份**的计算方式：给定一篇文档的内容身份视图（§2），任何合规实现都必须逐字节算出相同的 `content_hash` / `page_hash` / `doc_hash`。契约版本与投递协议版本是两个独立的轴；本文是契约 **1**，值前缀为 `dpe1:`。

**hash 只由契约版本和 category 决定**：不存在按文件类型或其他条件选择的 hash 策略（#3 S9）。

一致性向量（[vectors/](../vectors/)）是本契约的组成部分：实现 MUST 通过全部向量，向量与本文冲突时视为规范缺陷，按治理流程修订（见 CLAUDE.md）。

## 1. 值格式

| 类型 | 格式 | 说明 |
| --- | --- | --- |
| `content_hash` / `page_hash` / `doc_hash` | `dpe1:<64 lowercase hex>` | 完整 SHA-256，不截断，前缀标明契约版本 |
| blob 引用 | `sha256:<64 lowercase hex>` | 对 blob 原始字节的 SHA-256，与 OCI digest 惯例一致 |

- hex MUST 为小写。带前缀的完整字符串才是 hash 值；比较、存储、传输 MUST 携带前缀。
- 实现 MUST 拒绝无前缀或前缀未知的 hash 值（`DPE_CONTRACT_UNSUPPORTED`）。

## 2. 内容身份视图：按来源分类

字段按**由谁产生**分类（#3 S2，取代 v1 计划 §6 的三分类），每个字段 MUST 显式可归入一类：

| 类别 | 判定 | 进 hash | 投递 |
| --- | --- | --- | --- |
| **源提供** | 正文、category、页 title、doc / page / element 的 metadata——凡由 connector 或数据源给出的 | **进** | 是 |
| **服务端衍生** | 服务端计算或写回的字段（keywords、各类 id、hash 字段、抽取过程写回的字段） | 不进 | **否**（提交即拒绝） |
| **寻址坐标** | 绝对位置：元素 metadata 中的页码、页内序号、版面坐标等 | 不进 | 是（随 metadata，按 §2.1 过滤出 hash） |
| **访问方式** | 获取同一内容的途径（如图片 url）；标识不等于访问方式 | 不进 | 是 |
| **治理属性** | 访问控制等（creator / group 类） | 不进 | 是（走 attributes + revision CAS，见 core.md） |

规范性规则：

- **服务端写入的字段 MUST NOT 进入内容身份**（否则形成"写回 → hash 变 → 重算 → 再写回"的自触发循环）。
- **源提供的字段 MUST NOT 携带会自行变化的默认值**（如 `created_at` 缺省取当前时间）：值由源提供，源给不出就留空。
- 相对顺序（页序列、页内元素序列）**属于内容身份**，由 page_hash / doc_hash 的序列结构体现；绝对坐标（页码值、版面坐标）不属于。

### 2.1 metadata 的 hash 输入

doc / page / element 各有一个 metadata JSON 对象（源提供，键值自由）。进入 hash 前依次做两步：

1. **过滤保留键**（顶层键名匹配）：保留键集合是规范的一部分，SDK MUST 以常量导出，实现 MUST NOT 自行维护副本。契约 1 的保留键集合：

   ```
   寻址坐标：page_number, page_name, seq_in_page, coordinates
   服务端衍生：keywords
   ```

   > 集合的完整枚举需内核在 M1 评审中按现有字段确认（#3 S2 的三类反例来自内核实测），定稿后新增保留键需升契约版本。

2. **递归删除值为 null 的键**（#3 S5）：对象中值为 null 的键 MUST 在 JCS 之前删除，即**缺省 ≡ null**。否则实现方给模型新增一个可选字段，所有存量 hash 都会变化，等于一次隐性契约升级。数组元素不受此规则影响（`[null]` 原样保留）。

处理后的对象经 RFC 8785 JCS（§3.3）序列化为 UTF-8 字节，记作 `meta(m)`。metadata 缺省视同空对象：`meta(null) = meta({}) = "{}"` 的字节。

### 2.2 汇总

```
element:  category、按 §4 category 规定的内容字段、metadata（经 §2.1）
page:     number（见 §5 决策说明）、title、元素序列、metadata（经 §2.1）
document: 页序列、doc_metadata（经 §2.1）
```

`file_uri`、`file_type`、`attributes` 不参与 hash（`file_type` 的归属见 §8-3）。契约 1 **没有 doc title**（#3 S6：内核数据结构中不存在该字段，不为悬空字段留位；确有需要由后续契约引入）。

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
- 超出 IEEE-754 double 安全整数范围（|n| > 2^53−1）的数字 MUST 拒绝；
- 时间戳一律为 RFC 3339 UTC 字符串（`Z` 结尾），作为字符串参与 JCS，不做时区换算之外的改写。

JCS 实现的正确性由 `kind: "jcs"` 向量单独校验。

## 4. `content_hash`

```
content_hash = "dpe1:" + hex( H( text(category), part1, …, partN, meta(metadata) ) )
```

首段恒为 category 的字符串值（#3 S3：以什么语义角色呈现属于内容本身；`Title("Foo")` 与 `NarrativeText("Foo")` 是不同内容，category 改动不得被静默丢弃）。末段恒为元素 metadata（经 §2.1）。

category 是**封闭枚举**；遇到本契约未列出的 category，实现 MUST 拒绝（`DPE_CATEGORY_UNKNOWN`），MUST NOT 退化为 text-only hash。

### 4.1 category 表与内容字段（part1…partN）

| category | 内容字段（按序） |
| --- | --- |
| `Image` | `text(text)`、`text(blob_ref)`、`text(image_mime_type)` |
| `Table`、`Formula` | `text(text)`、`text(text_as_html)` |
| 其余（见下） | `text(text)` |

text-only category 全集：`UncategorizedText`、`CheckBox`、`CompositeElement`、`FigureCaption`、`NarrativeText`、`ListItem`、`Title`、`Address`、`EmailAddress`、`PageBreak`、`TableChunk`、`Header`、`Footer`、`CodeSnippet`、`PageNumber`、`FormKeysValues`、`tfchat`。

`FormKeysValues` 的键值对如由源随元素 metadata 提供，则经 §2.1 自动进入 hash，无需专门的内容字段。

新增 category 需要升契约次版本，并通过 capabilities 协商（§6）。

### 4.2 Image：身份只认 blob

```
blob_ref = "blob:" + blob 引用       （如 "blob:sha256:ab…"）
         | null                      （纯占位图片元素）
```

**图片的内容身份只认 blob hash**（#3 S4）：url 是访问方式，不是身份——同一张图换一个 CDN 地址不是内容变化。规则：

- 只有 url 的图片，由推送侧（SDK / 运行器）取回字节、算出 `sha256:` 引用后再投递；
- `image_url` MAY 随内容对象投递（供服务端选用的访问方式），但 MUST NOT 进入 hash；
- 服务端不负责抓取 url；
- OCR / 抽取出的文字若由服务端生成，属服务端衍生字段，不进 hash（图片型文档不再需要特殊 hash 策略，见 §8-S9 与 #3 S9）。

初版草案的 `path` 通道（服务端本地路径）已移出协议。

## 5. `page_hash` 与 `doc_hash`

```
page_hash = "dpe1:" + hex( H( ascii(number), text(title), meta(page_metadata), utf8(ch1), …, utf8(chn) ) )
```

- `ascii(number)`：页号的十进制 ASCII 字符串（如 `12`、`-3`），无前导零、无正号。
- `ch1…chn`：页内各元素 `content_hash` 的**完整字符串**（含 `dpe1:` 前缀）按数组顺序的 UTF-8 字节。数组顺序即阅读顺序，顺序本身就是内容身份。
- 重复元素照常重复出现：元素身份是 `content_hash`，**与位置无关**；同一内容在多个位置共用同一个内容对象。

> 决策说明（与 #3 S2 的"寻址坐标"类目存在张力，待评审裁决）：页号进入 page_hash。页号既是寻址坐标，也是页的配对身份；若不进 hash，纯改页号（内容不动）会得到相同 doc_hash，`head` 快路径漏推，服务端存的页号永久过期。与元素级坐标不同，页号变化只影响 page_hash / doc_hash，不改变任何 content_hash，因此**不会**造成 #3 S2 担心的"插入一页 → 元素连锁重学"（delta 按元素多重集计，保持为零）。

```
doc_hash = "dpe1:" + hex( H( meta(doc_metadata), utf8(ph1), …, utf8(phm) ) )
```

- `ph1…phm`：各页 `page_hash` 完整字符串按 `number` **升序**排列的 UTF-8 字节。
- `number` 在文档内 MUST 唯一。
- 空文档（0 页）合法：`doc_hash = "dpe1:" + hex(H(meta(doc_metadata)))`。空页（0 元素）同理。

## 6. 契约演进

- 契约版本在能力发现阶段协商（core.md §3.1）；hash 值自带版本前缀，跨版本的值不可比。
- **升级对应规则**（#3 S8）：服务端按已存的（页, 页内序号）用新契约**逐元素原位重算**，MUST NOT 重新做差异配对；元素身份（学习产物的归属）保持不变。过渡期 MAY 同时接受多个版本。
- 契约升级 MUST NOT 引起未变内容的重推或重学（判据见 plan §13-7）。
- 向量包含契约升级演练：同一份内容同时给出 `dpe1` 与假想 `dpe2` 的期望值，两个契约下的逐页、逐元素结果按位置一一对应，即为对应关系断言（`dpe2` 定义见 vectors/README.md，仅用于测试）。

## 7. 与内核现状（TFRobotV2）的差异

| 项 | 内核现状 | 契约 1 |
| --- | --- | --- |
| 值格式 | 截断 32 hex、版本记在旁路字段 | `dpe1:` + 完整 64 hex |
| doc_hash 范围 | 含 `file_uri`、`file_type`、title（恒空）、`json.dumps(doc_metadata)` | 无 file_uri / file_type / title；doc_metadata 经保留键过滤 + null 键删除 + JCS |
| category | 不进 hash（基类只有 text） | 进 hash，封闭枚举，未知拒绝 |
| metadata | 不进 element / page hash | 源提供的 metadata 进全部三层 hash（经 §2.1） |
| 页号 | 只用于排序 | 进 page_hash（待评审，见 §5） |
| 图片 | url / base64 / path 取首个非空 | 身份只认 `blob:sha256`，url 为访问方式 |
| hash 策略 | 按文件类型选择（default / image-source） | 不存在策略，只由契约版本 + category 决定 |
| 结构化值 | Python `json.dumps` | RFC 8785 JCS |
| 升级行为 | 策略 URI 不同即全删全建 | 原位重算，身份不变，禁止重新配对 |

## 8. 待评审决策点

1. **页号进 page_hash**（§5）：与 #3 S2 的"寻址坐标不进 hash"字面冲突，本文保留并给出理由，请内核侧就"纯改页号如何不被 head 快路径漏掉"表态。
2. **保留键集合的完整枚举**（§2.1）：初始集合来自 #3 的反例，需内核按现有字段核对补全（如全局指代字典的实际键名）。
3. **`file_type` 的归属**：#3 S2 按来源分类的字面规则会把它划入"源提供 → 进 hash"，但 v1 计划已明确其为正交属性且 #3 未要求推翻，本文暂维持不进 hash，请评审确认。
