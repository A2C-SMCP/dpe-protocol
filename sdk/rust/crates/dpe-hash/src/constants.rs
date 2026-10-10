//! 契约常量：消费方（服务端、dpe-sdk）直接引用，不自行维护副本。
//!
//! 测试断言它们与 `vectors/manifest.json` 一致（`file_types` / `category_content_fields`）。

/// 当前契约版本（hash 值前缀），各 hash 函数的常用契约参数。
pub const CONTRACT: &str = "dpe1";

/// 本 crate 支持的真实契约，供服务端 capabilities 的 `hash_contracts` 使用。
pub const SUPPORTED_CONTRACTS: &[&str] = &["dpe1"];

/// 假想契约 `dpe2`，**仅用于契约升级演练**（vectors/README.md）：摘要输入为 ASCII `dpe2`
/// 后接 JCS 原像字节。不在 [`SUPPORTED_CONTRACTS`] 中：只有调用方显式传 `"dpe2"` 时
/// hash 函数才接受它，[`parse_hash`](crate::parse_hash) 默认拒绝。
pub const DRILL_CONTRACT: &str = "dpe2";

/// 本 crate 认识的全部契约：真实契约加演练契约。消费方判断「契约能否由 dpe-hash 计算」时
/// 用它，不自行拼集合；它不是服务端应声明的契约（那是 [`SUPPORTED_CONTRACTS`]）。
pub const KNOWN_CONTRACTS: &[&str] = &["dpe1", DRILL_CONTRACT];

/// category 封闭枚举 → 允许的内容字段（契约 1 §4.1）：按 category 名排序，字段按表中顺序。
/// 元素对象另有必有的 `category` 与可选的 `metadata`，不在此列。按名查询用 [`content_fields`]。
pub const CATEGORY_CONTENT_FIELDS: &[(&str, &[&str])] = &[
    ("Address", TEXT),
    ("CheckBox", TEXT),
    ("CodeSnippet", TEXT),
    ("CompositeElement", TEXT),
    ("EmailAddress", TEXT),
    ("FigureCaption", TEXT),
    ("Footer", TEXT),
    ("FormKeysValues", TEXT),
    ("Formula", TEXT_HTML),
    ("Header", TEXT),
    ("Image", &["text", "blob", "mime_type"]),
    ("ListItem", TEXT),
    ("NarrativeText", TEXT),
    ("PageBreak", TEXT),
    ("PageNumber", TEXT),
    ("Table", TEXT_HTML),
    ("TableChunk", TEXT),
    ("Title", TEXT),
    ("UncategorizedText", TEXT),
    ("tfchat", TEXT),
];

const TEXT: &[&str] = &["text"];
const TEXT_HTML: &[&str] = &["text", "text_as_html"];

/// `category` 允许的内容字段（契约 1 §4.1）；不在封闭枚举内返回 `None`。
pub fn content_fields(category: &str) -> Option<&'static [&'static str]> {
    CATEGORY_CONTENT_FIELDS
        .binary_search_by(|(c, _)| (*c).cmp(category))
        .ok()
        .map(|i| CATEGORY_CONTENT_FIELDS[i].1)
}

/// file_type 推荐登记表（core.md §2.5，按规范表中顺序）。取值只是推荐：语法合法的未登记取值
/// 同样被接受（[`is_valid_file_type`](crate::is_valid_file_type)），接收方 MUST NOT 因未登记而拒收。
#[rustfmt::skip]
pub const RECOMMENDED_FILE_TYPES: &[&str] = &[
    "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json", "md", "msg",
    "ndjson", "odt", "org", "pdf", "png", "ppt", "pptx", "rst", "rtf", "tiff", "tsv", "txt",
    "wav", "xls", "xlsx", "xml", "zip", "git_repo", "java_repo", "python_repo", "javascript_repo",
    "typescript_repo", "unk", "empty", "jira_project", "jira_issue",
];
