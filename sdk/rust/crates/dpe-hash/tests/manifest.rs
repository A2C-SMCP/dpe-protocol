//! 契约常量与 vectors/manifest.json 互校：常量由规范产出，SDK 只能与之一致。

mod common;

use dpe_hash::{
    content_fields, CATEGORY_CONTENT_FIELDS, CONTRACT, RECOMMENDED_FILE_TYPES, SUPPORTED_CONTRACTS,
};
use serde_json::{json, Map, Value};

#[test]
fn contract() {
    assert_eq!(common::read("manifest.json")["contract"], CONTRACT);
    assert!(SUPPORTED_CONTRACTS.contains(&CONTRACT));
}

#[test]
fn file_types() {
    assert_eq!(
        json!(RECOMMENDED_FILE_TYPES),
        common::read("manifest.json")["file_types"]
    ); // 含规范表中顺序
}

#[test]
fn category_content_fields() {
    let ours: Map<String, Value> = CATEGORY_CONTENT_FIELDS
        .iter()
        .map(|(c, fields)| ((*c).to_owned(), json!(fields)))
        .collect();
    assert_eq!(
        Value::Object(ours),
        common::read("manifest.json")["category_content_fields"]
    );
    // 常量按 category 名排序且无重复：content_fields 的二分查找依赖于此
    assert!(CATEGORY_CONTENT_FIELDS.windows(2).all(|w| w[0].0 < w[1].0));
    for (category, fields) in CATEGORY_CONTENT_FIELDS {
        assert_eq!(content_fields(category), Some(*fields));
    }
    assert_eq!(content_fields("Video"), None);
}
