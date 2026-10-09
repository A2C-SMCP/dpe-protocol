# dpe-hash

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的独立 hash 核心：按 [hash 契约 1](https://doc.turingfocus.cn/dpe/latest/spec/hash-contract-1/) 计算元素、页、文档三层 hash（`dpe1:` 前缀），实现 file_uri 语法规范化，并导出契约常量。

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

重复键：serde_json 默认保留最后一个。需要按 I-JSON 拒绝重复键时（如服务端读取请求体），调用方须使用严格解析。
