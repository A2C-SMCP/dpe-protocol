# dpe-protocol（Rust）

DPE 投递协议层 SDK 的 Rust 实现：以 Document 为边界，把上层产出的 DPE 文档通过 dpe-push/1 增量投递到 TFRobot。connector 的形态、对接与编排都由上层负责，本 crate 不涉及。设计见 [docs/design.md](../docs/design.md)。

## 上层如何使用

**投递**：`StatefulPusher` 会自动带上次的 `base_doc_hash`，并记录源侧 fingerprint。

```rust
use dpe_protocol::push::DpePushClient;
use dpe_protocol::state::JsonFileStateStore;
use dpe_protocol::{make_dpe_uri, StatefulPusher};

let client = DpePushClient::with_reqwest("https://robot.example.com")?.with_token("<JWT with dpe:push>");
let state = JsonFileStateStore::new(".dpe-state/feishu.json");
let pusher = StatefulPusher::new(&client, &state);

for item in feishu.list_changed().await? {                       // 上层逻辑
    let uri = make_dpe_uri("feishu-acme", &format!("wiki/{}", item.token))?;
    if pusher.needs_push(uri.as_str(), Some(&item.revision)).await? {
        pusher.push(&feishu.build_document(&item).await?, Some(item.revision.clone())).await?;
    }
}
```

也可以不用状态：`client.push(&doc, last_doc_hash).await?`，自行保存返回的 `doc_hash`。

**自检**：dev-dependency 启用 `testing` feature，对同一份未修改的源数据产出两次文档后检查。

```rust
use dpe_protocol::testing::{check_documents, CheckOptions};

let options = CheckOptions { expected_file_uri: Some(uri.as_str()), ..Default::default() };
check_documents(&first, &second, options).await.assert_ok();
```

示例见 `examples/push_markdown_dir.rs`、`examples/push_dpe_json.rs`。

## Features

| feature | 默认 | 作用 |
| --- | --- | --- |
| `reqwest` | ✅ | 基于 reqwest + rustls 的 `ReqwestTransport`。关闭后可以实现自己的 `Transport` |
| `testing` | | `FakeRobotServer` 与文档一致性检查 `check_documents` |

## 与 Python SDK 的差异

- HTTP 层抽象为 `Transport` trait，可以替换成任意 HTTP 栈
- `JsonFileStateStore` 的文件格式与 Python 相同，两者可以互读

## 开发

```bash
cargo test                                          # 含共享内核向量 ../vectors/v1
cargo clippy --all-targets --all-features -- -D warnings
cargo fmt
cargo run --all-features --example push_markdown_dir -- <markdown-dir> <tenant> [prefix]
```

最低 Rust 版本：1.80。

## License

MIT
