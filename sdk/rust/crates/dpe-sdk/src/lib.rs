//! dpe-sdk：DPE 协议 Rust SDK。
//!
//! 依赖 `dpe-hash` 计算 hash；承载 sans-IO 协议核心、传输适配、增量推送（#20–#25）与
//! dpe-run 的运行器侧协议核心（`run`，connector 契约：清单、实例配置与实例定义校验，
//! §4.1–§4.4）。协议核心不绑定异步运行时：默认 feature `http` 才引入 reqwest / tokio 适配，
//! `--no-default-features` 下核心可独立编译（CI 有断言）。
//!
//! 已落地：[`models`]（三层对象的数据模型，core §2）、[`protocol`]（sans-IO 协议核心全部：
//! 只读操作、写操作（commit / delete / move）与暂存路径（negotiate / upload / deliver）、
//! CAS 前置条件与重试规则）、[`errors`]、[`wire`]、[`run`]（connector 契约的清单、实例
//! 配置与实例定义校验，§4.1–§4.4）与 [`http`]（reqwest / tokio 传输适配：异步客户端
//! [`http::DpeClient`]、[`credentials::CredentialProvider`]）与 [`pusher`]（增量推送器：
//! 同步状态与意图日志只作缓存，connector 契约 §7.2 的判定表与按操作回报；判定逻辑无 feature
//! 门控，异步驱动 `pusher::StatefulPusher` 随 `http`）与 [`media`]（媒体上传辅助：路径 / 可 seek
//! 的流 / connector `read_blob` 回调 → `BlobSource`，只做来源适配，只用 std I/O）与
//! [`remote_url`]（HTTP 绑定 §1 的 remote URL 文法，传输层与实例定义共用）。

#![forbid(unsafe_code)]

#[cfg(feature = "http")]
pub mod credentials;
#[cfg(feature = "http")]
pub mod http;
pub mod models;
#[cfg(feature = "http")]
mod proxy;

pub use models::{DocumentObject, ElementObject, ExpandedDocument, ExpandedPage, PageObject};

pub mod errors;
pub mod media;
pub mod protocol;
pub mod pusher;
pub mod remote_url;
pub mod wire;

pub use errors::{Error, ErrorKind};
pub use protocol::{
    drive, error_from_response, fetch_capabilities, local_request_error, not_sent,
    LocalRequestError, Operation, ProtocolCore, Request, Response, Step, TransportError,
};

pub mod run;

// 契约常量随模型一起导出：模型的每个入口都要显式传 contract，用户无需为此再依赖 dpe-hash
pub use dpe_hash::{CONTRACT, DRILL_CONTRACT};

/// 本 crate 的版本，与 `dpe-hash` 同版本（两个 crate 由 workspace 统一管理）。
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
