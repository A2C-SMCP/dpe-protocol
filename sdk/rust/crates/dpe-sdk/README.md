# dpe-sdk

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的 Rust SDK：把结构化文档正确、增量、可靠地投递到任意 DPE 服务端。

- sans-IO 协议核心 + reqwest / tokio 传输适配（默认 feature `http`，核心不绑定异步运行时）；
- 数据模型（[`models`](https://docs.rs/dpe-sdk/latest/dpe_sdk/models/)）：文档 / 页 / 元素三层对象的封闭 schema（core §2），校验与 hash 全部委托 `dpe-hash`（错误码与位置同一致性向量），保留输入的原样表示；
- 只接受 remote URL 与可插拔的 `CredentialProvider`，不封装任何服务端私有端点；
- hash 由 [`dpe-hash`](https://crates.io/crates/dpe-hash) 计算（同版本精确依赖）；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

```rust
use dpe_sdk::{ElementObject, ExpandedDocument, CONTRACT};
use serde_json::json;

let element =
    ElementObject::from_value(&json!({"category": "Title", "text": "季度报告"}), CONTRACT)?;
assert!(element.content_hash(CONTRACT)?.starts_with("dpe1:"));

let doc = ExpandedDocument::parse(
    r#"{"file_type": "md", "pages": [{"elements": [{"category": "Title", "text": "季度报告"}]}]}"#,
    CONTRACT,
)?;
assert!(doc.doc_hash(CONTRACT)?.starts_with("dpe1:"));
```

> 数据模型（`models`）已落地；协议核心与传输适配随 Milestone `rust-sdk v0.1.6` 落地。
