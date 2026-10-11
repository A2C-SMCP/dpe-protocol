//! 响应报文（HTTP 绑定 §4、§5）：与 Python `dpe_sdk.wire` 对等。
//!
//! - **信封对未知成员宽容**：服务端在同一协议版本内增加的响应成员（如新的 limits 键）不应让
//!   旧客户端失败；能力扩展点是 capabilities 的 `features`。
//! - **信封内嵌的三层对象仍是封闭 schema**（[`crate::models`]）：未定义字段照常拒绝，子 hash
//!   按显式传入的 `contract` 校验。
//! - **字段类型严格**：字符串必须是 JSON 字符串、限值必须是 JSON 整数（`8.0`、`"8"`、`true`
//!   一律不合规），与 Python 侧 pydantic 的 strict 校验对齐。
//!
//! 解析失败一律是客户端本地错误 [`ErrorKind::UnexpectedResponse`](crate::errors::ErrorKind)
//! （报文不合规，MUST NOT 冒充请求侧的 `DPE_VALIDATION`）；构造出的错误不带 HTTP 状态码，
//! 协议核心在解析响应时用 `with_status` 补上。

use std::fmt;

use serde_json::{Map, Value};

use crate::errors::Error;
use crate::models::{DocumentObject, PageObject};

/// `capabilities` 的 `limits`（HTTP 绑定 §4.1）：各项为正整数，会话 TTL 不少于 3600 秒。
///
/// 超过 `u64` 上限的整数字面量按 `u64::MAX` 处理：对分批、分块与超限判定而言，这与 Python
/// 侧的任意精度整数行为等价（这些值只参与与真实规模的比较）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pub max_payload_bytes: u64,
    pub page_max_bytes: u64,
    pub staging_ttl_seconds: u64,
    pub blob_max_bytes: u64,
    pub blob_chunk_bytes: u64,
    pub batch_head_max: u64,
    pub list_page_max: u64,
}

/// `GET capabilities`（HTTP 绑定 §4.1）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Capabilities {
    pub protocol: String,
    pub hash_contracts: Vec<String>,
    pub limits: Limits,
    pub content_encodings: Vec<String>,
    pub features: Vec<String>,
}

/// head / batch_head 的单项：文档存在时的 doc_hash（按请求声明的契约）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Head {
    pub doc_hash: String,
}

/// `POST heads`：与请求的 `uris` 一一对应，不存在的文档为 `None`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BatchHeads {
    pub heads: Vec<Option<Head>>,
}

/// `GET documents?uri=`：文档对象与按其顺序给出的全部页对象，不含元素对象。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Skeleton {
    pub file_uri: String,
    pub doc_hash: String,
    pub document: DocumentObject,
    pub pages: Vec<PageObject>,
}

/// `GET documents?prefix=` 的一项。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListEntry {
    pub file_uri: String,
    pub doc_hash: String,
}

/// `GET documents?prefix=`：`next_cursor` 为 `None` 表示结束。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListPage {
    pub documents: Vec<ListEntry>,
    pub next_cursor: Option<String>,
}

/// `DPE_MISSING_CONTENT` 的缺失清单，按层给出（core §3.3、HTTP 绑定 §5）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Missing {
    pub pages: Vec<String>,
    pub content_hashes: Vec<String>,
    pub blobs: Vec<String>,
}

// ---------------------------------------------------------------------------
// 解析入口（响应报文；失败为 UnexpectedResponse）
// ---------------------------------------------------------------------------

impl Limits {
    /// 由响应中的 `limits` 值构造。
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let object = value
            .as_object()
            .ok_or_else(|| invalid("Limits", "limits 必须是对象"))?;
        Self::from_object(object).map_err(|reason| invalid("Limits", reason))
    }

    /// 由对象构造；失败理由留给外层信封模型统一包装成消息。
    fn from_object(object: &Map<String, Value>) -> Result<Self, String> {
        Ok(Limits {
            max_payload_bytes: limit(object, "max_payload_bytes", 1)?,
            page_max_bytes: limit(object, "page_max_bytes", 1)?,
            staging_ttl_seconds: limit(object, "staging_ttl_seconds", 3600)?,
            blob_max_bytes: limit(object, "blob_max_bytes", 1)?,
            blob_chunk_bytes: limit(object, "blob_chunk_bytes", 1)?,
            batch_head_max: limit(object, "batch_head_max", 1)?,
            list_page_max: limit(object, "list_page_max", 1)?,
        })
    }
}

impl Capabilities {
    /// 由 `GET capabilities` 的响应体构造。
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        if !value.is_object() {
            return Err(invalid("Capabilities", "响应体必须是对象"));
        }
        Self::from_object(value.as_object().expect("已确认是对象"))
            .map_err(|reason| invalid("Capabilities", reason))
    }

    fn from_object(object: &Map<String, Value>) -> Result<Self, String> {
        Ok(Capabilities {
            protocol: string_field(object, "protocol")?,
            hash_contracts: string_list_field(object, "hash_contracts")?,
            limits: Limits::from_object(object_field(object, "limits")?)?,
            content_encodings: string_list_field(object, "content_encodings")?,
            features: string_list_field(object, "features")?,
        })
    }
}

impl BatchHeads {
    /// 由 `POST heads` 的响应体构造；`heads` 的每一项是 `null` 或 `{doc_hash}` 对象。
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let object = value
            .as_object()
            .ok_or_else(|| invalid("BatchHeads", "响应体必须是对象"))?;
        let mut heads = Vec::new();
        for (i, item) in array_field(object, "heads")
            .map_err(wrap("BatchHeads"))?
            .iter()
            .enumerate()
        {
            match item {
                Value::Null => heads.push(None),
                Value::Object(head) => {
                    let doc_hash = head
                        .get("doc_hash")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            invalid("BatchHeads", format!("heads[{i}].doc_hash 必须是字符串"))
                        })?
                        .to_owned();
                    heads.push(Some(Head { doc_hash }));
                }
                _ => {
                    return Err(invalid(
                        "BatchHeads",
                        format!("heads[{i}] 必须是对象或 null"),
                    ));
                }
            }
        }
        Ok(BatchHeads { heads })
    }
}

impl Skeleton {
    /// 由 `GET documents?uri=` 的响应体构造；内嵌的文档与页对象按 `contract` 校验
    /// （core §2.8，同 [`DocumentObject::from_value`] / [`PageObject::from_value`]）。
    pub fn from_value(value: &Value, contract: &str) -> Result<Self, Error> {
        let object = value
            .as_object()
            .ok_or_else(|| invalid("Skeleton", "响应体必须是对象"))?;
        // 顺序同 Python 侧模型的字段定义：file_uri → doc_hash → document → pages
        let file_uri = string_field(object, "file_uri").map_err(wrap("Skeleton"))?;
        let doc_hash = string_field(object, "doc_hash").map_err(wrap("Skeleton"))?;
        let document = DocumentObject::from_value(
            object
                .get("document")
                .ok_or_else(|| invalid("Skeleton", "缺少字段 document"))?,
            contract,
        )
        .map_err(|error| invalid("Skeleton", error))?;
        let pages_value = object
            .get("pages")
            .ok_or_else(|| invalid("Skeleton", "缺少字段 pages"))?;
        let pages = pages_value
            .as_array()
            .ok_or_else(|| invalid("Skeleton", "字段 pages 必须是数组"))?;
        let mut parsed_pages = Vec::with_capacity(pages.len());
        for page in pages {
            parsed_pages.push(
                PageObject::from_value(page, contract)
                    .map_err(|error| invalid("Skeleton", error))?,
            );
        }
        Ok(Skeleton {
            file_uri,
            doc_hash,
            document,
            pages: parsed_pages,
        })
    }
}

impl ListPage {
    /// 由 `GET documents?prefix=` 的响应体构造；`next_cursor` 必须出现，可为 `null`。
    pub fn from_value(value: &Value) -> Result<Self, Error> {
        let object = value
            .as_object()
            .ok_or_else(|| invalid("ListPage", "响应体必须是对象"))?;
        let mut documents = Vec::new();
        for (i, item) in array_field(object, "documents")
            .map_err(wrap("ListPage"))?
            .iter()
            .enumerate()
        {
            let entry = item
                .as_object()
                .ok_or_else(|| invalid("ListPage", format!("documents[{i}] 必须是对象")))?;
            let entry_error =
                |reason: String| invalid("ListPage", format!("documents[{i}]: {reason}"));
            let file_uri = string_field(entry, "file_uri").map_err(entry_error)?;
            let doc_hash = string_field(entry, "doc_hash").map_err(entry_error)?;
            documents.push(ListEntry { file_uri, doc_hash });
        }
        let next_cursor = match object
            .get("next_cursor")
            .ok_or_else(|| invalid("ListPage", "缺少字段 next_cursor"))?
        {
            Value::Null => None,
            Value::String(text) => Some(text.clone()),
            _ => return Err(invalid("ListPage", "字段 next_cursor 必须是字符串或 null")),
        };
        Ok(ListPage {
            documents,
            next_cursor,
        })
    }
}

// ---------------------------------------------------------------------------
// 解析辅助
// ---------------------------------------------------------------------------

/// 报文不合规（UnexpectedResponse；HTTP 状态码由协议核心补上）。
fn invalid(model: &str, reason: impl fmt::Display) -> Error {
    Error::unexpected(format!("响应体不是合法的 {model}：{reason}"), None, None)
}

/// 把「失败理由」签名（`Result<_, String>`）适配到内部 `?` 链；外层统一包装消息。
fn wrap(model: &'static str) -> impl Fn(String) -> Error {
    move |reason| invalid(model, reason)
}

/// 必有字段；缺省 → 失败理由。真正的类型判定由各 `*_field` 完成。
fn object_field<'a>(
    object: &'a Map<String, Value>,
    key: &str,
) -> Result<&'a Map<String, Value>, String> {
    object
        .get(key)
        .ok_or_else(|| format!("缺少字段 {key}"))?
        .as_object()
        .ok_or_else(|| format!("字段 {key} 必须是对象"))
}

fn array_field<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a Vec<Value>, String> {
    match object.get(key) {
        Some(Value::Array(items)) => Ok(items),
        Some(_) => Err(format!("字段 {key} 必须是数组")),
        None => Err(format!("缺少字段 {key}")),
    }
}

fn string_field(object: &Map<String, Value>, key: &str) -> Result<String, String> {
    match object.get(key) {
        Some(Value::String(text)) => Ok(text.clone()),
        Some(_) => Err(format!("字段 {key} 必须是字符串")),
        None => Err(format!("缺少字段 {key}")),
    }
}

fn string_list_field(object: &Map<String, Value>, key: &str) -> Result<Vec<String>, String> {
    let items = match object.get(key) {
        Some(Value::Array(items)) => items,
        Some(_) => return Err(format!("字段 {key} 必须是数组")),
        None => return Err(format!("缺少字段 {key}")),
    };
    let mut parsed = Vec::with_capacity(items.len());
    for item in items {
        match item {
            Value::String(text) => parsed.push(text.clone()),
            _ => return Err(format!("字段 {key} 的每一项必须是字符串")),
        }
    }
    Ok(parsed)
}

/// 限值：必须是非负的 JSON 整数，`min` 为下限（正整数限值为 1，会话 TTL 为 3600）。
fn limit(object: &Map<String, Value>, key: &str, min: u64) -> Result<u64, String> {
    let Some(value) = object.get(key) else {
        return Err(format!("缺少字段 {key}"));
    };
    match positive_integer(value) {
        Some(number) if number >= min => Ok(number),
        Some(number) => Err(format!("{key} 必须不小于 {min}（实际 {number}）")),
        None => Err(format!("{key} 必须是正整数")),
    }
}

/// JSON 整数（非负）。`8.0`、`1e2`、`"8"`、`true` 都不是整数；超过 `u64` 上限的整数字面量
/// 饱和为 `u64::MAX`（见 [`Limits`] 的说明）。
fn positive_integer(value: &Value) -> Option<u64> {
    if let Some(number) = value.as_u64() {
        return Some(number);
    }
    let text = value.as_number()?.to_string();
    if !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Some(u64::MAX);
    }
    None
}
