//! 本 crate 的统一错误类型。

use serde_json::Value;

/// dpe-push/1 协议错误码（push-protocol-v1 §7）。
pub mod codes {
    pub const MANIFEST_INVALID: &str = "DPE_MANIFEST_INVALID";
    pub const CONTENT_HASH_MISMATCH: &str = "DPE_CONTENT_HASH_MISMATCH";
    pub const HASH_STRATEGY_UNSUPPORTED: &str = "DPE_HASH_STRATEGY_UNSUPPORTED";
    pub const INSUFFICIENT_CONTENT: &str = "DPE_INSUFFICIENT_CONTENT";
    pub const PAYLOAD_TOO_LARGE: &str = "DPE_PAYLOAD_TOO_LARGE";
    pub const UNSUPPORTED_PROTOCOL_VERSION: &str = "DPE_UNSUPPORTED_PROTOCOL_VERSION";
    pub const RATE_LIMITED: &str = "DPE_RATE_LIMITED";
    pub const ROBOT_UNAVAILABLE: &str = "DPE_ROBOT_UNAVAILABLE";
}

/// 服务端返回的协议错误。`code` 可能是协议外的值（如网关错误 `HTTP_502`），故保留原始字符串。
#[derive(Debug, Clone, PartialEq)]
pub struct PushError {
    pub code: String,
    pub message: String,
    pub http_status: u16,
    pub data: Value,
    pub retry_after: Option<f64>,
}

impl PushError {
    pub fn retryable(&self) -> bool {
        self.code == codes::RATE_LIMITED || self.code == codes::ROBOT_UNAVAILABLE
    }
}

#[derive(Debug, thiserror::Error)]
pub enum DpeError {
    #[error("[{} {}] {}", .0.http_status, .0.code, .0.message)]
    Push(PushError),

    /// 投递前的本地校验失败：文档不满足 dpe-push/1 的约束，发出去也会被拒绝。
    #[error("validation failed: {}", .0.join("; "))]
    Validation(Vec<String>),

    /// SDK 支持的 hash 策略与服务端 allowlist 无交集。
    #[error("no compatible hash strategy: {0}")]
    NoCompatibleHashStrategy(String),

    /// SDK 未实现该 hash 策略 / 算法版本。
    #[error("unsupported hash strategy: {0}")]
    UnsupportedHashStrategy(String),

    /// 服务端响应不符合协议（格式错误、版本不支持、请求了文档中不存在的内容等）。
    #[error("protocol error: {0}")]
    Protocol(String),

    #[error("transport error: {0}")]
    Transport(String),

    #[error("invalid uri: {0}")]
    InvalidUri(String),

    #[error(transparent)]
    Json(#[from] serde_json::Error),

    #[error(transparent)]
    Io(#[from] std::io::Error),
}

impl DpeError {
    pub fn push_error(&self) -> Option<&PushError> {
        match self {
            Self::Push(err) => Some(err),
            _ => None,
        }
    }
}

pub type Result<T, E = DpeError> = std::result::Result<T, E>;
