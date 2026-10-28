# dpe-sdk

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的 Rust SDK：把结构化文档正确、增量、可靠地投递到任意 DPE 服务端。

> **从 0.4 升级**：全部不兼容变更与行为变化按读者汇总在 [0.5 升级须知](https://doc.turingfocus.cn/dpe/latest/docs/upgrade/v0.5/)，下文各处的升级说明只是摘要。

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
- remote 按 HTTP 绑定 §1 的文法校验（`dpe_sdk::remote_url::check_remote_url`，即 dpe-hash 的同一函数，与实例定义的 `remote.url` 同一个判定）：http / https、已是 core §1.1 规范化形式、无查询串与片段、无 userinfo、不写默认端口、路径不以 `/` 结尾且无点段；合法即原样使用，不合法报 `InvalidArgument`；
- 传输层只做 I/O：拼 URL、注入凭证、按策略 gzip（请求体，HTTP 绑定 §7）、把响应还原给核心；协议判定与写操作的重试全在 sans-IO 核心；
- 请求在本地构造阶段失败（非法 A-label 主机、请求头字段值不符合 HTTP 绑定 §6：CTL、obs-text、首尾 SP/HTAB）时，以 `ErrorKind::LocalRequest` 上报、默认 `outcome_unknown=false`（写操作此前有不确定尝试时最终仍为真）、不重发；消息固定，不回显 URL / 主机 / 凭证，原始库错误经 `source()` 取回（#207、#253）；
- 例外只有三类：`retry_safe` 的请求遇网络失败、任何请求遇「确定未发出」的建连失败、按 `code` 判定的可重试错误（`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`，等 `Retry-After` 后原样重发，等待超过 300 秒不吸收；`Retry-After` 只按 RFC 9110 文法解析，即 dpe-hash 的 `retry_after_seconds`，经 `dpe_sdk::protocol` 重新导出。0.4 起首尾只去 SP / HTAB（以前用 `trim()` 去掉全部 Unicode 空白），rfc850 两位年份在世纪交替前后改为取「不晚于现在加 50 年」的最晚年份，以前固定按当前世纪读）；
- **非协议错误的 `retry_after`**：错误状态码（≥ 400）的响应既无 problem 体、也无有效 `DPE-Error-Code` 时，`ErrorKind::UnexpectedResponse` 仍会解析 `Retry-After` 头并通过 `Error::retry_after()` 给出（无合法头时为 `None`）；成功状态码（2xx / 3xx）的响应体或形状不合规同样按该错误上报，但 `retry_after` 恒为 `None`。该值只作信息透传，传输层不会重试非协议错误；
- **代理只来自环境变量**（#132）：构造客户端时按下面的规则判定一次 remote 走哪个代理，**从不读操作系统的代理设置**（macOS / Windows 的系统代理、PAC 都不看）。两个 SDK 规则相同，共用一张用例表 `sdk/testdata/proxy_env.json`：
  - https remote 用 `HTTPS_PROXY`，http remote 用 `HTTP_PROXY`，都没有时用 `ALL_PROXY`；每个名字先看大写、再看小写，空值视为未设置；
  - 环境中存在 `REQUEST_METHOD`（CGI）时忽略全部代理变量；
  - `NO_PROXY` 逗号分隔：`*` 匹配一切；IP 与 CIDR（如 `10.0.0.0/8`、`::1`）按地址匹配；其余条目是域名，匹配自身及其子域（`example.com` 与 `.example.com` 等价，不匹配 `notexample.com`）；条目不带端口；
  - 代理 URL 只支持 `http://` / `https://`（不带 scheme 按 `http://`，`user:pass@` 作为代理的 Basic 认证；密码里的 `@` 须写成 `%40`）；`socks5://` 等在构造客户端时报参数错误；
  - userinfo 转成 `Proxy-Authorization` 的现状（两个 SDK 行为一致）：先做百分号解码（`u%40x:p%3Aw` → `Basic dUB4OnA6dw==`）；只有用户名（`user@`）时发 `Basic base64("user:")`（`dXNlcjo=`）；userinfo 为空（`@`）时两边都不发 `Proxy-Authorization`（空 userinfo 被 URL 解析丢弃，不会生成 `Basic Og==`）；`:pass@` 两边都发 `Basic base64(":pass")`；
  - 规则全部按字符判定，两个 SDK 不借助各自 URL 库的解析：空白只指 ASCII 空白（空格、`\t`、`\n`、`\r`、`\v`、`\f`），不区分大小写只按 ASCII；代理 URL 整体只能含 RFC 3986 允许的 ASCII 字符且不超过 4096 个（`\`、空白、控制字符、非 ASCII 都不合法），userinfo 只能含 RFC 3986 userinfo 字符；主机必须是方括号内的 IPv6（不带 zone）、点分十进制 IPv4 或 ASCII 字母 / 数字 / `-` 组成的域名（不接受 `xn--` 标签），端口写了就是不超过 65535 的十进制数，authority 之后只允许单个 `/`，不合法的值在构造客户端时报错（错误消息不回显值）；
  - 关闭代理：不设这些变量，或设 `NO_PROXY=*`；
  - 连不上代理、HTTPS 隧道（`CONNECT`）被代理拒绝都按「确定未发出」处理；明文 HTTP 经代理转发时代理的应答（如 502）是普通响应。

- **`deliver` 的选路**（core §3.2，#212，与 Python 一致）：新建（`IfAbsent`）整篇内联放得下 `max_payload_bytes` 就走快路径（一次 commit）；更新（`BaseHash` / `Force`）只有整篇内联不超过 `protocol::SMALL_UPDATE_BYTES`（64 KiB）才走快路径，否则走差量路径，**页与元素都只传缺失**：`negotiate` 只交文档对象得到缺失的页（没有就直接 commit，两次往返），再 `negotiate` 一次只附带这些页、得到其中缺失的元素，commit 引用会话并只内联缺失的元素（不缺 blob 时三次往返，与新页数量无关）；缺失的 blob 由 commit 的缺失清单给出、补传后重提；请求体放不下时逐层上传。选路只看前置条件与本地算出的大小，不额外读 remote；CAS 失败后以 `session` 复用会话时，差量路径内联过的元素不在会话里，会经缺失清单再上传一次；
- **超时**：缺省 30 秒，作用于**整个请求**（从开始连接到响应体读完，`reqwest` 的 total deadline）；连接阶段另有 90% 的连接预算（`connect_budget`，对齐 Python 侧 httpx 的 `ConnectTimeout` 语义），`DpeClientBuilder::timeout(None)` 关闭。不跟随重定向、大 blob 的源读取进工作线程；`send` / `drive` 是公开的驱动缝。与 Python 的分阶段超时不同，慢速流式响应可能在这里超时而在 Python 成功（见 [Python dpe-sdk README](https://github.com/A2C-SMCP/dpe-protocol/blob/main/sdk/python/packages/dpe-sdk/README.md)）；
- 所有操作的 future 都是 `Send`，可以交给 `tokio::spawn`。`deliver` 与推送器 `push` 的 `blobs` 回调（`BlobProvider`）须为 `Send`：捕获 `Rc`、`RefCell`、`Cell` 等非 `Send` 状态的闭包不能编译，请改用 `Arc`、`Mutex`、原子类型，或直接捕获 `&mut` 局部变量（见下文「媒体来源」的示例）。

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
- **意图日志**：写入前先经 `StateStore::save` 持久化预期的 remote 状态，`save` 返回 `Ok` 之后才发写请求；结果确定即清除，只有结果未知才保留，下一轮按意图对账。`StateStore` 的错误包装为 `ErrorKind::StateStore`，原错误经 `source()` 取回；`outcome_unknown()` 的规则：写前持久化失败为 `false`（写请求确定未发出），收尾保存失败按本批是否发过写请求（`wrote`）取 `true` / `false`，`flush()` 失败恒为 `false`；
- **绝不自动 force、不在内部重试冲突**：冲突与失败按条目如实回报（含服务端原样的错误码、重读到的 remote 状态、move 的问题侧别），`expected_base` 让上层以重读值重提；
- 缺省 `MemoryStateStore` 不保证崩溃持久，落盘实现属于运行器的状态目录（connector §4.4）；
- 任何一次保存失败后，store 都可能与内存不一致（收尾失败时落后、写前失败而落盘实现写完才报错时领先）：继续使用同一个实例直到下一次保存成功，在那之前不要用同一个 store 新建实例（跨实例的保证属于运行器）；
- `StateStore` 在异步方法里被同步调用、调用期间占住当前 worker，实现应尽量缩短耗时；多线程运行时上重 I/O（`fsync` 等）可用 `tokio::task::block_in_place` 包住（current_thread 运行时上不可用）；
- move 的目标已持有与源相同的内容时（读到的目标值等于 base，如失败轮已建出目标，#165），不发 move，以 base 删除源并判 `moved`；目标内容不同仍为条目失败（`Side::To`），绝不在目标上写入；
- 同一批 move 里任何 URI 在 from∪to 中至多出现一次（链式、共用目标须由调用方保序拆批）；
- **批量读被拒时逐条定位**（#145）：判定前的 `batch_head` 被确定、不可重试地拒绝（如其中一个 URI 不在授权前缀内）时，推送器逐 URI `head`（并发至多 `concurrency`），只让读被拒的条目以 `Failed` 返回（`error` 为原错误、code 原样、`side` 为 `None`），其余照常写入；逐条读都通过（拒绝与具体 URI 无关）时全部条目按逐条读到的值照常写入（connector §7.1，写入仍经 CAS）。两种情况都经 `log` 门面记一条 warn（target `dpe_sdk::pusher`，带原错误码与计数；只有文本消息、不带结构化键值——未启用 `log` 的 kv 特性，需要结构化的使用方自行解析或换 tracing 集成；Python 侧用 logging 的结构化 `extra`，见 [Python dpe-sdk README](https://github.com/A2C-SMCP/dpe-protocol/blob/main/sdk/python/packages/dpe-sdk/README.md)；新增依赖 `log`，随 feature `http`）。网络失败、可重试错误耗尽、不合规响应与契约不兼容仍整批返回 `Err`；
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
