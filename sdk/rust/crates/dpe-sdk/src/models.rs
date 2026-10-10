//! 三层对象的数据模型（core §2）：SDK 公共 API 的输入输出类型。
//!
//! - 线上对象：[`ElementObject`]、[`PageObject`]（`elements` 为 content_hash 列表）、
//!   [`DocumentObject`]（`pages` 为 page_hash 列表）；
//! - 展开视图（vectors/README.md）：[`ExpandedPage`]、[`ExpandedDocument`]，页与元素内联给出。
//!
//! 与 dpe-hash 的同名结构（只保证字段名、不做校验的 hash 输入结构）不同，本模块的模型带规范
//! 校验与「原样表示」语义，是 SDK 对外的主类型。
//!
//! # 校验只有一份实现
//!
//! [`parse`](ElementObject::parse) / [`from_value`](ElementObject::from_value) 在原始输入上调用
//! dpe-hash，按 core §2.8 的顺序校验整个对象（展开视图含全部子对象）；失败返回
//! [`dpe_hash::Error`]，`code()` 与 `path()` 同一致性向量，`path()` 相对于被构造的对象。
//! category 允许的字段、file_type 枚举都取自 dpe-hash，SDK 不维护副本。
//!
//! serde 反序列化（`serde_json::from_str::<ElementObject>` 等）只做结构层解析（拒绝未知字段），
//! **不做** core §2.8 校验，错误也不带规范错误码——需要规范错误码与位置时用 `parse` /
//! `from_value`。结构体字面量构造不被阻止；hash 方法每次都经 dpe-hash 重新校验，字面量构造
//! 的非法对象在算 hash 时同样被拒绝（hash 不缓存）。
//!
//! # 表示与等价
//!
//! 模型保留输入的原样表示（core §2.7：读回 SHOULD 返回原样）：可选字段是三态的
//! `Option<Option<T>>`——`None` 是缺省（序列化时省略）、`Some(None)` 是显式 null（序列化时输出
//! `null`）、`Some(Some(v))` 是值。null 与缺省、`page_metadata`/`doc_metadata`/`metadata`
//! 缺省与 `{}` 等内容等价由 hash 判定（契约 1 §3.2），模型的 `==` 只比较结构。
//!
//! # contract
//!
//! 子 hash 列表按显式传入的 `contract` 校验（通常为 `dpe_hash::CONTRACT`；`dpe2` 只在显式
//! 传入时接受，用于契约升级演练）。
//!
//! ```
//! use dpe_hash::CONTRACT;
//! use dpe_sdk::{DocumentObject, ElementObject, ExpandedDocument, PageObject};
//! use serde_json::json;
//!
//! // 逐层上溯：元素 → 页 → 文档
//! let element =
//!     ElementObject::from_value(&json!({"category": "Title", "text": "季度报告"}), CONTRACT)?;
//! let element_hash = element.content_hash(CONTRACT)?;
//! let page = PageObject::from_value(&json!({"elements": [element_hash.clone()]}), CONTRACT)?;
//! let page_hash = page.page_hash(CONTRACT)?;
//! let document = DocumentObject::from_value(
//!     &json!({"file_type": "pdf", "pages": [page_hash]}),
//!     CONTRACT,
//! )?;
//! assert!(document.doc_hash(CONTRACT)?.starts_with("dpe1:"));
//!
//! // 展开视图一次算出三层，与逐层上溯结果一致
//! let expanded = ExpandedDocument::from_value(
//!     &json!({
//!         "file_type": "pdf",
//!         "pages": [{"elements": [{"category": "Title", "text": "季度报告"}]}],
//!     }),
//!     CONTRACT,
//! )?;
//! assert_eq!(expanded.hashes(CONTRACT)?.pages[0].elements[0], element_hash);
//! # Ok::<(), dpe_hash::Error>(())
//! ```

use std::borrow::Cow;

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

use dpe_hash::{
    document_hashes, object_hash, page_hashes, parse_ijson, DocumentHashes, JsonObject, ObjectKind,
    Result, ToJson,
};

/// 三态可选字段的反序列化：字段存在时必定调用（外层 `Some` 包裹），`null` 反序列化为
/// `None`，因此 `Some(None)` 就是显式 null；字段缺失走 `#[serde(default)]`，得到外层 `None`。
fn double_option<'de, T, D>(deserializer: D) -> std::result::Result<Option<Option<T>>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    Deserialize::deserialize(deserializer).map(Some)
}

/// 元素对象（core §2.3）。内容字段是否允许取决于 `category`（契约 1 §4.1）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ElementObject {
    pub category: String,
    /// 显式 null 保留为 `Some(None)`，缺省为 `None`（下同）。
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<Option<String>>,
    /// 仅 `Table` / `Formula`。
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text_as_html: Option<Option<String>>,
    /// 仅允许携带 blob 的 category（目前为 `Image`）；blob 引用 `"sha256:…"`。
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blob: Option<Option<String>>,
    /// blob 字节的媒体类型（如 `image/png`）。
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<Option<String>>,
    /// 版面坐标、图片 url 等源提供的信息，缺省视同 `{}`。
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Option<JsonObject>>,
}

/// 页对象（core §2.2）：`elements` 为 content_hash 列表，数组顺序即页内阅读顺序。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PageObject {
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<Option<JsonObject>>,
    pub elements: Vec<String>,
}

/// 文档对象（core §2.1）：`pages` 为 page_hash 列表，数组顺序即页的阅读顺序。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DocumentObject {
    /// 封闭枚举；取值见 `dpe_hash::FILE_TYPES`。
    pub file_type: String,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<Option<JsonObject>>,
    pub pages: Vec<String>,
}

/// 展开视图中的页：`elements` 为元素对象本身（vectors/README.md）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExpandedPage {
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<Option<JsonObject>>,
    pub elements: Vec<ElementObject>,
}

/// 展开视图中的文档：页与元素内联给出，数组顺序即阅读顺序（vectors/README.md）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExpandedDocument {
    pub file_type: String,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(default, deserialize_with = "double_option")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<Option<JsonObject>>,
    pub pages: Vec<ExpandedPage>,
}

impl ElementObject {
    /// 从 JSON 文本构造：按 I-JSON 严格解析（core §2.8 第 0 步，拒绝重复键与孤立代理项），
    /// 再经 `content_hash` 校验；不是合法 JSON 或校验失败时返回 [`dpe_hash::Error`]。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `content_hash` 校验后转换；失败返回 [`dpe_hash::Error`]
    /// （`code()` / `path()` 同一致性向量）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        dpe_hash::content_hash(value, contract)?;
        Ok(Self::deserialize(value).expect("已通过 dpe-hash 校验，反序列化不会失败"))
    }

    /// 元素对象的 `content_hash`（契约 1 §4）：每次调用都经 dpe-hash 重新校验并计算，不缓存。
    pub fn content_hash(&self, contract: &str) -> Result<String> {
        dpe_hash::content_hash(self, contract)
    }
}

impl PageObject {
    /// 从 JSON 文本构造：I-JSON 严格解析后经 `page_hash` 校验（core §2.8）。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `page_hash` 校验后转换（`elements` 为 content_hash 列表，
    /// 按 `contract` 校验）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        object_hash(value, ObjectKind::Page, contract)?;
        Ok(Self::deserialize(value).expect("已通过 dpe-hash 校验，反序列化不会失败"))
    }

    /// 页对象的 `page_hash`（契约 1 §5）。
    pub fn page_hash(&self, contract: &str) -> Result<String> {
        object_hash(self, ObjectKind::Page, contract)
    }
}

impl DocumentObject {
    /// 从 JSON 文本构造：I-JSON 严格解析后经 `doc_hash` 校验（core §2.8）。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `doc_hash` 校验后转换（`pages` 为 page_hash 列表，
    /// 按 `contract` 校验）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        object_hash(value, ObjectKind::Document, contract)?;
        Ok(Self::deserialize(value).expect("已通过 dpe-hash 校验，反序列化不会失败"))
    }

    /// 文档对象的 `doc_hash`（契约 1 §5）。
    pub fn doc_hash(&self, contract: &str) -> Result<String> {
        object_hash(self, ObjectKind::Document, contract)
    }
}

impl ExpandedPage {
    /// 从 JSON 文本构造：I-JSON 严格解析后经 `page_hashes` 校验（core §2.8，含全部元素）。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `page_hashes` 校验后转换；错误位置相对于页自身。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        page_hashes(value, contract)?;
        Ok(Self::deserialize(value).expect("已通过 dpe-hash 校验，反序列化不会失败"))
    }
}

impl ExpandedDocument {
    /// 从 JSON 文本构造：I-JSON 严格解析后经 `document_hashes` 校验（core §2.8，含全部子对象）。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `document_hashes` 校验后转换（校验顺序：文档字段 → 各页
    /// 字段 → 各页的元素）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        document_hashes(value, contract)?;
        Ok(Self::deserialize(value).expect("已通过 dpe-hash 校验，反序列化不会失败"))
    }

    /// 三层 hash，形如 `{doc_hash, pages: [{page_hash, elements}…]}`；形状同向量的 `expected`。
    pub fn hashes(&self, contract: &str) -> Result<DocumentHashes> {
        document_hashes(self, contract)
    }

    /// 展开文档的 `doc_hash`；需要三层 hash 时用 [`hashes`](Self::hashes)。
    pub fn doc_hash(&self, contract: &str) -> Result<String> {
        self.hashes(contract).map(|hashes| hashes.doc_hash)
    }
}

/// 模型转 JSON 值供 dpe-hash 校验与计算：序列化输出保留原样表示（含显式 null）。
macro_rules! model_to_json {
    ($($ty:ty),*) => {$(
        impl ToJson for $ty {
            fn to_json(&self) -> Cow<'_, Value> {
                // 字段只有 String / Option / Vec / JSON 对象，序列化为 Value 不会失败
                Cow::Owned(serde_json::to_value(self).expect("数据模型总能序列化为 JSON 值"))
            }
        }
    )*};
}

model_to_json!(
    ElementObject,
    PageObject,
    DocumentObject,
    ExpandedPage,
    ExpandedDocument
);
