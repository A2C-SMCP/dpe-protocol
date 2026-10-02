//! dpe-hash：DPE hash 契约 1（`dpe1:`）的独立 hash 核心。
//!
//! 不绑定异步运行时、不含任何 I/O；对齐对象是 `spec/hash-contract-1.md`，
//! 一致性由仓库根目录 `vectors/` 的向量逐字节校验。
//! 当前为工程骨架：hash 入口与契约常量随 #19 落地，本版本只导出 [`VERSION`]。
//!
//! 消费方注意：喂给 hash 的 JSON 若含浮点，解析必须正确舍入——本 crate 已为
//! `serde_json` 开启 `float_roundtrip`（见 `sdk/README.md`）。

#![forbid(unsafe_code)]

/// 本 crate 的版本，与 `dpe-sdk` 同版本（两个 crate 由 workspace 统一管理）。
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
