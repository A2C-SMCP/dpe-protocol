//! DPE 数据模型。

mod datetime;
mod document;
mod metadata;
mod types;

pub use datetime::PyDateTime;
pub use document::{DocElement, DocPage, Document};
pub use metadata::{DocMetadata, ElementMetadata};
pub use types::{ElementCategory, FileType};
