//! hash 契约 1（`dpe1:`）——spec/hash-contract-1.md 的 SDK 实现。
//!
//! 输入是文档的 hash 输入视图（`serde_json::Value`，形状同 vectors/README.md）。
//! **源即内容**（plan §0.1 P2）：除 `file_uri` 外全部进 hash，没有保留键、
//! 没有过滤；三层 metadata 只做「递归删 null 键 + JCS」。
//! `dpe2` 仅用于契约升级演练：算法与 dpe1 相同，但每次摘要额外前置一个
//! 内容为 `b"dpe2"` 的段。

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::jcs::jcs;

/// 契约版本（hash 值前缀）
pub const CONTRACT: &str = "dpe1";

/// 安全整数范围（core.md §2.6；页号与 metadata 整数共用）
pub const MAX_SAFE_INT: i64 = (1 << 53) - 1;

/// file_type 封闭枚举（core.md §2.5，进 doc_hash；与 `vectors/manifest.json`
/// 的 `file_types` 一致，消费方直接使用本常量）。
pub const FILE_TYPES: &[&str] = &[
    "bmp",
    "csv",
    "doc",
    "docx",
    "eml",
    "epub",
    "heic",
    "html",
    "jpg",
    "json",
    "md",
    "msg",
    "ndjson",
    "odt",
    "org",
    "pdf",
    "png",
    "ppt",
    "pptx",
    "rst",
    "rtf",
    "tiff",
    "tsv",
    "txt",
    "wav",
    "xls",
    "xlsx",
    "xml",
    "zip",
    "java_repo",
    "python_repo",
    "javascript_repo",
    "typescript_repo",
    "unk",
    "empty",
    "tfchat",
    "jira_project",
    "jira_issue",
];

const TEXT_ONLY_CATEGORIES: &[&str] = &[
    "UncategorizedText",
    "CheckBox",
    "CompositeElement",
    "FigureCaption",
    "NarrativeText",
    "ListItem",
    "Title",
    "Address",
    "EmailAddress",
    "PageBreak",
    "TableChunk",
    "Header",
    "Footer",
    "CodeSnippet",
    "PageNumber",
    "FormKeysValues",
    "tfchat",
];
const HTML_CATEGORIES: &[&str] = &["Table", "Formula"];

fn hval(parts: &[Vec<u8>], contract: &str) -> Result<String, String> {
    let mut hasher = Sha256::new();
    match contract {
        "dpe1" => {}
        "dpe2" => {
            hasher.update(4u32.to_be_bytes());
            hasher.update(b"dpe2");
        }
        other => return Err(format!("unsupported hash contract: {other}")),
    }
    for p in parts {
        hasher.update((p.len() as u32).to_be_bytes());
        hasher.update(p);
    }
    Ok(format!("{contract}:{:x}", hasher.finalize()))
}

/// 文本段：null 与缺省取空字节串（段级规则，契约 1 §3.2）。
fn text(v: Option<&Value>) -> Result<Vec<u8>, String> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(s)) => Ok(s.as_bytes().to_vec()),
        Some(other) => Err(format!("text segment must be string or null, got {other}")),
    }
}

/// 递归删除对象中值为 null 的键（缺省 ≡ null）；数组元素不受影响。
fn strip_nulls(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .filter(|(_, v)| !v.is_null())
                .map(|(k, v)| (k.clone(), strip_nulls(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(strip_nulls).collect()),
        other => other.clone(),
    }
}

/// metadata 的 hash 输入：递归删 null 键 → JCS → UTF-8。没有保留键、没有过滤。
fn meta(m: Option<&Value>) -> Result<Vec<u8>, String> {
    match m {
        None | Some(Value::Null) => Ok(b"{}".to_vec()),
        Some(obj @ Value::Object(_)) => Ok(jcs(&strip_nulls(obj))?.into_bytes()),
        Some(other) => Err(format!("metadata must be object or null, got {other}")),
    }
}

/// 元素 `content_hash`（契约 1 §4）：首段 category，末段 metadata。
pub fn content_hash(element: &Value, contract: &str) -> Result<String, String> {
    let cat = element["category"]
        .as_str()
        .ok_or("element.category must be string")?;
    let mut parts: Vec<Vec<u8>> = vec![cat.as_bytes().to_vec()];
    if cat == "Image" {
        // 身份只认 blob（契约 1 §4.2）；图片 url 等源信息在 metadata 中照常进 hash
        let blob_ref = match element.get("image_blob") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(Value::String(format!("blob:{s}"))),
            Some(other) => return Err(format!("image_blob must be string or null, got {other}")),
        };
        parts.push(text(element.get("text"))?);
        parts.push(text(blob_ref.as_ref())?);
        parts.push(text(element.get("image_mime_type"))?);
    } else if HTML_CATEGORIES.contains(&cat) {
        parts.push(text(element.get("text"))?);
        parts.push(text(element.get("text_as_html"))?);
    } else if TEXT_ONLY_CATEGORIES.contains(&cat) {
        parts.push(text(element.get("text"))?);
    } else {
        // 封闭枚举，禁止退化为 text-only
        return Err(format!("unknown category: {cat}"));
    }
    parts.push(meta(element.get("metadata"))?);
    hval(&parts, contract)
}

/// `page_hash`（契约 1 §5）：页号 ASCII、title、metadata、元素 hash 序列。
pub fn page_hash(
    page: &Value,
    element_hashes: &[String],
    contract: &str,
) -> Result<String, String> {
    let number = page["number"]
        .as_i64()
        .ok_or("page.number must be integer")?;
    if number.unsigned_abs() > MAX_SAFE_INT as u64 {
        // core.md §2.6（#6 F4）：页号与 metadata 整数共用安全整数范围
        return Err(format!("page number out of IEEE-754 safe range: {number}"));
    }
    let mut parts: Vec<Vec<u8>> = vec![
        number.to_string().into_bytes(),
        text(page.get("title"))?,
        meta(page.get("page_metadata"))?,
    ];
    parts.extend(element_hashes.iter().map(|h| h.as_bytes().to_vec()));
    hval(&parts, contract)
}

/// 整篇文档的三层 hash，返回与向量 `expected` 同形的 JSON：
/// `{"doc_hash": …, "pages": [{"number", "page_hash", "elements"}…]}`。
pub fn document_hashes(document: &Value, contract: &str) -> Result<Value, String> {
    let file_type = document["file_type"]
        .as_str()
        .ok_or("document.file_type must be string")?;
    if !FILE_TYPES.contains(&file_type) {
        return Err(format!("unknown file_type: {file_type}"));
    }
    let empty = Vec::new();
    let pages = match document.get("pages") {
        None => &empty,
        Some(Value::Array(items)) => items,
        Some(other) => return Err(format!("pages must be array, got {other}")),
    };
    let mut pages_out = Vec::new();
    let mut ph_by_number: Vec<(i64, String)> = Vec::new();
    for page in pages {
        let number = page["number"]
            .as_i64()
            .ok_or("page.number must be integer")?;
        if ph_by_number.iter().any(|(n, _)| *n == number) {
            return Err(format!(
                "page numbers must be unique, got duplicate {number}"
            ));
        }
        let elements = match page.get("elements") {
            None => &empty,
            Some(Value::Array(items)) => items,
            Some(other) => return Err(format!("elements must be array, got {other}")),
        };
        let ehashes: Vec<String> = elements
            .iter()
            .map(|el| content_hash(el, contract))
            .collect::<Result<_, _>>()?;
        let ph = page_hash(page, &ehashes, contract)?;
        ph_by_number.push((number, ph.clone()));
        pages_out.push(json!({"number": number, "page_hash": ph, "elements": ehashes}));
    }
    ph_by_number.sort_by_key(|(n, _)| *n);
    let mut parts: Vec<Vec<u8>> = vec![
        file_type.as_bytes().to_vec(),
        meta(document.get("doc_metadata"))?,
    ];
    parts.extend(ph_by_number.iter().map(|(_, ph)| ph.as_bytes().to_vec()));
    Ok(json!({"doc_hash": hval(&parts, contract)?, "pages": pages_out}))
}
