//! dpe-hash：DPE hash 契约 1 的独立 hash 核心（M1 原型）。
//!
//! 不绑定异步运行时、不含任何 I/O；对齐对象是 spec/hash-contract-1.md，
//! 一致性由 vectors/ 逐字节校验。

mod contract1;
mod jcs;

pub use contract1::{content_hash, document_hashes, page_hash, CONTRACT, RESERVED_METADATA_KEYS};
pub use jcs::jcs;
