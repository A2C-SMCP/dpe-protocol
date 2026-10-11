//! dpe-sdk：DPE 协议 Rust SDK。
//!
//! 依赖 `dpe-hash` 计算 hash；承载 sans-IO 协议核心、传输适配、增量推送（#20–#25）与
//! dpe-run 的运行器侧协议核心（`run`，connector 契约：清单、实例配置与实例定义校验，
//! §4.1–§4.4）。协议核心不绑定异步运行时：默认 feature `http` 才引入 reqwest / tokio 适配，
//! `--no-default-features` 下核心可独立编译（CI 有断言）。
//!
//! 已落地：[`models`]（三层对象的数据模型，core §2）、[`protocol`]（sans-IO 协议核心：只读
//! 操作与写操作快路径——commit / delete / move、CAS 前置条件与重试规则）、[`errors`]、
//! [`wire`] 与 [`run`]（connector 契约的清单、实例配置与实例定义校验，§4.1–§4.4）；暂存
//! 路径与逐层协商（negotiate / upload / deliver）随 Milestone `rust-sdk v0.2` 落地。

#![forbid(unsafe_code)]

#[cfg(feature = "http")]
pub mod http;
pub mod models;

pub use models::{DocumentObject, ElementObject, ExpandedDocument, ExpandedPage, PageObject};

pub mod errors;
pub mod protocol;
pub mod wire;

pub use errors::{Error, ErrorKind};
pub use protocol::{
    drive, fetch_capabilities, Operation, ProtocolCore, Request, Response, Step, TransportError,
};

pub mod run;

// 契约常量随模型一起导出：模型的每个入口都要显式传 contract，用户无需为此再依赖 dpe-hash
pub use dpe_hash::{CONTRACT, DRILL_CONTRACT};

/// 本 crate 的版本，与 `dpe-hash` 同版本（两个 crate 由 workspace 统一管理）。
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
