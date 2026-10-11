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
//! 不受这条限制，它们不改变请求是否生效的判定。只读请求（`retry_safe` 为 `true`）原样重发总是
//! 安全的，传输层可以自行重发。
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

use std::collections::BTreeMap;
use std::fmt;

use serde_json::{json, Value};

use crate::errors::{Error, ErrorKind};
use crate::wire::{BatchHeads, Capabilities, ListPage, Skeleton};

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

/// 驱动方回送的传输异常（连接失败、超时等）。核心不窥探其内容，只按「响应丢失」判定。
pub type TransportError = Box<dyn std::error::Error + Send + Sync + 'static>;

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

/// 请求体。[`Body::Bytes`] 是完整字节；大 blob 的延迟区间（`BlobRange`）随后续任务加入。
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Body {
    None,
    Bytes(Vec<u8>),
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
/// `max_resends` 是写操作在响应丢失后原样重发的次数上限（见模块说明的驱动约定）。
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
            phase: Phase::Ready(self.request(Method::Head, target, Body::None, true)),
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
            phase: Phase::Ready(self.request(Method::Get, target, Body::None, true)),
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
            phase: Phase::Ready(self.request(Method::Get, target, Body::None, true)),
        })
    }

    // ------------------------------------------------------------------
    // 请求与响应处理
    // ------------------------------------------------------------------

    /// 带契约头的请求；有请求体时补 `Content-Type: application/json`。
    fn request(&self, method: Method, target: String, body: Body, retry_safe: bool) -> Request {
        let mut headers = Headers::new();
        headers.insert(CONTRACT_HEADER, self.contract.clone());
        if !matches!(body, Body::None) {
            headers.insert("Content-Type", "application/json");
        }
        Request {
            method,
            target,
            headers,
            body,
            retry_safe,
        }
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
        let request = self
            .core
            .request(Method::Post, "heads".to_owned(), json_body(&body), true);
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

/// 错误响应对应的错误（HTTP 绑定 §3.2、§5）：problem 体的 `code` 优先，其次
/// `DPE-Error-Code` 头，都没有则不是 DPE 错误。
fn error_from_response(response: &Response) -> Error {
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
