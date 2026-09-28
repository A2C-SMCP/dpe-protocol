//! dpe-push/1 报文模型（TFRobotServer `docs/protocol/dpe/push-protocol-v1.md`）。
//!
//! 响应模型对未知字段 must-ignore（serde 默认忽略未知字段）。

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::hashing::DocumentHashes;
use crate::schema::{DocElement, Document};

pub const PROTOCOL_VERSION: &str = "1";
pub const MEDIA_TYPE: &str = "application/vnd.tfrs.dpe.v1+json";
pub const WELL_KNOWN_PATH: &str = "/.well-known/dpe-push";

// ---------------------------------------------------------------- Phase 0

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AcceptedHashStrategy {
    pub uri: String,
    #[serde(default = "stable")]
    pub status: String,
    #[serde(default)]
    pub deprecated_at: Option<String>,
}

fn stable() -> String {
    "stable".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Endpoints {
    #[serde(default = "default_negotiate")]
    pub negotiate: String,
    #[serde(default = "default_commit")]
    pub commit: String,
}

fn default_negotiate() -> String {
    "/v1/memory/dpe:negotiate".into()
}

fn default_commit() -> String {
    "/v1/memory/dpe:commit".into()
}

impl Default for Endpoints {
    fn default() -> Self {
        Self { negotiate: default_negotiate(), commit: default_commit() }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Capabilities {
    pub protocol_versions: Vec<String>,
    pub accepted_hash_strategies: Vec<AcceptedHashStrategy>,
    #[serde(default)]
    pub default_hash_strategy_uri: Option<String>,
    #[serde(default)]
    pub max_payload_bytes: Option<u64>,
    #[serde(default = "identity")]
    pub content_encodings: Vec<String>,
    #[serde(default = "yes")]
    pub delta_supported: bool,
    #[serde(default)]
    pub endpoints: Endpoints,
}

fn identity() -> Vec<String> {
    vec!["identity".into()]
}

fn yes() -> bool {
    true
}

// ---------------------------------------------------------------- Phase 1

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ManifestElement {
    pub content_hash: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ManifestPage {
    pub number: i64,
    pub title: Option<String>,
    pub page_hash: String,
    pub elements: Vec<ManifestElement>,
}

/// 完整文档的 hash 骨架。永远覆盖整个文档（禁止局部 manifest）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Manifest {
    pub hash_strategy_uri: String,
    pub file_uri: String,
    pub file_type: String,
    /// 全量声明字段（含 null）。若省略 `created_at`，内核会用 `now()` 填充，
    /// 导致服务端 `doc_hash` 每次都不同。
    pub doc_metadata: Value,
    pub doc_hash: String,
    pub base_doc_hash: Option<String>,
    pub pages: Vec<ManifestPage>,
}

impl Manifest {
    pub fn build(doc: &Document, hashes: &DocumentHashes, base_doc_hash: Option<String>) -> Self {
        Self {
            hash_strategy_uri: hashes.strategy.to_uri(),
            file_uri: doc.file_uri.to_string(),
            file_type: doc.file_type.as_str().to_string(),
            doc_metadata: doc.doc_metadata.canonical_value(),
            doc_hash: hashes.doc_hash.clone(),
            base_doc_hash,
            pages: hashes
                .pages
                .iter()
                .map(|p| ManifestPage {
                    number: p.number,
                    title: p.title.clone(),
                    page_hash: p.page_hash.clone(),
                    elements: p.content_hashes.iter().map(|h| ManifestElement { content_hash: h.clone() }).collect(),
                })
                .collect(),
        }
    }

    pub fn content_hashes(&self) -> impl Iterator<Item = &str> {
        self.pages.iter().flat_map(|p| p.elements.iter().map(|e| e.content_hash.as_str()))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NegotiateStatus {
    Unchanged,
    Gap,
    New,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NegotiateResponse {
    pub status: NegotiateStatus,
    #[serde(default)]
    pub missing_content_hashes: Vec<String>,
    #[serde(default)]
    pub server_doc_exists: bool,
    #[serde(default)]
    pub server_doc_hash: Option<String>,
    #[serde(default)]
    pub base_doc_hash_matched: Option<bool>,
}

// ---------------------------------------------------------------- Phase 2

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContentItem {
    pub content_hash: String,
    /// 完整 element payload，不含任何 hash / seq 字段
    pub element: Value,
}

impl ContentItem {
    pub fn build(content_hash: &str, ele: &DocElement) -> Self {
        let mut element = serde_json::to_value(ele).expect("DocElement is always representable as JSON");
        // 与 Python SDK 的 exclude_none 对齐：剔除 ele_metadata 中值为 null 的字段
        if let Some(Value::Object(meta)) = element.get_mut("ele_metadata") {
            meta.retain(|_, v| !v.is_null());
        }
        Self { content_hash: content_hash.to_string(), element }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommitRequest {
    pub manifest: Manifest,
    pub contents: Vec<ContentItem>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CommitStatus {
    Created,
    Unchanged,
    Updated,
    Rebuilt,
    Applied,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitCounts {
    #[serde(default)]
    pub elements_added: u64,
    #[serde(default)]
    pub elements_updated: u64,
    #[serde(default)]
    pub elements_removed: u64,
    #[serde(default)]
    pub elements_synced: u64,
    #[serde(default)]
    pub elements_unchanged: u64,
    #[serde(default)]
    pub llm_calls_saved: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommitResponse {
    pub status: CommitStatus,
    /// 服务端权威 doc_hash，下次投递作为 `base_doc_hash`
    pub doc_hash: String,
    pub hash_strategy_uri: String,
    #[serde(default)]
    pub counts: CommitCounts,
}

/// TFRS 既有错误信封 `{code, message, data}`。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    pub code: Value,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub data: Value,
}

impl ErrorEnvelope {
    pub fn code_string(&self) -> String {
        match &self.code {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        }
    }
}
