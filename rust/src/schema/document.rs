//! DPE 三层结构（源端视角）：不含任何 hash 字段与数据库主键。
//!
//! hash 由 [`crate::hashing`] 按需计算，只出现在 manifest 中（dpe-push/1 §5「第 0 条校验规则」）。
//! `DocElement` 是单一类型，hash 多态由 hashing 层按 `category` 分派。

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use url::Url;

use super::metadata::{DocMetadata, ElementMetadata};
use super::types::{ElementCategory, FileType};
use crate::error::DpeError;

fn new_element_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// 最小内容单元。在页内的位置由数组顺序表达（数组顺序即阅读顺序）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DocElement {
    #[serde(default)]
    pub category: ElementCategory,
    #[serde(default)]
    pub text: String,
    #[serde(default = "new_element_id")]
    pub element_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keywords: Option<Vec<String>>,
    #[serde(default)]
    pub ele_metadata: ElementMetadata,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload: Option<Map<String, Value>>,
}

impl DocElement {
    pub fn new(category: ElementCategory, text: impl Into<String>) -> Self {
        Self {
            category,
            text: text.into(),
            element_id: new_element_id(),
            keywords: None,
            ele_metadata: ElementMetadata::default(),
            payload: None,
        }
    }

    pub fn with_metadata(mut self, ele_metadata: ElementMetadata) -> Self {
        self.ele_metadata = ele_metadata;
        self
    }
}

/// 页。`number` 是页身份：内核按 `number` 配对新旧页，数据源必须保证其稳定。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DocPage {
    pub number: i64,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keywords: Option<Vec<String>>,
    #[serde(default)]
    pub elements: Vec<DocElement>,
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub page_metadata: Map<String, Value>,
}

impl DocPage {
    pub fn new(number: i64, title: Option<String>, elements: Vec<DocElement>) -> Self {
        Self { number, title, keywords: None, elements, page_metadata: Map::new() }
    }
}

/// 文档。`file_uri` 是 Robot Memory 中的唯一键（PG UNIQUE）。
///
/// `file_uri` 使用 `url::Url`：pydantic 的 `AnyUrl` 底层同样是 `url` crate，规范化结果一致。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub file_uri: Url,
    pub file_type: FileType,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keywords: Option<Vec<String>>,
    #[serde(default)]
    pub pages: Vec<DocPage>,
    #[serde(default)]
    pub doc_metadata: DocMetadata,
}

impl Document {
    pub fn new(file_uri: Url, file_type: FileType, pages: Vec<DocPage>) -> Self {
        Self { file_uri, file_type, keywords: None, pages, doc_metadata: DocMetadata::default() }
    }

    /// 从 JSON 构造并校验（页编号唯一）。
    pub fn from_json(value: Value) -> Result<Self, DpeError> {
        let doc: Self = serde_json::from_value(value)?;
        doc.validate()?;
        Ok(doc)
    }

    /// 从内核 `Document` 的 dump（或上游解析服务按内核格式的产出）构造，剔除内核独有字段。
    pub fn from_kernel_dump(mut data: Value) -> Result<Self, DpeError> {
        const DOCUMENT_ONLY: &[&str] = &[
            "doc_id",
            "entrance_page_id",
            "doc_hash",
            "hash_strategy_uri",
            "source_file_hash",
            "creator_id",
            "group_id",
        ];
        const PAGE_ONLY: &[&str] = &["page_id", "doc_id", "entrance_ele_id", "page_hash"];
        const ELEMENT_ONLY: &[&str] = &["ele_id", "page_id", "seq_in_page", "content_hash"];
        fn strip(value: &mut Value, keys: &[&str]) {
            if let Some(obj) = value.as_object_mut() {
                obj.retain(|k, _| !keys.contains(&k.as_str()));
            }
        }
        strip(&mut data, DOCUMENT_ONLY);
        if let Some(pages) = data.get_mut("pages").and_then(Value::as_array_mut) {
            for page in pages {
                strip(page, PAGE_ONLY);
                if let Some(elements) = page.get_mut("elements").and_then(Value::as_array_mut) {
                    elements.iter_mut().for_each(|e| strip(e, ELEMENT_ONLY));
                }
            }
        }
        Self::from_json(data)
    }

    /// 页编号必须在文档内唯一。
    pub fn validate(&self) -> Result<(), DpeError> {
        let mut numbers: Vec<i64> = self.pages.iter().map(|p| p.number).collect();
        numbers.sort_unstable();
        if numbers.windows(2).any(|w| w[0] == w[1]) {
            return Err(DpeError::Validation(vec![format!(
                "page.number must be unique within a document, got {numbers:?}"
            )]));
        }
        Ok(())
    }

    /// 按页、按阅读顺序展开全部 element。
    pub fn iter_elements(&self) -> impl Iterator<Item = (&DocPage, &DocElement)> {
        self.pages.iter().flat_map(|p| p.elements.iter().map(move |e| (p, e)))
    }
}
