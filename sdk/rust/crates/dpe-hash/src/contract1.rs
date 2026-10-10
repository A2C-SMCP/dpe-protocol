//! hash 契约 1（`dpe1:`）——spec/hash-contract-1.md 的实现。
//!
//! 契约 1 是三层同构的 tree：元素、页、文档三层对象统一为
//! `H(obj) = "dpe1:" + hex(sha256(utf8(JCS(norm(obj)))))`。
//! `norm`：递归删除值为 null 的键（缺省 ≡ null）；三层 metadata 字段缺省视同 `{}`（§3.2）。
//! 三层对象都是封闭 schema，未定义字段一律拒绝（§3.4）。页没有页号，顺序只由上层数组表达。
//!
//! 本模块是唯一的规范化与校验实现，全部公开函数都只是它的不同入口：
//!
//! - [`content_hash`] / [`page_hash`] / [`doc_hash`]：逐层，用已有的子 hash 上溯；
//! - [`object_hash`]：直接接受线上原像（页带 `elements`、文档带 `pages`，服务端校验用）；
//! - [`document_hashes`]：展开视图一次算出三层；
//! - [`children`]：对象引用的下一层（缺失清单）。
//!
//! `contract` 参数用于契约 1 §6 的原位重算；`dpe2` 只在显式传入时接受（见
//! [`DRILL_CONTRACT`]）。校验顺序与 Python dpe-hash 逐函数一致（core §2.8），多处违例时
//! 返回的错误码相同。

use std::fmt::Display;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::constants::{content_fields, DRILL_CONTRACT, KNOWN_CONTRACTS, SUPPORTED_CONTRACTS};
use crate::depth::check_depth;
use crate::error::{Error, ErrorKind, Result};
use crate::file_type::is_valid_file_type;
use crate::jcs::{canonical, pointer, utf16_cmp, write_string};
use crate::models::{DocumentHashes, ObjectKind, PageHashes, ToJson};

const BLOB_PREFIX: &str = "sha256:";
const PAGE_FIELDS: &[&str] = &["title", "page_metadata"];
const DOCUMENT_FIELDS: &[&str] = &["file_type", "title", "doc_metadata"];

/// 一个字段的 JCS 片段：`(字段名, 已规范化的值)`。
type Part = (&'static str, String);

// ---------------------------------------------------------------------------
// 值格式（§1）
// ---------------------------------------------------------------------------

fn validation(message: impl Into<String>, path: impl Into<String>) -> Error {
    Error::new(ErrorKind::Validation, message, path)
}

fn unsupported(message: impl Into<String>, path: impl Into<String>) -> Error {
    Error::new(ErrorKind::ContractUnsupported, message, path)
}

/// 各契约的摘要前缀字节：dpe2 = ASCII "dpe2" ‖ 原像（vectors/README.md）。
fn salt(contract: &str) -> Result<&'static [u8]> {
    match contract {
        "dpe1" => Ok(b""),
        DRILL_CONTRACT => Ok(b"dpe2"),
        _ => Err(unsupported(format!("不支持的 hash 契约：{contract:?}"), "")),
    }
}

fn digest(preimage: &str, contract: &str, salt: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(salt);
    hasher.update(preimage.as_bytes());
    format!("{contract}:{:x}", hasher.finalize())
}

fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn split_hash<'a>(
    value: &'a str,
    accepted: impl Fn(&str) -> bool,
    at: &str,
) -> Result<(&'a str, &'a str)> {
    let Some((prefix, hex)) = value.split_once(':') else {
        return Err(unsupported("hash 值缺少契约前缀", at));
    };
    if !accepted(prefix) {
        return Err(unsupported(
            format!("未知或不支持的契约前缀：{prefix:?}"),
            at,
        ));
    }
    if !is_hex64(hex) {
        return Err(validation("hash 值的摘要部分必须是 64 位小写 hex", at));
    }
    Ok((prefix, hex))
}

fn blob_hex<'a>(value: &'a str, at: &str) -> Result<&'a str> {
    match value.strip_prefix(BLOB_PREFIX) {
        Some(hex) if is_hex64(hex) => Ok(hex),
        _ => Err(validation("blob 引用必须是 sha256:<64 位小写 hex>", at)),
    }
}

/// 校验并拆分 hash 值，返回 `(契约, 64 位 hex)`；只接受 [`SUPPORTED_CONTRACTS`]。
///
/// 无前缀或前缀不受支持返回 [`ErrorKind::ContractUnsupported`]（契约 1 §1）；摘要格式不对
/// 返回 [`ErrorKind::Validation`]。升级演练用 [`parse_hash_with`] 显式放行 `dpe2`。
pub fn parse_hash(value: &str) -> Result<(&str, &str)> {
    parse_hash_with(value, SUPPORTED_CONTRACTS)
}

/// 同 [`parse_hash`]，但接受 `contracts` 中的契约（如 `&["dpe1", DRILL_CONTRACT]`）。
/// `contracts` 中出现本 crate 不认识的契约（不在 [`KNOWN_CONTRACTS`]）返回
/// [`ErrorKind::ContractUnsupported`]。
pub fn parse_hash_with<'a>(value: &'a str, contracts: &[&str]) -> Result<(&'a str, &'a str)> {
    let mut unknown: Vec<&str> = contracts
        .iter()
        .copied()
        .filter(|c| !KNOWN_CONTRACTS.contains(c))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        unknown.dedup();
        return Err(unsupported(format!("不支持的 hash 契约：{unknown:?}"), ""));
    }
    split_hash(value, |prefix| contracts.contains(&prefix), "")
}

/// 校验 blob 引用 `sha256:<64 位小写 hex>`，返回 hex 部分；不合法返回 [`ErrorKind::Validation`]。
pub fn parse_blob_ref(value: &str) -> Result<&str> {
    blob_hex(value, "")
}

/// blob 原始字节的引用 `"sha256:<hex>"`（契约 1 §1）。
pub fn blob_ref(data: &[u8]) -> String {
    format!("{BLOB_PREFIX}{:x}", Sha256::digest(data))
}

// ---------------------------------------------------------------------------
// 三层对象的校验与规范化（§3–§5），返回 JCS 原像
// ---------------------------------------------------------------------------

fn mapping<'a>(value: &'a Value, at: &str, what: impl Display) -> Result<&'a Map<String, Value>> {
    match value {
        Value::Object(map) => Ok(map),
        other => Err(validation(
            format!("{what}必须是 JSON 对象，实际为 {}", type_name(other)),
            at,
        )),
    }
}

fn closed(
    obj: &Map<String, Value>,
    allowed: impl Fn(&str) -> bool,
    at: &str,
    what: impl Display,
) -> Result<()> {
    let first_extra = obj
        .keys()
        .filter(|k| !allowed(k))
        .min_by(|a, b| utf16_cmp(a, b));
    match first_extra {
        Some(key) => Err(Error::new(
            ErrorKind::UndefinedField,
            format!("{what}不允许字段 {key:?}"),
            pointer(at, &[key]),
        )),
        None => Ok(()),
    }
}

/// 可选字符串字段：缺省或 null 返回 `None`（不进原像）。
fn string_field<'a>(obj: &'a Map<String, Value>, field: &str, at: &str) -> Result<Option<&'a str>> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s)),
        Some(other) => Err(validation(
            format!("{field} 必须是字符串或 null，实际为 {}", type_name(other)),
            pointer(at, &[field]),
        )),
    }
}

fn jcs_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    write_string(s, &mut out);
    out
}

/// metadata 字段的 JCS 片段：缺省视同 `{}`，递归删 null 键，并完整校验全部值（§3.2）。
fn metadata_part(obj: &Map<String, Value>, field: &str, at: &str) -> Result<String> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok("{}".into()),
        Some(value) => mapping(value, "", format_args!("{field} "))
            .and_then(|_| canonical(value, true, ""))
            .map_err(|e| e.under(&pointer(at, &[field]))),
    }
}

/// 一个子对象 hash：必须是同一契约下的合法 hash 值（契约 1 §5）。
///
/// 前缀不是受支持的契约 → [`ErrorKind::ContractUnsupported`]；是受支持的契约、但与本对象的
/// 契约不同（契约混用）→ [`ErrorKind::Validation`]。「受支持」= [`SUPPORTED_CONTRACTS`] 加上
/// 本次调用显式选择的契约：演练契约 dpe2 只在被选中时才算受支持，因此 dpe1 对象引用 dpe2 子
/// hash 与生产服务端一样得到 `DPE_CONTRACT_UNSUPPORTED`。
fn check_child_hash(item: &str, contract: &str, at: &str) -> Result<()> {
    let accepted = |prefix: &str| prefix == contract || SUPPORTED_CONTRACTS.contains(&prefix);
    let (prefix, _) = split_hash(item, accepted, at)?;
    if prefix != contract {
        return Err(validation(
            format!("子对象 hash 的契约 {prefix} 与本对象的契约 {contract} 不一致"),
            at,
        ));
    }
    Ok(())
}

/// 已校验的子 hash 列表的 JCS 片段（hash 值只含 ASCII 字母数字与冒号，无需转义）。
fn hash_list_part<'a>(hashes: impl IntoIterator<Item = &'a str>) -> String {
    let mut out = String::from("[");
    for (i, h) in hashes.into_iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('"');
        out.push_str(h);
        out.push('"');
    }
    out.push(']');
    out
}

/// 线上原像中的子 hash 列表（JSON 值）：校验后返回其 JCS 片段。
fn wire_hash_list(value: &Value, contract: &str, at: &str) -> Result<String> {
    let Value::Array(items) = value else {
        return Err(validation(
            format!("必须是 hash 值数组，实际为 {}", type_name(value)),
            at,
        ));
    };
    let mut hashes = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let checked = match item {
            Value::String(h) => check_child_hash(h, contract, "").map(|()| h.as_str()),
            _ => Err(validation(
                format!("hash 值必须是字符串，实际为 {}", type_name(item)),
                "",
            )),
        };
        hashes.push(checked.map_err(|e| e.under(&pointer(at, &[&i.to_string()])))?);
    }
    Ok(hash_list_part(hashes))
}

/// 逐层 API 传入的子 hash 列表：校验后返回其 JCS 片段。
fn slice_hash_list<S: AsRef<str>>(hashes: &[S], contract: &str, at: &str) -> Result<String> {
    for (i, h) in hashes.iter().enumerate() {
        check_child_hash(h.as_ref(), contract, "")
            .map_err(|e| e.under(&pointer(at, &[&i.to_string()])))?;
    }
    Ok(hash_list_part(hashes.iter().map(AsRef::as_ref)))
}

/// 把各字段的 JCS 片段拼成对象原像。键都是规范定义的 ASCII 字段名，字节序即 UTF-16 码元序。
fn assemble(mut parts: Vec<Part>) -> String {
    parts.sort_unstable_by_key(|(key, _)| *key);
    let mut out = String::from("{");
    for (i, (key, value)) in parts.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('"');
        out.push_str(key);
        out.push_str("\":");
        out.push_str(value);
    }
    out.push('}');
    out
}

// 以下按 core.md §2.8 的顺序校验：形状 → category（元素）→ 封闭 schema → 按表中字段顺序
// 逐字段完整校验。每个字段校验通过即产出 JCS 片段，最后拼装原像，不对对象做第二遍遍历。

fn element_preimage(element: &Value, at: &str) -> Result<String> {
    let el = mapping(element, at, "元素对象")?;
    let category = match el.get("category") {
        // 必有字段为 null 视同缺省（§2.8 第 1 步）
        None | Some(Value::Null) => return Err(validation("元素对象缺少 category", at)),
        Some(Value::String(s)) => s.as_str(),
        Some(_) => {
            return Err(validation(
                "category 必须是字符串",
                pointer(at, &["category"]),
            ))
        }
    };
    // 封闭枚举，禁止退化为 text-only（§4）；先于封闭 schema（§2.8）
    let allowed = content_fields(category).ok_or_else(|| {
        Error::new(
            ErrorKind::CategoryUnknown,
            format!("未知 category：{category:?}"),
            pointer(at, &["category"]),
        )
    })?;
    closed(
        el,
        |k| k == "category" || k == "metadata" || allowed.contains(&k),
        at,
        format_args!("{category} 元素"),
    )?;
    let mut parts: Vec<Part> = vec![("category", jcs_string(category))];
    for &field in allowed {
        let Some(value) = string_field(el, field, at)? else {
            continue;
        };
        if field == "blob" {
            blob_hex(value, &pointer(at, &[field]))?;
        }
        parts.push((field, jcs_string(value)));
    }
    parts.push(("metadata", metadata_part(el, "metadata", at)?));
    Ok(assemble(parts))
}

/// 页对象除 `elements` 外的字段片段。`with_children`：`page` 自带 `elements` 键
/// （线上原像或展开视图），其值由调用方处理。
fn page_parts(page: &Map<String, Value>, at: &str, with_children: bool) -> Result<Vec<Part>> {
    if !with_children && page.contains_key("elements") {
        return Err(Error::new(
            ErrorKind::UndefinedField,
            "page_hash 的页字段不得带 elements（子 hash 单独传入；线上原像请用 object_hash）",
            pointer(at, &["elements"]),
        ));
    }
    closed(
        page,
        |k| PAGE_FIELDS.contains(&k) || (with_children && k == "elements"),
        at,
        "页对象",
    )?;
    let mut parts = Vec::with_capacity(3);
    if let Some(title) = string_field(page, "title", at)? {
        parts.push(("title", jcs_string(title)));
    }
    parts.push(("page_metadata", metadata_part(page, "page_metadata", at)?));
    Ok(parts)
}

/// 文档对象除 `pages` 外的字段片段（`with_children` 同 [`page_parts`]）。
fn document_parts(
    document: &Map<String, Value>,
    at: &str,
    with_children: bool,
) -> Result<Vec<Part>> {
    // 形状先于封闭 schema；null 视同缺省
    let file_type = match document.get("file_type") {
        None | Some(Value::Null) => return Err(validation("文档对象缺少 file_type", at)),
        Some(value) => value,
    };
    if !with_children && document.contains_key("pages") {
        return Err(Error::new(
            ErrorKind::UndefinedField,
            "doc_hash 的文档字段不得带 pages（子 hash 单独传入；线上原像请用 object_hash）",
            pointer(at, &["pages"]),
        ));
    }
    closed(
        document,
        |k| DOCUMENT_FIELDS.contains(&k) || (with_children && k == "pages"),
        at,
        "文档对象",
    )?;
    let Value::String(file_type) = file_type else {
        return Err(validation(
            "file_type 必须是字符串",
            pointer(at, &["file_type"]),
        ));
    };
    if !is_valid_file_type(file_type) {
        return Err(Error::new(
            ErrorKind::FileTypeInvalid,
            format!("file_type 不合语法：{file_type:?}"),
            pointer(at, &["file_type"]),
        ));
    }
    let mut parts: Vec<Part> = vec![("file_type", jcs_string(file_type))];
    if let Some(title) = string_field(document, "title", at)? {
        parts.push(("title", jcs_string(title)));
    }
    parts.push(("doc_metadata", metadata_part(document, "doc_metadata", at)?));
    Ok(parts)
}

fn required<'a>(
    obj: &'a Map<String, Value>,
    field: &str,
    at: &str,
    what: &str,
) -> Result<&'a Value> {
    match obj.get(field) {
        // null 视同缺省（§2.8 第 1 步）
        None | Some(Value::Null) => Err(validation(format!("{what}缺少 {field}"), at)),
        Some(value) => Ok(value),
    }
}

fn page_preimage(
    page: &Map<String, Value>,
    elements: &Value,
    contract: &str,
    at: &str,
) -> Result<String> {
    let mut parts = page_parts(page, at, true)?;
    parts.push((
        "elements",
        wire_hash_list(elements, contract, &pointer(at, &["elements"]))?,
    ));
    Ok(assemble(parts))
}

fn document_preimage(
    document: &Map<String, Value>,
    pages: &Value,
    contract: &str,
    at: &str,
) -> Result<String> {
    let mut parts = document_parts(document, at, true)?;
    parts.push((
        "pages",
        wire_hash_list(pages, contract, &pointer(at, &["pages"]))?,
    ));
    Ok(assemble(parts))
}

/// 各层对象的子 hash 列表字段（core §2.8 第 0 步：深度计数时不向内展开）。
fn child_field(kind: ObjectKind) -> Option<&'static str> {
    match kind {
        ObjectKind::Element => None,
        ObjectKind::Page => Some("elements"),
        ObjectKind::Document => Some("pages"),
    }
}

/// 线上原像的 JCS 原像（[`object_hash`] / [`children`] 共用）。
fn preimage(obj: &Value, kind: ObjectKind, contract: &str) -> Result<String> {
    check_depth(obj, child_field(kind))?;
    match kind {
        ObjectKind::Element => element_preimage(obj, ""),
        ObjectKind::Page => {
            let page = mapping(obj, "", "页对象")?;
            let elements = required(page, "elements", "", "页对象")?;
            page_preimage(page, elements, contract, "")
        }
        ObjectKind::Document => {
            let document = mapping(obj, "", "文档对象")?;
            let pages = required(document, "pages", "", "文档对象")?;
            document_preimage(document, pages, contract, "")
        }
    }
}

// ---------------------------------------------------------------------------
// 公开入口
// ---------------------------------------------------------------------------

/// 元素对象的 `content_hash`（契约 1 §4）。
pub fn content_hash(element: &(impl ToJson + ?Sized), contract: &str) -> Result<String> {
    let salt = salt(contract)?;
    let element = element.to_json();
    check_depth(&element, None)?;
    Ok(digest(&element_preimage(&element, "")?, contract, salt))
}

/// `page_hash`（契约 1 §5）：页自身字段 + 已有的 content_hash 列表（按页内顺序）。
///
/// `page` 带 `elements` 键即报错，避免两份元素列表的歧义；`element_hashes` 中的错误路径记为
/// `/elements/<i>`。
pub fn page_hash<S: AsRef<str>>(
    page: &(impl ToJson + ?Sized),
    element_hashes: &[S],
    contract: &str,
) -> Result<String> {
    let salt = salt(contract)?;
    let page = page.to_json();
    check_depth(&page, Some("elements"))?;
    let mut parts = page_parts(mapping(&page, "", "页对象")?, "", false)?;
    parts.push((
        "elements",
        slice_hash_list(element_hashes, contract, "/elements")?,
    ));
    Ok(digest(&assemble(parts), contract, salt))
}

/// `doc_hash`（契约 1 §5）：文档自身字段 + 已有的 page_hash 列表（按页序），不需加载元素。
///
/// `document` 带 `pages` 键即报错，避免两份页序的歧义；`page_hashes` 中的错误路径记为
/// `/pages/<i>`。
pub fn doc_hash<S: AsRef<str>>(
    document: &(impl ToJson + ?Sized),
    page_hashes: &[S],
    contract: &str,
) -> Result<String> {
    let salt = salt(contract)?;
    let document = document.to_json();
    check_depth(&document, Some("pages"))?;
    let mut parts = document_parts(mapping(&document, "", "文档对象")?, "", false)?;
    parts.push(("pages", slice_hash_list(page_hashes, contract, "/pages")?));
    Ok(digest(&assemble(parts), contract, salt))
}

/// 线上原像的 hash：一步完成封闭 schema 校验、规范化、JCS 与摘要。
///
/// 页对象须带 `elements`、文档对象须带 `pages`（子 hash 列表，契约须与 `contract` 一致）。
/// 对同一文档，结果与 [`content_hash`] / [`page_hash`] / [`doc_hash`] 逐层相等。
pub fn object_hash(
    obj: &(impl ToJson + ?Sized),
    kind: ObjectKind,
    contract: &str,
) -> Result<String> {
    let salt = salt(contract)?;
    let obj = obj.to_json();
    Ok(digest(&preimage(&obj, kind, contract)?, contract, salt))
}

/// 线上原像引用的下一层（先按 [`object_hash`] 的规则完整校验）。
///
/// 页 → `elements` 的 content_hash；文档 → `pages` 的 page_hash；元素 → `blob` 引用（没有则
/// 为空）。按出现顺序返回，重复保留，去重由调用方决定。
pub fn children(
    obj: &(impl ToJson + ?Sized),
    kind: ObjectKind,
    contract: &str,
) -> Result<Vec<String>> {
    salt(contract)?;
    let obj = obj.to_json();
    preimage(&obj, kind, contract)?;
    let field = match kind {
        ObjectKind::Element => {
            return Ok(match obj.get("blob") {
                Some(Value::String(blob)) => vec![blob.clone()],
                _ => Vec::new(),
            })
        }
        ObjectKind::Page => "elements",
        ObjectKind::Document => "pages",
    };
    // 已通过校验：必为字符串数组
    Ok(obj[field]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|h| h.as_str().map(str::to_owned))
        .collect())
}

/// 一个展开页的字段与形状校验（core §2.8 阶段 1，次序与 §2.2 的字段顺序一致）：
/// 嵌套深度（第 0 步，按页自身）→ 页是对象 → `elements` 存在且非 null（必有字段）→ 页自身字段
/// （封闭 schema、title、page_metadata）→ `elements` 是数组（第 4 步的字段类型检查）。
/// 返回页字段片段与 `elements` 数组；`at` 为页的错误路径前缀。
fn page_input<'a>(raw: &'a Value, at: &str) -> Result<(Vec<Part>, &'a [Value])> {
    check_depth(raw, Some("elements"))?;
    let page = mapping(raw, at, "页")?;
    let elements = required(page, "elements", at, "页")?;
    let parts = page_parts(page, at, true)?;
    let Value::Array(elements) = elements else {
        return Err(validation(
            "elements 必须是数组",
            pointer(at, &["elements"]),
        ));
    };
    Ok((parts, elements))
}

/// 已校验页的计算（core §2.8 阶段 2）：各元素的 content_hash，再组装出 page_hash。
fn page_hashes_of(
    mut parts: Vec<Part>,
    elements: &[Value],
    at: &str,
    contract: &str,
    salt: &[u8],
) -> Result<PageHashes> {
    let element_hashes = elements
        .iter()
        .enumerate()
        .map(|(j, el)| {
            check_depth(el, None)?;
            let preimage = element_preimage(el, "")
                .map_err(|e| e.under(&pointer(at, &["elements", &j.to_string()])))?;
            Ok(digest(&preimage, contract, salt))
        })
        .collect::<Result<Vec<_>>>()?;
    parts.push((
        "elements",
        hash_list_part(element_hashes.iter().map(String::as_str)),
    ));
    Ok(PageHashes {
        page_hash: digest(&assemble(parts), contract, salt),
        elements: element_hashes,
    })
}

/// 单个展开页的校验与 hash（vectors/README.md 的页视图）：页自身字段 → 各元素（core §2.8），
/// 元素对象内联给出；错误路径相对于页自身。输出形状同 `expected` 的页项。
///
/// 与 [`document_hashes`] 的页段同校验、同结果，用于单独构造/校验一个展开页，或按契约 1 §6
/// 原位重算单页（[`document_hashes`] 一次算整篇）。
pub fn page_hashes(page: &(impl ToJson + ?Sized), contract: &str) -> Result<PageHashes> {
    let salt = salt(contract)?;
    let page = page.to_json();
    let (parts, elements) = page_input(&page, "")?;
    page_hashes_of(parts, elements, "", contract, salt)
}

/// 展开视图（vectors/README.md）的三层 hash，形状同向量的 `expected`。
///
/// 页序即 `pages` 数组顺序，页内元素序即 `elements` 数组顺序。用于整篇计算与契约 1 §6 的
/// 原位重算（按任一受支持契约重算已存的骨架与内容，结果按位置一一对应）。
/// 校验与线上请求同序（core §2.8）：文档自身字段 → 各页自身字段 → 各页的元素。
pub fn document_hashes(
    document: &(impl ToJson + ?Sized),
    contract: &str,
) -> Result<DocumentHashes> {
    let salt = salt(contract)?;
    let document = document.to_json();
    check_depth(&document, Some("pages"))?;
    let doc = mapping(&document, "", "文档")?;
    let pages = required(doc, "pages", "", "文档")?;
    let doc_parts = document_parts(doc, "", true)?;
    let Value::Array(pages) = pages else {
        return Err(validation("pages 必须是数组", "/pages"));
    };
    // 阶段 1 与阶段 2 分开：全部页的自身字段先于任何页的元素（core §2.8）
    let mut page_inputs = Vec::with_capacity(pages.len());
    for (i, raw_page) in pages.iter().enumerate() {
        let at = pointer("", &["pages", &i.to_string()]);
        let (parts, elements) = page_input(raw_page, &at)?;
        page_inputs.push((at, parts, elements));
    }
    let mut out_pages = Vec::with_capacity(page_inputs.len());
    for (at, parts, elements) in page_inputs {
        out_pages.push(page_hashes_of(parts, elements, &at, contract, salt)?);
    }
    let mut doc_parts = doc_parts;
    doc_parts.push((
        "pages",
        hash_list_part(out_pages.iter().map(|p| p.page_hash.as_str())),
    ));
    Ok(DocumentHashes {
        doc_hash: digest(&assemble(doc_parts), contract, salt),
        pages: out_pages,
    })
}

/// 测试专用：三层对象的 JCS 原像，供一致性测试逐字节比对向量的 `preimages`。
/// 不属于公开 API，不承诺稳定。
#[doc(hidden)]
pub mod __private {
    use serde_json::Value;

    use super::{document_preimage, element_preimage, mapping, page_preimage, Result};

    /// 元素对象的原像。
    pub fn element_preimage_of(element: &Value) -> Result<String> {
        element_preimage(element, "")
    }

    /// 页对象（可带展开的 `elements`，其值被忽略）配合给定子 hash 的原像。
    pub fn page_preimage_of(
        page: &Value,
        element_hashes: &Value,
        contract: &str,
    ) -> Result<String> {
        page_preimage(mapping(page, "", "页对象")?, element_hashes, contract, "")
    }

    /// 文档对象（可带展开的 `pages`，其值被忽略）配合给定子 hash 的原像。
    pub fn document_preimage_of(
        document: &Value,
        page_hashes: &Value,
        contract: &str,
    ) -> Result<String> {
        document_preimage(
            mapping(document, "", "文档对象")?,
            page_hashes,
            contract,
            "",
        )
    }
}
