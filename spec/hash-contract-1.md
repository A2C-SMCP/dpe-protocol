# DPE Hash 契约 1（`dpe1`）

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §7–§8
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

Hash 契约定义**内容身份**的计算方式：给定一篇文档的内容身份视图（§2），任何合规实现都必须逐字节算出相同的 `content_hash` / `page_hash` / `doc_hash`。契约版本与投递协议版本是两个独立的轴；本文是契约 **1**，值前缀为 `dpe1:`。

一致性向量（[vectors/](../vectors/)）是本契约的组成部分：实现 MUST 通过全部向量，向量与本文冲突时视为规范缺陷，按治理流程修订（见 CLAUDE.md）。

## 1. 值格式

| 类型 | 格式 | 说明 |
| --- | --- | --- |
| `content_hash` / `page_hash` / `doc_hash` | `dpe1:<64 lowercase hex>` | 完整 SHA-256，不截断，前缀标明契约版本 |
| blob 引用 | `sha256:<64 lowercase hex>` | 对 blob 原始字节的 SHA-256，与 OCI digest 惯例一致 |

- hex MUST 为小写。带前缀的完整字符串才是 hash 值；比较、存储、传输 MUST 携带前缀。
- 实现 MUST 拒绝无前缀或前缀未知的 hash 值（`DPE_CONTRACT_UNSUPPORTED`）。

## 2. 内容身份视图

参与 hash 的字段**只有**下列内容身份字段（三分类见 core.md §2.4；正交属性与衍生字段 MUST NOT 进入任何 hash）：

```
document: title
page:     number, title, elements 数组顺序
element:  category, 以及 §4 按 category 规定的内容字段
```

`file_uri`、`file_type`、`doc_metadata`、`page_metadata`、`attributes` 均**不**参与 hash。

## 3. 基本原语

### 3.1 长度前缀拼接

```
seg(b)      = len(b) 的 4 字节大端无符号整数 || b        （b 为字节串，len < 2^32）
concat(P…)  = seg(P1) || seg(P2) || … || seg(Pn)
H(P…)       = SHA-256(concat(P…))
```

长度前缀消除 `["ab","c"]` 与 `["a","bc"]` 的歧义。空段（`len=0`）也 MUST 写出前缀 `00 00 00 00`。

### 3.2 文本段

```
text(s) = s 的 UTF-8 编码；s 为 null 时取空字节串
```

null 与空串产出相同字节。实现 MUST NOT 对 null 做隐式字符串化（如 `"None"` / `"null"`）。文本 MUST NOT 做任何 Unicode 规范化（NFC/NFD 等）：字节即身份。

### 3.3 结构化值（RFC 8785 JCS）

凡是有 JSON 结构化值需要进入 hash 或做规范化比较之处，MUST 使用 [RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785) 序列化后的 UTF-8 字节。要点：

- 对象键按 UTF-16 码元序排序；
- 数字按 ECMAScript `Number::toString` 序列化（整数不带小数点，指数不带前导零）；
- 超出 IEEE-754 double 安全整数范围（|n| > 2^53−1）的数字 MUST 拒绝；
- 时间戳一律为 RFC 3339 UTC 字符串（`…T…Z` 或 `+00:00` 归一为 `Z`），作为字符串参与 JCS。

**契约 1 的元素 hash_parts 中暂无结构化值**（§4），本节当前的规范性用途是：正交属性值的等值比较（core.md §2.5）与未来 category 的预置规则。JCS 实现的正确性由 `kind: "jcs"` 向量单独校验。

## 4. `content_hash`

```
content_hash = "dpe1:" + hex( H( text(category), part1, part2, … ) )
```

第一段恒为 category 的字符串值。category 是**封闭枚举**（§4.1）；遇到本契约未列出的 category，实现 MUST 拒绝（`DPE_CATEGORY_UNKNOWN`），MUST NOT 退化为 text-only hash。

> 决策说明：category 进入 hash（初版实现未包含）。category 决定服务端如何实例化与学习该元素（core.md §3），同文本不同 category 的元素不是同一内容。

### 4.1 category 表与 hash_parts

| category | hash_parts（按序） |
| --- | --- |
| `Image` | `text(text)`、`text(image_ref)`、`text(image_mime_type)` |
| `Table`、`Formula` | `text(text)`、`text(text_as_html)` |
| 其余（见下） | `text(text)` |

text-only category 全集：`UncategorizedText`、`CheckBox`、`CompositeElement`、`FigureCaption`、`NarrativeText`、`ListItem`、`Title`、`Address`、`EmailAddress`、`PageBreak`、`TableChunk`、`Header`、`Footer`、`CodeSnippet`、`PageNumber`、`FormKeysValues`、`tfchat`。

新增 category 需要升契约次版本，并通过 capabilities 协商（§6）。

### 4.2 Image 的 `image_ref`

```
image_ref = "blob:" + blob 引用        （如 "blob:sha256:ab…"，内容随协议投递）
          | 外部 URL 原文               （服务端不负责抓取，URL 字符串本身即身份）
          | null                        （纯占位图片元素）
```

一个 Image 元素 MUST 至多有 blob 与 url 之一，两者同时出现时实现 MUST 拒绝。初版草案的 `path` 通道已移出协议。URL MUST NOT 做规范化：改一个字符就是不同内容。

## 5. `page_hash` 与 `doc_hash`

```
page_hash = "dpe1:" + hex( H( ascii(number), text(page_title), utf8(ch1), …, utf8(chn) ) )
```

- `ascii(number)`：页号的十进制 ASCII 字符串（如 `12`、`-3`），无前导零、无正号。
- `ch1…chn`：页内各元素 `content_hash` 的**完整字符串**（含 `dpe1:` 前缀）按数组顺序的 UTF-8 字节。数组顺序即阅读顺序，顺序本身就是内容身份。
- 重复元素照常重复出现：元素身份是 `content_hash`，不含位置；同一内容在多个位置共用同一个内容对象。

> 决策说明：页号进入 page_hash（初版实现未包含）。页号是页的配对身份，若不进 hash，纯改号（内容不动）会得到相同的 doc_hash，`head` 快路径会漏推，破坏「增量与全量收敛一致」。

```
doc_hash = "dpe1:" + hex( H( text(doc_title), utf8(ph1), …, utf8(phm) ) )
```

- `ph1…phm`：各页 `page_hash` 完整字符串按 `number` **升序**排列的 UTF-8 字节。
- `number` 在文档内 MUST 唯一。
- 空文档（0 页）合法：`doc_hash = "dpe1:" + hex(H(text(title)))`。空页（0 元素）同理。

## 6. 契约演进

- 契约版本在能力发现阶段协商（core.md §3.1）；hash 值自带版本前缀，跨版本的值不可比。
- 服务端升级契约时，MUST 用已存内容离线重算新 hash，元素身份保持不变；过渡期 MAY 同时接受多个版本。
- 契约升级 MUST NOT 引起未变内容的重推或重学（判据见 plan §13-7）。
- 向量包含契约升级演练：同一份内容同时给出 `dpe1` 与假想 `dpe2` 的期望值。`dpe2` **仅用于测试**（定义见 vectors/README.md），不是真实契约。

## 7. 与初版草案（TFRS hash-contract-v1）的差异

| 项 | 旧草案 | 契约 1 |
| --- | --- | --- |
| 值格式 | 截断 32 hex、无前缀 | `dpe1:` + 完整 64 hex |
| doc_hash 范围 | 含 `file_uri`、`file_type`、`doc_metadata` | 只含内容身份 |
| category | 开放，未知按 text 处理 | 封闭枚举，未知拒绝；category 进 hash |
| 页号 | 不进 hash | 进 page_hash |
| 图片通道 | url / base64 / path 三通道取优先 | blob / url 二选一，`path` 移出协议 |
| 结构化值 | Python `json.dumps` 行为 | RFC 8785 JCS |
| 向量归属 | 内核生成 | 本仓库（规范）产出，内核与 SDK 消费 |

## 8. 待评审决策点

1. category 进入 content_hash（§4，偏离 plan §7 表格的字面描述，理由如上）。
2. 页号进入 page_hash（§5）。
3. `FormKeysValues` 暂按 text-only 处理；若其键值对属于内容身份，应在契约 1 定稿前改为 `text + JCS(form_keys_values)`，定稿后再改就要升契约版本。
