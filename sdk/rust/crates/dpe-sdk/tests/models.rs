//! 数据模型（#20）：向量一致、拒绝类向量的错误码与位置、内容等价与构造语义。
//!
//! Python 侧的「非 UTF-8」用例在 Rust 中无法构造（`&str` 保证 UTF-8），由类型系统保证，
//! 不在此重复。

mod common;

use dpe_hash::{content_hash, ErrorKind, JsonObject};
use dpe_sdk::{
    DocumentObject, ElementObject, ExpandedDocument, ExpandedPage, PageObject, CONTRACT,
    DRILL_CONTRACT,
};
use serde_json::{json, Value};

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

fn object(value: Value) -> JsonObject {
    value.as_object().unwrap().clone()
}

fn without(obj: &Value, field: &str) -> Value {
    let mut map = obj.as_object().unwrap().clone();
    map.remove(field);
    Value::Object(map)
}

/// 全部 document 向量用例：`(标签, 文档输入, 契约, expected（去掉 preimages）)`。
#[allow(clippy::type_complexity)]
fn document_cases() -> Vec<(String, Value, String, Value)> {
    let mut cases = Vec::new();
    for vec in common::load("document") {
        let name = vec["name"].as_str().unwrap().to_owned();
        for (key, document) in vec["documents"].as_object().unwrap() {
            for (contract, expected) in vec["expected"][key].as_object().unwrap() {
                cases.push((
                    format!("{name}/{key}/{contract}"),
                    document.clone(),
                    contract.clone(),
                    without(expected, "preimages"),
                ));
            }
        }
    }
    assert!(!cases.is_empty());
    cases
}

// ---------------------------------------------------------------------------
// document 向量
// ---------------------------------------------------------------------------

#[test]
fn document_vectors_expanded() {
    for (label, document, contract, expected) in document_cases() {
        let model = ExpandedDocument::from_value(&document, &contract)
            .unwrap_or_else(|e| panic!("{label}: {e}"));
        assert_eq!(
            serde_json::to_value(model.hashes(&contract).unwrap()).unwrap(),
            expected,
            "{label}"
        );
        assert_eq!(
            model.doc_hash(&contract).unwrap(),
            expected["doc_hash"],
            "{label}"
        );
        // 无损往返：序列化结果就是向量输入
        assert_eq!(serde_json::to_value(&model).unwrap(), document, "{label}");
        // JSON 文本入口（严格 I-JSON 解析）与值入口等价
        let from_text = ExpandedDocument::parse(&document.to_string(), &contract)
            .unwrap_or_else(|e| panic!("{label}: {e}"));
        assert_eq!(from_text, model, "{label}");
    }
}

#[test]
fn document_vectors_wire_objects() {
    // 线上对象逐层上溯：ElementObject → PageObject → DocumentObject。
    for (label, document, contract, expected) in document_cases() {
        let mut page_hashes = Vec::new();
        for (i, page) in document["pages"].as_array().unwrap().iter().enumerate() {
            let elements: Vec<ElementObject> = page["elements"]
                .as_array()
                .unwrap()
                .iter()
                .map(|el| {
                    ElementObject::from_value(el, &contract)
                        .unwrap_or_else(|e| panic!("{label}: {e}"))
                })
                .collect();
            let hashes: Vec<String> = elements
                .iter()
                .map(|el| el.content_hash(&contract).unwrap())
                .collect();
            assert_eq!(json!(hashes), expected["pages"][i]["elements"], "{label}");
            let round_tripped: Vec<Value> = elements
                .iter()
                .map(|el| serde_json::to_value(el).unwrap())
                .collect();
            assert_eq!(json!(round_tripped), page["elements"], "{label}");
            let mut wire = page.as_object().unwrap().clone();
            wire.insert("elements".into(), json!(hashes));
            let wire_page = PageObject::from_value(&Value::Object(wire), &contract)
                .unwrap_or_else(|e| panic!("{label}: {e}"));
            page_hashes.push(wire_page.page_hash(&contract).unwrap());
        }
        let mut wire = document.as_object().unwrap().clone();
        wire.insert("pages".into(), json!(page_hashes));
        let wire_document = DocumentObject::from_value(&Value::Object(wire), &contract)
            .unwrap_or_else(|e| panic!("{label}: {e}"));
        assert_eq!(
            wire_document.doc_hash(&contract).unwrap(),
            expected["doc_hash"],
            "{label}"
        );
    }
}

#[test]
fn page_hash_via_standalone_page() {
    // 单独构造的 ExpandedPage 与经 ExpandedDocument 构造的页相同（错误位置与结果都相对页）。
    for (label, document, contract, _) in document_cases() {
        let via_document = ExpandedDocument::from_value(&document, &contract)
            .unwrap_or_else(|e| panic!("{label}: {e}"));
        for (i, page) in document["pages"].as_array().unwrap().iter().enumerate() {
            let standalone = ExpandedPage::from_value(page, &contract)
                .unwrap_or_else(|e| panic!("{label}: {e}"));
            assert_eq!(standalone, via_document.pages[i], "{label}");
        }
    }
}

// ---------------------------------------------------------------------------
// 拒绝类向量（core §2.8）
// ---------------------------------------------------------------------------

/// 按 `object_kind` 分派到对应模型的构造入口；`input_json` 用例走 parse（含 I-JSON 解析阶段）。
fn construct_case(kind: &str, case: &Value, contract: &str) -> dpe_hash::Result<()> {
    macro_rules! construct {
        ($ty:ty) => {
            match case.get("input_json").and_then(Value::as_str) {
                Some(text) => <$ty>::parse(text, contract).map(|_| ()),
                None => <$ty>::from_value(&case["input"], contract).map(|_| ()),
            }
        };
    }
    match kind {
        "element" => construct!(ElementObject),
        "page" => construct!(PageObject),
        "document" => construct!(DocumentObject),
        "expanded_document" => construct!(ExpandedDocument),
        other => panic!("未知 object_kind：{other}"),
    }
}

#[test]
fn invalid_vectors() {
    // 每条都以一致的错误码被拒；声明了 `path` 的用例，位置也一致。
    let mut seen = 0;
    for vec in common::load("invalid") {
        for case in vec["cases"].as_array().unwrap() {
            let label = format!(
                "{}/{}",
                vec["name"].as_str().unwrap(),
                case["name"].as_str().unwrap()
            );
            let contract = case["contract"].as_str().unwrap();
            let kind = case["object_kind"].as_str().unwrap();
            let err = construct_case(kind, case, contract).expect_err(&label);
            assert_eq!(err.code(), case["code"].as_str().unwrap(), "{label}: {err}");
            if let Some(path) = case.get("path") {
                assert_eq!(err.path(), path.as_str().unwrap(), "{label}: {err}");
            }
            seen += 1;
        }
    }
    assert!(seen >= 52);
}

#[test]
fn invalid_vectors_page_standalone() {
    // 展开视图的页错误：单独构造 ExpandedPage 时位置相对于页本身。
    for vec in common::load("invalid") {
        for case in vec["cases"].as_array().unwrap() {
            if case["object_kind"] != "expanded_document" {
                continue;
            }
            let Some(path) = case.get("path").and_then(Value::as_str) else {
                continue; // 未声明 path 的用例校验顺序跨页，单页语义下不适用
            };
            // 假设带 path 的用例都发生在页 0（当前向量如此，与 Python 侧同款）；将来若有
            // /pages/1/... 的用例，这里需按 path 的页下标取页
            let label = case["name"].as_str().unwrap();
            let contract = case["contract"].as_str().unwrap();
            let err = ExpandedPage::from_value(&case["input"]["pages"][0], contract)
                .map(|_| ())
                .expect_err(label);
            assert_eq!(err.code(), case["code"].as_str().unwrap(), "{label}: {err}");
            assert_eq!(
                err.path(),
                path.strip_prefix("/pages/0").unwrap_or(path),
                "{label}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Issue 要求的非法输入（每类各有测试）
// ---------------------------------------------------------------------------

#[test]
fn undefined_field_rejected() {
    let err = PageObject::from_value(&json!({"elements": [], "number": 1}), CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::UndefinedField);
    assert_eq!(err.path(), "/number");
}

#[test]
fn category_disallowed_field_rejected_even_if_null() {
    for value in [json!("<p/>"), Value::Null] {
        let err = ElementObject::from_value(
            &json!({"category": "NarrativeText", "text_as_html": value}),
            CONTRACT,
        )
        .unwrap_err();
        assert_eq!(err.code(), "DPE_VALIDATION");
        assert_eq!(err.path(), "/text_as_html");
    }
}

#[test]
fn unknown_category_rejected() {
    let err = ElementObject::from_value(&json!({"category": "Video"}), CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CategoryUnknown);
    assert_eq!(err.path(), "/category");
}

#[test]
fn unknown_file_type_rejected() {
    let err =
        ExpandedDocument::from_value(&json!({"file_type": "markdown", "pages": []}), CONTRACT)
            .unwrap_err();
    assert_eq!(err.kind(), ErrorKind::FileTypeUnknown);
    assert_eq!(err.code(), "DPE_VALIDATION");
    assert_eq!(err.path(), "/file_type");
}

#[test]
fn integer_out_of_range_rejected() {
    for n in [9007199254740992i64, -9007199254740992i64] {
        let err = ElementObject::from_value(
            &json!({"category": "Title", "metadata": {"id": [n]}}),
            CONTRACT,
        )
        .unwrap_err();
        assert_eq!(err.kind(), ErrorKind::IntegerOutOfRange);
        assert_eq!(err.path(), "/metadata/id/0");
    }
}

#[test]
fn integer_at_bound_accepted() {
    let bound = 9007199254740991i64; // 2^53−1
    let el = ElementObject::from_value(
        &json!({"category": "Title", "metadata": {"a": bound, "b": -bound}}),
        CONTRACT,
    )
    .unwrap();
    let metadata = el.metadata.flatten().expect("metadata 已给出的");
    assert_eq!(metadata["a"], json!(bound));
    assert_eq!(metadata["b"], json!(-bound));
}

#[test]
fn json_not_ijson_rejected() {
    for text in [
        r#"{"category": "Title", "category": "Title"}"#, // 重复键
        r#"{"category": "Title", "metadata": {"x": NaN}}"#, // 非 JSON 数值
        r#"{"category": "Title""#,                       // 坏 JSON
        r#"{"category": "Title", "text": "\ud800"}"#,    // 孤立代理项
    ] {
        let err = ElementObject::parse(text, CONTRACT).expect_err(text);
        assert_eq!(err.code(), "DPE_VALIDATION", "{text}");
        assert_eq!(err.path(), "", "{text}");
    }
}

// ---------------------------------------------------------------------------
// 内容等价（core §2.7）
// ---------------------------------------------------------------------------

#[test]
fn null_equivalent_to_missing() {
    let a = ElementObject::from_value(
        &json!({"category": "Title", "text": null, "metadata": null}),
        CONTRACT,
    )
    .unwrap();
    let b = ElementObject::from_value(&json!({"category": "Title"}), CONTRACT).unwrap();
    let c =
        ElementObject::from_value(&json!({"category": "Title", "metadata": {}}), CONTRACT).unwrap();
    let hash = a.content_hash(CONTRACT).unwrap();
    assert_eq!(hash, b.content_hash(CONTRACT).unwrap());
    assert_eq!(hash, c.content_hash(CONTRACT).unwrap());
    // 原样表示保留：显式的 null 照样输出，未给出的字段不输出
    assert_eq!(
        serde_json::to_value(&a).unwrap(),
        json!({"category": "Title", "text": null, "metadata": null})
    );
    assert_eq!(
        serde_json::to_value(&b).unwrap(),
        json!({"category": "Title"})
    );
}

#[test]
fn three_state_null_distinction() {
    // 缺省 / 显式 null / 值三态在模型里可区分（原样表示），hash 只看内容。
    let missing = ElementObject::from_value(&json!({"category": "Title"}), CONTRACT).unwrap();
    let explicit =
        ElementObject::from_value(&json!({"category": "Title", "text": null}), CONTRACT).unwrap();
    assert_eq!(missing.text, None);
    assert_eq!(explicit.text, Some(None));
    assert_ne!(missing, explicit);
    assert_eq!(
        missing.content_hash(CONTRACT).unwrap(),
        explicit.content_hash(CONTRACT).unwrap()
    );
    let valued =
        ElementObject::from_value(&json!({"category": "Title", "text": "x"}), CONTRACT).unwrap();
    assert_eq!(valued.text, Some(Some("x".to_owned())));
}

#[test]
fn empty_string_distinct_from_null() {
    let empty =
        ElementObject::from_value(&json!({"category": "Title", "text": ""}), CONTRACT).unwrap();
    let null =
        ElementObject::from_value(&json!({"category": "Title", "text": null}), CONTRACT).unwrap();
    assert_ne!(
        empty.content_hash(CONTRACT).unwrap(),
        null.content_hash(CONTRACT).unwrap()
    );
}

#[test]
fn document_equivalence() {
    let a = ExpandedDocument::from_value(
        &json!({"file_type": "md", "title": null, "pages": [{"elements": [], "page_metadata": null}]}),
        CONTRACT,
    )
    .unwrap();
    let b = ExpandedDocument::from_value(
        &json!({"file_type": "md", "doc_metadata": {}, "pages": [{"elements": []}]}),
        CONTRACT,
    )
    .unwrap();
    assert_eq!(a.doc_hash(CONTRACT).unwrap(), b.doc_hash(CONTRACT).unwrap());
    assert_ne!(a, b); // == 只比较结构，内容等价由 hash 判定
}

// ---------------------------------------------------------------------------
// 构造语义
// ---------------------------------------------------------------------------

#[test]
fn typed_assembly() {
    // 类型化装配：ElementObject → ExpandedPage → ExpandedDocument，结果与值路径一致。
    let el =
        ElementObject::from_value(&json!({"category": "NarrativeText", "text": "a"}), CONTRACT)
            .unwrap();
    let page = ExpandedPage {
        elements: vec![el.clone()],
        ..Default::default()
    };
    let doc = ExpandedDocument {
        file_type: "txt".into(),
        pages: vec![page],
        ..Default::default()
    };
    let hashes = doc.hashes(CONTRACT).unwrap();
    assert_eq!(
        hashes.pages[0].elements,
        [el.content_hash(CONTRACT).unwrap()]
    );
    assert_eq!(
        hashes.doc_hash,
        ExpandedDocument::from_value(
            &json!({"file_type": "txt", "pages": [{"elements": [{"category": "NarrativeText", "text": "a"}]}]}),
            CONTRACT,
        )
        .unwrap()
        .doc_hash(CONTRACT)
        .unwrap()
    );
}

#[test]
fn literal_construction_revalidated_by_hash() {
    // 字面量构造不被阻止，但 hash 方法经 dpe-hash 重新校验，非法对象同样被拒。
    let bad = ElementObject {
        category: "Video".into(),
        ..Default::default()
    };
    let err = bad.content_hash(CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CategoryUnknown);
    assert_eq!(err.path(), "/category");
}

#[test]
fn metadata_change_reflected_in_hash() {
    // hash 不缓存：改动后按新内容计算（内容等价之外的差异即内容变化）。
    let mut el = ElementObject::from_value(
        &json!({"category": "Title", "metadata": {"a": 1}}),
        CONTRACT,
    )
    .unwrap();
    let before = el.content_hash(CONTRACT).unwrap();
    el.metadata = Some(Some(object(json!({"a": 2}))));
    let after = el.content_hash(CONTRACT).unwrap();
    assert_ne!(before, after);
    assert_eq!(
        after,
        content_hash(
            &json!({"category": "Title", "metadata": {"a": 2}}),
            CONTRACT
        )
        .unwrap()
    );
}

#[test]
fn wire_contract_from_argument() {
    // 子 hash 按显式 contract 校验：dpe2 只在显式传入时接受。
    let child = format!("dpe2:{}", "0".repeat(64));
    let err = PageObject::from_value(&json!({"elements": [child.clone()]}), CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ContractUnsupported);
    let page = PageObject::from_value(&json!({"elements": [child]}), DRILL_CONTRACT).unwrap();
    assert!(page.page_hash(DRILL_CONTRACT).unwrap().starts_with("dpe2:"));
}

// ---------------------------------------------------------------------------
// serde 结构层与其余入口（模块文档承诺的行为）
// ---------------------------------------------------------------------------

#[test]
fn serde_rejects_unknown_fields() {
    // 结构层解析（deny_unknown_fields）：五个类型都拒绝未知字段；不做 §2.8 校验
    assert!(serde_json::from_str::<ElementObject>(r#"{"category": "Title", "nope": 1}"#).is_err());
    assert!(serde_json::from_str::<PageObject>(r#"{"elements": [], "number": 1}"#).is_err());
    assert!(
        serde_json::from_str::<DocumentObject>(r#"{"file_type": "md", "pages": [], "x": 1}"#)
            .is_err()
    );
    assert!(serde_json::from_str::<ExpandedPage>(r#"{"elements": [], "x": 1}"#).is_err());
    assert!(serde_json::from_str::<ExpandedDocument>(
        r#"{"file_type": "md", "pages": [], "x": 1}"#
    )
    .is_err());
    // 结构合法但违反规范的值可以通过反序列化，规范校验在 hash 方法处兜底
    let element: ElementObject = serde_json::from_str(r#"{"category": "Video"}"#).unwrap();
    let err = element.content_hash(CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CategoryUnknown);
}

#[test]
fn parse_entry_covers_all_models() {
    // 其余三个模型的 JSON 文本入口与值入口等价
    assert_eq!(
        PageObject::parse(r#"{"title": "p", "elements": []}"#, CONTRACT).unwrap(),
        PageObject::from_value(&json!({"title": "p", "elements": []}), CONTRACT).unwrap()
    );
    assert_eq!(
        DocumentObject::parse(r#"{"file_type": "md", "pages": []}"#, CONTRACT).unwrap(),
        DocumentObject::from_value(&json!({"file_type": "md", "pages": []}), CONTRACT).unwrap()
    );
    let text = r#"{"title": "p", "elements": [{"category": "Title", "text": "t"}]}"#;
    assert_eq!(
        ExpandedPage::parse(text, CONTRACT).unwrap(),
        ExpandedPage::from_value(
            &json!({"title": "p", "elements": [{"category": "Title", "text": "t"}]}),
            CONTRACT
        )
        .unwrap()
    );
}

#[test]
fn literal_assembly_validated_in_spec_order() {
    // 字面量装配的实例随整树重新校验：错误位置相对于文档（对等 Python 的
    // test_mixed_instances_validated_in_spec_order）
    let doc = ExpandedDocument {
        file_type: "txt".into(),
        pages: vec![
            ExpandedPage::default(),
            ExpandedPage {
                elements: vec![ElementObject {
                    category: "Video".into(),
                    ..Default::default()
                }],
                ..Default::default()
            },
        ],
        ..Default::default()
    };
    let err = doc.hashes(CONTRACT).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::CategoryUnknown);
    assert_eq!(err.path(), "/pages/1/elements/0/category");
}
