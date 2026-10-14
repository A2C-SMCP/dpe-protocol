//! sans-IO 协议核心（core §3、HTTP 绑定）：只产出请求描述、消费响应描述，不做任何 I/O。
//!
//! 与 Python `dpe_sdk.protocol` 行为对等，API 形态按 Rust 惯例：**操作是显式状态机**
//! （[`Operation`]）。`poll` 产出下一个要发送的 [`Request`]（或完成时给出结果），驱动方发送它
//! 并把 [`Response`] 经 `on_response` 送回；多轮往返（batch_head 分批等）都在操作内部完成，
//! 同步传输用 [`drive`]，异步传输按同一契约自行循环——核心不引入任何异步运行时。
//!
//! **驱动约定**：发送请求失败的传输异常（连接失败、超时等）经 `on_transport_error` 回送给
//! 操作。写操作（commit / delete / move）把回送的任何异常，以及无 DPE 错误码的 502 / 504
//! （网关已转发、源站结果未知），都视为「请求可能已到达服务端、响应丢失」，由操作自己判定
//! ——只有操作知道自己在重发。请求的 [`retry_safe`](Request::retry_safe) 为 `false` 时，一旦
//! 请求可能已经发出，传输层 **MUST NOT 自行重发**；确定未发出的失败（如建连失败）与
//! `DPE_RATE_LIMITED` / `DPE_UNAVAILABLE` 的原样重试（[`retryable`](crate::Error::is_retryable)）
//! 不受这条限制，它们不改变请求是否生效的判定——带码的这两种响应依 core §6 表示请求未产生
//! 任何效果（服务端 MUST 只在请求未生效时返回它们）；不带码的 429 / 503 不在此列，按结果未知
//! 处理。只读请求（`retry_safe` 为 `true`）原样重发总是安全的，传输层可以自行重发。
//!
//! **状态机约定**：在等待响应时再次 `poll`、向没有在飞请求的操作回送响应或传输错误、操作
//! 完成后再 `poll`，都是用法错误，返回 [`ErrorKind::InvalidArgument`] 错误而不是 panic，且
//! **不改变状态**（对齐 Python 生成器的语义：`send` 到未启动的生成器报错后仍可正常驱动）。
//! `on_response` / `on_transport_error` 返回 `Err`（业务或传输错误）之后操作进入终态，
//! 此后一切驱动都是用法错误；这些用法错误不会从正常驱动循环产生。
//!
//! **契约**：只使用调用方显式指定的一个 hash 契约（如 [`CONTRACT`](crate::CONTRACT)），服务端
//! 不接受时失败，不换契约、不降级（core §3.1）；除 `GET capabilities` 外每个请求都带
//! `DPE-Hash-Contract`，响应中的每个 hash 都必须属于该契约。
//!
//! **错误**（HTTP 绑定 §3.2、§5）：按 code 分派，不按状态码。有 problem 体时取体中的 `code`，
//! 否则取 `DPE-Error-Code` 头（HEAD 不能带体）；两者都没有就不是 DPE 错误，抛
//! [`ErrorKind::UnexpectedResponse`]，不猜测 code。成功响应的报文不合规同样按非协议错误上报，
//! 不冒充请求侧的 `DPE_VALIDATION`。
//!
//! **请求目标**：`target` 是相对 remote 的路径与已编码的查询串（HTTP 绑定 §1），传输层把它
//! 原样拼在 remote 之后（remote 不带尾部 `/`），**MUST NOT 再次编码**。

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::fmt;
use std::sync::Arc;

use serde_json::{json, Value};

use crate::errors::{Error, ErrorKind};
use crate::models::{ElementObject, ExpandedDocument, PageObject};
use crate::wire::{
    BatchHeads, Capabilities, CommitResult, CommitStatus, Delta, ElementUploadMissing, ListPage,
    Missing, MoveResult, NegotiateResult, PageUploadMissing, Skeleton, StagingSession,
};

/// 本 SDK 实现的协议版本（capabilities 的 `protocol`）。
pub const PROTOCOL: &str = "dpe/1";
/// 写操作在响应丢失后原样重发的次数上限的缺省值（core §5.2；见 [`ProtocolCore::new`]）。
pub const DEFAULT_MAX_RESENDS: u32 = 2;
/// 除 `GET capabilities` 外每个请求都带的契约声明头（HTTP 绑定 §3.1）。
pub const CONTRACT_HEADER: &str = "DPE-Hash-Contract";
/// 文档资源的响应头：doc_hash 的权威值（HTTP 绑定 §3.1）。
pub const DOC_HASH_HEADER: &str = "DPE-Doc-Hash";
/// DPE 错误响应的错误码头（HTTP 绑定 §5）；HEAD 响应只有它、没有 problem 体。
pub const ERROR_CODE_HEADER: &str = "DPE-Error-Code";
/// 暂存会话续期后的过期时间（negotiate / upload / 断点查询的响应头；core §3.4）。
pub const SESSION_EXPIRES_HEADER: &str = "DPE-Session-Expires";
/// 分块上传与断点查询的本会话已收字节数（HTTP 绑定 §4.7）。
pub const UPLOAD_OFFSET_HEADER: &str = "DPE-Upload-Offset";

/// JSON 请求体的媒体类型。
const JSON: &str = "application/json";

/// 驱动方回送的传输异常（连接失败、超时等）。核心不窥探其内容，只按「响应丢失」判定；
/// 传输层能确定「请求没发出」时用 [`not_sent`] 包装，写操作据此判定「结果是否未知」
/// （见 [`Error::outcome_unknown`]）。
pub type TransportError = Box<dyn std::error::Error + Send + Sync + 'static>;

/// 「请求确定没有发出」的标记（如建连失败）：传输层在能确定时用 [`not_sent`] 包装。
///
/// 写操作只把**不能确定没发出**的传输错误记为「结果是否未知」为真（见
/// [`Error::outcome_unknown`]）：核心用 `downcast_ref::<NotSent>()` 判定，没有这个标记就
/// 按「可能已发出」处理。
#[derive(Debug)]
pub struct NotSent(TransportError);

impl NotSent {
    /// 原始传输错误（被标记为「确定没发出」）。
    pub fn error(&self) -> &(dyn std::error::Error + 'static) {
        self.0.as_ref()
    }
}

impl fmt::Display for NotSent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for NotSent {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.0.as_ref())
    }
}

/// 把传输错误标记为「请求确定没有发出」（如建连失败、请求尚未写入连接）：写操作据此把
/// 「本次写入是否可能已生效」记为否（[`Error::outcome_unknown`]）。传输层能确定时才用。
pub fn not_sent(error: impl Into<TransportError>) -> TransportError {
    Box::new(NotSent(error.into()))
}

/// 该传输错误是否确定「请求没发出」。
fn is_not_sent(error: &TransportError) -> bool {
    error.downcast_ref::<NotSent>().is_some()
}

/// HTTP 方法（v1 绑定只用到这五个）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Method {
    Get,
    Head,
    Post,
    Put,
    Delete,
}

impl Method {
    /// 线上拼写（大写）。
    pub fn as_str(self) -> &'static str {
        match self {
            Method::Get => "GET",
            Method::Head => "HEAD",
            Method::Post => "POST",
            Method::Put => "PUT",
            Method::Delete => "DELETE",
        }
    }
}

impl fmt::Display for Method {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// 头字段集合：名字按插入时的拼写保存，读取按大小写不敏感（HTTP 语义）；同名（不分大小写）
/// 后写覆盖先写；遍历按键的顺序（稳定、确定，不承诺插入序）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Headers(BTreeMap<String, String>);

impl Headers {
    /// 空集合。
    pub fn new() -> Self {
        Headers(BTreeMap::new())
    }

    /// 读取头字段（名字不区分大小写）。
    pub fn get(&self, name: &str) -> Option<&str> {
        if let Some(value) = self.0.get(name) {
            return Some(value);
        }
        self.0
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// 写入头字段；已有的同名（不分大小写）项被替换。
    pub fn insert(&mut self, name: impl Into<String>, value: impl Into<String>) {
        let name = name.into();
        if let Some(existing) = self
            .0
            .keys()
            .find(|key| key.eq_ignore_ascii_case(&name))
            .cloned()
        {
            self.0.remove(&existing);
        }
        self.0.insert(name, value.into());
    }

    /// 按键的顺序遍历 `(名字, 值)`。
    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
    }

    /// 头字段个数。
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// 是否为空。
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// 请求体：完整字节（[`Body::Bytes`]）或延迟的字节区间（[`Body::BlobRange`]，大 blob 的
/// 分块上传用）。适配层 MUST NOT 把 `BlobRange` 本身交给 HTTP 客户端——用
/// [`Request::body_bytes`] 取出字节。
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Body {
    None,
    Bytes(Vec<u8>),
    /// 延迟区间：字节由驱动方取出（同步直接读；异步自行放进工作线程）。
    BlobRange(BlobRange),
}

/// 写操作的前置条件（core §5.1）：三选一，没有「不带前置条件」的形式；写操作默认拒绝无
/// CAS 前置条件（`DPE_PRECONDITION_REQUIRED`）由服务端按此裁决。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Precondition {
    /// `base_hash = H`：H 是读到的 doc_hash（HTTP 绑定：`If-Match: "H"`）。发出前校验它是本次
    /// 声明契约的 hash——不接受裸 hash，也不接受别的契约的值；服务端按值比较、不做格式校验。
    BaseHash(String),
    /// `if_absent`：仅新建（HTTP 绑定：`If-None-Match: *`）。
    IfAbsent,
    /// 无条件覆盖（仅 commit；请求体 `"force": true`，不带条件头）。只在调用方显式传入时
    /// 使用；SDK 绝不自动改用 force（core §5.2）。
    Force,
}

impl Precondition {
    /// `base_hash` 的值（其余前置条件为 `None`）。
    pub fn base_hash(&self) -> Option<&str> {
        match self {
            Precondition::BaseHash(value) => Some(value),
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// 暂存路径的基础类型（core §3.4、HTTP 绑定 §4.5–§4.7）
// ---------------------------------------------------------------------------

/// 客户端持有的暂存会话（core §3.4）：`id` 与规范化 `uri` 供 upload / commit 引用。
///
/// `expires_at` 随每个响应续期（`DPE-Session-Expires` 头），**只作诊断**（日志、展示）——
/// 是否过期只信服务端信号：410（`DPE_SESSION_EXPIRED`）与断点查询响应里的过期时间。本地时钟
/// 偏差会让仍有效的会话被提前放弃（新会话从空开始、已暂存内容全部重传），因此不用本地时钟
/// 判定。会话只作缓存：失效时重开 negotiate，结果与不复用时一致。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    /// 会话 id（引用它上传与提交）。
    pub id: String,
    /// 会话绑定的 file_uri（请求 uri 的规范化形式，core §1.1）。
    pub uri: String,
    /// 过期时间（RFC 3339 UTC 原文）：诊断用，不参与判定。
    pub expires_at: String,
}

/// 缺失 blob 的字节来源回调（`deliver` 的参数）：`sha256:…` 引用 → 字节来源；没有对应
/// 来源时返回 `None`（`deliver` 会如实失败并给出缺失清单）。
///
/// 回调须为 `Send`（#131）：持有它的操作与 `DpeClient::deliver` 的 future 因此是 `Send`，
/// 可以交给 `tokio::spawn`。捕获 `Rc` / `RefCell` 等非 `Send` 状态的闭包改用 `Arc` /
/// `Mutex`（自 0.4 起的不兼容变更）。
///
/// **调用约束（规范性）**：核心只在 [`Operation::poll`] 中同步调用它，取得的 [`BlobSource`]
/// 自带读取器，字节读取（[`Request::body_bytes`]）不再经过回调；不在 `send` 期间或跨 await
/// 调用。增量推送器据此在单任务并发的多条 `deliver` 之间共享同一个回调（`pusher` 的
/// `SharedBlobs`），改动这一点须同步那里。
pub type BlobProvider<'a> = &'a mut (dyn FnMut(&str) -> Option<BlobSource> + Send);

/// blob 字节的读取器：按任意偏移给出字节（可 seek 的二进制流或内存字节都能适配）。
///
/// 实现方按请求的 `[offset, offset + length)` 返回字节；给的字节不足（越过末尾）或读取失败时
/// 返回错误，由 [`Request::body_bytes`] 上报给驱动方（调用方的源有问题，不发请求）。
pub trait BlobReader: Send + Sync {
    /// 读出 `[offset, offset + length)` 的字节；不足时如实返回短读（长度不符由调用方判定）。
    fn read(&self, offset: u64, length: u64) -> std::io::Result<Vec<u8>>;
}

/// blob 字节来源（sans-IO：核心不读源、不算摘要）。
///
/// `blob_ref`（`sha256:…`）与 `size` 在上传前确定：内存字节用 [`BlobSource::from_bytes`]
/// 本地算出，可 seek 的流用 [`BlobSource::from_seekable`]（摘要由调用方算好传入，读流是调用方
/// 的 I/O）；其他来源实现 [`BlobReader`] 后用 [`BlobSource::new`]。摘要由调用方算好传入
/// （如 `dpe_hash::blob_ref`），核心不做本地校验——服务端在字节到齐后校验，可能返回
/// `DPE_HASH_MISMATCH`（core §3.4）。
#[derive(Clone)]
pub struct BlobSource {
    blob_ref: String,
    size: u64,
    reader: Arc<dyn BlobReader>,
}

impl BlobSource {
    /// 由读取器构造；`blob_ref` 与 `size` 由调用方算好传入。
    pub fn new(blob_ref: impl Into<String>, size: u64, reader: Arc<dyn BlobReader>) -> Self {
        BlobSource {
            blob_ref: blob_ref.into(),
            size,
            reader,
        }
    }

    /// 由内存字节构造：`blob_ref` 与 `size` 本地算出（纯计算，不涉及 I/O）。
    pub fn from_bytes(data: impl Into<Vec<u8>>) -> Self {
        let raw = data.into();
        BlobSource {
            blob_ref: dpe_hash::blob_ref(&raw),
            size: raw.len() as u64,
            reader: Arc::new(raw),
        }
    }

    /// 把可 seek 的二进制流适配为来源（媒体辅助、运行器用）。
    ///
    /// `blob_ref` 与 `size` 由调用方算好传入（读取流算 sha256 是调用方的 I/O，不在核心）；
    /// 读取发生在 [`Request::body_bytes`] 被驱动方调用时（异步驱动方自行放到工作线程）。
    pub fn from_seekable<R>(stream: R, blob_ref: impl Into<String>, size: u64) -> Self
    where
        R: std::io::Read + std::io::Seek + Send + 'static,
    {
        BlobSource {
            blob_ref: blob_ref.into(),
            size,
            reader: Arc::new(SeekableReader(std::sync::Mutex::new(stream))),
        }
    }

    /// blob 引用（`sha256:<64hex>`）。
    pub fn blob_ref(&self) -> &str {
        &self.blob_ref
    }

    /// blob 总长度（字节）。
    pub fn size(&self) -> u64 {
        self.size
    }

    /// 读取 `[offset, offset + length)` 的字节（转发给读取器）。
    pub fn read(&self, offset: u64, length: u64) -> std::io::Result<Vec<u8>> {
        self.reader.read(offset, length)
    }
}

impl PartialEq for BlobSource {
    /// 按 `blob_ref` 与 `size` 比较（读取器不参与：它不承载身份）。
    fn eq(&self, other: &Self) -> bool {
        self.blob_ref == other.blob_ref && self.size == other.size
    }
}

impl Eq for BlobSource {}

impl fmt::Debug for BlobSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BlobSource")
            .field("blob_ref", &self.blob_ref)
            .field("size", &self.size)
            .finish_non_exhaustive()
    }
}

/// 内存字节的来源。
impl BlobReader for Vec<u8> {
    fn read(&self, offset: u64, length: u64) -> std::io::Result<Vec<u8>> {
        let start = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(self.len());
        let end = start
            .saturating_add(usize::try_from(length).unwrap_or(usize::MAX))
            .min(self.len());
        Ok(self[start..end].to_vec())
    }
}

/// 可 seek 流的来源（内部串行化访问：`BlobReader::read` 是 `&self`，seek 需要可变性）。
struct SeekableReader<R>(std::sync::Mutex<R>);

impl<R: std::io::Read + std::io::Seek + Send> BlobReader for SeekableReader<R> {
    fn read(&self, offset: u64, length: u64) -> std::io::Result<Vec<u8>> {
        let mut stream = self
            .0
            .lock()
            .map_err(|_| std::io::Error::other("blob 源的锁已中毒"))?;
        stream.seek(std::io::SeekFrom::Start(offset))?;
        let mut block = Vec::with_capacity(usize::try_from(length).unwrap_or(0));
        let mut remaining = length;
        while remaining > 0 {
            let mut piece = vec![0u8; usize::try_from(remaining.min(1 << 16)).unwrap_or(0)];
            let read = match stream.read(&mut piece) {
                Ok(read) => read,
                // 标准 `Read` 约定（同 `read_exact`）：被信号打断就重试
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            };
            if read == 0 {
                break;
            }
            piece.truncate(read);
            block.extend_from_slice(&piece);
            remaining -= read as u64;
        }
        Ok(block)
    }
}

/// 延迟的字节区间（请求体）：核心只描述「谁的 `[offset, offset + length)`」，字节由驱动方经
/// [`Request::body_bytes`] 取出（同步直接读；异步自行放进工作线程）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlobRange {
    pub source: BlobSource,
    pub offset: u64,
    pub length: u64,
}

/// 请求描述。
///
/// - `target`：相对 remote 的路径与已编码的查询串，传输层原样拼接，**MUST NOT 再次编码**；
/// - `retry_safe`：请求可能已到达服务端之后，传输层是否可以自行原样重发（见模块说明）。
///   只读操作为 `true`（capabilities / head / batch_head / get_skeleton / list，以及断点查询）；
///   写路径（commit / delete / move / negotiate / upload_*）为 `false`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: Method,
    pub target: String,
    pub headers: Headers,
    pub body: Body,
    pub retry_safe: bool,
}

impl Request {
    /// 取出请求体的字节（[`Body::BlobRange`] 在这里解出）。`Body::Bytes` 返回一份克隆；
    /// 源给的字节数与声明的区间不符时返回错误（调用方的源有问题），不发请求。
    pub fn body_bytes(&self) -> std::io::Result<Option<Vec<u8>>> {
        match &self.body {
            Body::None => Ok(None),
            Body::Bytes(bytes) => Ok(Some(bytes.clone())),
            Body::BlobRange(range) => {
                let data = range.source.read(range.offset, range.length)?;
                if data.len() as u64 != range.length {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        format!(
                            "blob 源 {} 在 [{}, {}) 上给了 {} 字节（应为 {}）",
                            range.source.blob_ref(),
                            range.offset,
                            range.offset + range.length,
                            data.len(),
                            range.length
                        ),
                    ));
                }
                Ok(Some(data))
            }
        }
    }
}

/// 响应描述。头字段名不区分大小写（构造时统一为小写）；HEAD 响应的 `body` 为空。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    status: u16,
    headers: Headers,
    body: Vec<u8>,
}

impl Response {
    /// 构造响应；头字段名统一转小写（同 Python 侧 `Response`）。
    pub fn new(
        status: u16,
        headers: impl IntoIterator<Item = (impl Into<String>, impl Into<String>)>,
        body: impl Into<Vec<u8>>,
    ) -> Self {
        let mut normalized = Headers::new();
        for (name, value) in headers {
            normalized.insert(name.into().to_ascii_lowercase(), value);
        }
        Response {
            status,
            headers: normalized,
            body: body.into(),
        }
    }

    /// HTTP 状态码。
    pub fn status(&self) -> u16 {
        self.status
    }

    /// 读取头字段（名字不区分大小写）。
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name)
    }

    /// 按键的顺序遍历 `(名字, 值)`（名字已小写）。
    pub fn headers(&self) -> impl Iterator<Item = (&str, &str)> {
        self.headers.iter()
    }

    /// 响应体字节（HEAD 响应为空）。
    pub fn body(&self) -> &[u8] {
        &self.body
    }
}

/// 操作的下一步：发一个请求，或完成并给出结果。
#[derive(Debug)]
pub enum Step<T> {
    /// 发送这个请求，然后把响应（或传输错误）回送给操作。
    Send(Request),
    /// 操作完成。
    Done(T),
}

/// 一次协议操作（sans-IO 状态机）。调用契约见模块说明。
///
/// 用法错误（在等待响应时 `poll`、向没有在飞请求的操作回送、完成后继续）返回
/// [`ErrorKind::InvalidArgument`] 错误；`on_response` / `on_transport_error` 返回 `Err` 后
/// 操作进入终态。
pub trait Operation {
    /// 操作的结果类型。
    type Output;

    /// 取下一步：`Send` 之后必须先回送响应或传输错误，才能再次调用。
    fn poll(&mut self) -> Result<Step<Self::Output>, Error>;

    /// 回送响应（发送成功时）。`Err` 意味着操作以该错误终止（用法错误除外——见
    /// [`ErrorKind::InvalidArgument`]，只报错、不改变状态）。
    fn on_response(&mut self, response: Response) -> Result<(), Error>;

    /// 回送传输异常（发送失败时）。操作要么把它作为终止错误返回，要么自行处理（写操作的
    /// 「响应丢失」判定）。`Err` 意味着操作以该错误终止（用法错误同上）。
    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error>;
}

/// 同步驱动一个操作直到完成；`send` 的错误经 [`Operation::on_transport_error`] 回送（见模块
/// 说明的驱动约定）。
pub fn drive<O: Operation>(
    mut op: O,
    mut send: impl FnMut(Request) -> Result<Response, TransportError>,
) -> Result<O::Output, Error> {
    loop {
        match op.poll()? {
            Step::Send(request) => match send(request) {
                Ok(response) => op.on_response(response)?,
                Err(error) => op.on_transport_error(error)?,
            },
            Step::Done(output) => return Ok(output),
        }
    }
}

/// 单一显式契约、已协商 limits 的协议核心（同 Python 的 `ProtocolCore`）。
///
/// `capabilities` 来自 [`fetch_capabilities`]。`contract` 必须是服务端 `hash_contracts` 之一，
/// 否则返回 [`ErrorKind::ContractUnsupported`]（本地判定，不发请求）；不是 dpe-hash 认识的
/// 契约返回 [`ErrorKind::InvalidArgument`]；服务端协议版本不是 `dpe/1` 返回
/// [`ErrorKind::IncompatibleServer`]。
///
/// `max_resends` 是写操作在响应丢失后原样重发的次数上限（见模块说明的驱动约定），也是
/// `deliver` 恢复轮的预算——只计没有进展的轮次（最多允许几轮没有进展，见 [`ProtocolCore::deliver`]）。
#[derive(Debug)]
pub struct ProtocolCore {
    capabilities: Capabilities,
    contract: String,
    max_resends: u32,
}

impl ProtocolCore {
    /// 校验能力与服务端兼容性后构造核心（校验顺序同 Python：协议版本 → 契约可知 → 契约被接受）。
    pub fn new(
        capabilities: Capabilities,
        contract: &str,
        max_resends: u32,
    ) -> Result<Self, Error> {
        if capabilities.protocol != PROTOCOL {
            return Err(Error::incompatible_server(format!(
                "服务端协议版本 {:?} 不是本 SDK 实现的 {PROTOCOL:?}",
                capabilities.protocol
            )));
        }
        if !dpe_hash::KNOWN_CONTRACTS.contains(&contract) {
            return Err(Error::invalid_argument(format!(
                "dpe-hash 不认识的 hash 契约：{contract:?}"
            )));
        }
        if !capabilities
            .hash_contracts
            .iter()
            .any(|item| item == contract)
        {
            return Err(Error::protocol(
                ErrorKind::ContractUnsupported,
                format!(
                    "服务端不接受 hash 契约 {contract:?}（接受 {:?}）",
                    capabilities.hash_contracts
                ),
            ));
        }
        Ok(ProtocolCore {
            capabilities,
            contract: contract.to_owned(),
            max_resends,
        })
    }

    /// 服务端声明的能力（HTTP 绑定 §4.1）。
    pub fn capabilities(&self) -> &Capabilities {
        &self.capabilities
    }

    /// 服务端声明的限额。
    pub fn limits(&self) -> &crate::wire::Limits {
        &self.capabilities.limits
    }

    /// 本次会话使用的 hash 契约。
    pub fn contract(&self) -> &str {
        &self.contract
    }

    /// 写操作在响应丢失后原样重发的次数上限。
    pub fn max_resends(&self) -> u32 {
        self.max_resends
    }

    // ------------------------------------------------------------------
    // 只读操作（core §3、HTTP 绑定 §4.2–§4.4）
    // ------------------------------------------------------------------

    /// `HEAD documents?uri=`：文档的 doc_hash，不存在为 `None`（HTTP 绑定 §3.3）。
    ///
    /// `uri` 不做本地校验（与 Python 一致）：非常规字符仍按 RFC 3986 编码发送，由服务端判
    /// `DPE_VALIDATION`。
    pub fn head(&self, uri: &str) -> HeadOp<'_> {
        let target = format!("documents?{}", query(&[("uri", Some(uri))]));
        HeadOp {
            core: self,
            phase: Phase::Ready(self.request(Method::Head, target, Body::None, &[], true)),
        }
    }

    /// `POST heads`：按 `batch_head_max` 分批；结果与 `uris` 一一对应，不存在为 `None`。
    ///
    /// `uris` 为空时不发请求（零往返直接完成）。
    pub fn batch_head<'a>(&'a self, uris: &'a [String]) -> BatchHeadOp<'a> {
        BatchHeadOp {
            core: self,
            uris,
            batch_size: self.capabilities.limits.batch_head_max,
            next_start: 0,
            awaiting: None,
            heads: Vec::new(),
            terminal: false,
        }
    }

    /// `GET documents?uri=`：文档对象与全部页对象，不存在为 `None`。
    ///
    /// 响应按声明的契约重算页与文档对象的 hash，必须与返回值一致（core §3）；响应的
    /// `file_uri` MUST 等于请求 `uri` 的规范化形式（core §1.1），否则视为响应不合规。
    /// `uri` 不合法（core §1.1 文法）时本地即返回错误（[`ErrorKind::Validation`]），不发请求。
    pub fn get_skeleton(&self, uri: &str) -> Result<GetSkeletonOp<'_>, Error> {
        let expected_uri = dpe_hash::normalize_file_uri(uri)
            .map_err(|error| Error::from_hash_error(&error, ""))?;
        let target = format!("documents?{}", query(&[("uri", Some(uri))]));
        Ok(GetSkeletonOp {
            core: self,
            expected_uri,
            phase: Phase::Ready(self.request(Method::Get, target, Body::None, &[], true)),
        })
    }

    /// `GET documents?prefix=&cursor=&limit=` 的一页；`next_cursor` 为 `None` 表示结束。
    ///
    /// `prefix` 总是发送（空串即列出全部）：查询串中有 `prefix` 才是 list，有 `uri` 是
    /// get_skeleton。`limit` 缺省取服务端的 `list_page_max`；给出时必须是 1 到
    /// `list_page_max` 之间（HTTP 绑定 §4.4），越界返回 [`ErrorKind::InvalidArgument`]。
    pub fn list_page(
        &self,
        prefix: &str,
        cursor: Option<&str>,
        limit: Option<u64>,
    ) -> Result<ListPageOp<'_>, Error> {
        if let Some(limit) = limit {
            if !(1..=self.capabilities.limits.list_page_max).contains(&limit) {
                return Err(Error::invalid_argument(format!(
                    "limit 必须在 1 到 {} 之间，实际为 {limit}",
                    self.capabilities.limits.list_page_max
                )));
            }
        }
        let limit_text = limit.map(|limit| limit.to_string());
        let target = format!(
            "documents?{}",
            query(&[
                ("prefix", Some(prefix)),
                ("cursor", cursor),
                ("limit", limit_text.as_deref()),
            ])
        );
        Ok(ListPageOp {
            core: self,
            phase: Phase::Ready(self.request(Method::Get, target, Body::None, &[], true)),
        })
    }

    // ------------------------------------------------------------------
    // 暂存路径（core §3.4、HTTP 绑定 §4.5–§4.7）
    // ------------------------------------------------------------------

    /// `POST negotiate`（HTTP 绑定 §4.5）：开启暂存会话并返回逐层缺失清单。
    ///
    /// 只提交文档对象——规范允许附带部分页对象（MAY），本 SDK 暂不提供该开关：增量场景下附带
    /// 会把服务端已有的页也当请求体重传，收益方向相反。`uri` 不合法（core §1.1）时本地即失败，
    /// 不发请求。
    pub fn negotiate(
        &self,
        uri: &str,
        document: &ExpandedDocument,
    ) -> Result<NegotiateOp<'_>, Error> {
        let parts = self.wire_parts(document)?;
        self.negotiate_op(uri, &parts)
    }

    /// `PUT staging/{sid}/pages/{page_hash}`（HTTP 绑定 §4.6）：上传一个页对象（对象本身即请求
    /// 体），返回它引用、而在去重范围内不可得的元素 content_hash（去重范围含本会话，core §3.3）。
    ///
    /// 整段超过 `max_payload_bytes` 时按 §4.7 分块（块上限 `blob_chunk_bytes`，总量上限
    /// `page_max_bytes`，超限本地即拒；上游应把文档拆成更多页）。重复上传幂等（200）。
    pub fn upload_page<'a>(
        &'a self,
        session: &'a mut Session,
        page: &PageObject,
    ) -> Result<UploadPageOp<'a>, Error> {
        let page_hash = page.page_hash(&self.contract)?;
        let payload = serde_json::to_vec(page).expect("数据模型总能序列化");
        let runner = page_upload_runner(self, &session.id, &page_hash, payload)?;
        Ok(UploadPageOp {
            core: self,
            session,
            runner,
            awaiting: false,
            result: None,
        })
    }

    /// `PUT staging/{sid}/objects/{content_hash}`（HTTP 绑定 §4.6）：上传一个元素对象，返回它
    /// 引用的缺失 blob。
    ///
    /// 元素对象不分块（core §3.2）：超过 `max_payload_bytes`（只可能是超大文本）本地即拒，
    /// 切分是上游的内容决策。重复上传幂等（200）。
    pub fn upload_element<'a>(
        &'a self,
        session: &'a mut Session,
        element: &ElementObject,
    ) -> Result<UploadElementOp<'a>, Error> {
        let content_hash = element.content_hash(&self.contract)?;
        let payload = serde_json::to_vec(element).expect("数据模型总能序列化");
        let runner = element_upload_runner(self, &session.id, &content_hash, payload)?;
        Ok(UploadElementOp {
            core: self,
            session,
            runner,
            awaiting: false,
            result: None,
        })
    }

    /// `PUT staging/{sid}/blobs/{sha256}`（HTTP 绑定 §4.7）：整段或分块上传一个 blob。
    ///
    /// 总量超过 `blob_max_bytes` 本地即拒；不超过 `max_payload_bytes` 时一次 PUT（幂等，响应
    /// 丢失原样重发），否则按 §4.7 分块并遵守「响应不确定先断点查询」的客户端义务。
    /// `media_type` 缺省 `application/octet-stream`。
    pub fn upload_blob<'a>(
        &'a self,
        session: &'a mut Session,
        source: BlobSource,
        media_type: Option<&str>,
    ) -> Result<UploadBlobOp<'a>, Error> {
        let content_type = media_type.unwrap_or("application/octet-stream");
        let blob_ref = source.blob_ref().to_owned();
        let runner = blob_upload_runner(self, &session.id, &blob_ref, source, content_type)?;
        Ok(UploadBlobOp {
            core: self,
            session,
            runner,
            awaiting: false,
            result: None,
        })
    }

    /// 暂存上传请求：带契约头与自定义 `Content-Type`；写路径 `retry_safe = false`。
    fn staging_request(&self, target: String, body: Body, content_type: &str) -> Request {
        let mut headers = Headers::new();
        headers.insert(CONTRACT_HEADER, self.contract.clone());
        headers.insert("Content-Type", content_type);
        Request {
            method: Method::Put,
            target,
            headers,
            body,
            retry_safe: false,
        }
    }

    /// 由 negotiate 响应对构造客户端会话；`expires_at` 必须是 RFC 3339。
    fn session_from_wire(
        &self,
        raw: &StagingSession,
        uri: &str,
        response: &Response,
    ) -> Result<Session, Error> {
        if !is_rfc3339(&raw.expires_at) {
            return Err(Error::unexpected(
                format!("会话过期时间不是 RFC 3339：{:?}", raw.expires_at),
                response.status(),
                None,
            ));
        }
        Ok(Session {
            id: raw.id.clone(),
            uri: uri.to_owned(),
            expires_at: raw.expires_at.clone(),
        })
    }

    /// 投递一篇文档（core §3.2）：自动选择快路径或暂存路径，只传变更。
    ///
    /// - 内联请求体不超过 `max_payload_bytes` 时走快路径（一次 `commit`）；否则逐层协商
    ///   （negotiate → 缺失页 → 缺失元素 → 缺失 blob → commit 引用会话），只传缺失的部分；
    /// - `DPE_MISSING_CONTENT`：按分层清单把缺失对象补传进**同一会话**后重新 commit；快路径下
    ///   用「内联 + `staging_session`」的请求体，其余对象不必重传（blob 永远不能内联，是快路径
    ///   唯一可能缺的一层）；
    /// - `DPE_SESSION_EXPIRED`：重开 negotiate，新会话从空开始、按重算的缺失清单重走；
    /// - `blobs`：缺失 blob 的字节来源（`sha256:…` → [`BlobSource`]；内存字节用
    ///   [`BlobSource::from_bytes`] 包一层）；缺省时若确需上传 blob，失败并给出缺失清单；
    /// - `session`：复用的暂存会话（core §3.4：CAS 失败后重新读取、以新前置条件引用同一会话
    ///   重提，免重传）。会话只作缓存：失效时自动重开，结果与不复用时一致；
    /// - 恢复轮预算（`max_resends`）只由**没有进展**的轮次消耗：`DPE_MISSING_CONTENT` 重新列出
    ///   本会话已上传过的对象（或清单为空）时计一次，列出的全是新对象则不计——截断的清单
    ///   （core §3.3）因此可以逐轮补齐；`DPE_SESSION_EXPIRED` 照旧每次计一次（同 Python）；
    /// - 恢复轮预算用尽时抛出当时的错误；错误带 [`Error::session`]（当前
    ///   会话，可能仍可复用），上层可重新读取后以其继续，结果与不限预算一致。
    ///
    /// **绝不自动 force**：`DPE_PRECONDITION_FAILED` / `DPE_ALREADY_EXISTS` / `DPE_NOT_FOUND`
    /// 等原样抛出，不做恢复；只处理上述两类。
    pub fn deliver<'a>(
        &'a self,
        uri: &str,
        document: &ExpandedDocument,
        precondition: Precondition,
        blobs: Option<BlobProvider<'a>>,
        session: Option<Session>,
    ) -> Result<DeliverOp<'a>, Error> {
        dpe_hash::normalize_file_uri(uri).map_err(|error| Error::from_hash_error(&error, ""))?;
        self.condition(&precondition)?;
        let parts = self.wire_parts(document)?;
        DeliverOp::new(self, uri, parts, precondition, blobs, session)
    }

    /// 由已构造的请求体发起 commit（[`Self::commit`] 与 `deliver` 共用）。
    fn commit_op(
        &self,
        uri: &str,
        body: Value,
        precondition: Precondition,
        expected: String,
        total: u64,
    ) -> Result<CommitOp<'_>, Error> {
        let headers = self.condition(&precondition)?;
        let target = format!("documents?{}", query(&[("uri", Some(uri))]));
        let request = self.write_request(Method::Put, target, Some(&body), &headers);
        Ok(CommitOp {
            core: self,
            uri: uri.to_owned(),
            request,
            precondition,
            expected,
            total,
            phase: CommitPhase::Submit,
            resends: 0,
            awaiting: false,
            cause: None,
            outcome_unknown: false,
        })
    }

    /// 由已拆分的部件发起 negotiate（[`Self::negotiate`] 与 `deliver` 共用）。
    fn negotiate_op(&self, uri: &str, parts: &WireParts) -> Result<NegotiateOp<'_>, Error> {
        let expected_uri = dpe_hash::normalize_file_uri(uri)
            .map_err(|error| Error::from_hash_error(&error, ""))?;
        let body = json!({"file_uri": uri, "document": &parts.document});
        let request = self.write_request(Method::Post, "negotiate".to_owned(), Some(&body), &[]);
        Ok(NegotiateOp {
            core: self,
            expected_uri,
            phase: Phase::Ready(request),
        })
    }

    // ------------------------------------------------------------------
    // 写操作（core §3.3、§4、§5）
    // ------------------------------------------------------------------

    /// `PUT documents?uri=`（core §3.2 的快路径）：一次提交文档对象、全部页对象与元素对象
    /// （页与元素按 hash 去重后各发一份）。CAS 前置条件三选一（[`Precondition`]）；`uri` 不
    /// 合法、`base_hash` 不属于本次声明契约时本地即失败，不发请求。
    ///
    /// 重试规则（core §5.2、HTTP 绑定 §3.2）：非 force 的响应丢失原样重发至多
    /// [`max_resends`](ProtocolCore::max_resends) 次（首次已生效则得到 `unchanged`，首次的
    /// delta 不可得）；force 的响应丢失（含无 DPE 错误码的 502 / 504）**不重放**，先 `head`
    /// 判定；无 DPE 错误码的 412 先 `head` 再判定（`If-None-Match` 与 DELETE 对称，允许
    /// 误判重试一次；`If-Match` 只判一次）；`DPE_PRECONDITION_FAILED` 等错误如实上报，
    /// **绝不自动改用 force**。
    pub fn commit(
        &self,
        uri: &str,
        document: &ExpandedDocument,
        precondition: Precondition,
        session: Option<&Session>,
    ) -> Result<CommitOp<'_>, Error> {
        dpe_hash::normalize_file_uri(uri).map_err(|error| Error::from_hash_error(&error, ""))?;
        let parts = self.wire_parts(document)?;
        let mut body = json!({"document": &parts.document});
        if let Some(session) = session {
            body["staging_session"] = Value::String(session.id.clone());
        } else {
            body["pages"] = json!(parts.pages);
            body["objects"] = json!(parts.objects);
        }
        if precondition == Precondition::Force {
            body["force"] = Value::Bool(true);
        }
        self.commit_op(uri, body, precondition, parts.doc_hash, parts.total)
    }

    /// `DELETE documents?uri=`（core §4）：CAS 走 `If-Match`（HTTP 绑定 §3.2）。
    ///
    /// 重试规则（core §5.2、HTTP 绑定 §3.2）：响应丢失时原样重发，重发后得到
    /// `DPE_NOT_FOUND` 视为成功（目标状态「不存在」已达成；首次请求得到它则如实上报）；
    /// 无 DPE 错误码的 412 先 `head`：不存在即成功，doc_hash 不等于 `base_hash` 抛
    /// `DPE_PRECONDITION_FAILED`，仍等于则是中间层误判，原样重发一次；重发后仍是这样的 412
    /// 时再 `head` 判定一次，doc_hash 仍等于 `base_hash` 才按非协议错误上报。
    pub fn delete(&self, uri: &str, base_hash: &str) -> Result<DeleteOp<'_>, Error> {
        dpe_hash::normalize_file_uri(uri).map_err(|error| Error::from_hash_error(&error, ""))?;
        self.check_base_hash(base_hash)?;
        let headers = vec![("If-Match", format!("\"{base_hash}\""))];
        let target = format!("documents?{}", query(&[("uri", Some(uri))]));
        let request = self.write_request(Method::Delete, target, None, &headers);
        Ok(DeleteOp {
            core: self,
            uri: uri.to_owned(),
            base_hash: base_hash.to_owned(),
            request,
            phase: DeletePhase::Submit,
            resends: 0,
            resent: false,
            awaiting: false,
            outcome_unknown: false,
        })
    }

    /// `POST move`：`from_uri → to_uri` 原子改名，前置条件在请求体（core §5.2；方法名对应
    /// Python 的 `ProtocolCore.move`，`move` 是 Rust 关键字）。
    ///
    /// 响应丢失时原样重发：服务端对「源已不存在、目标内容等于 `base_hash`」返回成功（core
    /// §5.2 第 4 步），其余错误如实上报。结果的 `doc_hash` 必须等于 `base_hash`（file_uri
    /// 不进 hash），`DPE-Doc-Hash` 出现时必须一致。
    pub fn move_document(
        &self,
        from_uri: &str,
        to_uri: &str,
        base_hash: &str,
    ) -> Result<MoveOp<'_>, Error> {
        dpe_hash::normalize_file_uri(from_uri)
            .map_err(|error| Error::from_hash_error(&error, ""))?;
        dpe_hash::normalize_file_uri(to_uri).map_err(|error| Error::from_hash_error(&error, ""))?;
        self.check_base_hash(base_hash)?;
        let body = json!({"from_uri": from_uri, "to_uri": to_uri, "base_hash": base_hash});
        let request = self.write_request(Method::Post, "move".to_owned(), Some(&body), &[]);
        Ok(MoveOp {
            core: self,
            base_hash: base_hash.to_owned(),
            request,
            phase: MovePhase::Submit,
            resends: 0,
            awaiting: false,
            outcome_unknown: false,
        })
    }
    // ------------------------------------------------------------------
    // 请求与响应处理
    // ------------------------------------------------------------------

    /// 带契约头的请求；`headers` 是附加头（条件头等），有请求体时补
    /// `Content-Type: application/json`。
    fn request(
        &self,
        method: Method,
        target: String,
        body: Body,
        headers: &[(&'static str, String)],
        retry_safe: bool,
    ) -> Request {
        let mut all = Headers::new();
        all.insert(CONTRACT_HEADER, self.contract.clone());
        for (name, value) in headers {
            all.insert(*name, value.clone());
        }
        if !matches!(body, Body::None) {
            all.insert("Content-Type", "application/json");
        }
        Request {
            method,
            target,
            headers: all,
            body,
            retry_safe,
        }
    }

    /// 写请求：带契约头与附加头、JSON 请求体（`None` 为无体）；写路径一律
    /// `retry_safe = false`（请求可能已发出之后传输层 MUST NOT 自行重发，见模块说明）。
    fn write_request(
        &self,
        method: Method,
        target: String,
        body: Option<&Value>,
        headers: &[(&'static str, String)],
    ) -> Request {
        let mut all = Headers::new();
        all.insert(CONTRACT_HEADER, self.contract.clone());
        for (name, value) in headers {
            all.insert(*name, value.clone());
        }
        let body = match body {
            Some(value) => {
                all.insert("Content-Type", "application/json");
                json_body(value)
            }
            None => Body::None,
        };
        Request {
            method,
            target,
            headers: all,
            body,
            retry_safe: false,
        }
    }

    /// `base_hash` 必须是本次声明契约的 hash：不接受裸 hash，也不接受别的契约的值。
    fn check_base_hash(&self, value: &str) -> Result<(), Error> {
        dpe_hash::parse_hash_with(value, &[self.contract.as_str()])
            .map(|_| ())
            .map_err(|error| Error::from_hash_error(&error, ""))
    }

    /// 前置条件对应的条件头（HTTP 绑定 §3.2）；force 不带条件头。
    fn condition(&self, precondition: &Precondition) -> Result<Vec<(&'static str, String)>, Error> {
        Ok(match precondition {
            Precondition::BaseHash(value) => {
                self.check_base_hash(value)?;
                vec![("If-Match", format!("\"{value}\""))]
            }
            Precondition::IfAbsent => vec![("If-None-Match", "*".to_owned())],
            Precondition::Force => Vec::new(),
        })
    }

    /// 把展开文档拆成线上部件（Python `_wire_parts` 的对等）：文档对象、按 hash 去重的页对象
    /// 与元素对象，以及 doc_hash 与元素总数（含重复，供 delta 校验）。
    fn wire_parts(&self, document: &ExpandedDocument) -> Result<WireParts, Error> {
        let hashes = document.hashes(&self.contract)?;
        let mut pages = Vec::new();
        let mut objects = Vec::new();
        let mut page_by_hash = std::collections::HashMap::new();
        let mut object_by_hash = std::collections::HashMap::new();
        let mut blob_mime = std::collections::HashMap::new();
        let mut total: u64 = 0;
        for (page, page_hashes) in document.pages.iter().zip(&hashes.pages) {
            // 元素总数（含重复）：u64 求和的是内存中的元素数，溢出不可达；饱和写法纯防护
            total = total.saturating_add(page_hashes.elements.len() as u64);
            if page_by_hash.contains_key(&page_hashes.page_hash) {
                continue; // 同一页对象的元素也已全部收录
            }
            let mut wire_page = serde_json::to_value(page).expect("数据模型总能序列化");
            wire_page["elements"] = json!(page_hashes.elements);
            page_by_hash.insert(page_hashes.page_hash.clone(), wire_page.clone());
            pages.push(wire_page);
            for (element, content_hash) in page.elements.iter().zip(&page_hashes.elements) {
                if object_by_hash.contains_key(content_hash) {
                    continue;
                }
                object_by_hash.insert(
                    content_hash.clone(),
                    serde_json::to_value(element).expect("数据模型总能序列化"),
                );
                objects.push(serde_json::to_value(element).expect("数据模型总能序列化"));
                let blob = element.blob.as_ref().and_then(|value| value.as_ref());
                if let Some(blob) = blob {
                    let mime = element.mime_type.as_ref().and_then(|value| value.clone());
                    blob_mime.entry(blob.clone()).or_insert(mime);
                }
            }
        }
        let mut document_object = serde_json::to_value(document).expect("数据模型总能序列化");
        document_object["pages"] = json!(hashes
            .pages
            .iter()
            .map(|page| page.page_hash.clone())
            .collect::<Vec<_>>());
        Ok(WireParts {
            document: document_object,
            pages,
            objects,
            page_by_hash,
            object_by_hash,
            blob_mime,
            doc_hash: hashes.doc_hash,
            total,
        })
    }

    /// commit 响应（HTTP 绑定 §4.8）：201 即 created，200 即 updated / unchanged；结果必须
    /// 自洽（doc_hash 等于本地算出的提交内容、delta 与元素个数和状态一致、`DPE-Doc-Hash`
    /// 出现时必须一致）。
    fn commit_result(
        &self,
        response: &Response,
        expected: &str,
        total: u64,
    ) -> Result<CommitResult, Error> {
        if response.status() >= 400 {
            return Err(error_from_response(response));
        }
        if response.status() != 200 && response.status() != 201 {
            return Err(Error::unexpected(
                format!("commit 期望 HTTP 200 或 201，得到 {}", response.status()),
                response.status(),
                None,
            ));
        }
        let result = parse_response(response, CommitResult::from_value)?;
        let broken = |reason: String| {
            Error::unexpected(
                format!("commit 结果不自洽：{reason}"),
                response.status(),
                None,
            )
        };
        let created = result.status == CommitStatus::Created;
        if (response.status() == 201) != created {
            return Err(broken(format!(
                "status {:?} 与 HTTP {} 不符",
                result.status.as_str(),
                response.status()
            )));
        }
        if result.doc_hash != expected {
            return Err(broken(format!(
                "doc_hash {:?} 不等于提交内容的 {expected:?}",
                result.doc_hash
            )));
        }
        let delta = result.delta;
        // 计数是服务端给的 u64：相加必须防溢出（Python 是任意精度 int，等价于 checked_add）
        if delta.added.checked_add(delta.retained) != Some(total) {
            return Err(broken(format!(
                "added + retained 应为提交的元素个数 {total}"
            )));
        }
        if created && (delta.removed != 0 || delta.retained != 0) {
            return Err(broken(
                "created 时旧文档为空，removed 与 retained 应为 0".to_owned(),
            ));
        }
        if result.status == CommitStatus::Unchanged && (delta.added != 0 || delta.removed != 0) {
            return Err(broken("unchanged 时 added 与 removed 应为 0".to_owned()));
        }
        self.check_doc_hash_header(response, expected)?;
        Ok(result)
    }

    /// `DPE-Doc-Hash` 出现时必须等于结果的 doc_hash。缺失时宽容（同 `get_skeleton`）：权威值
    /// 在响应体中，头只是副本；服务端漏写这个 MUST 头由一致性跑分器拦截。
    fn check_doc_hash_header(&self, response: &Response, expected: &str) -> Result<(), Error> {
        if let Some(header) = response.header(DOC_HASH_HEADER) {
            if header != expected {
                return Err(Error::unexpected(
                    format!("{DOC_HASH_HEADER} {header:?} 与结果的 doc_hash {expected:?} 不符"),
                    response.status(),
                    None,
                ));
            }
        }
        Ok(())
    }
    /// 响应中的 hash 必须属于本次声明的契约（core §3.1）。
    fn check_hash(&self, value: &str, status: u16, what: &str) -> Result<String, Error> {
        dpe_hash::parse_hash_with(value, &[self.contract.as_str()]).map_err(|_| {
            Error::unexpected(
                format!("{what} {value:?} 不是契约 {} 的 hash", self.contract),
                status,
                None,
            )
        })?;
        Ok(value.to_owned())
    }

    /// head 的结果判定（HTTP 绑定 §3.3）：`200` 读 `DPE-Doc-Hash`（缺头不合规），
    /// `DPE_NOT_FOUND` 为 `None`；没有错误码的 404 不是「文档不存在」，按非 DPE 错误上报。
    fn head_result(&self, response: &Response) -> Result<Option<String>, Error> {
        match expect_status(response, 200) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        }
        let value = response.header(DOC_HASH_HEADER).ok_or_else(|| {
            Error::unexpected(format!("head 响应缺少 {DOC_HASH_HEADER}"), 200, None)
        })?;
        self.check_hash(value, response.status(), DOC_HASH_HEADER)
            .map(Some)
    }

    /// 骨架自洽校验（core §3、§1.1）：`file_uri`、页数、逐页 page_hash、doc_hash 重算与
    /// `DPE-Doc-Hash` 头（出现时必须一致）。
    fn verify_skeleton(
        &self,
        skeleton: &Skeleton,
        expected_uri: &str,
        response: &Response,
    ) -> Result<(), Error> {
        let broken = |reason: String| {
            Error::unexpected(format!("骨架不自洽：{reason}"), response.status(), None)
        };
        if skeleton.file_uri != expected_uri {
            return Err(broken(format!(
                "file_uri {:?} 与请求 uri 的规范化形式 {expected_uri:?} 不符",
                skeleton.file_uri
            )));
        }
        let references = &skeleton.document.pages;
        if skeleton.pages.len() != references.len() {
            return Err(broken(format!(
                "文档对象引用 {} 页，返回了 {} 个页对象",
                references.len(),
                skeleton.pages.len()
            )));
        }
        for (i, (page, reference)) in skeleton.pages.iter().zip(references).enumerate() {
            if page.page_hash(&self.contract)? != *reference {
                return Err(broken(format!(
                    "pages[{i}] 的 page_hash 与 document.pages[{i}] 不符"
                )));
            }
        }
        let doc_hash = self.check_hash(&skeleton.doc_hash, response.status(), "doc_hash")?;
        if skeleton.document.doc_hash(&self.contract)? != doc_hash {
            return Err(broken(
                "文档对象重算的 doc_hash 与 doc_hash 不符".to_owned(),
            ));
        }
        if let Some(header) = response.header(DOC_HASH_HEADER) {
            if header != doc_hash {
                return Err(broken(format!(
                    "{DOC_HASH_HEADER} 与响应体的 doc_hash 不符"
                )));
            }
        }
        Ok(())
    }
}

/// `GET capabilities`（HTTP 绑定 §4.1）：唯一不带契约头的请求。
pub fn fetch_capabilities() -> CapabilitiesOp {
    CapabilitiesOp {
        phase: Phase::Ready(Request {
            method: Method::Get,
            target: "capabilities".to_owned(),
            headers: Headers::new(),
            body: Body::None,
            retry_safe: true,
        }),
    }
}

// ---------------------------------------------------------------------------
// 操作状态机
// ---------------------------------------------------------------------------

/// 单请求操作的状态：`Ready`（待发送）→ `Awaiting`（已发出）→ `Done`（结果就绪）或
/// `Failed`（以错误终止）；两个终态都不再接受任何驱动（结构上保证「`Err` 之后操作终态」）。
/// 用法错误只报错、不改变状态。
#[derive(Debug)]
enum Phase<T> {
    Ready(Request),
    Awaiting,
    Done(T),
    Failed,
}

impl<T> Phase<T> {
    /// `poll` 的公共实现：`Ready` → 产出请求；`Done` → 取出结果（进终态）；其余是用法错误。
    fn poll(&mut self) -> Result<Step<T>, Error> {
        match std::mem::replace(self, Phase::Failed) {
            Phase::Ready(request) => {
                *self = Phase::Awaiting;
                Ok(Step::Send(request))
            }
            Phase::Done(value) => Ok(Step::Done(value)),
            // 用法错误不改变状态（与 Python 生成器一致）：在飞响应仍可回送
            Phase::Awaiting => {
                *self = Phase::Awaiting;
                Err(usage_error(
                    "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
                ))
            }
            Phase::Failed => Err(usage_error("操作已完成或以错误终止：poll 不应再次调用")),
        }
    }

    /// 回送响应：`process` 的结果决定终态——`Ok` 结果就绪；`Err` 原样返回**并进入终态**
    /// （此后一切驱动都是用法错误）。
    fn respond(&mut self, process: impl FnOnce() -> Result<T, Error>) -> Result<(), Error> {
        self.accept()?;
        match process() {
            Ok(value) => {
                *self = Phase::Done(value);
                Ok(())
            }
            Err(error) => {
                *self = Phase::Failed;
                Err(error)
            }
        }
    }

    /// 以传输异常终止（操作不处理时）：进入终态，错误由调用方原样上报。
    fn fail(&mut self) -> Result<(), Error> {
        self.accept()?;
        *self = Phase::Failed;
        Ok(())
    }

    /// 回送的前置检查：只允许在 `Awaiting`（有在飞请求）时回送响应或传输错误；用法错误
    /// 不改变状态（与 Python 生成器的语义一致：send 到未启动的生成器报错后仍可正常驱动）。
    fn accept(&mut self) -> Result<(), Error> {
        match self {
            Phase::Awaiting => Ok(()),
            Phase::Failed => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
            Phase::Ready(_) | Phase::Done(_) => Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            )),
        }
    }
}

/// 状态机用法错误：本地判定，不发请求，不会从正常驱动循环产生。
fn usage_error(message: impl Into<String>) -> Error {
    Error::invalid_argument(message)
}

/// `fetch_capabilities` 的操作。
#[derive(Debug)]
pub struct CapabilitiesOp {
    phase: Phase<Capabilities>,
}

impl Operation for CapabilitiesOp {
    type Output = Capabilities;

    fn poll(&mut self) -> Result<Step<Capabilities>, Error> {
        self.phase.poll()
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.phase.respond(|| {
            expect_status(&response, 200)?;
            parse_response(&response, Capabilities::from_value)
        })
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.phase.fail()?;
        Err(Error::transport(error))
    }
}

/// `head` 的操作。
#[derive(Debug)]
pub struct HeadOp<'a> {
    core: &'a ProtocolCore,
    phase: Phase<Option<String>>,
}

impl Operation for HeadOp<'_> {
    type Output = Option<String>;

    fn poll(&mut self) -> Result<Step<Option<String>>, Error> {
        self.phase.poll()
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.phase.respond(|| self.core.head_result(&response))
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.phase.fail()?;
        Err(Error::transport(error))
    }
}

/// `batch_head` 的操作：按 `batch_head_max` 分批，结果按原顺序拼接；某一批出错立即停下
/// （`Err` 返回后进入终态，此后一切驱动都是用法错误）。
#[derive(Debug)]
pub struct BatchHeadOp<'a> {
    core: &'a ProtocolCore,
    uris: &'a [String],
    batch_size: u64,
    /// 下一批的起点。
    next_start: usize,
    /// 在飞批次的 `(起点, 长度)`。
    awaiting: Option<(usize, usize)>,
    heads: Vec<Option<String>>,
    /// 终态：全部批次完成（结果已取出），或某次回送返回了 `Err`。
    terminal: bool,
}

impl BatchHeadOp<'_> {
    /// 回送的前置检查：只允许在等待响应时回送；用法错误不改变状态。
    fn accept(&self) -> Result<(), Error> {
        if self.terminal {
            return Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            ));
        }
        if self.awaiting.is_none() {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        Ok(())
    }

    /// 处理一批响应（调用方已通过 [`BatchHeadOp::accept`]）。
    fn receive(&mut self, response: Response) -> Result<(), Error> {
        let (_, expected) = self.awaiting.take().expect("accept 已确认在飞批次");
        expect_status(&response, 200)?;
        let batch = parse_response(&response, BatchHeads::from_value)?;
        if batch.heads.len() != expected {
            return Err(Error::unexpected(
                format!("heads 有 {} 项，请求了 {expected} 项", batch.heads.len()),
                response.status(),
                None,
            ));
        }
        for (i, head) in batch.heads.into_iter().enumerate() {
            match head {
                None => self.heads.push(None),
                Some(head) => {
                    let hash = self.core.check_hash(
                        &head.doc_hash,
                        response.status(),
                        &format!("heads[{i}].doc_hash"),
                    )?;
                    self.heads.push(Some(hash));
                }
            }
        }
        Ok(())
    }
}

impl Operation for BatchHeadOp<'_> {
    type Output = Vec<Option<String>>;

    fn poll(&mut self) -> Result<Step<Vec<Option<String>>>, Error> {
        if self.terminal {
            return Err(usage_error("操作已完成或以错误终止：poll 不应再次调用"));
        }
        if self.awaiting.is_some() {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        if self.next_start >= self.uris.len() {
            self.terminal = true;
            return Ok(Step::Done(std::mem::take(&mut self.heads)));
        }
        let size = usize::try_from(self.batch_size).unwrap_or(usize::MAX);
        let end = self.next_start.saturating_add(size).min(self.uris.len());
        let chunk = &self.uris[self.next_start..end];
        let body = json!({"uris": chunk});
        let request = self.core.request(
            Method::Post,
            "heads".to_owned(),
            json_body(&body),
            &[],
            true,
        );
        self.awaiting = Some((self.next_start, chunk.len()));
        self.next_start = end;
        Ok(Step::Send(request))
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.accept()?;
        let outcome = self.receive(response);
        if outcome.is_err() {
            self.terminal = true;
            self.awaiting = None;
        }
        outcome
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.accept()?;
        self.terminal = true;
        self.awaiting = None;
        Err(Error::transport(error))
    }
}

/// `get_skeleton` 的操作。
#[derive(Debug)]
pub struct GetSkeletonOp<'a> {
    core: &'a ProtocolCore,
    /// 请求 `uri` 的规范化形式（core §1.1），响应 `file_uri` 必须等于它。
    expected_uri: String,
    phase: Phase<Option<Skeleton>>,
}

impl Operation for GetSkeletonOp<'_> {
    type Output = Option<Skeleton>;

    fn poll(&mut self) -> Result<Step<Option<Skeleton>>, Error> {
        self.phase.poll()
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.phase.respond(|| {
            match expect_status(&response, 200) {
                Ok(()) => {}
                Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
                Err(error) => return Err(error),
            }
            let skeleton = parse_response(&response, |value| {
                Skeleton::from_value(value, self.core.contract())
            })?;
            self.core
                .verify_skeleton(&skeleton, &self.expected_uri, &response)?;
            Ok(Some(skeleton))
        })
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.phase.fail()?;
        Err(Error::transport(error))
    }
}

/// `list` 一页的操作。
#[derive(Debug)]
pub struct ListPageOp<'a> {
    core: &'a ProtocolCore,
    phase: Phase<ListPage>,
}

impl Operation for ListPageOp<'_> {
    type Output = ListPage;

    fn poll(&mut self) -> Result<Step<ListPage>, Error> {
        self.phase.poll()
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.phase.respond(|| {
            expect_status(&response, 200)?;
            let page = parse_response(&response, ListPage::from_value)?;
            for (i, entry) in page.documents.iter().enumerate() {
                self.core.check_hash(
                    &entry.doc_hash,
                    response.status(),
                    &format!("documents[{i}].doc_hash"),
                )?;
            }
            Ok(page)
        })
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.phase.fail()?;
        Err(Error::transport(error))
    }
}

// ---------------------------------------------------------------------------
// 写操作的状态机（fast path）
// ---------------------------------------------------------------------------

/// 展开文档的线上部件（快路径请求体与暂存上传共用同一份推导）。
#[derive(Debug)]
struct WireParts {
    document: Value,
    pages: Vec<Value>,
    objects: Vec<Value>,
    /// 页 hash → 线上页对象（暂存路径按缺失清单取用）。
    page_by_hash: std::collections::HashMap<String, Value>,
    /// content_hash → 线上元素对象。
    object_by_hash: std::collections::HashMap<String, Value>,
    /// blob 引用 → 媒体类型（元素未给 `mime_type` 时为 `None`）；键集合即本文档引用的全部 blob。
    blob_mime: std::collections::HashMap<String, Option<String>>,
    doc_hash: String,
    /// 元素总数（含重复，供 delta 校验）。
    total: u64,
}

/// head 请求的判定结果（写操作的回退判定用）。
enum HeadStep {
    /// 命中网关丢失、预算内：原样重发（保持在当前阶段）。
    Resend,
    /// 得到读数（文档不存在为 `None`）。
    Done(Option<String>),
}

/// head 回退判定用的请求（只读、可重发）。
fn head_request(core: &ProtocolCore, uri: &str) -> Request {
    core.request(
        Method::Head,
        format!("documents?{}", query(&[("uri", Some(uri))])),
        Body::None,
        &[],
        true,
    )
}

/// 处理一次 head 响应（`_send` 的重发语义；head 是只读请求，可原样重发）：命中网关丢失且
/// 预算内时返回 [`HeadStep::Resend`]（调用方保持当前阶段、预算不清零），否则给出读数。
fn head_step(
    core: &ProtocolCore,
    response: Response,
    resends: &mut u32,
) -> Result<HeadStep, Error> {
    if is_gateway_lost(&response) && *resends < core.max_resends() {
        *resends += 1;
        return Ok(HeadStep::Resend);
    }
    Ok(HeadStep::Done(core.head_result(&response)?))
}

/// 经 `head` 确认的成功（core §5.2）：与原样重发得到的 `unchanged` 相同，首次写入的 delta
/// 不可得。
fn recovered_result(doc_hash: &str, total: u64) -> CommitResult {
    CommitResult {
        status: CommitStatus::Unchanged,
        doc_hash: doc_hash.to_owned(),
        delta: Delta {
            added: 0,
            removed: 0,
            retained: total,
        },
    }
}

/// `commit` 的操作（见 [`ProtocolCore::commit`]）。
#[derive(Debug)]
pub struct CommitOp<'a> {
    core: &'a ProtocolCore,
    uri: String,
    /// 提交请求（重发时原样再发一份克隆）。
    request: Request,
    precondition: Precondition,
    /// 提交内容的 doc_hash（结果校验与 force 判定用）。
    expected: String,
    /// 元素总数（delta 校验用）。
    total: u64,
    phase: CommitPhase,
    /// 当前发送序列的重发记账（每次重新开始一次发送序列时清零）。
    resends: u32,
    awaiting: bool,
    /// force 的响应丢失原因（PUT 的传输异常或无 DPE 错误码的网关响应），作为
    /// `ForceNotConfirmed` 错误的 cause（core §5.2）。
    cause: Option<Error>,
    /// 本次写入是否可能已经生效（只针对提交请求本身；白名单判据见 [`Error::outcome_unknown`]）。
    outcome_unknown: bool,
}

#[derive(Debug)]
enum CommitPhase {
    /// 提交请求（`_send`：响应丢失原样重发至多 `max_resends` 次）。
    Submit,
    /// 裸 412 的 head 判定；`retried` 表示已原样重发过一次（仅 `IfAbsent` 会重发）。
    HeadAfter412 { retried: bool },
    /// 误判重发一次的提交请求。
    Resend412,
    /// force 的响应丢失：head 确认（只读、可重发）。
    ConfirmForce,
    /// 结果就绪（poll 取出后进入 `Finished`）。
    Done(CommitResult),
    /// 终态。
    Finished,
}

impl CommitOp<'_> {
    /// 本次写入是否可能已经生效（只针对提交请求本身；`deliver` 据此累计）。
    pub(crate) fn outcome_unknown(&self) -> bool {
        self.outcome_unknown
    }

    /// 提交响应的结果校验并落位结果。
    fn finish(&mut self, response: Response) -> Result<(), Error> {
        let result = self
            .core
            .commit_result(&response, &self.expected, self.total)?;
        self.phase = CommitPhase::Done(result);
        Ok(())
    }

    /// 经 head 确认的成功。
    fn recovered(&mut self) {
        self.phase = CommitPhase::Done(recovered_result(&self.expected, self.total));
    }

    /// `Submit` 的响应。
    fn step_submit(&mut self, response: Response) -> Result<(), Error> {
        if self.precondition == Precondition::Force {
            // force 不带条件头：任何无码结局都无从判定（裸 412 也不例外）
            self.outcome_unknown |= !is_dpe_error(&response);
            if is_gateway_lost(&response) {
                // 不重放 force：先 head 判定（core §5.2）
                self.cause = Some(error_from_response(&response));
                self.resends = 0;
                self.phase = CommitPhase::ConfirmForce;
                return Ok(());
            }
            return self.finish(response);
        }
        self.outcome_unknown |= response_outcome_unknown(&response);
        if is_gateway_lost(&response) && self.resends < self.core.max_resends() {
            self.resends += 1;
            return Ok(());
        }
        if is_bare_412(&response) {
            self.resends = 0;
            self.phase = CommitPhase::HeadAfter412 { retried: false };
            return Ok(());
        }
        self.finish(response)
    }

    /// `HeadAfter412` 的响应：判定前置条件是否实际成立。
    fn step_head_after_412(&mut self, response: Response, retried: bool) -> Result<(), Error> {
        match head_step(self.core, response, &mut self.resends) {
            Ok(HeadStep::Resend) => Ok(()),
            Ok(HeadStep::Done(current)) => self.judge_412(current, retried),
            Err(error) => {
                // head 判定本身失败：裸 412 无从化解，本次写入结果不确定
                self.outcome_unknown = true;
                Err(error)
            }
        }
    }

    /// 裸 412 的判定（HTTP 绑定 §3.2）：
    /// - `If-None-Match: *`（与 DELETE 对称）：文档不存在即前置条件实际成立，是中间层误判，
    ///   原样重发一次；重发后仍是这样的 412 时再判定一次，文档仍不存在才按非协议错误上报
    ///   （MUST NOT 报 `DPE_NOT_FOUND`）。文档存在时按内容判定：等于提交内容 → 成功，否则
    ///   `DPE_ALREADY_EXISTS`。
    /// - `If-Match: "H"`：不重发；等于提交内容 → 成功；不存在 → `DPE_NOT_FOUND`；否则
    ///   `DPE_PRECONDITION_FAILED`。
    fn judge_412(&mut self, current: Option<String>, retried: bool) -> Result<(), Error> {
        match self.precondition {
            Precondition::IfAbsent => {
                if current.is_none() {
                    if retried {
                        // 重发后仍是裸 412：没能化解
                        self.outcome_unknown = true;
                        return Err(Error::unexpected(
                            format!(
                                "{}：文档仍不存在（if_absent 的前置条件成立），重发后仍是无 DPE 错误码的 412",
                                self.uri
                            ),
                            412,
                            None,
                        ));
                    }
                    self.resends = 0;
                    self.phase = CommitPhase::Resend412;
                    return Ok(());
                }
                if current.as_deref() == Some(self.expected.as_str()) {
                    self.recovered();
                    return Ok(());
                }
                Err(Error::protocol(
                    ErrorKind::AlreadyExists,
                    format!("{} 已存在（412 无 DPE 错误码，经 head 判定）", self.uri),
                ))
            }
            Precondition::BaseHash(_) => {
                if current.as_deref() == Some(self.expected.as_str()) {
                    self.recovered();
                    return Ok(());
                }
                if current.is_none() {
                    return Err(Error::protocol(
                        ErrorKind::NotFound,
                        format!("{} 不存在（412 无 DPE 错误码，经 head 判定）", self.uri),
                    ));
                }
                Err(Error::protocol(
                    ErrorKind::PreconditionFailed,
                    format!(
                        "expected {}, current {}（412 无 DPE 错误码，经 head 判定）",
                        self.precondition.base_hash().unwrap_or_default(),
                        current.unwrap_or_default()
                    ),
                ))
            }
            Precondition::Force => unreachable!("force 不带条件头，裸 412 无从谈起"),
        }
    }

    /// `Resend412` 的响应（原样重发一次之后的）。
    fn step_resend_412(&mut self, response: Response) -> Result<(), Error> {
        self.outcome_unknown |= response_outcome_unknown(&response);
        if is_gateway_lost(&response) && self.resends < self.core.max_resends() {
            self.resends += 1;
            return Ok(());
        }
        if is_bare_412(&response) {
            self.resends = 0;
            self.phase = CommitPhase::HeadAfter412 { retried: true };
            return Ok(());
        }
        self.finish(response)
    }

    /// `ConfirmForce` 的响应：等于提交内容即成功，否则无法确认（core §5.2）。
    fn step_confirm_force(&mut self, response: Response) -> Result<(), Error> {
        match head_step(self.core, response, &mut self.resends) {
            Ok(HeadStep::Resend) => Ok(()),
            Ok(HeadStep::Done(current)) => {
                if current.as_deref() == Some(self.expected.as_str()) {
                    self.recovered();
                    return Ok(());
                }
                Err(Error::unconfirmed_force(
                    format!(
                        "force 提交的响应丢失，当前 doc_hash 为 {current:?}，不等于提交内容 {:?}",
                        self.expected
                    ),
                    self.expected.clone(),
                    current,
                    true,
                    self.cause.take().expect("进入 ConfirmForce 时必有原因"),
                ))
            }
            Err(error) => Err(Error::unconfirmed_force(
                "force 提交的响应丢失，head 也失败，无法确认是否生效",
                self.expected.clone(),
                None,
                false,
                error,
            )),
        }
    }
}

impl Operation for CommitOp<'_> {
    type Output = CommitResult;

    fn poll(&mut self) -> Result<Step<CommitResult>, Error> {
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        match std::mem::replace(&mut self.phase, CommitPhase::Finished) {
            CommitPhase::Finished => Err(usage_error("操作已完成或以错误终止：poll 不应再次调用")),
            CommitPhase::Done(result) => Ok(Step::Done(result)),
            CommitPhase::Submit => {
                self.phase = CommitPhase::Submit;
                self.awaiting = true;
                Ok(Step::Send(self.request.clone()))
            }
            CommitPhase::Resend412 => {
                self.phase = CommitPhase::Resend412;
                self.awaiting = true;
                Ok(Step::Send(self.request.clone()))
            }
            CommitPhase::HeadAfter412 { retried } => {
                self.phase = CommitPhase::HeadAfter412 { retried };
                self.awaiting = true;
                Ok(Step::Send(head_request(self.core, &self.uri)))
            }
            CommitPhase::ConfirmForce => {
                self.phase = CommitPhase::ConfirmForce;
                self.awaiting = true;
                Ok(Step::Send(head_request(self.core, &self.uri)))
            }
        }
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            CommitPhase::Submit => self.step_submit(response),
            CommitPhase::Resend412 => self.step_resend_412(response),
            CommitPhase::HeadAfter412 { retried } => self.step_head_after_412(response, retried),
            CommitPhase::ConfirmForce => self.step_confirm_force(response),
            CommitPhase::Done(_) | CommitPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = CommitPhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            // force：响应丢失不重放，先 head 判定
            CommitPhase::Submit if self.precondition == Precondition::Force => {
                self.outcome_unknown |= !is_not_sent(&error);
                self.cause = Some(Error::transport(error));
                self.resends = 0;
                self.phase = CommitPhase::ConfirmForce;
                Ok(())
            }
            // 写请求可能已到达：原样重发（幂等由内容保证），至多 max_resends 次
            CommitPhase::Submit | CommitPhase::Resend412 => {
                self.outcome_unknown |= !is_not_sent(&error);
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            // head 是只读请求：同样可原样重发
            CommitPhase::HeadAfter412 { .. } => {
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            // force 的 head 确认失败（重发已用尽）：无法确认
            CommitPhase::ConfirmForce => {
                let cause = Error::transport(error);
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    Err(Error::unconfirmed_force(
                        "force 提交的响应丢失，head 也失败，无法确认是否生效",
                        self.expected.clone(),
                        None,
                        false,
                        cause,
                    ))
                }
            }
            CommitPhase::Done(_) | CommitPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = CommitPhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }
}

/// `delete` 的操作（见 [`ProtocolCore::delete`]）。
#[derive(Debug)]
pub struct DeleteOp<'a> {
    core: &'a ProtocolCore,
    uri: String,
    base_hash: String,
    request: Request,
    phase: DeletePhase,
    resends: u32,
    /// 是否发生过原样重发：重发后得到 `DPE_NOT_FOUND` 视为成功（core §5.2）。
    resent: bool,
    awaiting: bool,
    /// 本次写入是否可能已经生效（只针对删除请求本身）。
    outcome_unknown: bool,
}

#[derive(Debug)]
enum DeletePhase {
    Submit,
    HeadAfter412 { retried: bool },
    Resend412,
    Done,
    Finished,
}

impl DeleteOp<'_> {
    fn done(&mut self) {
        self.phase = DeletePhase::Done;
    }

    /// 成功（204）或如实上报的错误；重发后得到 `DPE_NOT_FOUND` 视为成功（core §5.2）。
    fn finish(&mut self, response: Response) -> Result<(), Error> {
        if response.status() == 204 {
            self.done();
            return Ok(());
        }
        if response.status() >= 400 {
            let error = error_from_response(&response);
            if error.kind() == ErrorKind::NotFound && self.resent {
                self.done();
                return Ok(());
            }
            return Err(error);
        }
        Err(Error::unexpected(
            format!("删除期望 HTTP 204，得到 {}", response.status()),
            response.status(),
            None,
        ))
    }

    fn step_submit(&mut self, response: Response) -> Result<(), Error> {
        self.outcome_unknown |= response_outcome_unknown(&response);
        if is_gateway_lost(&response) && self.resends < self.core.max_resends() {
            self.resends += 1;
            self.resent = true;
            return Ok(());
        }
        if is_bare_412(&response) {
            self.resends = 0;
            self.phase = DeletePhase::HeadAfter412 { retried: false };
            return Ok(());
        }
        self.finish(response)
    }

    /// 裸 412 的 head 判定（HTTP 绑定 §3.2）：不存在即成功；不等于 `base_hash` 抛
    /// `DPE_PRECONDITION_FAILED`；仍等于则是中间层误判，原样重发一次。
    fn step_head_after_412(&mut self, response: Response, retried: bool) -> Result<(), Error> {
        let current = match head_step(self.core, response, &mut self.resends) {
            Ok(HeadStep::Resend) => return Ok(()),
            Ok(HeadStep::Done(current)) => current,
            Err(error) => {
                // head 判定本身失败：裸 412 无从化解，本次写入结果不确定
                self.outcome_unknown = true;
                return Err(error);
            }
        };
        if current.as_deref() == Some(self.base_hash.as_str()) {
            if retried {
                // 重发后仍是裸 412：没能化解
                self.outcome_unknown = true;
                return Err(Error::unexpected(
                    format!(
                        "{}：doc_hash 等于 If-Match，重发后仍是无 DPE 错误码的 412",
                        self.uri
                    ),
                    412,
                    None,
                ));
            }
            self.resent = true;
            self.resends = 0;
            self.phase = DeletePhase::Resend412;
            return Ok(());
        }
        if current.is_none() {
            self.done();
            return Ok(());
        }
        Err(Error::protocol(
            ErrorKind::PreconditionFailed,
            format!(
                "expected {}, current {}（412 无 DPE 错误码，经 head 判定）",
                self.base_hash,
                current.unwrap_or_default()
            ),
        ))
    }

    fn step_resend_412(&mut self, response: Response) -> Result<(), Error> {
        self.outcome_unknown |= response_outcome_unknown(&response);
        if is_gateway_lost(&response) && self.resends < self.core.max_resends() {
            self.resends += 1;
            self.resent = true;
            return Ok(());
        }
        if is_bare_412(&response) {
            self.resends = 0;
            self.phase = DeletePhase::HeadAfter412 { retried: true };
            return Ok(());
        }
        self.finish(response)
    }
}

impl Operation for DeleteOp<'_> {
    type Output = ();

    fn poll(&mut self) -> Result<Step<()>, Error> {
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        match std::mem::replace(&mut self.phase, DeletePhase::Finished) {
            DeletePhase::Finished => Err(usage_error("操作已完成或以错误终止：poll 不应再次调用")),
            DeletePhase::Done => Ok(Step::Done(())),
            DeletePhase::Submit => {
                self.phase = DeletePhase::Submit;
                self.awaiting = true;
                Ok(Step::Send(self.request.clone()))
            }
            DeletePhase::Resend412 => {
                self.phase = DeletePhase::Resend412;
                self.awaiting = true;
                Ok(Step::Send(self.request.clone()))
            }
            DeletePhase::HeadAfter412 { retried } => {
                self.phase = DeletePhase::HeadAfter412 { retried };
                self.awaiting = true;
                Ok(Step::Send(head_request(self.core, &self.uri)))
            }
        }
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            DeletePhase::Submit => self.step_submit(response),
            DeletePhase::Resend412 => self.step_resend_412(response),
            DeletePhase::HeadAfter412 { retried } => self.step_head_after_412(response, retried),
            DeletePhase::Done | DeletePhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = DeletePhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            DeletePhase::Submit | DeletePhase::Resend412 => {
                self.outcome_unknown |= !is_not_sent(&error);
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    self.resent = true;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            DeletePhase::HeadAfter412 { .. } => {
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            DeletePhase::Done | DeletePhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = DeletePhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }
}

/// `move` 的操作（见 [`ProtocolCore::move_document`]）。
#[derive(Debug)]
pub struct MoveOp<'a> {
    core: &'a ProtocolCore,
    base_hash: String,
    request: Request,
    phase: MovePhase,
    resends: u32,
    awaiting: bool,
    /// 本次写入是否可能已经生效（只针对 move 请求本身）。
    outcome_unknown: bool,
}

#[derive(Debug)]
enum MovePhase {
    Submit,
    Done(MoveResult),
    Finished,
}

impl MoveOp<'_> {
    /// move 响应（HTTP 绑定 §4.9）：`doc_hash` 必须等于 `base_hash`；`DPE-Doc-Hash` 出现时
    /// 必须一致。
    fn finish(&mut self, response: Response) -> Result<(), Error> {
        expect_status(&response, 200)?;
        let result = parse_response(&response, MoveResult::from_value)?;
        if result.doc_hash != self.base_hash {
            return Err(Error::unexpected(
                format!(
                    "move 结果的 doc_hash {:?} 不等于 base_hash {:?}",
                    result.doc_hash, self.base_hash
                ),
                response.status(),
                None,
            ));
        }
        self.core
            .check_doc_hash_header(&response, &self.base_hash)?;
        self.phase = MovePhase::Done(result);
        Ok(())
    }
}

impl Operation for MoveOp<'_> {
    type Output = MoveResult;

    fn poll(&mut self) -> Result<Step<MoveResult>, Error> {
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        match std::mem::replace(&mut self.phase, MovePhase::Finished) {
            MovePhase::Finished => Err(usage_error("操作已完成或以错误终止：poll 不应再次调用")),
            MovePhase::Done(result) => Ok(Step::Done(result)),
            MovePhase::Submit => {
                self.phase = MovePhase::Submit;
                self.awaiting = true;
                Ok(Step::Send(self.request.clone()))
            }
        }
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            MovePhase::Submit => {
                // move 没有条件头求值回退：任何无码结局都无从判定
                self.outcome_unknown |= !is_dpe_error(&response);
                if is_gateway_lost(&response) && self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    self.finish(response)
                }
            }
            MovePhase::Done(_) | MovePhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = MovePhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = match self.phase {
            MovePhase::Submit => {
                self.outcome_unknown |= !is_not_sent(&error);
                if self.resends < self.core.max_resends() {
                    self.resends += 1;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            MovePhase::Done(_) | MovePhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        if outcome.is_err() {
            self.phase = MovePhase::Finished;
        }
        outcome.map_err(|error| error.with_outcome_unknown(self.outcome_unknown))
    }
}

// ---------------------------------------------------------------------------
// 暂存路径的状态机（HTTP 绑定 §4.5–§4.7）
// ---------------------------------------------------------------------------

/// [`ProtocolCore::negotiate`] 的结果（HTTP 绑定 §4.5）：会话与逐层缺失清单。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Negotiated {
    pub session: Session,
    /// 文档对象引用、而不可得的页对象。
    pub missing_pages: Vec<String>,
    /// 附带的页对象中引用、而不可得的元素对象（本 SDK 不附带页对象，因此恒为空）。
    pub missing_content_hashes: Vec<String>,
}

/// `negotiate` 的操作（见 [`ProtocolCore::negotiate`]）。
#[derive(Debug)]
pub struct NegotiateOp<'a> {
    core: &'a ProtocolCore,
    /// 请求 `uri` 的规范化形式（core §1.1），会话绑定它。
    expected_uri: String,
    phase: Phase<Negotiated>,
}

impl Operation for NegotiateOp<'_> {
    type Output = Negotiated;

    fn poll(&mut self) -> Result<Step<Negotiated>, Error> {
        self.phase.poll()
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        self.phase.respond(|| {
            expect_status(&response, 200)?;
            let result = parse_response(&response, NegotiateResult::from_value)?;
            for (i, page_hash) in result.missing_pages.iter().enumerate() {
                self.core.check_hash(
                    page_hash,
                    response.status(),
                    &format!("missing_pages[{i}]"),
                )?;
            }
            for (i, content_hash) in result.missing_content_hashes.iter().enumerate() {
                self.core.check_hash(
                    content_hash,
                    response.status(),
                    &format!("missing_content_hashes[{i}]"),
                )?;
            }
            let session = self.core.session_from_wire(
                &result.staging_session,
                &self.expected_uri,
                &response,
            )?;
            Ok(Negotiated {
                session,
                missing_pages: result.missing_pages,
                missing_content_hashes: result.missing_content_hashes,
            })
        })
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        self.phase.fail()?;
        Err(Error::transport(error))
    }
}

/// 暂存上传的载荷（分块时按区间给出请求体）。
#[derive(Debug)]
enum ChunkPayload {
    /// 页对象：完整字节（块是内存切片）。
    Bytes(Arc<Vec<u8>>),
    /// blob：延迟区间（字节不进核心）。
    Blob(BlobSource),
}

/// 分块上传与恢复的状态机（HTTP 绑定 §4.7；页对象与 blob 共用）。
///
/// 任何一块的响应丢失或不确定（驱动回送的异常、无错误码的 502 / 504）时 MUST NOT 盲目重发：
/// 先断点查询（HEAD 同一 URL，只读、不续期）再按已收偏移续传；查到的偏移等于声明的总量即上传
/// 已完成，重发末块（非空区间）取缺失清单；偏移不连续（`DPE_VALIDATION` 且响应带
/// `DPE-Upload-Offset`）直接按该头重同步（免一次断点查询）。每一步恢复计入 `max_resends`
/// 预算，用尽后如实抛出；断点查询自身按只读请求重发（`_send` 语义，每次查询预算独立）。
///
/// `202` 表示「已追加本块、未到齐」（§4.7 第 7 步）：偏移必须恰好前进本块的长度且严格小于
/// 总量，否则服务端在重复旧状态——继续重发同一个块会陷入无进展的循环，因此判不合规（每轮
/// 202 严格前进一块，循环由结构保证有界，不占用恢复预算）。
#[derive(Debug)]
struct Chunked<'a> {
    core: &'a ProtocolCore,
    target: String,
    total: u64,
    /// 块大小上限：`blob_chunk_bytes` 与 `max_payload_bytes` 的较小者。
    chunk_size: u64,
    payload: ChunkPayload,
    content_type: String,
    /// 本会话已收偏移。
    offset: u64,
    /// 末块的起点（偏移等于总量时重发它取缺失清单，不发空区间）。
    last: u64,
    /// 剩余恢复预算。
    recoveries: u32,
    /// 当前断点查询自身的重发记账（每次新查询独立）。
    resume_resends: u32,
    /// 在飞的动作。
    inflight: Option<ChunkFlight>,
    /// 恢复步骤产出的待发请求（由操作侧的 poll 取出）。
    pending: Option<Request>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ChunkFlight {
    /// 数据块（起点、长度）。
    Chunk { from: u64, length: u64 },
    /// 断点查询（HEAD）。
    Resume,
}

/// 分块机器的回送结果。
enum ChunkOutcome {
    /// 还有请求要发。
    Continue,
    /// 上传完成：最终响应。
    Done(Response),
}

impl<'a> Chunked<'a> {
    fn new(
        core: &'a ProtocolCore,
        target: String,
        total: u64,
        payload: ChunkPayload,
        content_type: &str,
    ) -> Self {
        Chunked {
            chunk_size: core
                .limits()
                .blob_chunk_bytes
                .min(core.limits().max_payload_bytes),
            core,
            target,
            total,
            payload,
            content_type: content_type.to_owned(),
            offset: 0,
            last: 0,
            recoveries: core.max_resends(),
            resume_resends: 0,
            inflight: None,
            pending: None,
        }
    }

    /// 产出下一个要发的请求（有恢复步骤留下的待发请求时先取出）。
    fn next_request(&mut self) -> Result<Request, Error> {
        if let Some(request) = self.pending.take() {
            return Ok(request);
        }
        debug_assert!(self.inflight.is_none(), "有在飞请求时不应再取");
        if self.offset < self.total {
            self.last = self.offset; // 末块的起点
        }
        let from = if self.offset < self.total {
            self.offset
        } else {
            self.last
        };
        let length = self.chunk_size.min(self.total - from);
        self.inflight = Some(ChunkFlight::Chunk { from, length });
        Ok(self.chunk_request(from, length))
    }

    /// 数据块的请求（`Content-Range: bytes {from}-{to}/{total}`）。
    fn chunk_request(&self, from: u64, length: u64) -> Request {
        let body = match &self.payload {
            ChunkPayload::Bytes(bytes) => {
                Body::Bytes(bytes[from as usize..(from + length) as usize].to_vec())
            }
            ChunkPayload::Blob(source) => Body::BlobRange(BlobRange {
                source: source.clone(),
                offset: from,
                length,
            }),
        };
        let mut request = self
            .core
            .staging_request(self.target.clone(), body, &self.content_type);
        request.headers.insert(
            "Content-Range",
            format!("bytes {}-{}/{}", from, from + length - 1, self.total),
        );
        request
    }

    /// 断点查询请求（HEAD 同一 URL，只读：`retry_safe = true`，不续期）。
    fn resume_request(&self) -> Request {
        self.core
            .request(Method::Head, self.target.clone(), Body::None, &[], true)
    }

    /// 开始一次断点查询（重置它自身的重发记账）。
    fn start_resume(&mut self) -> ChunkOutcome {
        self.resume_resends = 0;
        self.inflight = Some(ChunkFlight::Resume);
        self.pending = Some(self.resume_request());
        ChunkOutcome::Continue
    }

    /// 开始一次断点查询并继续（内部使用：`?` 友好的包装）。
    fn resume_continue(&mut self) -> ChunkOutcome {
        self.start_resume()
    }

    /// 回送响应并推进。
    fn feed(&mut self, response: Response, session: &mut Session) -> Result<ChunkOutcome, Error> {
        let flight = self.inflight.take().expect("feed 只在有在飞请求时调用");
        match flight {
            ChunkFlight::Resume => {
                // 断点查询：只读，可原样重发（`_send` 语义）
                if is_gateway_lost(&response) && self.resume_resends < self.core.max_resends() {
                    self.resume_resends += 1;
                    self.inflight = Some(ChunkFlight::Resume);
                    self.pending = Some(self.resume_request());
                    return Ok(ChunkOutcome::Continue);
                }
                if response.status() >= 400 {
                    return Err(error_from_response(&response));
                }
                if response.status() != 200 {
                    return Err(Error::unexpected(
                        format!("断点查询期望 HTTP 200，得到 {}", response.status()),
                        response.status(),
                        None,
                    ));
                }
                renew_session(session, &response);
                let offset = require_offset(&response, &self.target)?;
                if offset > self.total {
                    return Err(Error::unexpected(
                        format!(
                            "{} 的已收偏移 {offset} 超过总量 {}",
                            self.target, self.total
                        ),
                        response.status(),
                        None,
                    ));
                }
                self.offset = offset;
                self.pending = Some(self.next_request()?);
                Ok(ChunkOutcome::Continue)
            }
            ChunkFlight::Chunk { from, length } => {
                if is_gateway_lost(&response) {
                    if self.recoveries == 0 {
                        return Err(error_from_response(&response));
                    }
                    self.recoveries -= 1;
                    return Ok(self.resume_continue());
                }
                if response.status() == 202 {
                    renew_session(session, &response);
                    let offset = require_offset(&response, &self.target)?;
                    let appended = from + length;
                    if offset != appended || offset >= self.total {
                        return Err(Error::unexpected(
                            format!(
                                "{} 的 202 已收偏移 {offset}：本块追加后应为 {appended} 且未到齐（总量 {}）",
                                self.target, self.total
                            ),
                            response.status(),
                            None,
                        ));
                    }
                    self.offset = offset;
                    self.pending = Some(self.next_request()?);
                    return Ok(ChunkOutcome::Continue);
                }
                if response.status() < 400 {
                    return Ok(ChunkOutcome::Done(response));
                }
                let error = error_from_response(&response);
                if error.kind() == ErrorKind::Validation {
                    if let Some(hint) = offset_hint(&response) {
                        // 偏移不连续的错误响应带本会话已收字节数：直接据它重同步（免一次断点查询）
                        if hint > self.total {
                            return Err(Error::unexpected(
                                format!(
                                    "{} 的偏移重同步值 {hint} 超过总量 {}",
                                    self.target, self.total
                                ),
                                response.status(),
                                None,
                            ));
                        }
                        if self.recoveries > 0 {
                            self.recoveries -= 1;
                            self.offset = hint;
                            self.pending = Some(self.next_request()?);
                            return Ok(ChunkOutcome::Continue);
                        }
                    }
                }
                Err(error)
            }
        }
    }

    /// 回送传输异常并推进。
    fn feed_transport_error(&mut self, error: TransportError) -> Result<ChunkOutcome, Error> {
        let flight = self.inflight.take().expect("feed 只在有在飞请求时调用");
        match flight {
            ChunkFlight::Resume => {
                if self.resume_resends < self.core.max_resends() {
                    self.resume_resends += 1;
                    self.inflight = Some(ChunkFlight::Resume);
                    self.pending = Some(self.resume_request());
                    Ok(ChunkOutcome::Continue)
                } else {
                    Err(Error::transport(error))
                }
            }
            ChunkFlight::Chunk { .. } => {
                if self.recoveries == 0 {
                    return Err(Error::transport(error));
                }
                self.recoveries -= 1;
                Ok(self.resume_continue())
            }
        }
    }
}

/// 一次上传的执行状态（整段 / 分块），三个 upload 操作共用；结果处理由各操作自己做。
#[derive(Debug)]
struct UploadRunner<'a> {
    phase: UploadPhase<'a>,
}

#[derive(Debug)]
enum UploadPhase<'a> {
    /// 整段 PUT（`_send` 语义：响应丢失与网关丢失原样重发）。
    Whole { request: Request, resends: u32 },
    /// 分块（`_chunked` 语义）。
    Chunked(Box<Chunked<'a>>),
    /// 终态。
    Finished,
}

impl<'a> UploadRunner<'a> {
    fn whole(request: Request) -> Self {
        UploadRunner {
            phase: UploadPhase::Whole {
                request,
                resends: 0,
            },
        }
    }

    fn chunked(
        core: &'a ProtocolCore,
        target: String,
        total: u64,
        payload: ChunkPayload,
        content_type: &str,
    ) -> Self {
        UploadRunner {
            phase: UploadPhase::Chunked(Box::new(Chunked::new(
                core,
                target,
                total,
                payload,
                content_type,
            ))),
        }
    }

    fn finish(&mut self) {
        self.phase = UploadPhase::Finished;
    }

    /// 取下一个要发的请求。
    fn next_request(&mut self) -> Result<Request, Error> {
        match &mut self.phase {
            UploadPhase::Whole { request, .. } => Ok(request.clone()),
            UploadPhase::Chunked(chunked) => chunked.next_request(),
            UploadPhase::Finished => Err(usage_error("操作已完成或以错误终止：poll 不应再次调用")),
        }
    }

    /// 回送响应；`Ok(Some(response))` 表示上传完成（200/201），`Ok(None)` 表示还有请求要发。
    fn feed(
        &mut self,
        response: Response,
        session: &mut Session,
        core: &ProtocolCore,
    ) -> Result<Option<Response>, Error> {
        match &mut self.phase {
            UploadPhase::Whole { resends, .. } => {
                if is_gateway_lost(&response) && *resends < core.max_resends() {
                    *resends += 1;
                    return Ok(None);
                }
                Ok(Some(response))
            }
            UploadPhase::Chunked(chunked) => match chunked.feed(response, session)? {
                ChunkOutcome::Continue => Ok(None),
                ChunkOutcome::Done(response) => Ok(Some(response)),
            },
            UploadPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        }
    }

    /// 回送传输异常；`Ok(None)` 表示原样重发。
    fn feed_transport_error(
        &mut self,
        error: TransportError,
        core: &ProtocolCore,
    ) -> Result<(), Error> {
        match &mut self.phase {
            UploadPhase::Whole { resends, .. } => {
                if *resends < core.max_resends() {
                    *resends += 1;
                    Ok(())
                } else {
                    Err(Error::transport(error))
                }
            }
            UploadPhase::Chunked(chunked) => match chunked.feed_transport_error(error)? {
                ChunkOutcome::Continue => Ok(()),
                ChunkOutcome::Done(_) => unreachable!("分块的传输异常路径不会完成"),
            },
            UploadPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        }
    }
}

/// `upload_page` 的操作（见 [`ProtocolCore::upload_page`]）。
#[derive(Debug)]
pub struct UploadPageOp<'a> {
    core: &'a ProtocolCore,
    session: &'a mut Session,
    runner: UploadRunner<'a>,
    awaiting: bool,
    result: Option<Vec<String>>,
}

impl Operation for UploadPageOp<'_> {
    type Output = Vec<String>;

    fn poll(&mut self) -> Result<Step<Vec<String>>, Error> {
        if let Some(result) = self.result.take() {
            self.runner.finish();
            return Ok(Step::Done(result));
        }
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        let request = self.runner.next_request()?;
        self.awaiting = true;
        Ok(Step::Send(request))
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.receive(response);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.runner.feed_transport_error(error, self.core);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }
}

impl UploadPageOp<'_> {
    /// 上传完成的响应：续期、校验状态码与响应体，返回缺失元素清单。
    fn receive(&mut self, response: Response) -> Result<(), Error> {
        let Some(response) = self.runner.feed(response, self.session, self.core)? else {
            return Ok(());
        };
        upload_ok(self.session, &response)?;
        let missing = parse_response(&response, PageUploadMissing::from_value)?;
        for (i, content_hash) in missing.missing_content_hashes.iter().enumerate() {
            self.core.check_hash(
                content_hash,
                response.status(),
                &format!("missing_content_hashes[{i}]"),
            )?;
        }
        self.result = Some(missing.missing_content_hashes);
        Ok(())
    }
}

/// `upload_element` 的操作（见 [`ProtocolCore::upload_element`]）。
#[derive(Debug)]
pub struct UploadElementOp<'a> {
    core: &'a ProtocolCore,
    session: &'a mut Session,
    runner: UploadRunner<'a>,
    awaiting: bool,
    result: Option<Vec<String>>,
}

impl Operation for UploadElementOp<'_> {
    type Output = Vec<String>;

    fn poll(&mut self) -> Result<Step<Vec<String>>, Error> {
        if let Some(result) = self.result.take() {
            self.runner.finish();
            return Ok(Step::Done(result));
        }
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        let request = self.runner.next_request()?;
        self.awaiting = true;
        Ok(Step::Send(request))
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.receive(response);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.runner.feed_transport_error(error, self.core);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }
}

impl UploadElementOp<'_> {
    /// 上传完成的响应：续期、校验状态码与响应体，返回缺失 blob 清单。
    fn receive(&mut self, response: Response) -> Result<(), Error> {
        let Some(response) = self.runner.feed(response, self.session, self.core)? else {
            return Ok(());
        };
        upload_ok(self.session, &response)?;
        let missing = parse_response(&response, ElementUploadMissing::from_value)?;
        for (i, blob) in missing.missing_blobs.iter().enumerate() {
            check_blob_ref(blob, response.status(), &format!("missing_blobs[{i}]"))?;
        }
        self.result = Some(missing.missing_blobs);
        Ok(())
    }
}

/// `upload_blob` 的操作（见 [`ProtocolCore::upload_blob`]）。
#[derive(Debug)]
pub struct UploadBlobOp<'a> {
    core: &'a ProtocolCore,
    session: &'a mut Session,
    runner: UploadRunner<'a>,
    awaiting: bool,
    /// 完成标志（`Some` = 结果就绪，poll 取出后进入终态）。
    result: Option<()>,
}

impl Operation for UploadBlobOp<'_> {
    type Output = ();

    fn poll(&mut self) -> Result<Step<()>, Error> {
        if self.result.take().is_some() {
            self.runner.finish();
            return Ok(Step::Done(()));
        }
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        let request = self.runner.next_request()?;
        self.awaiting = true;
        Ok(Step::Send(request))
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.receive(response);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.runner.feed_transport_error(error, self.core);
        if outcome.is_err() {
            self.runner.finish();
        }
        outcome
    }
}

impl UploadBlobOp<'_> {
    /// 上传完成的响应：续期并校验状态码（blob 没有下一层清单）。
    fn receive(&mut self, response: Response) -> Result<(), Error> {
        let Some(response) = self.runner.feed(response, self.session, self.core)? else {
            return Ok(());
        };
        upload_ok(self.session, &response)?;
        self.result = Some(());
        Ok(())
    }
}

/// 按 `DPE-Session-Expires` 更新会话的过期时间（诊断用）：缺失或无法解析时沿用旧值——
/// 它只是副本，是否过期以服务端判定为准（core §3.4）。
fn renew_session(session: &mut Session, response: &Response) {
    let Some(value) = response.header(SESSION_EXPIRES_HEADER) else {
        return;
    };
    if is_rfc3339(value) {
        session.expires_at = value.to_owned();
    }
}

/// upload 的成功响应（201 本会话新写入 / 200 重复，HTTP 绑定 §4.6）：续期并校验，失败
/// （含 410 会话不可用）按 problem 体的 code 抛出。
fn upload_ok(session: &mut Session, response: &Response) -> Result<(), Error> {
    if response.status() >= 400 {
        return Err(error_from_response(response));
    }
    if response.status() != 200 && response.status() != 201 {
        return Err(Error::unexpected(
            format!("upload 期望 HTTP 200 或 201，得到 {}", response.status()),
            response.status(),
            None,
        ));
    }
    renew_session(session, response);
    Ok(())
}

/// 响应里可用的 `DPE-Upload-Offset`（本会话已收字节数）；不可用时为 `None`。
fn offset_hint(response: &Response) -> Option<u64> {
    let value = response.header(UPLOAD_OFFSET_HEADER)?;
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value.parse().ok() // 超长数字串（放不下 u64）按「头不可用」处理，不抛裸转换错误
}

/// 必须带合法 `DPE-Upload-Offset` 的响应。
fn require_offset(response: &Response, what: &str) -> Result<u64, Error> {
    offset_hint(response).ok_or_else(|| {
        Error::unexpected(
            format!("{what} 的响应缺少合法的 {UPLOAD_OFFSET_HEADER}"),
            response.status(),
            None,
        )
    })
}

/// 报文中的 blob 引用必须是合法的 `sha256:<64hex>`（契约 1 §1）。
fn check_blob_ref(value: &str, status: u16, what: &str) -> Result<(), Error> {
    dpe_hash::parse_blob_ref(value).map(|_| ()).map_err(|_| {
        Error::unexpected(
            format!("{what} {value:?} 不是合法的 blob 引用"),
            status,
            None,
        )
    })
}

// ---------------------------------------------------------------------------
// deliver：快路径 / 暂存路径的组合机器（core §3.2–§3.4）
// ---------------------------------------------------------------------------

/// 投递模式。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DeliverMode {
    /// 快路径：内联请求体（恢复时加 `staging_session`）。
    Inline,
    /// 暂存路径：请求体只含文档对象与 `staging_session`。
    Staged,
}

/// negotiate 完成后进入的分支。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DeliverGoal {
    /// 快路径恢复：算「内联 + 会话」的请求体重提；加上会话 id 即超限时回落暂存路径。
    InlineRecovery,
    /// 暂存路径：按新清单补传后重新提交。
    Staged,
}

/// `deliver` 的操作（见 [`ProtocolCore::deliver`]）。
pub struct DeliverOp<'a> {
    core: &'a ProtocolCore,
    uri: String,
    parts: WireParts,
    precondition: Precondition,
    expected: String,
    total: u64,
    /// 快路径的当前内联体（恢复时替换为「内联 + `staging_session`」）。
    inline: Option<Value>,
    /// 缺失 blob 的字节来源。
    blobs: Option<BlobProvider<'a>>,
    /// 当前暂存会话（会话只作缓存；续期只影响诊断字段）。
    session: Option<Session>,
    /// 恢复轮预算：只由没有进展的轮次消耗（见 [`DeliverOp::stalled`]）。
    budget: u32,
    /// 本会话已上传的 hash（页、元素、blob）；换会话时清空。
    uploaded: HashSet<String>,
    /// 待补传的清单（来自 commit 的 `DPE_MISSING_CONTENT` 或 negotiate）。
    pending: Option<Missing>,
    queue: ManifestQueue,
    /// 当前清单是否已展开进队列（重开 negotiate / 收到新清单时重置）。
    queue_active: bool,
    mode: DeliverMode,
    /// negotiate 完成后要做的事。
    after_negotiate: Option<DeliverGoal>,
    phase: DeliverPhase<'a>,
    awaiting: bool,
    /// 本次写入是否可能已经生效（来自 commit 请求本身的不确定尝试；negotiate / upload 不算）。
    outcome_unknown: bool,
}

#[derive(Debug)]
enum DeliverPhase<'a> {
    InlineCommit(Box<CommitOp<'a>>),
    Negotiate(Box<NegotiateOp<'a>>),
    Upload(Box<ManifestJob<'a>>),
    StagedCommit(Box<CommitOp<'a>>),
    Finished,
}

impl fmt::Debug for DeliverOp<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DeliverOp")
            .field("uri", &self.uri)
            .field("mode", &self.mode)
            .field("budget", &self.budget)
            .field("session", &self.session)
            .field("awaiting", &self.awaiting)
            .finish_non_exhaustive()
    }
}

impl<'a> DeliverOp<'a> {
    fn new(
        core: &'a ProtocolCore,
        uri: &str,
        parts: WireParts,
        precondition: Precondition,
        blobs: Option<BlobProvider<'a>>,
        session: Option<Session>,
    ) -> Result<Self, Error> {
        let expected = parts.doc_hash.clone();
        let total = parts.total;
        let mut op = DeliverOp {
            core,
            uri: uri.to_owned(),
            parts,
            precondition,
            expected,
            total,
            inline: None,
            blobs,
            session: None,
            budget: core.max_resends(),
            uploaded: HashSet::new(),
            pending: None,
            queue: ManifestQueue::default(),
            queue_active: false,
            mode: DeliverMode::Inline,
            after_negotiate: None,
            phase: DeliverPhase::Finished,
            awaiting: false,
            outcome_unknown: false,
        };
        if let Some(session) = session {
            // 复用的会话（内容应已在其中）：直接 commit 引用它，缺什么由缺失清单驱动补传。
            op.mode = DeliverMode::Staged;
            op.session = Some(session);
            op.start_commit()?;
            return Ok(op);
        }
        let inline = op.build_inline_body();
        if json_body_len(&inline) <= core.limits().max_payload_bytes {
            op.inline = Some(inline);
            op.start_commit()?;
        } else {
            op.mode = DeliverMode::Staged;
            op.start_negotiate(DeliverGoal::Staged)?;
        }
        Ok(op)
    }

    /// 快路径的内联体（文档对象 + 全部页对象与元素对象；force 带 `"force": true`）。
    fn build_inline_body(&self) -> Value {
        let mut body = json!({
            "document": &self.parts.document,
            "pages": &self.parts.pages,
            "objects": &self.parts.objects,
        });
        if self.precondition == Precondition::Force {
            body["force"] = Value::Bool(true);
        }
        body
    }

    /// 快路径恢复的请求体（内联 + `staging_session`）；加上会话 id 会超过
    /// `max_payload_bytes` 时为 `None`——会话已开出，改由暂存路径继续（它的请求体只含文档对象；
    /// 传输层按整段请求体判 413，见 core §3.3 第 0 步）。
    fn recovery_body(&self, session: &Session) -> Option<Value> {
        let mut body = self.inline.clone()?;
        body["staging_session"] = Value::String(session.id.clone());
        (json_body_len(&body) <= self.core.limits().max_payload_bytes).then_some(body)
    }

    /// 当前会话的 id（暂存路径必有会话）。
    fn session_id(&self) -> &str {
        &self.session.as_ref().expect("暂存路径必有会话").id
    }

    /// 发起 commit（按当前模式与当前请求体）。
    fn start_commit(&mut self) -> Result<(), Error> {
        let body = match self.mode {
            DeliverMode::Inline => self.inline.clone().expect("快路径必有内联体"),
            DeliverMode::Staged => {
                let mut body = json!({
                    "document": &self.parts.document,
                    "staging_session": self.session_id(),
                });
                if self.precondition == Precondition::Force {
                    body["force"] = Value::Bool(true);
                }
                body
            }
        };
        let op = self.core.commit_op(
            &self.uri,
            body,
            self.precondition.clone(),
            self.expected.clone(),
            self.total,
        )?;
        self.phase = match self.mode {
            DeliverMode::Inline => DeliverPhase::InlineCommit(Box::new(op)),
            DeliverMode::Staged => DeliverPhase::StagedCommit(Box::new(op)),
        };
        Ok(())
    }

    /// 发起 negotiate。
    fn start_negotiate(&mut self, goal: DeliverGoal) -> Result<(), Error> {
        let op = self.core.negotiate_op(&self.uri, &self.parts)?;
        self.after_negotiate = Some(goal);
        self.phase = DeliverPhase::Negotiate(Box::new(op));
        Ok(())
    }

    /// negotiate 完成后的推进。
    fn after_negotiated(&mut self, negotiated: Negotiated) -> Result<(), Error> {
        let goal = self
            .after_negotiate
            .take()
            .expect("negotiate 完成后必有目标");
        self.session = Some(negotiated.session.clone());
        self.uploaded.clear(); // 新会话从空开始（同 Python：换会话清空本会话已上传）
        match goal {
            DeliverGoal::InlineRecovery => {
                match self.recovery_body(&negotiated.session) {
                    Some(body) => {
                        self.inline = Some(body);
                        // 会话已开：先在新会话里补传记住的清单（若有），再以「内联 + 会话」重提
                        self.queue_active = false;
                        self.advance()
                    }
                    None => {
                        // 加上会话 id 即超限：会话已开出，改由暂存路径继续（清单丢弃，
                        // 缺什么由下一次 commit 的缺失清单重新给出）。暂存阶段是新的恢复轮
                        // （同 Python：`_inline` 与 `_staged` 各自持有 `budget = max_resends`）。
                        self.mode = DeliverMode::Staged;
                        self.budget = self.core.max_resends();
                        self.pending = None;
                        self.queue_active = false;
                        self.start_commit()
                    }
                }
            }
            DeliverGoal::Staged => {
                self.pending = Some(Missing {
                    pages: negotiated.missing_pages,
                    content_hashes: negotiated.missing_content_hashes,
                    blobs: Vec::new(),
                });
                self.queue_active = false;
                self.advance()
            }
        }
    }

    /// 主循环：展开待补传清单 → 逐项上传（层序）→ 无待传时 commit。
    ///
    /// 清单在整轮完成前一直保留：上传中途会话失效重开 negotiate 时，清单要在新会话里重传
    /// （Python `_inline` / `_staged` 的 `pending` 语义）。
    fn advance(&mut self) -> Result<(), Error> {
        if let Some(missing) = self.pending.as_ref() {
            if !self.queue_active {
                self.queue = ManifestQueue::default();
                self.queue.fill(missing.clone())?;
                self.queue_active = true;
            }
        }
        match self.next_job()? {
            Some(job) => {
                self.phase = DeliverPhase::Upload(Box::new(job));
                Ok(())
            }
            None => {
                self.pending = None;
                self.queue_active = false;
                self.start_commit()
            }
        }
    }

    /// 队列里的下一个上传任务（层序：页 → 元素 → blob；blob 层先整体校验再上传）。
    fn next_job(&mut self) -> Result<Option<ManifestJob<'a>>, Error> {
        if let Some(page_hash) = self.queue.pages.pop_front() {
            let Some(page) = self.parts.page_by_hash.get(&page_hash).cloned() else {
                return Err(Error::unexpected(
                    format!("缺失清单里的页 {page_hash} 不是本次提交引用的对象"),
                    400,
                    None,
                ));
            };
            let payload = serde_json::to_vec(&page).expect("JSON 值总能序列化");
            let runner = page_upload_runner(self.core, self.session_id(), &page_hash, payload)?;
            return Ok(Some(ManifestJob {
                kind: JobKind::Page,
                target: page_hash,
                runner,
                awaiting: false,
            }));
        }
        if let Some(content_hash) = self.queue.objects.pop_front() {
            let Some(element) = self.parts.object_by_hash.get(&content_hash).cloned() else {
                return Err(Error::unexpected(
                    format!("缺失清单里的元素 {content_hash} 不是本次提交引用的对象"),
                    400,
                    None,
                ));
            };
            let payload = serde_json::to_vec(&element).expect("JSON 值总能序列化");
            let runner =
                element_upload_runner(self.core, self.session_id(), &content_hash, payload)?;
            return Ok(Some(ManifestJob {
                kind: JobKind::Element,
                target: content_hash,
                runner,
                awaiting: false,
            }));
        }
        if !self.queue.blobs.is_empty() {
            if !self.queue.blobs_validated {
                self.queue.blobs_validated = true;
                // 清单来自 400 的 DPE_MISSING_CONTENT：格式与成员资格先于取字节校验，口径同页与元素
                for blob in &self.queue.blobs {
                    check_blob_ref(blob, 400, "缺失清单里的 blob")?;
                    if !self.parts.blob_mime.contains_key(blob) {
                        return Err(Error::unexpected(
                            format!("缺失清单里的 blob {blob} 不是本次提交引用的对象"),
                            400,
                            None,
                        ));
                    }
                }
                if self.blobs.is_none() {
                    // 本地判定：缺少字节来源。直接逃出恢复循环（不消耗预算），对外仍是带缺失
                    // 清单的 `DPE_MISSING_CONTENT` 类错误。
                    let missing = Missing {
                        pages: Vec::new(),
                        content_hashes: Vec::new(),
                        blobs: self.queue.blobs.iter().cloned().collect(),
                    };
                    return Err(Error::local_missing_content(
                        "缺少 blob 的字节来源（deliver 的 blobs 参数或直接 upload_blob），无法补传",
                        missing,
                    ));
                }
            }
            let blob = self.queue.blobs.pop_front().expect("上面已判非空");
            // 同步调用来源回调（见 `BlobProvider` 的调用约束：只在 poll 中、不跨 await）
            let source = (self.blobs.as_mut().expect("上面已判有来源"))(&blob);
            let Some(source) = source else {
                return Err(Error::invalid_argument(format!(
                    "blob 来源回调没有给出 {blob} 的字节来源"
                )));
            };
            let media_type = self.parts.blob_mime.get(&blob).cloned().flatten();
            let runner = blob_upload_runner(
                self.core,
                self.session_id(),
                &blob,
                source,
                media_type.as_deref().unwrap_or("application/octet-stream"),
            )?;
            return Ok(Some(ManifestJob {
                kind: JobKind::Blob,
                target: blob,
                runner,
                awaiting: false,
            }));
        }
        Ok(None)
    }

    /// `DPE_MISSING_CONTENT` 这一轮是否没有进展：清单为空，或重新列出了本会话已上传过的对象。
    /// 有进展的轮次不消耗预算——截断的清单（core §3.3）因此可以逐轮补齐；每个有进展的轮次
    /// 至少上传一个新对象、对象总数有限，循环必然终止。
    fn stalled(&self, error: &Error) -> bool {
        let Some(missing) = error.missing() else {
            return true;
        };
        let mut listed = missing
            .pages
            .iter()
            .chain(&missing.content_hashes)
            .chain(&missing.blobs)
            .peekable();
        if listed.peek().is_none() {
            return true;
        }
        listed.any(|hash| self.uploaded.contains(hash))
    }

    /// `Err` 的两类恢复（core §3.4）或如实上报。
    fn handle_error(&mut self, error: Error) -> Result<(), Error> {
        if error.kind() == ErrorKind::MissingContent {
            // 只有没有进展的轮次消耗预算（同 Python `_stalled`）
            let stalled = self.stalled(&error);
            if stalled && self.budget == 0 {
                return Err(error);
            }
            if self.session.is_none() {
                if self.mode == DeliverMode::Inline {
                    // 快路径：先开会话，再以「内联 + 会话」补传重提
                    if stalled {
                        self.budget -= 1;
                    }
                    self.pending = error.missing().cloned();
                    self.queue_active = false;
                    return self.start_negotiate(DeliverGoal::InlineRecovery);
                }
                // 暂存路径还没有会话（如首个 negotiate 就报 MISSING_CONTENT）：没有可补传的
                // 会话，不进入恢复循环，如实上报（同 Python：`deliver` 的初始 `_negotiate`
                // 不在 `_staged` 的恢复循环内）。
                return Err(error);
            }
            if stalled {
                self.budget -= 1;
            }
            self.pending = error.missing().cloned();
            self.queue_active = false;
            return self.advance();
        }
        if error.kind() == ErrorKind::SessionExpired {
            if self.budget == 0 {
                return Err(error);
            }
            self.budget -= 1;
            let goal = match self.mode {
                DeliverMode::Inline => DeliverGoal::InlineRecovery,
                DeliverMode::Staged => DeliverGoal::Staged,
            };
            return self.start_negotiate(goal);
        }
        Err(error)
    }

    /// 统一收尾：出错时进入终态并把当前会话附到错误上（Python `_attach_session` 的对等）。
    fn settle(&mut self, outcome: Result<(), Error>) -> Result<(), Error> {
        match outcome {
            Ok(()) => Ok(()),
            Err(error) => {
                self.phase = DeliverPhase::Finished;
                Err(error
                    .with_session(self.session.clone())
                    .with_outcome_unknown(self.outcome_unknown))
            }
        }
    }

    /// 回送响应（阶段分派）。
    fn receive(&mut self, response: Response) -> Result<(), Error> {
        let stage = std::mem::replace(&mut self.phase, DeliverPhase::Finished);
        match stage {
            DeliverPhase::InlineCommit(mut op) => match op.on_response(response) {
                Ok(()) => {
                    self.phase = DeliverPhase::InlineCommit(op);
                    Ok(())
                }
                Err(error) => {
                    self.outcome_unknown |= op.outcome_unknown();
                    self.handle_error(error)
                }
            },
            DeliverPhase::StagedCommit(mut op) => match op.on_response(response) {
                Ok(()) => {
                    self.phase = DeliverPhase::StagedCommit(op);
                    Ok(())
                }
                Err(error) => {
                    self.outcome_unknown |= op.outcome_unknown();
                    self.handle_error(error)
                }
            },
            DeliverPhase::Negotiate(mut op) => match op.on_response(response) {
                Ok(()) => match op.poll() {
                    Ok(Step::Done(negotiated)) => self.after_negotiated(negotiated),
                    other => Err(Error::invalid_argument(format!(
                        "内部错误：negotiate 完成态异常（{other:?}）"
                    ))),
                },
                Err(error) => self.handle_error(error),
            },
            DeliverPhase::Upload(mut job) => {
                let session = self.session.as_mut().expect("暂存路径必有会话");
                match job.receive(response, self.core, session) {
                    Ok(None) => {
                        self.phase = DeliverPhase::Upload(job);
                        Ok(())
                    }
                    Ok(Some(JobOutcome::MissingElements(hashes))) => {
                        self.uploaded.insert(job.target.clone());
                        self.queue.extend_objects(hashes);
                        self.advance()
                    }
                    Ok(Some(JobOutcome::MissingBlobs(blobs))) => {
                        self.uploaded.insert(job.target.clone());
                        self.queue.extend_blobs(blobs);
                        self.advance()
                    }
                    Ok(Some(JobOutcome::Done)) => {
                        self.uploaded.insert(job.target.clone());
                        self.advance()
                    }
                    Err(error) => self.handle_error(error),
                }
            }
            DeliverPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        }
    }
}

impl Operation for DeliverOp<'_> {
    type Output = CommitResult;

    fn poll(&mut self) -> Result<Step<CommitResult>, Error> {
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        /// 各阶段归一化后的 poll 结果。
        enum Polled {
            Send(Request),
            Done(CommitResult),
        }
        let polled: Result<Polled, Error> =
            match std::mem::replace(&mut self.phase, DeliverPhase::Finished) {
                DeliverPhase::Finished => {
                    return Err(usage_error("操作已完成或以错误终止：poll 不应再次调用"))
                }
                DeliverPhase::InlineCommit(mut op) => {
                    let outcome = op.poll().map(|step| match step {
                        Step::Send(request) => Polled::Send(request),
                        Step::Done(result) => Polled::Done(result),
                    });
                    if outcome.is_ok() {
                        self.phase = DeliverPhase::InlineCommit(op);
                    }
                    outcome
                }
                DeliverPhase::StagedCommit(mut op) => {
                    let outcome = op.poll().map(|step| match step {
                        Step::Send(request) => Polled::Send(request),
                        Step::Done(result) => Polled::Done(result),
                    });
                    if outcome.is_ok() {
                        self.phase = DeliverPhase::StagedCommit(op);
                    }
                    outcome
                }
                DeliverPhase::Negotiate(mut op) => {
                    let outcome = match op.poll() {
                        Ok(Step::Send(request)) => Ok(Polled::Send(request)),
                        // negotiate 的结果由 on_response 立即取出，poll 不会见到完成态
                        Ok(Step::Done(_)) => Err(Error::invalid_argument(
                            "内部错误：negotiate 在 poll 中完成",
                        )),
                        Err(error) => Err(error),
                    };
                    if outcome.is_ok() {
                        self.phase = DeliverPhase::Negotiate(op);
                    }
                    outcome
                }
                DeliverPhase::Upload(mut job) => {
                    let outcome = job.next_request().map(Polled::Send);
                    if outcome.is_ok() {
                        self.phase = DeliverPhase::Upload(job);
                    }
                    outcome
                }
            };
        match polled {
            Ok(Polled::Send(request)) => {
                self.awaiting = true;
                Ok(Step::Send(request))
            }
            Ok(Polled::Done(result)) => {
                self.phase = DeliverPhase::Finished;
                Ok(Step::Done(result))
            }
            Err(error) => {
                self.phase = DeliverPhase::Finished;
                Err(error)
            }
        }
    }

    fn on_response(&mut self, response: Response) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let outcome = self.receive(response);
        self.settle(outcome)
    }

    fn on_transport_error(&mut self, error: TransportError) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let stage = std::mem::replace(&mut self.phase, DeliverPhase::Finished);
        let outcome = match stage {
            DeliverPhase::InlineCommit(mut op) => match op.on_transport_error(error) {
                Ok(()) => {
                    self.phase = DeliverPhase::InlineCommit(op);
                    Ok(())
                }
                Err(error) => {
                    self.outcome_unknown |= op.outcome_unknown();
                    self.handle_error(error)
                }
            },
            DeliverPhase::StagedCommit(mut op) => match op.on_transport_error(error) {
                Ok(()) => {
                    self.phase = DeliverPhase::StagedCommit(op);
                    Ok(())
                }
                Err(error) => {
                    self.outcome_unknown |= op.outcome_unknown();
                    self.handle_error(error)
                }
            },
            DeliverPhase::Negotiate(mut op) => match op.on_transport_error(error) {
                Ok(()) => {
                    self.phase = DeliverPhase::Negotiate(op);
                    Ok(())
                }
                Err(error) => self.handle_error(error),
            },
            DeliverPhase::Upload(mut job) => match job.on_transport_error(error, self.core) {
                Ok(()) => {
                    self.phase = DeliverPhase::Upload(job);
                    Ok(())
                }
                Err(error) => self.handle_error(error),
            },
            DeliverPhase::Finished => Err(usage_error(
                "操作已完成或以错误终止：不能再回送响应或传输错误",
            )),
        };
        self.settle(outcome)
    }
}

/// 补传清单里的一个上传任务（页 / 元素 / blob）。
#[derive(Debug)]
struct ManifestJob<'a> {
    kind: JobKind,
    /// 本任务上传的对象 hash（完成后记入本会话已上传）。
    target: String,
    runner: UploadRunner<'a>,
    awaiting: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum JobKind {
    Page,
    Element,
    Blob,
}

/// 上传任务完成时的产物（按层给出下一层缺失的对象）。
#[derive(Debug)]
enum JobOutcome {
    /// 页上传完成：它引用的缺失元素。
    MissingElements(Vec<String>),
    /// 元素上传完成：它引用的缺失 blob。
    MissingBlobs(Vec<String>),
    /// blob 上传完成。
    Done,
}

impl<'a> ManifestJob<'a> {
    /// 下一个要发的请求。
    fn next_request(&mut self) -> Result<Request, Error> {
        if self.awaiting {
            return Err(usage_error(
                "poll 在等待响应时被调用：操作已发出请求，必须先回送响应或传输错误",
            ));
        }
        let request = self.runner.next_request()?;
        self.awaiting = true;
        Ok(request)
    }

    /// 回送响应；`Ok(None)` 表示还有请求要发。
    fn receive(
        &mut self,
        response: Response,
        core: &ProtocolCore,
        session: &mut Session,
    ) -> Result<Option<JobOutcome>, Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        let Some(response) = self.runner.feed(response, session, core)? else {
            return Ok(None);
        };
        upload_ok(session, &response)?;
        match self.kind {
            JobKind::Page => {
                let missing = parse_response(&response, PageUploadMissing::from_value)?;
                for (i, content_hash) in missing.missing_content_hashes.iter().enumerate() {
                    core.check_hash(
                        content_hash,
                        response.status(),
                        &format!("missing_content_hashes[{i}]"),
                    )?;
                }
                Ok(Some(JobOutcome::MissingElements(
                    missing.missing_content_hashes,
                )))
            }
            JobKind::Element => {
                let missing = parse_response(&response, ElementUploadMissing::from_value)?;
                for (i, blob) in missing.missing_blobs.iter().enumerate() {
                    check_blob_ref(blob, response.status(), &format!("missing_blobs[{i}]"))?;
                }
                Ok(Some(JobOutcome::MissingBlobs(missing.missing_blobs)))
            }
            JobKind::Blob => Ok(Some(JobOutcome::Done)),
        }
    }

    /// 回送传输异常。
    fn on_transport_error(
        &mut self,
        error: TransportError,
        core: &ProtocolCore,
    ) -> Result<(), Error> {
        if !self.awaiting {
            return Err(usage_error(
                "on_response / on_transport_error 在未发出请求时被调用：请先 poll 并发送请求",
            ));
        }
        self.awaiting = false;
        self.runner.feed_transport_error(error, core)
    }
}

/// 补传清单展开后的上传队列（层序：页 → 元素 → blob；blob 层先整体校验再上传）。
#[derive(Default, Debug)]
struct ManifestQueue {
    pages: VecDeque<String>,
    objects: VecDeque<String>,
    blobs: VecDeque<String>,
    seen_pages: HashSet<String>,
    seen_objects: HashSet<String>,
    seen_blobs: HashSet<String>,
    /// blob 层是否已整体校验（格式 + 成员资格 + 字节来源）。
    blobs_validated: bool,
}

impl ManifestQueue {
    /// 由清单填充队列（成员资格在上传消费时校验，与 Python 的消费顺序一致）；重复项只上传一次。
    fn fill(&mut self, missing: Missing) -> Result<(), Error> {
        for page_hash in missing.pages {
            if self.seen_pages.insert(page_hash.clone()) {
                self.pages.push_back(page_hash);
            }
        }
        for content_hash in missing.content_hashes {
            self.push_object(content_hash);
        }
        self.extend_blobs(missing.blobs);
        Ok(())
    }

    /// 追加一个待传元素（去重）。
    fn push_object(&mut self, content_hash: String) {
        if self.seen_objects.insert(content_hash.clone()) {
            self.objects.push_back(content_hash);
        }
    }

    /// 追加一个待传 blob（去重）。
    fn extend_blobs(&mut self, blobs: impl IntoIterator<Item = String>) {
        for blob in blobs {
            if self.seen_blobs.insert(blob.clone()) {
                self.blobs.push_back(blob);
            }
        }
    }

    /// 追加一批待传元素。
    fn extend_objects(&mut self, hashes: impl IntoIterator<Item = String>) {
        for content_hash in hashes {
            self.push_object(content_hash);
        }
    }
}

/// 页对象上传的 runner（本地超限即拒；整段或分块）。
fn page_upload_runner<'a>(
    core: &'a ProtocolCore,
    session_id: &str,
    page_hash: &str,
    payload: Vec<u8>,
) -> Result<UploadRunner<'a>, Error> {
    if payload.len() as u64 > core.limits().page_max_bytes {
        return Err(Error::protocol(
            ErrorKind::PayloadTooLarge,
            format!(
                "页对象 {} 字节，超过 page_max_bytes={}；上游应把文档拆成更多页",
                payload.len(),
                core.limits().page_max_bytes
            ),
        ));
    }
    let target = format!("staging/{session_id}/pages/{page_hash}");
    Ok(bytes_upload_runner(core, target, payload, JSON))
}

/// 元素对象上传的 runner（不分块：超限本地即拒，切分是上游的内容决策）。
fn element_upload_runner<'a>(
    core: &'a ProtocolCore,
    session_id: &str,
    content_hash: &str,
    payload: Vec<u8>,
) -> Result<UploadRunner<'a>, Error> {
    if payload.len() as u64 > core.limits().max_payload_bytes {
        return Err(Error::protocol(
            ErrorKind::PayloadTooLarge,
            format!(
                "元素对象 {} 字节，超过 max_payload_bytes={}；元素对象不分块，上游应切分文本",
                payload.len(),
                core.limits().max_payload_bytes
            ),
        ));
    }
    let target = format!("staging/{session_id}/objects/{content_hash}");
    Ok(bytes_upload_runner(core, target, payload, JSON))
}

/// blob 上传的 runner：本地校验 ref 与总量，整段或分块。
fn blob_upload_runner<'a>(
    core: &'a ProtocolCore,
    session_id: &str,
    blob_ref: &str,
    source: BlobSource,
    content_type: &str,
) -> Result<UploadRunner<'a>, Error> {
    if dpe_hash::parse_blob_ref(source.blob_ref()).is_err() {
        return Err(Error::invalid_argument(format!(
            "blob 来源的 ref 不是 sha256 引用：{:?}",
            source.blob_ref()
        )));
    }
    if source.blob_ref() != blob_ref {
        return Err(Error::invalid_argument(format!(
            "blob 来源的 ref {} 与需要的 {blob_ref} 不符",
            source.blob_ref()
        )));
    }
    if source.size() > core.limits().blob_max_bytes {
        return Err(Error::protocol(
            ErrorKind::PayloadTooLarge,
            format!(
                "blob {} 字节，超过 blob_max_bytes={}",
                source.size(),
                core.limits().blob_max_bytes
            ),
        ));
    }
    let target = format!("staging/{session_id}/blobs/{blob_ref}");
    let size = source.size();
    if size <= core.limits().max_payload_bytes {
        let body = Body::BlobRange(BlobRange {
            source,
            offset: 0,
            length: size,
        });
        Ok(UploadRunner::whole(core.staging_request(
            target,
            body,
            content_type,
        )))
    } else {
        Ok(UploadRunner::chunked(
            core,
            target,
            size,
            ChunkPayload::Blob(source),
            content_type,
        ))
    }
}

/// 内存字节上传的 runner（整段或分块）。
fn bytes_upload_runner<'a>(
    core: &'a ProtocolCore,
    target: String,
    payload: Vec<u8>,
    content_type: &str,
) -> UploadRunner<'a> {
    if payload.len() as u64 <= core.limits().max_payload_bytes {
        UploadRunner::whole(core.staging_request(target, Body::Bytes(payload), content_type))
    } else {
        UploadRunner::chunked(
            core,
            target,
            payload.len() as u64,
            ChunkPayload::Bytes(Arc::new(payload)),
            content_type,
        )
    }
}

/// JSON 请求体的字节数（尺寸预检与实际发送共用同一序列化）。
fn json_body_len(value: &Value) -> u64 {
    serde_json::to_vec(value).expect("JSON 值总能序列化").len() as u64
}

// ---------------------------------------------------------------------------
// 响应判定（HTTP 绑定 §3.2、§5）
// ---------------------------------------------------------------------------

/// 状态码必须是 `expected`；错误状态码按 [`error_from_response`] 上报，其他状态码（如 302、
/// 204）视为响应不合规（只有 ≥ 400 才可能是 DPE 错误）。
fn expect_status(response: &Response, expected: u16) -> Result<(), Error> {
    if response.status == expected {
        return Ok(());
    }
    if response.status >= 400 {
        return Err(error_from_response(response));
    }
    Err(Error::unexpected(
        format!("期望 HTTP {expected}，得到 {}", response.status),
        response.status,
        None,
    ))
}

/// 网关类状态码：请求可能已转发到源站，结果未知（RFC 9110 §15.6.3 Bad Gateway、§15.6.5
/// Gateway Timeout）。服务端自己的暂时不可用是带错误码的 `DPE_UNAVAILABLE`（503），不在此列。
const GATEWAY_STATUSES: [u16; 2] = [502, 504];

/// 响应带 DPE 错误码：problem 体的 `code` 或 `DPE-Error-Code` 头（HTTP 绑定 §5）。
fn is_dpe_error(response: &Response) -> bool {
    problem_body(response).is_some()
        || !response
            .header(ERROR_CODE_HEADER)
            .unwrap_or("")
            .trim()
            .is_empty()
}

/// 无 DPE 错误码的 412，如网关自行求值条件头的结果（HTTP 绑定 §3.2）。
fn is_bare_412(response: &Response) -> bool {
    response.status() == 412 && !is_dpe_error(response)
}

/// 写请求的响应是否让「本次写入是否可能已生效」变得不确定。
///
/// 只有三种结局算「确定」：带 DPE 错误码的响应；成功响应且核心的自洽校验全部通过；
/// 带 [`NotSent`] 标记的传输错误。其余一律不确定——任何**无码**响应都可能是中间层产物
/// （无码 5xx / 4xx、解析或自洽校验失败的成功响应、没能化解的裸 412）。裸 412 例外：它可能
/// 经 `head` 判定化解，置位交给判定路径（化解成功即确定，判定失败才置位）。
fn response_outcome_unknown(response: &Response) -> bool {
    !is_dpe_error(response) && !is_bare_412(response)
}

/// 无 DPE 错误码的 502 / 504：写操作按响应丢失处理（core §5.2、HTTP 绑定 §3.2）。
fn is_gateway_lost(response: &Response) -> bool {
    GATEWAY_STATUSES.contains(&response.status()) && !is_dpe_error(response)
}
/// 错误响应对应的错误（HTTP 绑定 §3.2、§5）：problem 体的 `code` 优先，其次
/// `DPE-Error-Code` 头，都没有则不是 DPE 错误。
///
/// 传输适配层用它判定「传输层可自行重试的响应」：`code` 属于 core §6、且
/// [`is_retryable`](Error::is_retryable) 为真时按 [`retry_after`](Error::retry_after) 等待后原样重发
/// ——按 `code` 判定，不按状态码分派（HTTP 绑定 §3.2）。
pub fn error_from_response(response: &Response) -> Error {
    let retry_after = response.header("retry-after").and_then(retry_after_seconds);
    if let Some(problem) = problem_body(response) {
        if let Some(error) = Error::from_problem(&problem, retry_after) {
            return error;
        }
    }
    let code = response.header(ERROR_CODE_HEADER).unwrap_or("").trim();
    if !code.is_empty() {
        if let Some(error) = Error::from_problem(&json!({"code": code}), retry_after) {
            return error;
        }
    }
    Error::unexpected(
        "错误响应既无 problem 体、也无 DPE-Error-Code 头，不是 DPE 错误",
        response.status,
        retry_after,
    )
}

/// problem 体（`application/problem+json`，含非空字符串 `code`）；否则为 `None`。
fn problem_body(response: &Response) -> Option<Value> {
    let media_type = response
        .header("content-type")
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if media_type != "application/problem+json" || response.body.is_empty() {
        return None;
    }
    let value = dpe_hash::parse_ijson(std::str::from_utf8(&response.body).ok()?).ok()?;
    let code = value.get("code")?.as_str()?;
    if code.is_empty() {
        return None;
    }
    Some(value)
}

/// 解析成功响应体并交给 `parse`（wire 模型）：I-JSON 严格解析，失败与形状不符一律是
/// [`ErrorKind::UnexpectedResponse`]（补上响应状态码）。
fn parse_response<T>(
    response: &Response,
    parse: impl FnOnce(&Value) -> Result<T, Error>,
) -> Result<T, Error> {
    let json_error = |reason: String| {
        Error::unexpected(
            format!("响应体不是合法的 JSON：{reason}"),
            response.status,
            None,
        )
    };
    let text =
        std::str::from_utf8(&response.body).map_err(|error| json_error(error.to_string()))?;
    let value = dpe_hash::parse_ijson(text).map_err(|error| json_error(error.to_string()))?;
    parse(&value).map_err(|error| error.with_status(response.status))
}

/// JSON 请求体的字节。
fn json_body(value: &Value) -> Body {
    Body::Bytes(serde_json::to_vec(value).expect("JSON 值总能序列化为字节"))
}

// ---------------------------------------------------------------------------
// 查询串编码（HTTP 绑定 §1）
// ---------------------------------------------------------------------------

/// 查询串：按 RFC 3986 百分号编码的值；值为 `None` 的参数省略（等价 Python 的
/// `quote(value, safe="")`）。
fn query(params: &[(&str, Option<&str>)]) -> String {
    params
        .iter()
        .filter_map(|(key, value)| value.map(|value| format!("{key}={}", percent_encode(value))))
        .collect::<Vec<_>>()
        .join("&")
}

/// RFC 3986 百分号编码：`unreserved`（字母、数字、`-._~`）原样，其余字节按 UTF-8 编码为
/// 大写 hex 三元组（同 Python 的 `quote(value, safe="")`）。
fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for &byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

// ---------------------------------------------------------------------------
// Retry-After（RFC 9110 §10.2.3）
// ---------------------------------------------------------------------------

/// `Retry-After` 头换算成秒（RFC 9110 §10.2.3）：`delay-seconds`（`1*DIGIT`）或 `HTTP-date`
/// （§5.6.7 的三种形式），相对当前 UTC 时间；已过去的时刻为 0，不合文法或无法解析时为
/// `None`（「没有给出提示」）。合法但大到无法表示的 delay-seconds 为 `f64::INFINITY`：它
/// 仍是「等待很久」的提示，不能退化成「没有提示」——调用方可据此区分「没有给」「有限值」
/// 「无穷」三种情况。
///
/// 只接受 RFC 9110 的文法（区分大小写、时区只认字面 `GMT`，见 [`parse_http_date`]）；
/// 与 Python 侧 0.4 之前的行为差异记录在 #109（Python 经 email.utils 解析）：不合文法的
/// 输入上 Python 更宽松；rfc850-date 的两位年份 Python 按 POSIX 世纪规则（大于 68 为
/// 1900+）解释，本实现按 §5.6.7 的 50 年规则。收紧与统一属 0.4 周期的规范/向量工作。
pub fn retry_after_seconds(value: &str) -> Option<f64> {
    retry_after_seconds_at(value, unix_now_seconds())
}

/// [`retry_after_seconds`] 的显式时钟版本（测试与可控时钟用）；`now_unix_seconds` 只用于
/// rfc850-date 两位年份的 50 年规则（RFC 9110 §5.6.7）。
pub fn retry_after_seconds_at(value: &str, now_unix_seconds: i64) -> Option<f64> {
    let text = value.trim();
    if !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()) {
        // delay-seconds：超长数字串（无法表示的秒数）解析为 inf，不当作「没有给」
        return Some(text.parse::<f64>().unwrap_or(f64::INFINITY));
    }
    let when = parse_http_date(text, now_unix_seconds)?;
    Some((when as f64 - now_unix_seconds as f64).max(0.0))
}

/// 当前 Unix 秒（UTC）；系统时间早于纪元或不可用时为 0。
fn unix_now_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0)
}

/// HTTP-date（RFC 9110 §5.6.7）→ Unix 秒（UTC）；不合文法或日期不存在时为 `None`。
///
/// §5.6.7 的三种形式都必须接受：IMF-fixdate（`Sun, 06 Nov 1994 08:49:37 GMT`）、
/// rfc850-date（`Sunday, 06-Nov-94 08:49:37 GMT`）与 asctime-date
/// （`Sun Nov  6 08:49:37 1994`）。文法**区分大小写**，SP 的个数是文法的一部分（按 `' '`
/// 逐字切分，asctime 的 `date3` 只在 `SP SP 1DIGIT` 形式下允许 1 位日）；时区只认字面
/// `GMT`。星期名不与日期核对（§5.6.7：接收方允许不校验，日期本身合法即可）；日期必须真实
/// 存在（`31 Feb`、非闰年的 `29 Feb` 拒绝）；时刻按 `hour 00-23` / `minute 00-59` /
/// `second 00-60`（闰秒）。`now_unix_seconds` 只用于 rfc850 两位年份的 50 年规则。
fn parse_http_date(text: &str, now_unix_seconds: i64) -> Option<i64> {
    imf_fixdate(text)
        .or_else(|| rfc850_date(text, now_unix_seconds))
        .or_else(|| asctime_date(text))
}

/// IMF-fixdate：`day-name "," SP date1 SP time-of-day SP GMT`；`date1 = day SP month SP year`
/// （RFC 9110 §5.6.7）。
fn imf_fixdate(text: &str) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let [weekday, day, month, year, time, "GMT"] = parts.as_slice() else {
        return None;
    };
    if !DAY_NAMES.contains(&weekday.strip_suffix(',')?) {
        return None;
    }
    timestamp(
        four_digits(year)?,
        month_number(month)?,
        two_digits(day)?,
        time_of_day(time)?,
    )
}

/// rfc850-date：`day-name-l "," SP date2 SP time-of-day SP GMT`；`date2 = day "-" month "-"`
/// 加两位年份（RFC 9110 §5.6.7，年份按 50 年规则解释，见 [`two_digit_year`]）。
fn rfc850_date(text: &str, now_unix_seconds: i64) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let [weekday, date2, time, "GMT"] = parts.as_slice() else {
        return None;
    };
    if !DAY_NAMES_LONG.contains(&weekday.strip_suffix(',')?) {
        return None;
    }
    let mut date = date2.split('-');
    let day = two_digits(date.next()?)?;
    let month = month_number(date.next()?)?;
    let year2 = two_digits(date.next()?)?;
    if date.next().is_some() {
        return None;
    }
    let time = time_of_day(time)?;
    let year = two_digit_year(year2, now_unix_seconds, month, day, time);
    timestamp(year, month, day, time)
}

/// asctime-date：`day-name SP date3 SP time-of-day SP year`，
/// `date3 = month SP ( 2DIGIT / ( SP 1DIGIT ) )`（RFC 9110 §5.6.7）。
fn asctime_date(text: &str) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let (weekday, month, day, time, year) = match parts.as_slice() {
        [weekday, month, day, time, year] => (*weekday, *month, two_digits(day)?, *time, *year),
        // `SP SP 1DIGIT`：日只有 1 位，与月份名之间有两个 SP
        [weekday, month, "", day, time, year] => (*weekday, *month, one_digit(day)?, *time, *year),
        _ => return None,
    };
    if !DAY_NAMES.contains(&weekday) {
        return None;
    }
    timestamp(
        four_digits(year)?,
        month_number(month)?,
        day,
        time_of_day(time)?,
    )
}

/// 时间戳是 RFC 3339（core：时间戳一律 RFC 3339 UTC）：`YYYY-MM-DD` + 分隔符（`T` / `t` /
/// 空格，RFC 3339 §5.6 NOTE）+ `HH:MM:SS[.fraction]` + 偏移（`Z` / `z` / `±HH:MM`）。日期必须
/// 真实存在；`second` 允许 60（闰秒）。
///
/// 与 Python 侧 `datetime.fromisoformat` 的差异（#109 同类：语言库内部宽松，不复制）：只接受
/// 上述文法，不接受缺偏移、缺秒、非 `-` 分隔日期等 Python 额外放行的写法。
fn is_rfc3339(text: &str) -> bool {
    if text.len() < 20 || !text.is_ascii() {
        return false;
    }
    let (date, rest) = text.split_at(10);
    if !matches!(rest.as_bytes().first(), Some(b'T' | b't' | b' ')) {
        return false;
    }
    let body = &rest[1..];
    let Some(zone_at) = body.find(['Z', 'z', '+', '-']) else {
        return false;
    };
    let (time, zone) = body.split_at(zone_at);
    // 日期：YYYY-MM-DD
    let date_parts: Vec<&str> = date.split('-').collect();
    let [year, month, day] = date_parts.as_slice() else {
        return false;
    };
    if year.len() != 4 || month.len() != 2 || day.len() != 2 {
        return false;
    }
    let (Some(year), Some(month), Some(day)) =
        (parse_digits(year), parse_digits(month), parse_digits(day))
    else {
        return false;
    };
    let (month, day) = (month as u32, day as u32);
    if !(1..=12).contains(&month) || day < 1 || i64::from(day) > days_in_month(year as i64, month) {
        return false;
    }
    // 时间：HH:MM:SS[.fraction]
    let (clock, fraction) = match time.split_once('.') {
        Some((clock, fraction)) => (clock, Some(fraction)),
        None => (time, None),
    };
    if let Some(fraction) = fraction {
        if fraction.is_empty() || !fraction.bytes().all(|byte| byte.is_ascii_digit()) {
            return false;
        }
    }
    let clock_parts: Vec<&str> = clock.split(':').collect();
    let [hour, minute, second] = clock_parts.as_slice() else {
        return false;
    };
    let (Some(hour), Some(minute), Some(second)) = (
        parse_digits(hour),
        parse_digits(minute),
        parse_digits(second),
    ) else {
        return false;
    };
    if hour > 23 || minute > 59 || second > 60 {
        return false;
    }
    // 偏移：Z / z / ±HH:MM
    match zone {
        "Z" | "z" => true,
        _ => {
            let offset_parts: Vec<&str> = zone[1..].split(':').collect();
            let [hours, minutes] = offset_parts.as_slice() else {
                return false;
            };
            match (parse_digits(hours), parse_digits(minutes)) {
                (Some(hours), Some(minutes)) => hours <= 23 && minutes <= 59,
                _ => false,
            }
        }
    }
}

/// 十进制无符号整数；非数字或放不下 `u64` 时为 `None`。
fn parse_digits(text: &str) -> Option<u64> {
    if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// §5.6.7 的 `day-name`（区分大小写）。
const DAY_NAMES: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/// §5.6.7 的 `day-name-l`（rfc850-date 的完整星期名）。
const DAY_NAMES_LONG: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];

/// 月份字面 `%s"Jan"` 等（区分大小写）→ 月份号。
fn month_number(text: &str) -> Option<u32> {
    Some(match text {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    })
}

/// `2DIGIT`：恰好两位十进制。
fn two_digits(text: &str) -> Option<u32> {
    if text.len() != 2 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `1DIGIT`。
fn one_digit(text: &str) -> Option<u32> {
    if text.len() != 1 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `4DIGIT` 年份。
fn four_digits(text: &str) -> Option<i64> {
    if text.len() != 4 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `time-of-day = hour ":" minute ":" second`（各 `2DIGIT`；hour ≤ 23、minute ≤ 59、
/// second ≤ 60——RFC 9110 §5.6.7 允许闰秒）。
fn time_of_day(text: &str) -> Option<(i64, i64, i64)> {
    let mut parts = text.split(':');
    let hour = two_digits(parts.next()?)?;
    let minute = two_digits(parts.next()?)?;
    let second = two_digits(parts.next()?)?;
    if parts.next().is_some() || hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    Some((hour.into(), minute.into(), second.into()))
}

/// 组装 Unix 秒；日期不存在（如 11 月 31 日、非闰年的 2 月 29 日）为 `None`。
fn timestamp(
    year: i64,
    month: u32,
    day: u32,
    (hour, minute, second): (i64, i64, i64),
) -> Option<i64> {
    if day < 1 || i64::from(day) > days_in_month(year, month) {
        return None;
    }
    Some(days_from_civil(year, month, i64::from(day)) * 86400 + hour * 3600 + minute * 60 + second)
}

/// rfc850 的两位年份 → 四位年份（RFC 9110 §5.6.7 的 50 年规则）：先按「当前世纪 + 两位年」
/// 读；该时刻落在「现在」之后 50 年以上时（逐字段比较，含时刻），回退一个世纪——即取过去
/// 100 年内末两位相同的最近一年。
fn two_digit_year(
    year2: u32,
    now_unix_seconds: i64,
    month: u32,
    day: u32,
    time: (i64, i64, i64),
) -> i64 {
    let (now_year, now_month, now_day) = civil_from_days(now_unix_seconds.div_euclid(86400));
    let seconds_of_day = now_unix_seconds.rem_euclid(86400);
    let now_time = (
        seconds_of_day / 3600,
        seconds_of_day % 3600 / 60,
        seconds_of_day % 60,
    );
    let candidate = now_year.div_euclid(100) * 100 + i64::from(year2);
    let beyond_fifty_years = candidate > now_year + 50
        || (candidate == now_year + 50 && (month, day, time) > (now_month, now_day, now_time));
    if beyond_fifty_years {
        candidate - 100
    } else {
        candidate
    }
}

/// 距 1970-01-01 的天数 → 公历日期（Howard Hinnant 的 civil_from_days 算法）。
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = if shifted >= 0 {
        shifted
    } else {
        shifted - 146_096
    } / 146_097;
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (year + i64::from(month <= 2), month as u32, day as u32)
}

fn days_in_month(year: i64, month: u32) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn is_leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

/// 公历日期 → 距 1970-01-01 的天数（Howard Hinnant 的 days_from_civil 算法）。
fn days_from_civil(year: i64, month: u32, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year =
        (153 * i64::from(if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146097 + day_of_era - 719468
}
