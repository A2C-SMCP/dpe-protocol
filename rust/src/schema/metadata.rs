//! DPE 元数据：镜像内核 `TFDocMetadata` / `TFElementMetadata`。
//!
//! `DocMetadata` 整体进入 `doc_hash`（内核对 `model_dump(mode="json")` 做稳定序列化），
//! 因此**声明字段集**与序列化方式必须与内核逐字节一致：未赋值的声明字段以 `null` 出现，
//! 扩展字段原样出现。唯一有意的偏离与 Python SDK 相同：`created_at` 默认 `None` 而非 `now()`。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use url::Url;

use super::datetime::PyDateTime;

/// Document 级元数据（进入 `doc_hash`）。序列化时**不跳过** `None`，与内核规范化形式一致。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DocMetadata {
    #[serde(default)]
    pub created_at: Option<PyDateTime>,
    #[serde(default)]
    pub last_modified: Option<PyDateTime>,
    #[serde(default)]
    pub forward_citation_uris: Option<Vec<Url>>,
    #[serde(default)]
    pub backward_citation_uris: Option<Vec<Url>>,
    #[serde(default)]
    pub languages: Option<Vec<String>>,
    #[serde(default)]
    pub filename: Option<String>,
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub global_dict: Option<BTreeMap<String, String>>,
    /// 扩展字段，同样进入 hash
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl DocMetadata {
    /// 规范化 JSON 值：全部声明字段（含 null）+ 扩展字段。
    pub fn canonical_value(&self) -> Value {
        serde_json::to_value(self).expect("DocMetadata is always representable as JSON")
    }
}

/// Element 级元数据。
///
/// 只有 `text_as_html`（Table / Formula）与四个图片字段（Image）进入 `content_hash`，
/// 其余字段仅随内容投递。未声明的字段经 `extra` 原样透传，由内核负责校验。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ElementMetadata {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text_as_html: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_base64: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_mime_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub category_depth: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub filename: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page_number: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub languages: Option<Vec<String>>,
    /// 其余 `TFElementMetadata` 字段与扩展字段（`null` 值在投递时剔除）
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}
