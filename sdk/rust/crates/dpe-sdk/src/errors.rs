//! 协议错误（core §6）与客户端本地错误（HTTP 绑定 §5）。
//!
//! 与 Python `dpe_sdk.errors` 对等：core §6 的每个错误码对应一个 [`ErrorKind`]，错误经
//! [`Error::from_problem`] 按 problem 体的 `code` 分派（HEAD 响应没有体，调用方先取出
//! `DPE-Error-Code` 头再交给它），**MUST NOT 按 HTTP 状态码分派**（HTTP 绑定 §3.2）。
//!
//! - `retryable` 只表示「原样重试同一请求可能成功」（core §6）：`DPE_RATE_LIMITED` 与
//!   `DPE_UNAVAILABLE` 为真，其余一律为假；core §6 之外的码只采信 problem 体中为 `true`
//!   的声明。
//! - `path` 是违例位置（RFC 6901 JSON Pointer，core §2.8），只用于诊断；HTTP problem 体
//!   不携带。
//! - 客户端本地错误（[`ErrorKind::UnexpectedResponse`] / [`ErrorKind::IncompatibleServer`] /
//!   [`ErrorKind::ForceNotConfirmed`] / [`ErrorKind::InvalidArgument`]）不是协议错误，没有
//!   core §6 的码；传输异常经 [`ErrorKind::Transport`] 透传（见 [`crate::protocol`] 的驱动约定）；
//!   增量推送器的状态存储故障经 [`ErrorKind::StateStore`] 透传（同样没有 core §6 的码）。

use std::fmt;

use serde_json::{Map, Value};

use crate::protocol::TransportError;
use crate::wire::Missing;

/// 错误类别：core §6 的规范错误码 + 客户端本地错误（与 Python 的异常子类一一对应）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    // —— DPE 协议错误（core §6；code 是规范码，按 code 分派） ——
    /// 报文不合法（`DPE_VALIDATION`）。
    Validation,
    /// hash 契约版本不被支持（`DPE_CONTRACT_UNSUPPORTED`）。
    ContractUnsupported,
    /// category 不在契约的封闭枚举内（`DPE_CATEGORY_UNKNOWN`）。
    CategoryUnknown,
    /// 写操作缺少 CAS 前置条件（`DPE_PRECONDITION_REQUIRED`）。
    PreconditionRequired,
    /// 当前 doc_hash 与 `base_hash` 不符（`DPE_PRECONDITION_FAILED`）。
    PreconditionFailed,
    /// `if_absent` 冲突或 move 目标已存在（`DPE_ALREADY_EXISTS`）。
    AlreadyExists,
    /// 文档不存在，含已删除（`DPE_NOT_FOUND`）。
    NotFound,
    /// 暂存会话不可用（过期、已消费、不存在、不属于调用者或该 file_uri，不区分原因，
    /// `DPE_SESSION_EXPIRED`，core §3.4）。
    SessionExpired,
    /// 暂存上传的对象与路径中声明的 hash 不符（`DPE_HASH_MISMATCH`，仅 upload）。
    HashMismatch,
    /// commit 引用了去重范围内不可得的对象（`DPE_MISSING_CONTENT`）；清单见
    /// [`Error::missing`]。
    MissingContent,
    /// 请求、元素对象、分块页对象或 blob 超限（`DPE_PAYLOAD_TOO_LARGE`）。
    PayloadTooLarge,
    /// 无权限，含前缀授权与 force 权限（`DPE_FORBIDDEN`）。
    Forbidden,
    /// 限流（`DPE_RATE_LIMITED`），按 `retry_after` 秒后原样重试。
    RateLimited,
    /// 服务端暂时不可用（`DPE_UNAVAILABLE`），按 `retry_after` 秒后原样重试。
    Unavailable,
    /// core §6 之外的码：原码保留在 [`Error::code`]，`retryable` 只采信对方为 `true` 的声明。
    Unknown,
    // —— 客户端本地错误：不是协议错误，没有 core §6 的码 ——
    /// 响应不是 DPE 的合规响应（HTTP 绑定 §5）：错误状态码却没有 DPE 错误码、成功响应的
    /// 报文不合规、骨架不自洽等。客户端 MUST NOT 猜测 code。
    UnexpectedResponse,
    /// 服务端的协议版本（capabilities 的 `protocol`）不是本 SDK 实现的版本。
    IncompatibleServer,
    /// force 提交的响应丢失，事后 `head` 未能确认它已生效（core §5.2）；细节见
    /// [`Error::force_not_confirmed`]。
    ForceNotConfirmed,
    /// 调用参数不合法（本地判定，不发请求；对应 Python 侧的 `ValueError` / `TypeError`）。
    InvalidArgument,
    /// 驱动方回送的传输异常（连接失败、超时等）：操作未处理时经
    /// [`Error::transport`] 原样上报，`source()` 可取回原始错误。
    Transport,
    /// 增量推送器的状态存储（[`StateStore`](crate::pusher::StateStore)）读写失败：本地故障，
    /// 不是传输异常；`source()` 可取回存储实现给出的原始错误（对应 Python 侧原样抛出的
    /// store 异常）。写前持久化意图失败时写请求确定未发出（[`Error::outcome_unknown`] 为假）。
    StateStore,
}

impl ErrorKind {
    /// core §6 的规范错误码；客户端本地错误与未知码没有固定的码名（未知码的原码见
    /// [`Error::code`]）。
    pub fn code(self) -> Option<&'static str> {
        Some(match self {
            ErrorKind::Validation => "DPE_VALIDATION",
            ErrorKind::ContractUnsupported => "DPE_CONTRACT_UNSUPPORTED",
            ErrorKind::CategoryUnknown => "DPE_CATEGORY_UNKNOWN",
            ErrorKind::PreconditionRequired => "DPE_PRECONDITION_REQUIRED",
            ErrorKind::PreconditionFailed => "DPE_PRECONDITION_FAILED",
            ErrorKind::AlreadyExists => "DPE_ALREADY_EXISTS",
            ErrorKind::NotFound => "DPE_NOT_FOUND",
            ErrorKind::SessionExpired => "DPE_SESSION_EXPIRED",
            ErrorKind::HashMismatch => "DPE_HASH_MISMATCH",
            ErrorKind::MissingContent => "DPE_MISSING_CONTENT",
            ErrorKind::PayloadTooLarge => "DPE_PAYLOAD_TOO_LARGE",
            ErrorKind::Forbidden => "DPE_FORBIDDEN",
            ErrorKind::RateLimited => "DPE_RATE_LIMITED",
            ErrorKind::Unavailable => "DPE_UNAVAILABLE",
            ErrorKind::Unknown
            | ErrorKind::UnexpectedResponse
            | ErrorKind::IncompatibleServer
            | ErrorKind::ForceNotConfirmed
            | ErrorKind::InvalidArgument
            | ErrorKind::Transport
            | ErrorKind::StateStore => return None,
        })
    }

    /// 原样重试同一请求可能成功（core §6）：仅 `DPE_RATE_LIMITED` 与 `DPE_UNAVAILABLE`。
    pub fn retryable(self) -> bool {
        matches!(self, ErrorKind::RateLimited | ErrorKind::Unavailable)
    }

    /// 规范错误码 → 类别；不是 core §6 定义的码返回 `None`。
    fn from_code(code: &str) -> Option<ErrorKind> {
        Some(match code {
            "DPE_VALIDATION" => ErrorKind::Validation,
            "DPE_CONTRACT_UNSUPPORTED" => ErrorKind::ContractUnsupported,
            "DPE_CATEGORY_UNKNOWN" => ErrorKind::CategoryUnknown,
            "DPE_PRECONDITION_REQUIRED" => ErrorKind::PreconditionRequired,
            "DPE_PRECONDITION_FAILED" => ErrorKind::PreconditionFailed,
            "DPE_ALREADY_EXISTS" => ErrorKind::AlreadyExists,
            "DPE_NOT_FOUND" => ErrorKind::NotFound,
            "DPE_SESSION_EXPIRED" => ErrorKind::SessionExpired,
            "DPE_HASH_MISMATCH" => ErrorKind::HashMismatch,
            "DPE_MISSING_CONTENT" => ErrorKind::MissingContent,
            "DPE_PAYLOAD_TOO_LARGE" => ErrorKind::PayloadTooLarge,
            "DPE_FORBIDDEN" => ErrorKind::Forbidden,
            "DPE_RATE_LIMITED" => ErrorKind::RateLimited,
            "DPE_UNAVAILABLE" => ErrorKind::Unavailable,
            _ => return None,
        })
    }
}

/// force 提交的结果无法确认（[`ErrorKind::ForceNotConfirmed`] 的细节，core §5.2）。
///
/// SDK 不自动重放 force：重放会覆盖首次提交之后他人的写入，由上层重新读取后决定是否再次
/// 发起。`checked` 为真时 `current` 是 `head` 读到的当前 doc_hash（文档不存在为 `None`），
/// 为假时 `head` 本身也失败了（原始错误见 `Error::source` 之前的 cause 链，`current` 无意义）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForceNotConfirmed {
    /// 本次提交内容的 doc_hash。
    pub doc_hash: String,
    /// `head` 读到的当前 doc_hash（文档不存在为 `None`）。
    pub current: Option<String>,
    /// `head` 是否成功完成。
    pub checked: bool,
}

/// 协议错误：类别决定 `code` 与 `retryable`，其余字段按类别携带诊断信息。
///
/// 不实现 `Clone`：传输透传的原始错误（`source()`）不可克隆。访问器之外的字段都是私有的，
/// 构造入口是 [`Error::from_problem`] 与 [`Error::from_hash_error`]（以及 crate 内部的
/// 判定路径）。
#[derive(Debug)]
pub struct Error {
    kind: ErrorKind,
    message: String,
    path: String,
    retryable: bool,
    retry_after: Option<f64>,
    status: Option<u16>,
    /// 少见的附加载荷（未知码原码、缺失清单、force 细节、传输透传）；装箱存放，`Error`
    /// 本身保持小尺寸（`Result` 的 `Err` 变体不宜过大）。
    details: Option<Box<Details>>,
}

/// [`Error`] 的少见载荷。
#[derive(Debug, Default)]
struct Details {
    /// 未知码的原码（core §6 之外的 `code`）。
    unknown_code: Option<String>,
    /// `DPE_MISSING_CONTENT` 的缺失清单。
    missing: Option<Missing>,
    /// 缺失清单是否不可信（缺省、形状不对或三层全空）。
    missing_truncated: bool,
    /// force 提交无法确认时的细节。
    force: Option<ForceNotConfirmed>,
    /// 透传的原始错误：传输异常（[`ErrorKind::Transport`]）或状态存储的错误
    /// （[`ErrorKind::StateStore`]），经 `source()` 取回。
    transport: Option<TransportError>,
    /// 本错误由哪个错误引起（如无法确认 force 时的 head 失败），经 `source()` 取回。
    cause: Option<Box<Error>>,
    /// `deliver` 在失败时附加的当前暂存会话（core §3.4；会话只作缓存）。
    session: Option<crate::protocol::Session>,
    /// 写操作失败时显式置位：本次写入是否可能已经生效（见 [`Error::outcome_unknown`]）。
    outcome_unknown: Option<bool>,
}

impl Error {
    /// 空的骨架：各构造入口按类别填充（`retryable` 取自类别，未知码在调用点采信对方声明）。
    fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Error {
            kind,
            message: message.into(),
            path: String::new(),
            retryable: kind.retryable(),
            retry_after: None,
            status: None,
            details: None,
        }
    }

    /// 附加载荷（首次使用时分配）。
    fn details_mut(&mut self) -> &mut Details {
        self.details.get_or_insert_with(Default::default)
    }

    /// 协议错误（已知码；本地判定与服务端返回共用，同 Python 的 `DpeError` 子类）。
    pub(crate) fn protocol(kind: ErrorKind, message: impl Into<String>) -> Self {
        debug_assert!(kind.code().is_some() || matches!(kind, ErrorKind::Unknown));
        Self::new(kind, message)
    }

    /// 响应不是合规的 DPE 响应（[`ErrorKind::UnexpectedResponse`]）；`status` 是收到的
    /// HTTP 状态码（wire 层解析出的不合规响应此时还不知道状态码，传 `None`，由协议核心用
    /// [`Error::with_status`] 补上），`retry_after` 直接来自响应头（可能为 `None`）。
    pub(crate) fn unexpected(
        message: impl Into<String>,
        status: impl Into<Option<u16>>,
        retry_after: Option<f64>,
    ) -> Self {
        let mut error = Self::new(ErrorKind::UnexpectedResponse, message);
        error.status = status.into();
        error.retry_after = retry_after;
        error
    }

    /// 服务端协议版本不兼容（capabilities 的 `protocol`）。
    pub(crate) fn incompatible_server(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::IncompatibleServer, message)
    }

    /// 调用参数不合法（本地判定，不发请求）。
    pub(crate) fn invalid_argument(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::InvalidArgument, message)
    }

    /// 传输异常透传（驱动方回送的原始错误，操作未处理）。
    pub(crate) fn transport(error: TransportError) -> Self {
        let mut wrapped = Self::new(ErrorKind::Transport, error.to_string());
        wrapped.details_mut().transport = Some(error);
        wrapped
    }

    /// 增量推送器的状态存储读写失败（[`ErrorKind::StateStore`]），原始错误经 `source()` 取回。
    /// 唯一调用方是 `http` 下的推送器驱动。
    #[cfg_attr(not(feature = "http"), allow(dead_code))]
    pub(crate) fn state_store(error: TransportError) -> Self {
        let mut wrapped = Self::new(ErrorKind::StateStore, format!("状态存储失败：{error}"));
        wrapped.details_mut().transport = Some(error);
        wrapped
    }

    /// force 提交的响应丢失、事后未能确认它已生效（core §5.2）；`cause` 是导致无法确认的
    /// 原始错误（`head` 的传输异常、`head` 得到的 DPE 错误，或 PUT 的响应丢失本身），
    /// 经 `source()` 取回。
    pub(crate) fn unconfirmed_force(
        message: impl Into<String>,
        doc_hash: impl Into<String>,
        current: Option<String>,
        checked: bool,
        cause: Error,
    ) -> Self {
        let mut error = Self::new(ErrorKind::ForceNotConfirmed, message);
        let details = error.details_mut();
        details.force = Some(ForceNotConfirmed {
            doc_hash: doc_hash.into(),
            current,
            checked,
        });
        details.cause = Some(Box::new(cause));
        error
    }

    /// 本地判定的 `DPE_MISSING_CONTENT`（`deliver` 缺少 blob 的字节来源）：错误码与清单照常
    /// 给出，但由客户端判定，MUST NOT 被 `deliver` 的恢复循环当作「服务端要求补传」再消费预算。
    pub(crate) fn local_missing_content(message: impl Into<String>, missing: Missing) -> Self {
        let mut error = Self::new(ErrorKind::MissingContent, message);
        error.details_mut().missing = Some(missing);
        error
    }

    /// 置位「本次写入是否可能已经生效」（写操作在失败时显式给出）。
    pub(crate) fn with_outcome_unknown(mut self, value: bool) -> Self {
        self.details_mut().outcome_unknown = Some(value);
        self
    }

    /// 附上当前暂存会话（`deliver` 失败时用）：上层可重新读取文档后以新的前置条件引用同一
    /// 会话重提，已上传到会话的内容无需重传（core §3.4）。无会话时也照样标记 `None`。
    pub(crate) fn with_session(mut self, session: Option<crate::protocol::Session>) -> Self {
        self.details_mut().session = session;
        self
    }

    /// 补上响应状态码（wire 层解析出的不合规响应由协议核心补 response.status）。
    pub(crate) fn with_status(mut self, status: u16) -> Self {
        self.status = Some(status);
        self
    }

    /// 由 RFC 9457 problem 体（HTTP 绑定 §5）构造协议错误，按 `code` 分派；`code` 缺失或
    /// 不是非空字符串（即不是 DPE 的 problem 体）时返回 `None`，由调用方按 §3.2 自行判定。
    ///
    /// - `retry_after`：响应 `Retry-After` 头换算成的秒数，传给任何码（含未知码）；
    /// - 已知码的 `retryable` 以本地码表（core §6）为准，不采信 problem 体声明的值；未知码
    ///   只采信为 `true` 的声明；
    /// - `DPE_MISSING_CONTENT` 的 `missing` / `missing_truncated` 解析进 [`Error::missing`]：
    ///   `missing` 缺省、形状不对或三层全空时降级为空清单并置 `missing_truncated`，错误码不丢；
    /// - 其余字段不合法都只降级，不失败。message 取第一个非空字符串的 `detail` / `title`。
    pub fn from_problem(problem: &Value, retry_after: Option<f64>) -> Option<Error> {
        let object = problem.as_object()?;
        let code = object
            .get("code")?
            .as_str()
            .filter(|code| !code.is_empty())?;
        let message = problem_message(object);
        let mut error = match ErrorKind::from_code(code) {
            Some(ErrorKind::MissingContent) => {
                let (missing, well_formed) = parse_missing(object.get("missing"));
                let mut error = Self::new(ErrorKind::MissingContent, message);
                let truncated =
                    object.get("missing_truncated") == Some(&Value::Bool(true)) || !well_formed;
                let details = error.details_mut();
                details.missing = Some(missing);
                details.missing_truncated = truncated;
                error
            }
            Some(kind) => Self::new(kind, message),
            None => {
                let mut error = Self::new(ErrorKind::Unknown, message);
                error.details_mut().unknown_code = Some(code.to_owned());
                if object.get("retryable") == Some(&Value::Bool(true)) {
                    error.retryable = true;
                }
                error
            }
        };
        error.retry_after = retry_after;
        Some(error)
    }

    /// 把 dpe-hash 的校验错误转换为同码的协议错误；`prefix` 是被校验对象在外层报文中的位置
    /// （RFC 6901），拼在原位置之前，如 `/objects/3`。
    pub fn from_hash_error(error: &dpe_hash::Error, prefix: &str) -> Error {
        let kind = match error.kind() {
            dpe_hash::ErrorKind::CategoryUnknown => ErrorKind::CategoryUnknown,
            dpe_hash::ErrorKind::ContractUnsupported => ErrorKind::ContractUnsupported,
            // 其余 dpe-hash 类别（Validation / UndefinedField / FileTypeInvalid /
            // InvalidUnicode / IntegerOutOfRange）的规范码都是 DPE_VALIDATION
            _ => ErrorKind::Validation,
        };
        let mut converted = Self::new(kind, error.message());
        converted.path = format!("{prefix}{}", error.path());
        converted
    }

    /// 错误类别（分派的首选依据）。
    pub fn kind(&self) -> ErrorKind {
        self.kind
    }

    /// 规范错误码：已知码的名、未知码的原码；客户端本地错误与传输错误为 `None`。
    pub fn code(&self) -> Option<&str> {
        if let Some(code) = self.kind.code() {
            return Some(code);
        }
        self.details
            .as_deref()
            .and_then(|details| details.unknown_code.as_deref())
    }

    /// 原样重试同一请求可能成功（core §6）。
    pub fn is_retryable(&self) -> bool {
        self.retryable
    }

    /// 可读消息（不含位置与状态码）。
    pub fn message(&self) -> &str {
        &self.message
    }

    /// 违例位置的 RFC 6901 JSON Pointer（core §2.8），相对于被校验的对象；不是校验错误时为空串。
    pub fn path(&self) -> &str {
        &self.path
    }

    /// `Retry-After` 换算成的秒数（`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE` 的提示；数值大到
    /// 无法表示时为 `f64::INFINITY`，见 [`crate::protocol::retry_after_seconds`]）。
    pub fn retry_after(&self) -> Option<f64> {
        self.retry_after
    }

    /// 收到的 HTTP 状态码：响应不合规（[`ErrorKind::UnexpectedResponse`]）时有值；不是响应
    /// 产生的错误（如本地判定、传输透传）为 `None`。
    pub fn status(&self) -> Option<u16> {
        self.status
    }

    /// `DPE_MISSING_CONTENT` 的缺失清单；不是该错误时为 `None`。
    pub fn missing(&self) -> Option<&Missing> {
        self.details
            .as_deref()
            .and_then(|details| details.missing.as_ref())
    }

    /// 缺失清单是否不可信（缺省、形状不对或三层全空，见 [`Error::from_problem`]）。
    pub fn missing_truncated(&self) -> bool {
        self.details
            .as_deref()
            .is_some_and(|details| details.missing_truncated)
    }

    /// force 提交无法确认时的细节；不是该错误时为 `None`。
    pub fn force_not_confirmed(&self) -> Option<&ForceNotConfirmed> {
        self.details
            .as_deref()
            .and_then(|details| details.force.as_ref())
    }

    /// 本次写入是否可能已经生效（写操作 `commit` / `delete` / `move_document` / `deliver`
    /// 失败时置位；**只针对写请求本身**，不含 `negotiate` / `upload_*` / `head` 的失败）。
    ///
    /// **只有三种结局算「确定」（不置真）**：带 DPE 错误码的响应；成功响应且核心的自洽校验
    /// 全部通过；带 [`NotSent`](crate::protocol::NotSent) 标记的传输错误（传输层确定请求没
    /// 发出）。其余一律为真——无 `NotSent` 的传输错误、任何**无码**响应（无码 4xx / 5xx、
    /// 2xx 但自洽校验或解析失败、没能化解的裸 412）。经 `head` 判定化解的裸 412 与
    /// `negotiate` / `upload_*` / `head` 阶段的失败不影响本值（后者在写请求未发出时为假）。
    /// **没有置位的错误（`None`）保守地返回真**——调用方对不确定的结果应先重新读取。
    pub fn outcome_unknown(&self) -> bool {
        self.details
            .as_deref()
            .and_then(|details| details.outcome_unknown)
            .unwrap_or(true)
    }

    /// 失败时附加的当前暂存会话（`deliver` 抛出的错误上有值）：上层可在重新读取文档后以新的
    /// 前置条件引用同一会话重提（core §3.4），已上传到会话的内容无需重传；会话只作缓存，失效
    /// 时重开 negotiate 即可，结果与不复用时一致。不是 `deliver` 的错误为 `None`。
    pub fn session(&self) -> Option<&crate::protocol::Session> {
        self.details
            .as_deref()
            .and_then(|details| details.session.as_ref())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // 与 Python 的 __str__ 对齐：本地错误带 HTTP 状态码，其余带（非空的）位置
        let text = if self.message.is_empty() {
            self.code().unwrap_or("DPE 错误")
        } else {
            self.message.as_str()
        };
        if let Some(status) = self.status {
            return write!(f, "{text}（HTTP {status}）");
        }
        if self.path.is_empty() {
            write!(f, "{text}")
        } else {
            write!(f, "{text}（位置 {}）", self.path)
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        let details = self.details.as_deref()?;
        if let Some(cause) = details.cause.as_deref() {
            return Some(cause);
        }
        details.transport.as_ref().map(|error| error.as_ref() as _)
    }
}

impl From<dpe_hash::Error> for Error {
    /// dpe-hash 的校验错误按同码协议错误上报（同 [`Error::from_hash_error`]，无位置前缀）。
    fn from(error: dpe_hash::Error) -> Self {
        Error::from_hash_error(&error, "")
    }
}

/// problem 体的 message：第一个非空字符串的 `detail` / `title`（RFC 9457 两者都是字符串）。
fn problem_message(object: &Map<String, Value>) -> String {
    for key in ["detail", "title"] {
        if let Some(text) = object.get(key).and_then(Value::as_str) {
            if !text.is_empty() {
                return text.to_owned();
            }
        }
    }
    String::new()
}

/// 解析 `DPE_MISSING_CONTENT` 的 `missing`，返回清单与它是否可信。
///
/// 规范要求该错误带缺失清单（core §3.3）：缺省、形状不对、三层全空都视为不可信（调用方
/// 不能据此认为「什么都不缺」）。清单只用于补传，不能因为它不合法就丢掉错误码。
fn parse_missing(raw: Option<&Value>) -> (Missing, bool) {
    let Some(object) = raw.and_then(Value::as_object) else {
        return (Missing::default(), false);
    };
    let mut missing = Missing::default();
    let layers: [(&str, &mut Vec<String>); 3] = [
        ("pages", &mut missing.pages),
        ("content_hashes", &mut missing.content_hashes),
        ("blobs", &mut missing.blobs),
    ];
    for (key, target) in layers {
        match object.get(key) {
            None => {}
            Some(value) => {
                let Some(items) = value.as_array() else {
                    return (Missing::default(), false);
                };
                for item in items {
                    let Some(text) = item.as_str() else {
                        return (Missing::default(), false);
                    };
                    target.push(text.to_owned());
                }
            }
        }
    }
    let well_formed = !missing.pages.is_empty()
        || !missing.content_hashes.is_empty()
        || !missing.blobs.is_empty();
    (missing, well_formed)
}
