//! `algo_version=v1` 三层 hash（hash-contract-v1 §3–§5）。
//!
//! 本模块不对外暴露：hash 值只在同一 `hash_strategy_uri` 下可比，
//! 对外入口统一走 [`crate::hashing::compute_hashes`]。

use std::collections::BTreeMap;

use super::primitives::{concat_parts, digest, json_stable};
use crate::schema::{DocElement, Document, ElementCategory};

fn text_bytes(value: Option<&str>) -> &[u8] {
    value.unwrap_or("").as_bytes()
}

/// 按 `category` 多态分派 hash 输入。全库恰好四种形态：Image / Table / Formula / 其它。
pub(crate) fn element_hash(ele: &DocElement) -> String {
    let meta = &ele.ele_metadata;
    let text = ele.text.as_bytes();
    match ele.category {
        ElementCategory::Image => {
            // 固定优先级取第一个非空通道（与 Python `a or b or c` 一致：空串视为缺失）
            let channel = [&meta.image_url, &meta.image_base64, &meta.image_path]
                .into_iter()
                .flatten()
                .find(|s| !s.is_empty())
                .map(String::as_str);
            digest(&concat_parts(&[text, text_bytes(channel), text_bytes(meta.image_mime_type.as_deref())]))
        }
        ElementCategory::Table | ElementCategory::Formula => {
            digest(&concat_parts(&[text, text_bytes(meta.text_as_html.as_deref())]))
        }
        _ => digest(&concat_parts(&[text])),
    }
}

/// 页标题 + element `content_hash` 序列（数组顺序）。
pub(crate) fn page_hash(title: Option<&str>, content_hashes: &[String]) -> String {
    let mut parts: Vec<&[u8]> = vec![text_bytes(title)];
    parts.extend(content_hashes.iter().map(|h| h.as_bytes()));
    digest(&concat_parts(&parts))
}

/// 文档元信息 + 按 `page.number` 升序排列的 `page_hash` 序列。
/// `title` 位当前恒为空串（内核 `Document` 未声明 `title`），但占位必须保留。
pub(crate) fn doc_hash(doc: &Document, page_hashes_by_number: &BTreeMap<i64, String>) -> String {
    let file_uri = doc.file_uri.as_str();
    let meta_json = json_stable(&doc.doc_metadata.canonical_value());
    let mut parts: Vec<&[u8]> = vec![file_uri.as_bytes(), doc.file_type.as_str().as_bytes(), b"", meta_json.as_bytes()];
    parts.extend(page_hashes_by_number.values().map(|h| h.as_bytes()));
    digest(&concat_parts(&parts))
}
