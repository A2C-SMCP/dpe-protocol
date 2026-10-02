//! dpe-sdk：DPE 协议 Rust SDK。
//!
//! 依赖 `dpe-hash` 计算 hash；承载 sans-IO 协议核心、传输适配与增量推送（#20–#25）。
//! 协议核心不绑定异步运行时：默认 feature `http` 才引入 reqwest / tokio 适配，
//! `--no-default-features` 下核心可独立编译（CI 有断言）。
//! 当前为工程骨架，本版本只导出 [`VERSION`]。

#![forbid(unsafe_code)]

#[cfg(feature = "http")]
pub mod http;

/// 本 crate 的版本，与 `dpe-hash` 同版本（两个 crate 由 workspace 统一管理）。
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
