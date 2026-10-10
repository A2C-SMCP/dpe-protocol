//! dpe-hash：DPE hash 契约的独立 hash 核心。
//!
//! 不绑定异步运行时、不含任何 I/O；实现 hash 契约 1（`spec/hash-contract-1.md`，`dpe1:`）与
//! file_uri 语法规范化（`spec/core.md` §1.1），并导出契约常量、三层对象的类型化结构与带规范
//! 错误码的错误类型。一致性由仓库根目录 `vectors/` 的向量逐字节校验，与 Python dpe-hash
//! 行为对等。
//!
//! ```
//! use dpe_hash::{content_hash, doc_hash, page_hash, PageFields, DocumentFields, CONTRACT};
//! use serde_json::json;
//!
//! let element = json!({"category": "Title", "text": "季度报告"});
//! let element_hash = content_hash(&element, CONTRACT)?;
//! let page = page_hash(&PageFields::default(), &[&element_hash], CONTRACT)?;
//! let document = DocumentFields { file_type: "pdf".into(), ..Default::default() };
//! assert!(doc_hash(&document, &[page], CONTRACT)?.starts_with("dpe1:"));
//! # Ok::<(), dpe_hash::Error>(())
//! ```
//!
//! # I-JSON 与数值（core §2.8）
//!
//! - 第 0 步 I-JSON：用 [`parse_ijson`] 严格解析（自建单遍解析器，拒绝重复键与孤立代理项；
//!   不复用 serde_json 的 `Value` 解析，理由见 [`parse_ijson`] 的模块文档）；serde_json 默认
//!   保留重复键的最后一个，直接 `from_str` 的调用方须自行改用 [`parse_ijson`]。
//! - 本 crate 为 serde_json 开启 `float_roundtrip`（浮点正确舍入）与 `arbitrary_precision`
//!   （`1e400` 这类越界数值保留到第 4 步再拒绝）。后者在下游统一生效：同一构建中的
//!   `#[serde(flatten)]` 与 untagged enum 遇到数字会反序列化失败，消费方的模型应避开这两种写法。
//! - **在 Rust 中构造的 NaN / Infinity 不会被拒绝，而是被当作缺省**：`serde_json::Value`
//!   无法承载非有限数，`json!` / `Value::from(f64)` / `serde_json::to_value` 会把它们静默
//!   转成 `null`，而 metadata 中值为 null 的键在规范化时被删除（契约 1 §3.2）。因此
//!   `{"x": f64::NAN}` 与 `{}` 得到相同的 hash。Python dpe-hash 对同样的输入报
//!   `DPE_VALIDATION`。用计算得到的浮点（如版面坐标）构造 metadata 时，调用方 MUST 先自行
//!   确认它是有限数（`f64::is_finite`）。

#![forbid(unsafe_code)]

mod constants;
mod contract1;
mod error;
mod ijson;
mod jcs;
mod models;
mod uri;

pub use constants::{
    content_fields, CATEGORY_CONTENT_FIELDS, CONTRACT, DRILL_CONTRACT, FILE_TYPES, KNOWN_CONTRACTS,
    SUPPORTED_CONTRACTS,
};
#[doc(hidden)]
pub use contract1::__private;
pub use contract1::{
    blob_ref, children, content_hash, doc_hash, document_hashes, object_hash, page_hash,
    page_hashes, parse_blob_ref, parse_hash, parse_hash_with,
};
pub use error::{Error, ErrorKind, Result};
pub use ijson::parse_ijson;
pub use jcs::jcs;
pub use models::{
    DocumentFields, DocumentHashes, DocumentObject, ElementObject, ExpandedDocument, ExpandedPage,
    JsonObject, ObjectKind, PageFields, PageHashes, PageObject, ToJson, UnknownObjectKind,
};
pub use uri::normalize_file_uri;

/// 本 crate 的版本，与 `dpe-sdk` 同版本（两个 crate 由 workspace 统一管理）。
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
