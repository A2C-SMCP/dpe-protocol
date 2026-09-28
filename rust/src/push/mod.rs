//! dpe-push/1：向 TFRobot Memory 增量投递 DPE 文档的客户端。

mod client;
pub mod models;
pub mod transport;

pub use client::{check_pushable, DpePushClient, PushOptions, PushResult, TokenProvider};
pub use models::{
    Capabilities, CommitCounts, CommitRequest, CommitResponse, CommitStatus, ContentItem, Manifest, NegotiateResponse,
    NegotiateStatus, MEDIA_TYPE, PROTOCOL_VERSION,
};
#[cfg(feature = "reqwest")]
pub use transport::ReqwestTransport;
pub use transport::{HttpRequest, HttpResponse, Method, Transport};
