//! core §2.8 第 0 步第三项：对象嵌套深度上界（#94）。
//!
//! 消费 `vectors/nesting_depth.json`：对象级入口（本文件）与文本入口（`input_json` 经
//! [`parse_ijson`] 严格解析后校验）对同一条输入得出同一结论；越界输入由校验按对象自身判决，
//! 解析器的防护上限（64 + 展开视图的 4 层信封）只防栈耗尽、不判决合法性。
//!
//! vector-kind: nesting_depth

mod common;

use dpe_hash::{
    children, content_hash, doc_hash, document_hashes, object_hash, page_hash, page_hashes,
    parse_ijson, ErrorKind, ObjectKind, MAX_NESTING_DEPTH,
};
use serde_json::{json, Value};

use common::load;

/// 用例要经过的对象级入口（按对象层级），返回各自的错误（接受时为 None）。
fn entry_errors(kind: &str, obj: &Value, contract: &str) -> Vec<Option<dpe_hash::Error>> {
    if kind == "expanded_document" {
        return vec![document_hashes(obj, contract).err()];
    }
    let kind: ObjectKind = kind.parse().unwrap();
    let mut errors = vec![
        object_hash(obj, kind, contract).err(),
        children(obj, kind, contract).err(),
    ];
    if kind == ObjectKind::Element {
        errors.push(content_hash(obj, contract).err());
    }
    errors
}

#[test]
fn nesting_depth_vector_both_entries() {
    let mut checked = 0;
    for vec in load("nesting_depth") {
        assert_eq!(vec["limit"].as_u64().unwrap(), u64::from(MAX_NESTING_DEPTH));
        for case in vec["cases"].as_array().unwrap() {
            let label = case["name"].as_str().unwrap();
            let contract = case["contract"].as_str().unwrap();
            let kind = case["object_kind"].as_str().unwrap();
            let accept = case.get("accept").and_then(Value::as_bool).unwrap_or(false);
            // 文本入口：严格解析 input_json；解析期防护拒绝同样视为 DPE_VALIDATION
            let mut values = vec![case["input"].clone()];
            match parse_ijson(case["input_json"].as_str().unwrap()) {
                Ok(value) => values.push(value),
                Err(e) => {
                    assert!(!accept, "{label}: 文本入口不应拒绝：{e}");
                    assert_eq!(e.code(), case["code"], "{label}: {e}");
                    assert_eq!(e.path(), "", "{label}: {e}");
                    assert_eq!(e.kind(), ErrorKind::Validation, "{label}: {e}");
                }
            }
            for obj in &values {
                for err in entry_errors(kind, obj, contract) {
                    if accept {
                        assert!(err.is_none(), "{label}: 应为接受，实得 {err:?}");
                        continue;
                    }
                    let err = err.expect(label);
                    assert_eq!(err.code(), case["code"].as_str().unwrap(), "{label}: {err}");
                    if let Some(path) = case.get("path") {
                        assert_eq!(err.path(), path.as_str().unwrap(), "{label}: {err}");
                    }
                }
            }
            checked += 1;
        }
    }
    assert!(checked >= 9, "向量应有元素/页/文档/展开视图的边界用例");
}

/// 恰好 levels 层的容器链，最深一层是空容器。
fn nested_empty(levels: u32, leaf: Value) -> Value {
    let mut value = leaf;
    for _ in 1..levels {
        value = json!({ "k": value });
    }
    value
}

fn element_at(depth: u32) -> Value {
    json!({
        "category": "Title",
        "text": "x",
        "metadata": nested_empty(depth - 1, json!({})),
    })
}

#[test]
fn boundary_is_accepted_and_next_level_rejected() {
    // 恰好 MAX_NESTING_DEPTH 层（最深一层是空容器）接受；加一层拒绝、位置为对象自身
    content_hash(&element_at(MAX_NESTING_DEPTH), "dpe1").expect("恰好上界应接受");
    let e = content_hash(&element_at(MAX_NESTING_DEPTH + 1), "dpe1").expect_err("越界一层应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
    assert_eq!(e.path(), "");
    assert_eq!(e.kind(), ErrorKind::Validation);
}

#[test]
fn counting_convention_boundary_shapes() {
    // 页/文档的子 hash 列表自身记 1：`elements: []` 的页恰好 64 层、加一层拒绝
    let page = |depth: u32| {
        json!({
            "elements": [],
            "page_metadata": nested_empty(depth - 1, json!([])),
        })
    };
    object_hash(&page(MAX_NESTING_DEPTH), ObjectKind::Page, "dpe1").expect("恰好上界应接受");
    let e = object_hash(&page(MAX_NESTING_DEPTH + 1), ObjectKind::Page, "dpe1")
        .expect_err("越界一层应被拒绝");
    assert_eq!((e.code(), e.path()), ("DPE_VALIDATION", ""));

    let document = |depth: u32| {
        json!({
            "file_type": "md",
            "pages": [],
            "doc_metadata": nested_empty(depth - 1, json!({})),
        })
    };
    object_hash(&document(MAX_NESTING_DEPTH), ObjectKind::Document, "dpe1")
        .expect("恰好上界应接受");
    let e = object_hash(
        &document(MAX_NESTING_DEPTH + 1),
        ObjectKind::Document,
        "dpe1",
    )
    .expect_err("越界一层应被拒绝");
    assert_eq!((e.code(), e.path()), ("DPE_VALIDATION", ""));
}

#[test]
fn layered_entries_check_depth() {
    // page_hash / doc_hash / page_hashes 等逐层入口同样在第 0 步判定深度（core §2.8：每个公开入口）
    let page = json!({"page_metadata": nested_empty(MAX_NESTING_DEPTH - 1, json!({}))});
    page_hash(&page, &[] as &[&str], "dpe1").expect("恰好上界应接受");
    let over_page = json!({"page_metadata": nested_empty(MAX_NESTING_DEPTH, json!({}))});
    let e = page_hash(&over_page, &[] as &[&str], "dpe1").expect_err("越界应被拒绝");
    assert_eq!((e.code(), e.path()), ("DPE_VALIDATION", ""));

    let document = json!({
        "file_type": "md",
        "doc_metadata": nested_empty(MAX_NESTING_DEPTH - 1, json!({})),
    });
    doc_hash(&document, &[] as &[&str], "dpe1").expect("恰好上界应接受");
    let over_document = json!({
        "file_type": "md",
        "doc_metadata": nested_empty(MAX_NESTING_DEPTH, json!({})),
    });
    let e = doc_hash(&over_document, &[] as &[&str], "dpe1").expect_err("越界应被拒绝");
    assert_eq!((e.code(), e.path()), ("DPE_VALIDATION", ""));

    // 展开页入口：内联元素的深度按元素自身判定
    let ok_page = json!({"elements": [element_at(MAX_NESTING_DEPTH)]});
    page_hashes(&ok_page, "dpe1").expect("元素恰好上界应接受");
    let over_page = json!({"elements": [element_at(MAX_NESTING_DEPTH + 1)]});
    let e = page_hashes(&over_page, "dpe1").expect_err("元素越界应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
}

#[test]
fn depth_checked_before_category() {
    // 第 0 步先于第 2 步：超深且 category 未知时报 DPE_VALIDATION，不是 DPE_CATEGORY_UNKNOWN
    let over = json!({
        "category": "Video",
        "metadata": nested_empty(MAX_NESTING_DEPTH, json!({})),
    });
    let e = content_hash(&over, "dpe1").expect_err("超深应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
}

#[test]
fn expanded_view_depth_is_per_object() {
    // 展开视图的深度按每个对象自身计：内联元素恰好 64 层时文档与页不被它的层数放大
    let document = json!({
        "file_type": "md",
        "pages": [{ "elements": [element_at(MAX_NESTING_DEPTH)] }],
    });
    document_hashes(&document, "dpe1").expect("元素恰好上界的展开文档应接受");
    let over = json!({
        "file_type": "md",
        "pages": [{ "elements": [element_at(MAX_NESTING_DEPTH + 1)] }],
    });
    let e = document_hashes(&over, "dpe1").expect_err("元素越界应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
}

#[test]
fn deep_object_entry_rejects_without_recursion() {
    // 远超上界（在解析防护上限之内构造）：对象级入口迭代判定，干净地报 DPE_VALIDATION
    let mut deep = json!({ "k": {} });
    for _ in 0..200 {
        deep = json!({ "k": deep });
    }
    let element = json!({ "category": "Title", "text": "x", "metadata": deep });
    let e = content_hash(&element, "dpe1").expect_err("超深应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
    assert_eq!(e.path(), "");
}
