# dpe-hash

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的独立 hash 核心：按 [hash 契约 1](https://doc.turingfocus.cn/dpe/latest/spec/hash-contract-1/) 计算元素、页、文档三层 hash（`dpe1:` 前缀），并导出契约常量。

- 零运行时依赖、纯 Python（≥ 3.11），带类型标记（`py.typed`）；
- 逐字节通过规范仓库的一致性向量；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

```bash
pip install "dpe-hash>=0.1.5,<0.2.0"
```

## 版本约定

hash 契约升级对应 minor 版本（`0.2.0` = 契约 2），不影响 hash 行为的修订走 patch。依赖时锁定 minor：`dpe-hash>=0.1.5,<0.2.0`。

## API

```python
from dpe_hash import content_hash, page_hash, doc_hash, object_hash, document_hashes

h = content_hash({"category": "NarrativeText", "text": "Hello, DPE."})  # 元素对象
p = page_hash({"title": "p1"}, [h])  # 页字段 + 已有 content_hash
d = doc_hash({"file_type": "md", "title": "报告"}, [p])  # 文档字段 + 已有 page_hash
assert d == object_hash({"file_type": "md", "title": "报告", "pages": [p]}, "document")  # 线上原像
```

| 入口 | 用途 |
| --- | --- |
| `content_hash(element, contract=CONTRACT)` | 元素对象的 `content_hash` |
| `page_hash(page, element_hashes, contract)` | 页自身字段 + 已有的 content_hash 列表；`page` 带 `elements` 即报错 |
| `doc_hash(document, page_hashes, contract)` | 文档自身字段 + 已有的 page_hash 列表，不需加载元素；`document` 带 `pages` 即报错 |
| `object_hash(obj, kind, contract)` | 线上原像（页带 `elements`、文档带 `pages`），`kind` 为 `"element"` / `"page"` / `"document"` |
| `document_hashes(expanded, contract)` | 展开视图一次算出三层，结果同向量的 `expected` |
| `children(obj, kind, contract)` | 原像引用的下一层：页 → content_hash，文档 → page_hash，元素 → blob 引用 |
| `parse_hash(s, contracts=SUPPORTED_CONTRACTS)` / `parse_blob_ref(s)` | 校验并拆分 `dpe1:<hex>` / `sha256:<hex>` |
| `blob_ref(data)` | blob 字节的引用 `sha256:<hex>` |
| `jcs(value)` | RFC 8785 规范化字符串 |

逐层入口的参数是 TypedDict，便于在编译期发现字段拼错；从线上反序列化、未加类型的 JSON（`Any`）可以直接传入，已标注为 `dict[str, Any]` 的数据请用 `object_hash`（接受 `Mapping[str, Any]`），或先 `cast`。

所有入口都对封闭 schema 做完整校验，未定义字段、未知 category / file_type、超过 2^53−1 的整数、NaN 等一律拒绝。

**原位重算**（契约 1 §6）：各入口的 `contract` 参数决定按哪个契约计算。`DRILL_CONTRACT`（`dpe2`）是仅用于升级演练的假想契约，只在显式传入时接受，不在 `SUPPORTED_CONTRACTS` 中，`parse_hash` 默认拒绝它。

## 常量与类型

- `CONTRACT`（`"dpe1"`）、`SUPPORTED_CONTRACTS`（服务端 capabilities 的 `hash_contracts`）、`DRILL_CONTRACT`、`KNOWN_CONTRACTS`（本包认识的全部契约，含演练契约）；
- `FILE_TYPES`：file_type 封闭枚举，按规范顺序；`CATEGORY_CONTENT_FIELDS`：category → 允许的内容字段（只读映射）。两者与规范 `vectors/manifest.json` 一致，消费方直接 import，不维护副本；
- TypedDict：`ElementObject`、`PageObject`、`DocumentObject`（线上原像），`PageFields`、`DocumentFields`（不含子列表），`ExpandedDocument` / `ExpandedPage`（展开视图），`DocumentHashes` / `PageHashes`（结果）。

## 异常

校验按规范 [core §2.8](https://doc.turingfocus.cn/dpe/latest/spec/core/) 的顺序进行：形状 → category → 封闭 schema → 逐字段。因此同一输入的错误码与其他实现一致，并由规范的拒绝类向量 `invalid_objects` 校验。

全部继承 `DpeHashError`（它是 `ValueError` 的子类）。每个异常带两个属性：`.code` 是规范错误码，`.path` 是出错位置的 RFC 6901 JSON Pointer（相对于传入的对象），可用于日志或 problem 的 `detail` 文本（HTTP problem 体不携带违例位置，core §2.8）。

| 异常 | `code` |
| --- | --- |
| `ValidationError`，及其子类 `UndefinedFieldError`、`FileTypeUnknownError`、`IntegerOutOfRangeError`、`InvalidUnicodeError`（I-JSON：孤立代理项） | `DPE_VALIDATION` |
| `CategoryUnknownError` | `DPE_CATEGORY_UNKNOWN` |
| `ContractUnsupportedError` | `DPE_CONTRACT_UNSUPPORTED` |

本包接收的是已解析的对象。core §2.8 第 0 步要求整个请求体是 I-JSON，这一步先于契约头判定，由服务端负责：

- **重复键**：解析时用拒绝重复键的解析器，例如 `json.loads(..., object_pairs_hook=...)`；
- **孤立代理项**：解析后先用 `has_invalid_unicode(body)` 检查整个请求体（含 `staging_session` 等信封字段），命中即返回 `DPE_VALIDATION`。Python 的 `json` 会放行孤立代理项。
