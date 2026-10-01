//! hash 契约 1（`dpe1:`）——spec/hash-contract-1.md 的 SDK 实现。
//!
//! 契约 1 是三层同构的 tree hash（PR #9）：元素、页、根三层对象统一为
//! `H(obj) = "dpe1:" + hex(sha256(utf8(JCS(norm(obj)))))`。
//! `norm`：递归删除值为 null 的键（缺省 ≡ null）；三层 metadata 字段缺省视同 `{}`。
//! 页没有页号字段，页序即 `pages` 数组顺序。
//!
//! `dpe2` 仅用于契约升级演练：摘要输入为 ASCII `dpe2` 后接 JCS 原像字节。

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::jcs::jcs;

/// 契约版本（hash 值前缀）
pub const CONTRACT: &str = "dpe1";

/// category → 允许的内容字段（契约 1 §4.1；#7 要求的统一映射表）
pub const CATEGORY_CONTENT_FIELDS: &[(&str, &[&str])] = &[
    ("UncategorizedText", &["text"]),
    ("CheckBox", &["text"]),
    ("CompositeElement", &["text"]),
    ("FigureCaption", &["text"]),
    ("NarrativeText", &["text"]),
    ("ListItem", &["text"]),
    ("Title", &["text"]),
    ("Address", &["text"]),
    ("EmailAddress", &["text"]),
    ("PageBreak", &["text"]),
    ("TableChunk", &["text"]),
    ("Header", &["text"]),
    ("Footer", &["text"]),
    ("CodeSnippet", &["text"]),
    ("PageNumber", &["text"]),
    ("FormKeysValues", &["text"]),
    ("tfchat", &["text"]),
    ("Table", &["text", "text_as_html"]),
    ("Formula", &["text", "text_as_html"]),
    ("Image", &["text", "image_blob", "image_mime_type"]),
];

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

fn hval(obj: &Value, contract: &str) -> Result<String, String> {
    let preimage = jcs(obj)?;
    let mut hasher = Sha256::new();
    match contract {
        "dpe1" => {}
        "dpe2" => hasher.update(b"dpe2"),
        other => return Err(format!("unsupported hash contract: {other}")),
    }
    hasher.update(preimage.as_bytes());
    Ok(format!("{contract}:{:x}", hasher.finalize()))
}

/// 递归删除对象中值为 null 的键（缺省 ≡ null，契约 1 §3.2）；数组元素不受影响。
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

/// metadata 规范化：缺省视同 `{}`，递归删 null 键。在原像中总是出现（§3.2）。
fn meta(m: Option<&Value>) -> Result<Value, String> {
    match m {
        None | Some(Value::Null) => Ok(Value::Object(Map::new())),
        Some(obj @ Value::Object(_)) => Ok(strip_nulls(obj)),
        Some(other) => Err(format!("metadata must be object or null, got {other}")),
    }
}

fn str_or_none<'a>(obj: &'a Value, field: &str) -> Result<Option<&'a str>, String> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s)),
        Some(other) => Err(format!("{field} must be string or null, got {other}")),
    }
}

fn check_closed(obj: &Value, allowed: &[&str], what: &str) -> Result<(), String> {
    for key in obj.as_object().into_iter().flat_map(|m| m.keys()) {
        if !allowed.contains(&key.as_str()) {
            return Err(format!("field not allowed on {what}: {key}"));
        }
    }
    Ok(())
}

/// 元素 `content_hash`（契约 1 §4）：`H({category, 内容字段…, metadata})`。
pub fn content_hash(element: &Value, contract: &str) -> Result<String, String> {
    let cat = element["category"]
        .as_str()
        .ok_or("element.category must be string")?;
    let content_fields = CATEGORY_CONTENT_FIELDS
        .iter()
        .find(|(c, _)| *c == cat)
        .map(|(_, fields)| *fields)
        // 封闭枚举，禁止退化为 text-only
        .ok_or(format!("unknown category: {cat}"))?;
    let allowed: Vec<&str> = ["category", "metadata"]
        .iter()
        .chain(content_fields)
        .copied()
        .collect();
    check_closed(element, &allowed, &format!("category {cat}"))?;
    let mut obj = Map::new();
    obj.insert("category".into(), Value::String(cat.into()));
    for field in content_fields {
        if let Some(value) = str_or_none(element, field)? {
            obj.insert((*field).into(), Value::String(value.into()));
        }
    }
    obj.insert("metadata".into(), meta(element.get("metadata"))?);
    hval(&Value::Object(obj), contract)
}

/// `page_hash`（契约 1 §5）：`H({title?, page_metadata, elements})`。页没有页号字段。
pub fn page_hash(
    page: &Value,
    element_hashes: &[String],
    contract: &str,
) -> Result<String, String> {
    check_closed(page, &["title", "page_metadata", "elements"], "page")?;
    let mut obj = Map::new();
    if let Some(title) = str_or_none(page, "title")? {
        obj.insert("title".into(), Value::String(title.into()));
    }
    obj.insert("page_metadata".into(), meta(page.get("page_metadata"))?);
    obj.insert(
        "elements".into(),
        Value::Array(
            element_hashes
                .iter()
                .map(|h| Value::String(h.clone()))
                .collect(),
        ),
    );
    hval(&Value::Object(obj), contract)
}

/// 整篇文档的三层 hash，返回与向量 `expected` 同形的 JSON：
/// `{"doc_hash": …, "pages": [{"page_hash", "elements"}…]}`。页序即数组顺序。
pub fn document_hashes(document: &Value, contract: &str) -> Result<Value, String> {
    check_closed(
        document,
        &["file_type", "doc_metadata", "pages"],
        "document",
    )?;
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
    let mut page_hashes = Vec::new();
    for page in pages {
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
        page_hashes.push(Value::String(ph.clone()));
        pages_out.push(json!({"page_hash": ph, "elements": ehashes}));
    }
    let root = json!({
        "file_type": file_type,
        "doc_metadata": meta(document.get("doc_metadata"))?,
        "pages": page_hashes,
    });
    Ok(json!({"doc_hash": hval(&root, contract)?, "pages": pages_out}))
}
