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

> 数据模型（`models`）与完整的协议核心（`protocol`：只读操作、commit / delete / move、暂存路径 negotiate / upload / deliver）已落地；传输适配（`http::DpeClient`，reqwest / tokio 的异步客户端）随 #22 落地，对参考服务端的跨语言集成验收见 #25。

## 传输适配（默认 feature `http`）

```rust,no_run
use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::Precondition;
use dpe_sdk::{ExpandedDocument, CONTRACT};

# async fn example() -> Result<(), dpe_sdk::Error> {
let client = DpeClient::new("https://example.com/r/42")?;
let document = ExpandedDocument::parse(
    r#"{"file_type": "md", "pages": [{"elements": [{"category": "Title", "text": "季度报告"}]}]}"#,
    CONTRACT,
)?;
let result = client
    .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
    .await?;
assert_eq!(result.status.as_str(), "created");
# Ok(())
# }
```

- 只接受 remote URL 与可插拔的 `CredentialProvider`（每个请求发送前取一次，轮换中的短时凭证在重发前会重新取）；
- 传输层只做 I/O：拼 URL、注入凭证、按策略 gzip（请求体，HTTP 绑定 §7）、把响应还原给核心；协议判定与写操作的重试全在 sans-IO 核心；
- 例外只有三类：`retry_safe` 的请求遇网络失败、任何请求遇「确定未发出」的建连失败、按 `code` 判定的可重试错误（`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`，等 `Retry-After` 后原样重发，等待超过 300 秒不吸收）；
- 缺省超时 30 秒、不跟随重定向、大 blob 的源读取进工作线程；`send` / `drive` 是公开的驱动缝。
