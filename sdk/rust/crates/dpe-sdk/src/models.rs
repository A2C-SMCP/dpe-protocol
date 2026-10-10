//! 三层对象的数据模型（core §2）：SDK 公共 API 的输入输出类型。
//!
//! - 线上对象：[`ElementObject`]、[`PageObject`]（`elements` 为 content_hash 列表）、
//!   [`DocumentObject`]（`pages` 为 page_hash 列表）；
//! - 展开视图（vectors/README.md）：[`ExpandedPage`]、[`ExpandedDocument`]，页与元素内联给出。
//!
//! 与 dpe-hash 的同名结构（只保证字段名、不做校验的 hash 输入结构）不同，本模块的模型带规范
//! 校验与「原样表示」语义，是 SDK 对外的主类型。
//!
//! # 构造入口
//!
//! [`parse`](ElementObject::parse)（JSON 文本，I-JSON 严格解析）与
//! [`from_value`](ElementObject::from_value)（已解析的值）是唯一的反序列化入口：都在原始输入
//! 上调用 dpe-hash，按 core §2.8 的顺序校验整个对象（展开视图含全部子对象）；失败返回
//! [`dpe_hash::Error`]，`code()` 与 `path()` 同一致性向量，`path()` 相对于被构造的对象。
//! category 允许的字段、file_type 枚举都取自 dpe-hash，SDK 不维护副本。校验通过后直接提取
//! 字段（只做搬运与克隆），不经 serde 的 `Value` 反序列化——`arbitrary_precision` 下它会
//! 把「首键为内部数字 token `$serde_json::private::Number` 的对象」改写为数字或直接报错，
//! 而源数据里同形的对象是内容（P2：不设保留键、不做过滤）。因此模型只 derive `Serialize`
//! （序列化输出原样表示），**不提供** serde 反序列化。
//!
//! 结构体字面量构造不被阻止；hash 方法每次都经 dpe-hash 重新校验，字面量构造的非法对象在
//! 算 hash 时同样被拒绝（hash 不缓存）。
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
//! 子 hash 列表按显式传入的 `contract` 校验（通常为 [`CONTRACT`](crate::CONTRACT)；`dpe2`
//! 只在显式传入时接受，用于契约升级演练）。
//!
//! ```
//! use dpe_sdk::{DocumentObject, ElementObject, ExpandedDocument, PageObject, CONTRACT};
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

use serde::Serialize;
use serde_json::{Map, Value};

use dpe_hash::{
    document_hashes, object_hash, page_hashes, parse_ijson, DocumentHashes, JsonObject, ObjectKind,
    Result, ToJson,
};

/// 元素对象（core §2.3）。内容字段是否允许取决于 `category`（契约 1 §4.1）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ElementObject {
    pub category: String,
    /// 显式 null 保留为 `Some(None)`，缺省为 `None`（下同）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<Option<String>>,
    /// 仅 `Table` / `Formula`。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text_as_html: Option<Option<String>>,
    /// 仅允许携带 blob 的 category（目前为 `Image`）；blob 引用 `"sha256:…"`。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blob: Option<Option<String>>,
    /// blob 字节的媒体类型（如 `image/png`）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<Option<String>>,
    /// 版面坐标、图片 url 等源提供的信息，缺省视同 `{}`。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Option<JsonObject>>,
}

/// 页对象（core §2.2）：`elements` 为 content_hash 列表，数组顺序即页内阅读顺序。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct PageObject {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<Option<JsonObject>>,
    pub elements: Vec<String>,
}

/// 文档对象（core §2.1）：`pages` 为 page_hash 列表，数组顺序即页的阅读顺序。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct DocumentObject {
    /// 封闭枚举；取值见 `dpe_hash::FILE_TYPES`。
    pub file_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<Option<JsonObject>>,
    pub pages: Vec<String>,
}

/// 展开视图中的页：`elements` 为元素对象本身（vectors/README.md）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ExpandedPage {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<Option<JsonObject>>,
    pub elements: Vec<ElementObject>,
}

/// 展开视图中的文档：页与元素内联给出，数组顺序即阅读顺序（vectors/README.md）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ExpandedDocument {
    pub file_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<Option<String>>,
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

    /// 从已解析的 JSON 值构造：经 `content_hash` 校验后提取；失败返回 [`dpe_hash::Error`]
    /// （`code()` / `path()` 同一致性向量）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        dpe_hash::content_hash(value, contract)?;
        Ok(element_of(value))
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

    /// 从已解析的 JSON 值构造：经 `page_hash` 校验后提取（`elements` 为 content_hash 列表，
    /// 按 `contract` 校验）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        object_hash(value, ObjectKind::Page, contract)?;
        Ok(page_of(value))
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

    /// 从已解析的 JSON 值构造：经 `doc_hash` 校验后提取（`pages` 为 page_hash 列表，
    /// 按 `contract` 校验）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        object_hash(value, ObjectKind::Document, contract)?;
        Ok(document_of(value))
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

    /// 从已解析的 JSON 值构造：经 `page_hashes` 校验后提取；错误位置相对于页自身。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        page_hashes(value, contract)?;
        Ok(expanded_page_of(value))
    }
}

impl ExpandedDocument {
    /// 从 JSON 文本构造：I-JSON 严格解析后经 `document_hashes` 校验（core §2.8，含全部子对象）。
    pub fn parse(text: &str, contract: &str) -> Result<Self> {
        Self::from_value(&parse_ijson(text)?, contract)
    }

    /// 从已解析的 JSON 值构造：经 `document_hashes` 校验后提取（校验顺序：文档字段 → 各页
    /// 字段 → 各页的元素）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self> {
        document_hashes(value, contract)?;
        Ok(expanded_document_of(value))
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

// ---------------------------------------------------------------------------
// 已校验值的字段提取
// ---------------------------------------------------------------------------
//
// 前提：调用方（上面的 `from_value`）已用 dpe-hash 按 core §2.8 校验过同一个值，形状由校验的
// 契约语义保证（元素：category 是字符串、允许的内容字段是字符串或 null、metadata 是对象或
// null；页与文档：见 core §2.1–§2.3、契约 1 §5）。这里的提取只做搬运与 clone，`panic` 分支
// 只能由 dpe-hash 违反自身校验契约触发，公开入口无法到达。
//
// 不经过 serde 的 `Value` 反序列化：`arbitrary_precision` 下它会把「首键为内部数字 token
// `$serde_json::private::Number` 的对象」改写为数字或直接报错，而源数据里同形的对象是内容
// （P2：不设保留键、不做过滤）；clone 只复制 JSON 树，原样保留。

fn expect_object(value: &Value) -> &Map<String, Value> {
    value
        .as_object()
        .unwrap_or_else(|| panic!("dpe-hash 校验已保证值是对象"))
}

/// 必有字符串字段（`category` / `file_type`）。
fn required_string(map: &Map<String, Value>, key: &str) -> String {
    match map.get(key) {
        Some(Value::String(text)) => text.clone(),
        _ => panic!("dpe-hash 校验已保证 {key} 是非 null 字符串"),
    }
}

/// 可选字符串字段（三态：缺省 / 显式 null / 值）。
fn triple_string(map: &Map<String, Value>, key: &str) -> Option<Option<String>> {
    match map.get(key) {
        None => None,
        Some(Value::Null) => Some(None),
        Some(Value::String(text)) => Some(Some(text.clone())),
        Some(_) => panic!("dpe-hash 校验已保证 {key} 是字符串或 null"),
    }
}

/// 可选对象字段（三态）。
fn triple_object(map: &Map<String, Value>, key: &str) -> Option<Option<JsonObject>> {
    match map.get(key) {
        None => None,
        Some(Value::Null) => Some(None),
        Some(Value::Object(obj)) => Some(Some(obj.clone())),
        Some(_) => panic!("dpe-hash 校验已保证 {key} 是对象或 null"),
    }
}

/// 字符串数组字段（`elements` / `pages` 的 hash 列表）。
fn string_list(map: &Map<String, Value>, key: &str) -> Vec<String> {
    map.get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("dpe-hash 校验已保证 {key} 是数组"))
        .iter()
        .map(|item| {
            item.as_str()
                .unwrap_or_else(|| panic!("dpe-hash 校验已保证 {key} 的每项是字符串"))
                .to_owned()
        })
        .collect()
}

fn element_of(value: &Value) -> ElementObject {
    let map = expect_object(value);
    ElementObject {
        category: required_string(map, "category"),
        text: triple_string(map, "text"),
        text_as_html: triple_string(map, "text_as_html"),
        blob: triple_string(map, "blob"),
        mime_type: triple_string(map, "mime_type"),
        metadata: triple_object(map, "metadata"),
    }
}

fn page_of(value: &Value) -> PageObject {
    let map = expect_object(value);
    PageObject {
        title: triple_string(map, "title"),
        page_metadata: triple_object(map, "page_metadata"),
        elements: string_list(map, "elements"),
    }
}

fn document_of(value: &Value) -> DocumentObject {
    let map = expect_object(value);
    DocumentObject {
        file_type: required_string(map, "file_type"),
        title: triple_string(map, "title"),
        doc_metadata: triple_object(map, "doc_metadata"),
        pages: string_list(map, "pages"),
    }
}

fn expanded_page_of(value: &Value) -> ExpandedPage {
    let map = expect_object(value);
    ExpandedPage {
        title: triple_string(map, "title"),
        page_metadata: triple_object(map, "page_metadata"),
        elements: map
            .get("elements")
            .and_then(Value::as_array)
            .unwrap_or_else(|| panic!("dpe-hash 校验已保证 elements 是数组"))
            .iter()
            .map(element_of)
            .collect(),
    }
}

fn expanded_document_of(value: &Value) -> ExpandedDocument {
    let map = expect_object(value);
    ExpandedDocument {
        file_type: required_string(map, "file_type"),
        title: triple_string(map, "title"),
        doc_metadata: triple_object(map, "doc_metadata"),
        pages: map
            .get("pages")
            .and_then(Value::as_array)
            .unwrap_or_else(|| panic!("dpe-hash 校验已保证 pages 是数组"))
            .iter()
            .map(expanded_page_of)
            .collect(),
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
