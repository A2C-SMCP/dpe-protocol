//! HTTP 传输适配（默认 feature `http`）：reqwest / tokio 的异步客户端 [`DpeClient`]。
//!
//! **职责边界**：只做 I/O——拼 URL（remote 之后接核心产出的已编码 target，**不再编码**）、
//! 注入凭证、按策略 gzip、把线上响应还原为 [`Response`]；协议语义全部在 sans-IO 核心，
//! 核心不依赖本模块、不依赖异步运行时。同步驱动用核心的 [`drive`](crate::protocol::drive)，
//! 异步驱动用 [`DpeClient::drive`]（本模块），两者是同一契约的两种循环。与 Python 侧
//! `dpe_sdk.transport` 行为对等。
//!
//! **传输层可以自行重发的情形只有三类**（其余失败原样抛出，交核心按 core §5.2 处理）。本地
//! 构造失败（非法 URL 主机、请求头字段值不符合 HTTP 绑定 §6）不在其列：`send` 在发出前用
//! [`local_request_error`] 标记返回，驱动层据此直接终止、不重发（#207、#253）：
//!
//! 1. [`retry_safe`](Request::retry_safe) 为真的请求（只读操作与断点查询）遇网络失败——
//!    超时、连接中断、响应丢失等；重发安全由该字段决定，**不按 HTTP 方法推断**（`batch_head`
//!    走 POST 但只读）；
//! 2. 任何请求遇**确定未发出**的失败（建连失败、建连超时，即 reqwest 的 `is_connect`）——
//!    对端连字节都没收到，原样重发不改变请求是否生效的判定；写请求也不例外。放弃发送时用
//!    [`not_sent`] 把异常包装成 [`NotSent`](crate::protocol::NotSent)（写操作据此把「确定未生效」
//!    与「结果未知」分开），原始异常经 `downcast_ref` 可取回；
//! 3. 响应是可重试的 DPE 错误——按 `code` 的 `retryable` 判定（不按状态码，HTTP 绑定 §3.2），
//!    即 `DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`——等待 `Retry-After` 后原样重发。等待超过
//!    [`MAX_RETRY_WAIT`]（含 `inf`）时不吸收，响应原样交回核心、错误带 `retry_after` 如实抛出
//!    （何时重试由调用方决定）。
//!
//! 写请求在连续建连失败时两级的预算会叠加（传输层每轮与核心写路径的 `max_resends`，最坏
//! `(1 + max_retries) × (1 + max_resends)` 次尝试）——两者都只覆盖「请求确定未生效」的情形，
//! 叠加是安全的：建连失败确定未发出；带码的 `DPE_RATE_LIMITED` / `DPE_UNAVAILABLE` 依 core §6
//! 表示请求未产生任何效果。
//!
//! **「确定未发出」标记按单次尝试的异常判定**（同 Python：逐异常判定，不是「整次 send 的尝试
//! 全部未发出」的累计语义）：写请求只会在「确定未发出」的失败、或按 `code` 判定的可重试响应
//! 之后重发，后者是服务端「请求未被处理」的明确裁决（core §6），两者都不让先前的尝试变得
//! 「可能已生效」；所以最后的异常只要是建连类失败，这次 send 的每个尝试就都没有生效。
//!
//! **gzip**（HTTP 绑定 §7）：请求体在满足全部条件时压缩并带 `Content-Encoding: gzip`——服务端
//! capabilities 的 `content_encodings` 含 `gzip`、媒体类型为 `application/json`、体不小于
//! 1024 字节（更小的体压缩后反而更大）、且不是分块请求（带 `Content-Range` 的请求体按原始
//! 字节处理、不走解码路径，MUST NOT 压缩）。压缩是确定性的（mtime=0），重发各轮复用同一份
//! 字节。
//!
//! **响应侧不请求也不解压压缩**：本适配不发送 `Accept-Encoding`，合规服务端按 identity 应答
//! （响应压缩不在 HTTP 绑定内，参考服务端也不压响应）。reqwest 的响应解压 feature 会引入
//! 要求 Rust ≥ 1.83 的依赖链、超出本 workspace 的 MSRV 1.80；真遇到未经请求的压缩响应会按
//! 「响应不合规」如实上报，不静默出错。同理不开 http2，与 Python 侧（纯 HTTP/1.1）同形态。
//!
//! **remote**：一个客户端绑定一个 remote（HTTP 绑定 §1 的 base URL，如
//! `https://example.com/anything/r/42`），按 HTTP 绑定 §1 的文法校验（[`crate::remote_url`]：
//! http / https、规范化不动点、无查询串与片段、无 userinfo（凭证走 [`CredentialProvider`]）、
//! 不以 `/` 结尾等），合法即原样使用。capabilities 在客户端
//! 实例内快照一次（核心的契约与 limits 与它绑定；并发首次访问只协商一次），服务端变更能力后
//! 应新建客户端。
//!
//! **缺省**：超时 30 秒（[`DpeClientBuilder::timeout`] 可配，`None` 为不限时）——整个请求受
//! 它约束，连接阶段取其中的 90%（[`connect_budget`]：连接计时器必须先到点，连接阶段的超时
//! 才落在连接类、「确定未发出」）；不跟随重定向（协议未定义）；分块上传等大 blob 的源读取
//! （[`Body::BlobRange`]）经 `spawn_blocking` 在工作线程取出，不阻塞执行器线程。
//!
//! **代理只来自环境变量**（#132）：[`build`](DpeClientBuilder::build) 按固定规则从
//! `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 判定 remote 走哪个代理（规则见
//! `src/proxy.rs` 与 crate README 传输一节，与 Python 侧同一张用例表），先关掉 reqwest 自身的代理探测、再
//! 显式挂上判定结果——reqwest 的 `system-proxy` feature 若经依赖图合并被打开，会读 macOS /
//! Windows 的系统代理设置，这里不受它影响。代理 scheme 只支持 http / https，其他 scheme 在构造
//! 时报 `invalid_argument`。连不上代理、HTTPS 隧道（`CONNECT`）被拒都发生在请求到达 remote
//! 之前，属于 reqwest 的 `is_connect`，即「确定未发出」；明文 HTTP 经代理转发时，代理的应答
//! （如 502）是普通响应。

use std::fmt;
use std::io::Write as _;
use std::sync::Arc;
use std::time::Duration;

use flate2::write::GzEncoder;
use flate2::Compression;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use tokio::sync::OnceCell;

use crate::credentials::CredentialProvider;
use crate::errors::Error;
use crate::models::{ElementObject, ExpandedDocument, PageObject};
use crate::protocol::{
    error_from_response, fetch_capabilities, local_request_error, not_sent, BlobProvider,
    BlobSource, Body, Method, Negotiated, Operation, Precondition, PreparedDocument, ProtocolCore,
    Request, Response, Session, Step, TransportError, DEFAULT_MAX_RESENDS,
};
use crate::remote_url::check_remote_url;
use crate::wire::{Capabilities, CommitResult, ListPage, MoveResult, Skeleton};

/// 客户端缺省超时：连接与整个请求都受它约束（同 Python 侧的 30 秒）。
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);

/// 传输层自行重发的缺省预算（每次 `send` 的次数上限，同 Python 侧的 `max_retries`）；
/// 与核心写路径的 [`DEFAULT_MAX_RESENDS`] 相互独立，两者只覆盖「请求确定未生效」的情形。
pub const DEFAULT_MAX_RETRIES: u32 = 2;

/// 传输层愿在一次 send 内等待的 `Retry-After` 上限：更长的等待不吸收、响应原样交回核心
/// （错误带 `retry_after`）——库不在一次调用里替调用方长时间阻塞（同 Python）。
pub const MAX_RETRY_WAIT: f64 = 300.0;

/// 可重试错误没给 `Retry-After` 时的等待秒数：对齐规范给服务端的缺省提示值（HTTP 绑定 §5）。
const DEFAULT_RETRY_AFTER: f64 = 1.0;

/// 小于该字节数的请求体不压缩：JSON 头与 gzip 尾的开销会让小体反而变大（同 Python）。
const MIN_COMPRESS_BYTES: usize = 1024;

const JSON: &str = "application/json";

/// 按 HTTP 绑定 §1 的文法校验 remote，原样返回（下文路径直接拼在它之后；同 Python 的
/// `_remote_base`）。
fn remote_base(remote: &str) -> Result<String, Error> {
    check_remote_url(remote).map_err(|error| Error::invalid_argument(error.to_string()))?;
    Ok(remote.to_owned())
}

/// 读取请求头（`Headers` 的读取本就大小写不敏感）。
fn request_header<'a>(request: &'a Request, name: &str) -> Option<&'a str> {
    request.headers.get(name)
}

/// 媒体类型（去掉参数、统一小写）。
fn media_type(value: Option<&str>) -> String {
    value
        .unwrap_or_default()
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
}

/// 一次发送失败的分类：决定能否原样重发、以及是否打「确定未发出」标记（见模块说明）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failure {
    /// 确定未发出（建连失败、建连超时）：任何请求都可原样重发，放弃时打 [`not_sent`] 标记。
    NotSent,
    /// 网络失败（超时、连接中断、响应丢失等）：只有 `retry_safe` 的请求可原样重发。
    Network,
    /// 其余本地问题（如解压失败）：重发没有意义，原样抛出且不打标记。URL / 头字段值的构造
    /// 失败在进入本分类之前就已被 [`local_request_error`] 标记（#207）。
    Other,
}

/// 把 reqwest 的发送失败归类（分类口径见模块说明）。`is_connect` 覆盖建连失败与建连超时：
/// 后者由 `connect_timeout` 在连接阶段触发，错误同样出自连接器（hyper-util 标为 connect）。
fn classify(error: &reqwest::Error) -> Failure {
    if error.is_connect() {
        Failure::NotSent
    } else if error.is_timeout() || error.is_request() || error.is_body() {
        Failure::Network
    } else {
        Failure::Other
    }
}

/// 该失败下原样重发是否安全：确定未发出的任何请求都可以；网络失败只在 `retry_safe` 的请求上
/// （写请求可能已发出，交核心按 core §5.2 判定）。
fn resendable(request: &Request, failure: Failure) -> bool {
    match failure {
        Failure::NotSent => true,
        Failure::Network => request.retry_safe,
        Failure::Other => false,
    }
}

/// 响应是可重试的 DPE 错误时给出等待时长，否则 `None`（不重发）。
///
/// 按 `code` 的 `retryable` 判定（不按状态码），等待值取 `Retry-After`（缺省对齐规范给服务端
/// 的 1 秒）；超过 [`MAX_RETRY_WAIT`]（含 `inf`）时不吸收——响应原样交回核心，`retry_after`
/// 不丢。
fn retry_delay(response: &Response) -> Option<Duration> {
    if response.status() < 400 {
        return None;
    }
    let error = error_from_response(response);
    if !error.is_retryable() {
        return None;
    }
    let seconds = error.retry_after().unwrap_or(DEFAULT_RETRY_AFTER);
    if (0.0..=MAX_RETRY_WAIT).contains(&seconds) {
        Some(Duration::from_secs_f64(seconds))
    } else {
        None
    }
}

/// 确定性 gzip（mtime=0，不掺入压缩时刻）：重发各轮复用同一份字节。
fn gzip(data: &[u8]) -> std::io::Result<Vec<u8>> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data)?;
    encoder.finish()
}

/// 连接阶段的超时预算：整体超时的 90%，保证连接计时器先于请求计时器到点（两者的启动差是
/// 微秒级，90% 余量足够大），于是连接阶段挂住时错误落在连接类、按「确定未发出」处理——
/// 与 Python 侧 httpx 的 `ConnectTimeout` 语义对齐。对正常的慢连接这 10% 没有实际影响；
/// 极小的整体超时至少留 1 毫秒（0 会让连接永远失败），此时不再有严格先后——微秒级预算
/// 没有区分的意义，取保守的一侧即可。
fn connect_budget(timeout: Duration) -> Duration {
    (timeout * 9 / 10).max(Duration::from_millis(1))
}

/// 核心的 [`Method`] → reqwest 的方法。
fn http_method(method: Method) -> reqwest::Method {
    match method {
        Method::Get => reqwest::Method::GET,
        Method::Head => reqwest::Method::HEAD,
        Method::Post => reqwest::Method::POST,
        Method::Put => reqwest::Method::PUT,
        Method::Delete => reqwest::Method::DELETE,
    }
}

/// reqwest / tokio 的异步 DPE 客户端：一个实例绑定一个 remote（HTTP 绑定 §1），
/// 操作方法与 sans-IO 核心一一对应（协议逻辑在核心，这里只是驱动 + 传输）。
///
/// ```no_run
/// use dpe_sdk::http::DpeClient;
///
/// # async fn example() -> Result<(), dpe_sdk::Error> {
/// let client = DpeClient::new("https://example.com/r/42")?;
/// let hash = client.head("s3://bucket/a.md").await?;
/// # let _ = hash;
/// # Ok(())
/// # }
/// ```
///
/// - capabilities 在实例内快照一次（并发首次访问只协商一次）；服务端变更能力后应新建实例；
/// - [`send`](DpeClient::send) 与 [`drive`](DpeClient::drive) 是公开的驱动缝：自定义
///   [`Operation`] 可以直接经它们执行；
/// - 可跨任务共享（`Send + Sync`）。
pub struct DpeClient {
    remote: String,
    contract: String,
    max_resends: u32,
    max_retries: u32,
    credential: Option<Arc<dyn CredentialProvider>>,
    http: reqwest::Client,
    core: OnceCell<ProtocolCore>,
}

impl fmt::Debug for DpeClient {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DpeClient")
            .field("remote", &self.remote)
            .field("contract", &self.contract)
            .field("max_resends", &self.max_resends)
            .field("max_retries", &self.max_retries)
            .field("credential", &self.credential.is_some())
            .finish_non_exhaustive()
    }
}

impl DpeClient {
    /// 以缺省配置构造（见 [`DpeClientBuilder`]）；remote 不合法返回
    /// [`ErrorKind::InvalidArgument`](crate::ErrorKind::InvalidArgument)。
    pub fn new(remote: impl Into<String>) -> Result<Self, Error> {
        DpeClientBuilder::new(remote).build()
    }

    /// 逐个配置选项的构造器。
    pub fn builder(remote: impl Into<String>) -> DpeClientBuilder {
        DpeClientBuilder::new(remote)
    }

    /// remote（已通过 HTTP 绑定 §1 的文法，原样保存）。
    pub fn remote(&self) -> &str {
        &self.remote
    }

    /// 与 remote 协商后的协议核心（首次访问取 capabilities 并构造，之后快照复用）。
    ///
    /// 契约与 limits 与快照绑定：服务端变更能力后应新建客户端。并发首次访问只协商一次
    /// （其余调用等在同一处初始化上，拿到同一实例）。
    pub async fn core(&self) -> Result<&ProtocolCore, Error> {
        self.core
            .get_or_try_init(|| async {
                let capabilities = self.drive(fetch_capabilities()).await?;
                ProtocolCore::new(capabilities, &self.contract, self.max_resends)
            })
            .await
    }

    /// 服务端能力（`GET capabilities`，HTTP 绑定 §4.1）：客户端实例内快照一次。
    pub async fn capabilities(&self) -> Result<&Capabilities, Error> {
        Ok(self.core().await?.capabilities())
    }

    /// 发送一个请求，返回响应；自行重发的规则与「确定未发出」标记见模块说明。
    ///
    /// 本地构造失败（非法 URL 主机、请求头字段值不符合 HTTP 绑定 §6）返回 [`LocalRequestError`] 标记（#207、#253）；
    /// 驱动层据此以 [`crate::ErrorKind::LocalRequest`] 终止操作、不重发。
    ///
    /// 其余失败（网络失败的异常、以及交给核心的 4xx / 5xx 响应）原样抛出或原样返回——异常
    /// 类型不包装（reqwest 的错误类型可 `downcast_ref::<reqwest::Error>()` 取回），调用方按
    /// 类型区分连接失败、超时与响应丢失；写操作的结果判定请用核心的
    /// [`Error::outcome_unknown`](crate::Error::outcome_unknown)。
    pub async fn send(&self, request: Request) -> Result<Response, TransportError> {
        let body = self.body_bytes(&request).await?;
        let (body, gzipped) = self.wire_body(&request, body);
        let url = format!("{}/{}", self.remote, request.target);
        let method = http_method(request.method);
        let mut attempt: u32 = 0;
        loop {
            let headers = self.headers(&request, gzipped)?; // 每轮重发前重新取凭证

            // 与 reqwest 内部同一个解析器预解析：非法 A-label 在发出前即失败（#207）
            let target = reqwest::Url::parse(&url).map_err(local_request_error)?;
            let reply = self
                .attempt(method.clone(), target, headers, body.clone())
                .await;
            match reply {
                Ok(response) => {
                    let Some(delay) = retry_delay(&response) else {
                        return Ok(response);
                    };
                    if attempt >= self.max_retries {
                        return Ok(response);
                    }
                    attempt += 1;
                    tokio::time::sleep(delay).await;
                }
                Err(error) => {
                    let failure = classify(&error);
                    if attempt >= self.max_retries || !resendable(&request, failure) {
                        return Err(if failure == Failure::NotSent {
                            not_sent(error) // 底标：写操作据此判定「确定未生效」
                        } else {
                            Box::new(error)
                        });
                    }
                    attempt += 1;
                }
            }
        }
    }

    /// 异步驱动一个操作直到完成（[`drive`](crate::protocol::drive) 的异步版本，同一契约）：
    /// [`send`](DpeClient::send) 的失败经 [`Operation::on_transport_error`] 回送。本地构造失败
    /// 由操作按 [`LocalRequestError`] 直接终止、不重发（#207）。
    pub async fn drive<O: Operation>(&self, mut op: O) -> Result<O::Output, Error> {
        loop {
            match op.poll()? {
                Step::Send(request) => match self.send(request).await {
                    Ok(response) => op.on_response(response)?,
                    Err(error) => op.on_transport_error(error)?,
                },
                Step::Done(output) => return Ok(output),
            }
        }
    }

    // ------------------------------------------------------------------
    // 与 ProtocolCore 一一对应的操作（薄包装，协议逻辑在核心）
    // ------------------------------------------------------------------

    /// `ProtocolCore::head`（HTTP 绑定 §3.3）：文档的 doc_hash，不存在为 `None`。
    pub async fn head(&self, uri: &str) -> Result<Option<String>, Error> {
        let core = self.core().await?;
        self.drive(core.head(uri)).await
    }

    /// `ProtocolCore::batch_head`：按 `batch_head_max` 分批，与入参一一对应。
    pub async fn batch_head(&self, uris: &[String]) -> Result<Vec<Option<String>>, Error> {
        let core = self.core().await?;
        self.drive(core.batch_head(uris)).await
    }

    /// `ProtocolCore::get_skeleton`（HTTP 绑定 §4.3）：文档对象与全部页对象。
    pub async fn get_skeleton(&self, uri: &str) -> Result<Option<Skeleton>, Error> {
        let core = self.core().await?;
        self.drive(core.get_skeleton(uri)?).await
    }

    /// `ProtocolCore::list_page`（HTTP 绑定 §4.4）：`next_cursor` 为 `None` 即结束。
    pub async fn list_page(
        &self,
        prefix: &str,
        cursor: Option<&str>,
        limit: Option<u64>,
    ) -> Result<ListPage, Error> {
        let core = self.core().await?;
        self.drive(core.list_page(prefix, cursor, limit)?).await
    }

    /// `ProtocolCore::negotiate`（HTTP 绑定 §4.5）：开启暂存会话并返回逐层缺失清单。
    pub async fn negotiate(
        &self,
        uri: &str,
        document: &ExpandedDocument,
    ) -> Result<Negotiated, Error> {
        let core = self.core().await?;
        self.drive(core.negotiate(uri, document)?).await
    }

    /// `ProtocolCore::upload_page`（HTTP 绑定 §4.6）：上传页对象，返回缺失的元素 hash。
    pub async fn upload_page(
        &self,
        session: &mut Session,
        page: &PageObject,
    ) -> Result<Vec<String>, Error> {
        let core = self.core().await?;
        self.drive(core.upload_page(session, page)?).await
    }

    /// `ProtocolCore::upload_element`（HTTP 绑定 §4.6）：上传元素对象，返回缺失的 blob。
    pub async fn upload_element(
        &self,
        session: &mut Session,
        element: &ElementObject,
    ) -> Result<Vec<String>, Error> {
        let core = self.core().await?;
        self.drive(core.upload_element(session, element)?).await
    }

    /// `ProtocolCore::upload_blob`（HTTP 绑定 §4.7）：整段或分块上传 blob（[`BlobSource`] 的
    /// 源读取在工作线程）。`media_type` 缺省 `application/octet-stream`。
    pub async fn upload_blob(
        &self,
        session: &mut Session,
        source: BlobSource,
        media_type: Option<&str>,
    ) -> Result<(), Error> {
        let core = self.core().await?;
        self.drive(core.upload_blob(session, source, media_type)?)
            .await
    }

    /// `ProtocolCore::commit`（core §3.2）：快路径或引用暂存会话提交，绝不自动 force。
    pub async fn commit(
        &self,
        uri: &str,
        document: &ExpandedDocument,
        precondition: Precondition,
        session: Option<&Session>,
    ) -> Result<CommitResult, Error> {
        let core = self.core().await?;
        self.drive(core.commit(uri, document, precondition, session)?)
            .await
    }

    /// `ProtocolCore::deliver`（core §3.2）：增量投递，只传变更，绝不自动 force。
    ///
    /// `blobs` 是缺失 blob 的字节来源；缺省时若确需上传 blob，失败并给出缺失清单。
    ///
    /// `BlobProvider` 的回调须为 `Send`（#131），因此本方法返回的 future 是 `Send`，可以交给
    /// `tokio::spawn`。
    pub async fn deliver<'a>(
        &'a self,
        uri: &str,
        document: &ExpandedDocument,
        precondition: Precondition,
        blobs: Option<BlobProvider<'a>>,
        session: Option<Session>,
    ) -> Result<CommitResult, Error> {
        let core = self.core().await?;
        self.drive(core.deliver(uri, document, precondition, blobs, session)?)
            .await
    }

    /// 推送器内部通道：用预算好的三层 hash 与 dump 投递（契约不符由核心回退）。
    pub(crate) async fn deliver_prepared<'a>(
        &'a self,
        uri: &str,
        document: &ExpandedDocument,
        prepared: &PreparedDocument,
        precondition: Precondition,
        blobs: Option<BlobProvider<'a>>,
        session: Option<Session>,
    ) -> Result<CommitResult, Error> {
        let core = self.core().await?;
        self.drive(core.deliver_prepared(uri, document, prepared, precondition, blobs, session)?)
            .await
    }

    /// `ProtocolCore::delete`（core §4）：按 CAS 删除文档。
    pub async fn delete(&self, uri: &str, base_hash: &str) -> Result<(), Error> {
        let core = self.core().await?;
        self.drive(core.delete(uri, base_hash)?).await
    }

    /// `ProtocolCore::move_document`（core §5.2）：按 CAS 原子改名，学习产物原样保留。
    pub async fn move_document(
        &self,
        from_uri: &str,
        to_uri: &str,
        base_hash: &str,
    ) -> Result<MoveResult, Error> {
        let core = self.core().await?;
        self.drive(core.move_document(from_uri, to_uri, base_hash)?)
            .await
    }

    // ------------------------------------------------------------------
    // 线上编解码（纯计算，不做 I/O）
    // ------------------------------------------------------------------

    /// 请求体的字节：`BlobRange` 可能是阻塞的源读取，放进工作线程；bytes 体不绕线程
    /// （同 Python 的 `AsyncDpeClient._body`）。源给的字节数与声明区间不符时如实报错
    /// （调用方的源有问题），不发请求，也不打「确定未发出」标记（保守：按可能已发出处理）。
    async fn body_bytes(&self, request: &Request) -> Result<Option<Vec<u8>>, TransportError> {
        if let Body::BlobRange(_) = &request.body {
            let request = request.clone();
            let data = tokio::task::spawn_blocking(move || request.body_bytes())
                .await
                .map_err(|error| -> TransportError {
                    Box::new(std::io::Error::other(format!(
                        "读取 blob 源的任务失败：{error}"
                    )))
                })?;
            return data.map_err(|error| -> TransportError { Box::new(error) });
        }
        request
            .body_bytes()
            .map_err(|error| -> TransportError { Box::new(error) })
    }

    /// (请求体, 是否已 gzip)：一次算好，重发各轮复用（大体重压一次即可）。
    fn wire_body(&self, request: &Request, body: Option<Vec<u8>>) -> (Option<Vec<u8>>, bool) {
        let Some(body) = body else {
            return (None, false);
        };
        if !self.compressible(request, &body) {
            return (Some(body), false);
        }
        // 压缩到内存缓冲不会失败；万一失败（如内存不足）退化为 identity——服务端恒接受
        // identity，不因压缩器的问题让请求失败
        match gzip(&body) {
            Ok(compressed) => (Some(compressed), true),
            Err(_) => (Some(body), false),
        }
    }

    /// gzip 策略（HTTP 绑定 §7）：四个条件全部满足才压缩（见模块说明）。
    fn compressible(&self, request: &Request, body: &[u8]) -> bool {
        let offers_gzip = self
            .core
            .get()
            .map(|core| {
                core.capabilities()
                    .content_encodings
                    .iter()
                    .any(|encoding| encoding == "gzip")
            })
            .unwrap_or(false); // 核心尚未建立：还没有带体的请求（capabilities 是第一个）
        body.len() >= MIN_COMPRESS_BYTES
            && request_header(request, "Content-Range").is_none()
            && media_type(request_header(request, "Content-Type")) == JSON
            && offers_gzip
    }

    /// 本轮请求头：核心给的字段 + 凭证。凭证**每轮发送前重新取**（provider 的调用时机就是
    /// 「请求发送前」）——网络失败后的重发、按 `Retry-After` 等待数百秒后的重试，都能拿到
    /// 轮换后的凭证。
    fn headers(&self, request: &Request, gzipped: bool) -> Result<HeaderMap, TransportError> {
        let mut headers = HeaderMap::new();
        for (name, value) in request.headers.iter() {
            let name = HeaderName::from_bytes(name.as_bytes()).map_err(local_request_error)?;
            let value = Self::header_value(value)?;
            headers.insert(name, value);
        }
        if gzipped {
            headers.insert(
                reqwest::header::CONTENT_ENCODING,
                HeaderValue::from_static("gzip"),
            );
        }
        if let Some(provider) = &self.credential {
            if let Some(value) = provider.authorization() {
                let value = Self::header_value(&value)?;
                headers.insert(reqwest::header::AUTHORIZATION, value);
            }
        }
        Ok(headers)
    }

    /// 请求头字段值字符集（HTTP 绑定 §6，#207/#253）：空值合法；否则首尾 VCHAR、中间
    /// VCHAR / SP / HTAB（不允许 obs-text 与 CTL）。规则与 Python SDK、跑分器共用
    /// `sdk/testdata/header_values.json`，三边各持一份实现（跑分器不依赖 dpe-sdk）。
    fn field_value_is_valid(value: &str) -> bool {
        if value.is_empty() {
            return true;
        }
        let bytes = value.as_bytes();
        let allowed = |byte: u8| byte.is_ascii_graphic() || byte == b' ' || byte == b'\t';
        bytes.iter().all(|byte| allowed(*byte))
            && bytes[0].is_ascii_graphic()
            && bytes[bytes.len() - 1].is_ascii_graphic()
    }

    /// 按 HTTP 绑定 §6 判定后构造头值；非法时以固定消息标「确定未发出」。
    fn header_value(value: &str) -> Result<HeaderValue, TransportError> {
        if !Self::field_value_is_valid(value) {
            return Err(local_request_error(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "请求头字段值不符合 HTTP 绑定 §6",
            )));
        }
        HeaderValue::from_str(value).map_err(local_request_error)
    }

    /// 一轮发送：发请求、读完响应体（响应体的读失败同样是发送失败，走同一套分类）。
    async fn attempt(
        &self,
        method: reqwest::Method,
        url: reqwest::Url,
        headers: HeaderMap,
        body: Option<Vec<u8>>,
    ) -> Result<Response, reqwest::Error> {
        let mut builder = self.http.request(method, url).headers(headers);
        if let Some(body) = body {
            builder = builder.body(body);
        }
        let reply = builder.send().await?;
        let status = reply.status().as_u16();
        // 头字段名统一小写（`Response::new` 也会归一化）；值按字节如实还原
        let headers: Vec<(String, String)> = reply
            .headers()
            .iter()
            .map(|(name, value)| {
                (
                    name.as_str().to_owned(),
                    String::from_utf8_lossy(value.as_bytes()).into_owned(),
                )
            })
            .collect();
        let body = reply.bytes().await?;
        Ok(Response::new(status, headers, body.to_vec()))
    }
}

/// [`DpeClient`] 的构造器：remote 之外全部有缺省值。
///
/// remote 在 [`build`](DpeClientBuilder::build) 时按 HTTP 绑定 §1 的文法校验
/// （[`crate::remote_url::check_remote_url`]）。
pub struct DpeClientBuilder {
    remote: String,
    credential: Option<Arc<dyn CredentialProvider>>,
    contract: String,
    max_resends: u32,
    max_retries: u32,
    timeout: Option<Duration>,
}

impl DpeClientBuilder {
    /// 缺省：`contract = dpe1`、`max_resends = 2`、`max_retries = 2`、超时 30 秒、无凭证。
    pub fn new(remote: impl Into<String>) -> Self {
        DpeClientBuilder {
            remote: remote.into(),
            credential: None,
            contract: crate::CONTRACT.to_owned(),
            max_resends: DEFAULT_MAX_RESENDS,
            max_retries: DEFAULT_MAX_RETRIES,
            timeout: Some(DEFAULT_TIMEOUT),
        }
    }

    /// 可插拔的凭证提供者（HTTP 绑定 §6）：每个请求发送前取一次。
    pub fn credential(mut self, provider: Arc<dyn CredentialProvider>) -> Self {
        self.credential = Some(provider);
        self
    }

    /// 本次会话使用的 hash 契约（缺省 [`CONTRACT`](crate::CONTRACT)）；服务端不接受时在
    /// 首次访问 [`DpeClient::core`] 时报 `DPE_CONTRACT_UNSUPPORTED`，不换契约、不降级。
    pub fn contract(mut self, contract: impl Into<String>) -> Self {
        self.contract = contract.into();
        self
    }

    /// 核心写操作在响应丢失后原样重发的次数上限（core §5.2）。
    pub fn max_resends(mut self, max_resends: u32) -> Self {
        self.max_resends = max_resends;
        self
    }

    /// 传输层自行重发的次数上限（只覆盖模块说明的三类情形）。
    pub fn max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = max_retries;
        self
    }

    /// 连接与整个请求的超时（缺省 30 秒；`None` 为不限时）。
    pub fn timeout(mut self, timeout: Option<Duration>) -> Self {
        self.timeout = timeout;
        self
    }

    /// 校验 remote、按环境变量判定代理（见模块说明）并构造客户端。
    pub fn build(self) -> Result<DpeClient, Error> {
        let remote = remote_base(&self.remote)?;
        let proxy = crate::proxy::proxy_for(&remote, |name| {
            std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
        })
        .map_err(Error::invalid_argument)?;
        let mut http = reqwest::Client::builder()
            // 协议未定义重定向：不跟随，3xx 原样交回核心
            .redirect(reqwest::redirect::Policy::none())
            // 关掉 reqwest 自身的代理探测（环境变量与可能经 feature 合并打开的系统设置）
            .no_proxy();
        if let Some(choice) = proxy {
            // 只回显变量名：代理 URL 可能带凭证
            let proxy = reqwest::Proxy::all(choice.url.as_str()).map_err(|_| {
                Error::invalid_argument(format!("环境变量 {} 的代理 URL 不合法", choice.variable))
            })?;
            http = http.proxy(proxy);
        }
        if let Some(timeout) = self.timeout {
            // 连接超时单独设置、且必须比整体超时**早到**：reqwest 的连接计时器晚于请求计时器
            // 启动（实测两者相同时整体先触发、错误落在超时而非连接类），而连接阶段的超时要按
            // 「确定未发出」处理（见模块说明）——对齐 Python 侧 httpx 的 `ConnectTimeout` 语义。
            http = http
                .timeout(timeout)
                .connect_timeout(connect_budget(timeout));
        }
        let http = http.build().map_err(|error| {
            Error::invalid_argument(format!("HTTP 客户端构造失败：{error}（TLS 后端不可用？）"))
        })?;
        Ok(DpeClient {
            remote,
            contract: self.contract,
            max_resends: self.max_resends,
            max_retries: self.max_retries,
            credential: self.credential,
            http,
            core: OnceCell::new(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 重发决策矩阵：`reqwest::Error` 没有公开构造器，「确定未发出的失败对写请求也重发」
    /// 这一格在端到端侧探不到（建连失败的端口不会又冒出一个监听者），只能在纯判定上下探；
    /// 真实异常到 [`Failure`] 的归类与其余各格由 `tests/http_client.rs` 端到端覆盖。
    #[test]
    fn the_resend_decision_matrix() {
        let mut request = Request {
            method: Method::Put,
            target: "documents?uri=s3%3A%2F%2Fbucket%2Fa.md".to_owned(),
            headers: crate::protocol::Headers::new(),
            body: Body::Bytes(Vec::new()),
            retry_safe: true,
        };
        assert!(resendable(&request, Failure::NotSent));
        assert!(resendable(&request, Failure::Network));
        assert!(!resendable(&request, Failure::Other));

        request.retry_safe = false; // 写请求：只有「确定未发出」可以原样重发
        assert!(resendable(&request, Failure::NotSent));
        assert!(!resendable(&request, Failure::Network));
        assert!(!resendable(&request, Failure::Other));
    }

    /// 连接预算恒为正、且严格小于整体超时（连接计时器必须先到点，见 [`connect_budget`]）。
    #[test]
    fn the_connect_budget_is_positive_and_shorter_than_the_overall_timeout() {
        for timeout in [
            Duration::from_millis(1),
            Duration::from_millis(150),
            Duration::from_secs(1),
            DEFAULT_TIMEOUT,
        ] {
            let connect = connect_budget(timeout);
            assert!(!connect.is_zero(), "{timeout:?}");
            assert!(connect <= timeout, "{timeout:?}");
        }
    }
}
