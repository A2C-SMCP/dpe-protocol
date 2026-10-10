//! 三层对象的类型化结构（serde）：与 Python dpe-hash 的 TypedDict 一一对应。
//!
//! 运行时校验（封闭 schema、各 category 允许的字段、file_type 枚举、hash 格式）仍由 hash
//! 函数完成，与类型无关；这些结构只保证字段名不拼错，字段是公开的，用结构体字面量（配合
//! `..Default::default()`）构造。
//!
//! **只实现 `Serialize`、不实现 `Deserialize`**：`arbitrary_precision` 下 serde_json 用内部
//! 保留键 `$serde_json::private::Number` 的单键对象表示数字，其 `Value` 反序列化会把源数据里
//! 恰好同形的对象静默读成数字（或报错）——实现引入事实上的保留键，违反北极星 P2（源即内容）。
//! 从 JSON 文本构造值请用 [`parse_ijson`](crate::parse_ijson)（保真、拒绝重复键与孤立代理项）；
//! 需要带规范校验与「原样表示」的数据模型用 dpe-sdk 的 `models`（本结构的校验对等物）。
//!
//! - 线上原像（契约 1 §4–§5）：[`ElementObject`]、[`PageObject`]（带 `elements`）、
//!   [`DocumentObject`]（带 `pages`）；
//! - 不含子对象列表的自身字段：[`PageFields`]、[`DocumentFields`]，配合
//!   [`page_hash`](crate::page_hash) / [`doc_hash`](crate::doc_hash) 用已存的子 hash 上溯；
//! - 展开视图（vectors/README.md）：[`ExpandedDocument`] / [`ExpandedPage`]，配合
//!   [`document_hashes`](crate::document_hashes)；其输出为 [`DocumentHashes`] / [`PageHashes`]
//!   （纯字符串结构，`Deserialize` 保留——不含任意 JSON 值，无上述缺陷）。
//!
//! 结构体都不使用 `#[serde(flatten)]`：serde_json 的 `arbitrary_precision` 下 flatten 无法
//! 处理数字。
//!
//! 因此规范给对象新增字段时，这里加字段属于不兼容变更，按版本约定与文档、Python SDK 同步升
//! 次版本（`bump minor`）。

use std::borrow::Cow;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// JSON 对象（metadata 的类型）。
pub type JsonObject = Map<String, Value>;

/// 元素对象（core.md §2.3）。内容字段是否允许取决于 `category`（契约 1 §4.1）。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ElementObject {
    pub category: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text_as_html: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blob: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<JsonObject>,
}

/// 页对象除 `elements` 之外的字段。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct PageFields {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<JsonObject>,
}

/// 页对象（core.md §2.2）：`elements` 为 content_hash 列表。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct PageObject {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<JsonObject>,
    pub elements: Vec<String>,
}

/// 文档对象除 `pages` 之外的字段。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct DocumentFields {
    pub file_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<JsonObject>,
}

/// 文档对象（core.md §2.1）：`pages` 为 page_hash 列表。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct DocumentObject {
    pub file_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<JsonObject>,
    pub pages: Vec<String>,
}

/// 展开视图中的页：`elements` 为元素对象本身。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ExpandedPage {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page_metadata: Option<JsonObject>,
    pub elements: Vec<ElementObject>,
}

/// 展开视图中的文档：`pages` 为展开的页。
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ExpandedDocument {
    pub file_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub doc_metadata: Option<JsonObject>,
    pub pages: Vec<ExpandedPage>,
}

/// 一页的 hash：`page_hash` 与页内元素的 content_hash（按页内顺序）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PageHashes {
    pub page_hash: String,
    pub elements: Vec<String>,
}

/// [`document_hashes`](crate::document_hashes) 的结果，形状同向量的 `expected`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DocumentHashes {
    pub doc_hash: String,
    pub pages: Vec<PageHashes>,
}

/// [`object_hash`](crate::object_hash) / [`children`](crate::children) 的对象层级。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ObjectKind {
    Element,
    Page,
    Document,
}

impl ObjectKind {
    /// 规范中的层级名：`element` / `page` / `document`。
    pub fn as_str(self) -> &'static str {
        match self {
            ObjectKind::Element => "element",
            ObjectKind::Page => "page",
            ObjectKind::Document => "document",
        }
    }
}

impl fmt::Display for ObjectKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// 层级名不是 `element` / `page` / `document`（[`ObjectKind`] 的 `FromStr`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownObjectKind(pub String);

impl fmt::Display for UnknownObjectKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "未知的对象层级：{:?}", self.0)
    }
}

impl std::error::Error for UnknownObjectKind {}

impl FromStr for ObjectKind {
    type Err = UnknownObjectKind;

    /// 由规范中的层级名解析（与 [`ObjectKind::as_str`] 互逆）。
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "element" => Ok(ObjectKind::Element),
            "page" => Ok(ObjectKind::Page),
            "document" => Ok(ObjectKind::Document),
            other => Err(UnknownObjectKind(other.to_owned())),
        }
    }
}

/// hash 函数的输入：可视为 JSON 值的对象。
///
/// [`Value`] 借用、零拷贝（未加类型的线上 JSON 走这条路）；[`JsonObject`] 与本模块的类型化
/// 结构先转换为 JSON 值（整体复制一次；性能敏感的大文档原位重算宜直接传 `&Value`）。
/// 调用方的自有类型也可实现本 trait。
///
/// 转换为 JSON 值时，非有限 f64 已被 serde_json 变成 `null`，hash 视同缺省而非报错
/// （见 crate 文档「I-JSON 与数值」）。
pub trait ToJson {
    /// 本对象的 JSON 值。
    fn to_json(&self) -> Cow<'_, Value>;
}

impl ToJson for Value {
    fn to_json(&self) -> Cow<'_, Value> {
        Cow::Borrowed(self)
    }
}

impl ToJson for JsonObject {
    fn to_json(&self) -> Cow<'_, Value> {
        Cow::Owned(Value::Object(self.clone()))
    }
}

macro_rules! typed_to_json {
    ($($ty:ty),*) => {$(
        impl ToJson for $ty {
            fn to_json(&self) -> Cow<'_, Value> {
                // 字段只有 String / Option / Vec / JSON 对象，序列化为 Value 不会失败
                Cow::Owned(serde_json::to_value(self).expect("类型化结构总能序列化为 JSON 值"))
            }
        }
    )*};
}

typed_to_json!(
    ElementObject,
    PageFields,
    PageObject,
    DocumentFields,
    DocumentObject,
    ExpandedPage,
    ExpandedDocument
);

impl<T: ToJson + ?Sized> ToJson for &T {
    fn to_json(&self) -> Cow<'_, Value> {
        (**self).to_json()
    }
}
