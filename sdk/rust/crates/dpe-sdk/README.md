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
- remote 按 HTTP 绑定 §1 的文法校验（`dpe_sdk::remote_url::check_remote_url`，即 dpe-hash 的同一函数，与实例定义的 `remote.url` 同一个判定）：http / https、已是 core §1.1 规范化形式、无查询串与片段、无 userinfo、不写默认端口、路径不以 `/` 结尾且无点段；合法即原样使用。**升级须知**（0.4 起，不兼容）：此前构造时去掉的尾部 `/`，以及大写 scheme、显式默认端口、`127.1` 这类数字主机、非 RFC 5952 形式的 IPv6，现在都报 `InvalidArgument`；
- 传输层只做 I/O：拼 URL、注入凭证、按策略 gzip（请求体，HTTP 绑定 §7）、把响应还原给核心；协议判定与写操作的重试全在 sans-IO 核心；
- 例外只有三类：`retry_safe` 的请求遇网络失败、任何请求遇「确定未发出」的建连失败、按 `code` 判定的可重试错误（`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`，等 `Retry-After` 后原样重发，等待超过 300 秒不吸收）；
- 缺省超时 30 秒、不跟随重定向、大 blob 的源读取进工作线程；`send` / `drive` 是公开的驱动缝；
- 所有操作的 future 都是 `Send`，可以交给 `tokio::spawn`。**不兼容变更（自 0.3.x 之后的下一个次版本起）**：`deliver` 与推送器 `push` 的 `blobs` 回调（`BlobProvider`）须为 `Send`；捕获 `Rc`、`RefCell`、`Cell` 等非 `Send` 状态的闭包不再能编译，请改用 `Arc`、`Mutex`、原子类型，或直接捕获 `&mut` 局部变量（见下文「媒体来源」的示例）。

## 增量推送（`pusher`）

`StatefulPusher` 把一批文档变成「只推变化的部分」，删除与移动意图按 connector 契约 §7.2 执行与回报，与 Python 侧 `dpe_sdk.pusher` 行为对等：

```rust,no_run
use std::collections::HashMap;
use std::sync::Arc;

use dpe_sdk::http::DpeClient;
use dpe_sdk::pusher::{MemoryStateStore, StatefulPusher};
use dpe_sdk::{ExpandedDocument, CONTRACT};

# async fn example() -> Result<(), dpe_sdk::Error> {
let client = DpeClient::new("https://example.com/r/42")?;
let mut pusher = StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), 4)?;
let document = ExpandedDocument::parse(
    r#"{"file_type": "md", "pages": [{"elements": [{"category": "Title", "text": "季度报告"}]}]}"#,
    CONTRACT,
)?;
let results = pusher
    .push(&[("s3://bucket/a.md".to_owned(), document)], &HashMap::new(), None)
    .await?;
println!("{}", results[0].status); // created / updated / unchanged / achieved / conflict / failed
pusher
    .move_documents(&[("s3://bucket/a.md".to_owned(), "s3://bucket/b.md".to_owned())], &HashMap::new())
    .await?;
pusher.delete(&["s3://bucket/b.md".to_owned()], &HashMap::new()).await?;
# Ok(())
# }
```

- **本地状态只作缓存**：同步状态记录每篇文档最近一次确认的 remote doc_hash，丢失时退化为以即时读到的值作 base，结果仍然正确；
- **意图日志**：写入前先经 `StateStore::save` 持久化预期的 remote 状态，`save` 返回 `Ok` 之后才发写请求；结果确定即清除，只有结果未知才保留，下一轮按意图对账；
- **绝不自动 force、不在内部重试冲突**：冲突与失败按条目如实回报（含服务端原样的错误码、重读到的 remote 状态、move 的问题侧别），`expected_base` 让上层以重读值重提；
- 缺省 `MemoryStateStore` 不保证崩溃持久，落盘实现属于运行器的状态目录（connector §4.4）；
- 任何一次保存失败后，store 都可能与内存不一致（收尾失败时落后、写前失败而落盘实现写完才报错时领先）：继续使用同一个实例直到下一次保存成功，在那之前不要用同一个 store 新建实例（跨实例的保证属于运行器）；
- `StateStore` 在异步方法里被同步调用、调用期间占住当前 worker，实现应尽量缩短耗时；多线程运行时上重 I/O（`fsync` 等）可用 `tokio::task::block_in_place` 包住（current_thread 运行时上不可用）；
- move 的目标已持有与源相同的内容时（读到的目标值等于 base，如失败轮已建出目标，#165），不发 move，以 base 删除源并判 `moved`；目标内容不同仍为条目失败（`Side::To`），绝不在目标上写入；
- 同一批 move 里任何 URI 在 from∪to 中至多出现一次（链式、共用目标须由调用方保序拆批）；
- 写操作在调用方的同一个任务内有界并发（`concurrency`），不经 `tokio::spawn`；`push` / `delete` / `move_documents` 的 future 都是 `Send`，调用方可以把整次操作交给 `tokio::spawn`（推送器借用客户端，任务内持有 `Arc<DpeClient>` 再构造推送器即可满足 `'static`；`blobs` 回调须为 `Send`，见上文「传输适配」）；
- `push` 的最后一个参数 `blobs` 原样交给每条 `deliver`，按 `sha256:` 引用给出服务端缺少的 blob 字节（可用下文 `media` 构造来源），未变文档不会调用它；
- `flush()` 是补存入口：任何一次保存失败后、结束使用前，用同一个实例调用它，把内存状态整批写回 store；
- `client()` 返回构造时传入的客户端，运行器经它取得 remote 能力，不另建客户端。

## 媒体来源（`media`）

媒体字节只经 blob 上传通道投递，元素对象的 `blob` 字段只认 `sha256:` 引用。`media` 只做**来源适配**，把调用方显式给出的来源变成核心的 `BlobSource`：本地路径、可 seek 的流，或 connector 契约 §6.7 的 `read_blob` 回调。它会算出引用与总长，并支持按任意偏移读取。分块、断点续传与偏移重同步都在核心里完成。行为与 Python 侧 `dpe_sdk.media` 对等：

```rust,no_run
use dpe_sdk::http::DpeClient;
use dpe_sdk::media::{source_from_path, DEFAULT_CHUNK_BYTES};
use dpe_sdk::protocol::Precondition;
use dpe_sdk::{ExpandedDocument, CONTRACT};

# async fn example() -> Result<(), Box<dyn std::error::Error>> {
let client = DpeClient::new("https://example.com/r/42")?;
let path = "figures/cover.png";
let reference = source_from_path(path, DEFAULT_CHUNK_BYTES)?.blob_ref().to_owned();
let document = ExpandedDocument::parse(
    &format!(
        r#"{{"file_type": "md", "pages": [{{"elements": [{{"category": "Image", "blob": "{reference}", "mime_type": "image/png"}}]}}]}}"#
    ),
    CONTRACT,
)?;
// 来源在 blobs 回调里才构造：未变文档不会要求 blob，大文件也就不会被再读一遍。
// 回调只能返回 Option，I/O 错误先存下来，deliver 失败时据此给出真实原因
let mut source_error = None;
let mut blobs = |_: &str| {
    source_from_path(path, DEFAULT_CHUNK_BYTES)
        .map_err(|error| source_error = Some(error))
        .ok()
};
let delivered = client
    .deliver("s3://bucket/a.md", &document, Precondition::IfAbsent, Some(&mut blobs), None)
    .await;
if let Some(error) = source_error {
    return Err(error.into());
}
delivered?;
# Ok(())
# }
```

- 内存占用不随文件大小增长：路径与流按块流式计算哈希。`read_blob` 来源读两趟：第一趟算出摘要与总长，之后按偏移请求同一份快照。
- 不解析 url，也不识别 metadata 里的路径字符串，来源一律由调用方显式给出。
- 文件在投递期间必须保持不变；内容变了会被服务端以 `DPE_HASH_MISMATCH` 如实拒绝。
- 句柄声明了 `size` 时，`source_from_read_blob` 请一并传入 `expected_size`。
- 错误一律是 `std::io::Error`：参数不合法（含不可 seek 的流）为 `InvalidInput`；`read_blob` 违反契约或总长与声明不符为 `InvalidData`；其余 I/O 错误原样透传。
