# dpe-hash

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的独立 hash 核心：按 [hash 契约 1](https://doc.turingfocus.cn/dpe/latest/spec/hash-contract-1/) 计算元素、页、文档三层 hash（`dpe1:` 前缀），实现 file_uri 语法规范化、file_type 语法校验（`validate_file_type` / `is_valid_file_type`，core §2.5）与 I-JSON 严格解析（`parse_ijson`，core §2.8 第 0 步），并导出契约常量（含嵌套深度上界 `MAX_NESTING_DEPTH` 与计数函数 `nesting_depth`）与 file_type 推荐登记表（`RECOMMENDED_FILE_TYPES`）。

- 不绑定异步运行时、不含 I/O，仅依赖 `sha2`、`serde`、`serde_json` 与 `ryu`；
- 逐字节通过规范仓库的一致性向量，与 Python [`dpe-hash`](https://pypi.org/project/dpe-hash/) 行为对等；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

```rust
use dpe_hash::{content_hash, document_hashes, object_hash, ObjectKind, CONTRACT};
use serde_json::json;

let element = json!({"category": "Title", "text": "季度报告"});
let h = content_hash(&element, CONTRACT)?;                       // 逐层：content_hash / page_hash / doc_hash
assert_eq!(h, object_hash(&element, ObjectKind::Element, CONTRACT)?); // 线上原像

let doc = json!({"file_type": "md", "pages": [{"elements": [element]}]});
let hashes = document_hashes(&doc, CONTRACT)?;                   // 展开视图一次算出三层
assert_eq!(hashes.pages[0].elements, [h]);
```

错误为 `dpe_hash::Error`：`kind()` 与 Python 异常子类一一对应，`code()` 是规范错误码（`DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` / `DPE_CONTRACT_UNSUPPORTED`），`path()` 是出错位置的 RFC 6901 JSON Pointer。

## serde_json feature

本 crate 为 serde_json 开启：

- `float_roundtrip`：浮点解析正确舍入，JCS 的数字序列化以此为前提；
- `arbitrary_precision`：`1e400` 这类越界数值在解析阶段保留，到校验第 4 步再拒绝（core §2.8）。

cargo feature 在同一构建内统一生效：开启 `arbitrary_precision` 后，`#[serde(flatten)]` 与 untagged enum 遇到数字会反序列化失败，依赖本 crate 的模型应避开这两种写法。

## I-JSON 解析

`parse_ijson` 把 JSON 文本严格解析为值（core §2.8 校验顺序第 0 步的解析部分）：拒绝重复键与孤立代理项，错误为 `DPE_VALIDATION`、位置为对象自身；数值不在解析阶段拒绝（越界留到校验第 4 步在出错的值上报告；字面量经 serde_json 构造为 `Number`，拼写可能被规范化，如 `1e2` → `1e+2`、`-0` → `0`，值不变、hash 不受影响）。

实现是自建的单遍解析器，不复用 serde_json 的 `Value` 解析：`arbitrary_precision` 下 serde_json 用内部数字 token `$serde_json::private::Number` 的单键对象表示数字，会把源数据里恰好同形的对象读成数字或误拒——而源即内容（P2），实现不得引入保留键。解析器另设防护性上限（68 层容器 = core §2.8 的 64 加展开视图固定的 4 层），只防递归耗尽栈、不判决合法性：对象的嵌套深度由校验按对象自身判定（`MAX_NESTING_DEPTH` / `nesting_depth`），只要每个 DPE 对象都在上界内，解析不因深度拒收。

hash 函数接受 `&Value`：从 JSON 文本自行构造值时必须用 `parse_ijson`——serde_json 直接 `from_str` 默认保留重复键的最后一个，且 `arbitrary_precision` 下会把与内部数字 token 同形的对象改写为数字。类型化结构（`ElementObject` 等）只实现 `Serialize`、未实现 `Deserialize`（同一原因），用结构体字面量构造；需要带规范校验与「原样表示」的数据模型用 dpe-sdk 的 `models`。
