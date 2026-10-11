//! 凭证注入（HTTP 绑定 §6）：SDK 通过可插拔的 [`CredentialProvider`] 取出 `Authorization`
//! 头的值，不感知鉴权方案——方案与凭证形式完全归 provider 的实现方（运行器、connector 的
//! 使用者与测试），SDK 侧只有「一个头部值」。头字段与错误映射保持中性：不出现具体产品名与
//! 私有头（与 Python 侧 `dpe_sdk.credentials` 对等）。
//!
//! provider 在**每个请求发送前**调用一次（同一客户端可据此轮换凭证）；传输层自行重发的请求
//! （网络失败后的重连、按 `Retry-After` 等待后的重试）同样算一次发送，会重新取凭证——轮换中
//! 的短时凭证在长时间等待后仍可用。
//!
//! 实现必须快速、非阻塞：`DpeClient` 的调用发生在驱动它的异步任务线程上；需要在网络处刷新
//! 凭证的实现应自行预取或缓存（同 Python 侧的约定）。trait 要求 `Send + Sync`：客户端会被
//! 跨任务共享，凭证取值可能从任一任务线程上发起。

/// 按请求给出 `Authorization` 头的值（HTTP 绑定 §6）。
pub trait CredentialProvider: Send + Sync {
    /// `Authorization` 头的完整值（含方案名，如 `"Bearer <token>"`）；`None` 表示本次请求不带
    /// 凭证。
    fn authorization(&self) -> Option<String>;
}

/// 固定凭证的简单实现（运行器从环境注入、脚本与测试使用）。
#[derive(Debug, Clone)]
pub struct StaticCredentialProvider {
    value: String,
}

impl StaticCredentialProvider {
    /// 以给定的头部值（含方案名）构造。
    pub fn new(value: impl Into<String>) -> Self {
        StaticCredentialProvider {
            value: value.into(),
        }
    }

    /// 头部值（含方案名）。
    pub fn value(&self) -> &str {
        &self.value
    }
}

impl CredentialProvider for StaticCredentialProvider {
    fn authorization(&self) -> Option<String> {
        Some(self.value.clone())
    }
}
