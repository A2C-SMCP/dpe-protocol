//! 每类违例的错误类别、规范错误码与 JSON Pointer 路径（对等 Python 的 test_errors.py）。
//!
//! Python 侧的「非 JSON 值」「非字符串键」「孤立代理项」用例在 Rust 中无法构造（`Value` 只能
//! 承载合法 JSON，`str` 不能含孤立代理项），由类型系统与解析阶段保证，不在此重复。

use dpe_hash::{
    children, children_with, content_hash, doc_hash, doc_hash_with, document_hashes, jcs,
    object_hash, object_hash_with, page_hash, page_hash_with, parse_blob_ref, parse_hash, Error,
    ErrorKind, ObjectKind, Result, DRILL_CONTRACT, SUPPORTED_CONTRACTS,
};
use serde_json::{json, Value};

fn h() -> String {
    format!("dpe1:{}", "a".repeat(64))
}

#[track_caller]
fn assert_err<T: std::fmt::Debug>(result: Result<T>, kind: ErrorKind, code: &str, path: &str) {
    let err: Error = result.expect_err("应当被拒绝");
    assert_eq!(err.kind(), kind, "{err}");
    assert_eq!(err.code(), code, "{err}");
    assert_eq!(err.path(), path, "{err}");
}

const V: &str = "DPE_VALIDATION";

#[test]
fn object_hash_errors() {
    use ErrorKind::*;
    use ObjectKind::{Document, Element, Page};
    let cases: Vec<(ErrorKind, &str, &str, Value, ObjectKind)> = vec![
        (
            CategoryUnknown,
            "DPE_CATEGORY_UNKNOWN",
            "/category",
            json!({"category": "Video"}),
            Element,
        ),
        (Validation, V, "", json!({"text": "x"}), Element),
        (
            UndefinedField,
            V,
            "/text_as_html",
            json!({"category": "NarrativeText", "text_as_html": "<p/>"}),
            Element,
        ),
        (
            Validation,
            V,
            "/text",
            json!({"category": "NarrativeText", "text": 1}),
            Element,
        ),
        (
            Validation,
            V,
            "/blob",
            json!({"category": "Image", "blob": "https://a/x.png"}),
            Element,
        ),
        (
            Validation,
            V,
            "/metadata",
            json!({"category": "Title", "metadata": [1]}),
            Element,
        ),
        (
            IntegerOutOfRange,
            V,
            "/metadata/a~1b/0",
            json!({"category": "Title", "metadata": {"a/b": [1u64 << 53]}}),
            Element,
        ),
        (
            IntegerOutOfRange,
            V,
            "/metadata/n",
            json!({"category": "Title", "metadata": {"n": u64::MAX}}),
            Element,
        ),
        (
            UndefinedField,
            V,
            "/number",
            json!({"number": 1, "elements": []}),
            Page,
        ),
        (Validation, V, "", json!({"title": "p"}), Page),
        (
            ContractUnsupported,
            "DPE_CONTRACT_UNSUPPORTED",
            "/elements/0",
            json!({"elements": ["a".repeat(64)]}),
            Page,
        ),
        (
            ContractUnsupported,
            "DPE_CONTRACT_UNSUPPORTED",
            "/elements/1",
            json!({"elements": [h(), format!("dpex:{}", "a".repeat(64))]}),
            Page,
        ),
        (
            Validation,
            V,
            "/elements/0",
            json!({"elements": ["dpe1:ABC"]}),
            Page,
        ),
        (Validation, V, "/elements/0", json!({"elements": [1]}), Page),
        (Validation, V, "/elements", json!({"elements": h()}), Page),
        (
            FileTypeInvalid,
            V,
            "/file_type",
            json!({"file_type": "Markdown", "pages": []}),
            Document,
        ),
        (Validation, V, "", json!({"file_type": "md"}), Document),
        (
            UndefinedField,
            V,
            "/attributes",
            json!({"file_type": "md", "pages": [], "attributes": {}}),
            Document,
        ),
    ];
    for (kind, code, path, obj, object_kind) in cases {
        assert_err(object_hash(&obj, object_kind, "dpe1"), kind, code, path);
        assert_err(children(&obj, object_kind, "dpe1"), kind, code, path); // children 先完整校验
    }
}

#[test]
fn float_overflow_from_parsed_literal() {
    // arbitrary_precision：1e400 在解析阶段保留，到第 4 步（JCS）才拒绝
    let obj: Value =
        serde_json::from_str(r#"{"category": "Title", "metadata": {"x": -1e400}}"#).unwrap();
    assert_err(
        object_hash(&obj, ObjectKind::Element, "dpe1"),
        ErrorKind::Validation,
        V,
        "/metadata/x",
    );
    let big: Value = serde_json::from_str(
        r#"{"category": "Title", "metadata": {"x": 123456789012345678901234567890}}"#,
    )
    .unwrap();
    assert_err(
        object_hash(&big, ObjectKind::Element, "dpe1"),
        ErrorKind::IntegerOutOfRange,
        V,
        "/metadata/x",
    );
}

#[test]
fn page_hash_rejects_elements_key() {
    let none: [&str; 0] = [];
    assert_err(
        page_hash(&json!({"elements": []}), &none, "dpe1"),
        ErrorKind::UndefinedField,
        V,
        "/elements",
    );
}

#[test]
fn doc_hash_rejects_pages_key() {
    let none: [&str; 0] = [];
    assert_err(
        doc_hash(&json!({"file_type": "md", "pages": []}), &none, "dpe1"),
        ErrorKind::UndefinedField,
        V,
        "/pages",
    );
}

#[test]
fn child_hash_paths_in_layered_api() {
    assert_err(
        page_hash(&json!({}), &[h(), "dpe1:zz".into()], "dpe1"),
        ErrorKind::Validation,
        V,
        "/elements/1",
    );
    assert_err(
        doc_hash(&json!({"file_type": "md"}), &["x"], "dpe1"),
        ErrorKind::ContractUnsupported,
        "DPE_CONTRACT_UNSUPPORTED",
        "/pages/0",
    );
}

/// dpex 页对象引用 dpe1 元素：契约混用。
#[test]
fn mixed_contracts_rejected() {
    assert_err(
        page_hash(&json!({}), &[h()], "dpex"),
        ErrorKind::Validation,
        V,
        "/elements/0",
    );
}

#[test]
fn unsupported_contract_argument() {
    assert_err(
        content_hash(&json!({"category": "Title"}), "dpe9"),
        ErrorKind::ContractUnsupported,
        "DPE_CONTRACT_UNSUPPORTED",
        "",
    );
}

#[test]
fn document_hashes_error_path() {
    let doc = json!({
        "file_type": "md",
        "pages": [
            {"elements": []},
            {"elements": [{"category": "Title"}, {"category": "Title", "metadata": {"n": -(1i64 << 60)}}]},
        ],
    });
    assert_err(
        document_hashes(&doc, "dpe1"),
        ErrorKind::IntegerOutOfRange,
        V,
        "/pages/1/elements/1/metadata/n",
    );
}

#[test]
fn parse_hash_errors() {
    use ErrorKind::*;
    for (value, kind) in [
        ("a".repeat(64), ContractUnsupported),
        (format!("sha256:{}", "a".repeat(64)), ContractUnsupported),
        (format!(":{}", "a".repeat(64)), ContractUnsupported),
        (format!("dpe1:{}", "A".repeat(64)), Validation),
        (format!("dpe1:{}", "a".repeat(63)), Validation),
    ] {
        assert_eq!(parse_hash(&value).unwrap_err().kind(), kind, "{value}");
    }
}

#[test]
fn parse_blob_ref_errors() {
    for value in [
        format!("sha256:{}", "a".repeat(63)),
        format!("dpe1:{}", "a".repeat(64)),
        format!("SHA256:{}", "a".repeat(64)),
    ] {
        assert_err(parse_blob_ref(&value), ErrorKind::Validation, V, "");
    }
}

#[test]
fn jcs_rejects_out_of_range_integers() {
    assert_err(
        jcs(&json!([-(1i64 << 53)])),
        ErrorKind::IntegerOutOfRange,
        V,
        "/0",
    );
    assert_eq!(jcs(&json!((1u64 << 53) - 1)).unwrap(), "9007199254740991");
    assert_eq!(
        jcs(&json!(-((1i64 << 53) - 1))).unwrap(),
        "-9007199254740991"
    );
    assert_eq!(
        jcs(&serde_json::from_str::<Value>("-0").unwrap()).unwrap(),
        "0"
    );
}

/// 展开视图同样按 core §2.8：文档自身字段先于各页，页自身字段先于元素。
#[test]
fn expanded_view_checks_document_fields_before_pages() {
    let bad_element = json!({"category": "Video"});
    assert_err(
        document_hashes(
            &json!({"file_type": "Markdown", "pages": [{"elements": [bad_element]}]}),
            "dpe1",
        ),
        ErrorKind::FileTypeInvalid,
        V,
        "/file_type",
    );
    assert_err(
        document_hashes(
            &json!({"file_type": "md", "pages": [{"page_metadata": {"n": 1u64 << 53}, "elements": [bad_element]}]}),
            "dpe1",
        ),
        ErrorKind::IntegerOutOfRange,
        V,
        "/pages/0/page_metadata/n",
    );
}

#[test]
fn required_field_null_is_missing() {
    assert_err(
        object_hash(&json!({"elements": null}), ObjectKind::Page, "dpe1"),
        ErrorKind::Validation,
        V,
        "",
    );
    assert_err(
        object_hash(&json!({"category": null}), ObjectKind::Element, "dpe1"),
        ErrorKind::Validation,
        V,
        "",
    );
    assert_err(
        object_hash(
            &json!({"file_type": null, "pages": []}),
            ObjectKind::Document,
            "dpe1",
        ),
        ErrorKind::Validation,
        V,
        "",
    );
}

#[test]
fn error_display_includes_path() {
    let err = object_hash(&json!({"category": "Video"}), ObjectKind::Element, "dpe1").unwrap_err();
    assert!(err.to_string().ends_with("（位置 /category）"), "{err}");
    let err = content_hash(&json!({"category": "Title"}), "dpe9").unwrap_err();
    assert!(!err.to_string().contains("位置"), "{err}");
}

/// serde_json 的 `Value` 无法承载 NaN / Infinity：Rust 中构造时已变成 null，hash 视同缺省。
/// 固定这一行为（文档已警告调用方先确认有限性），一旦 serde_json 改变语义即在此暴露。
#[test]
fn rust_constructed_non_finite_floats_become_null() {
    for x in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(jcs(&json!([x])).unwrap(), "[null]");
        assert_eq!(
            content_hash(&json!({"category": "Title", "metadata": {"x": x}}), "dpe1").unwrap(),
            content_hash(&json!({"category": "Title"}), "dpe1").unwrap()
        );
    }
}

/// 声明多契约的服务端传入的集合：受支持契约加演练契约。
fn declared() -> Vec<&'static str> {
    let mut contracts = SUPPORTED_CONTRACTS.to_vec();
    contracts.push(DRILL_CONTRACT);
    contracts
}

#[test]
fn declared_contracts_turn_drill_child_into_mixed_reference() {
    // contracts 参数：声明的契约集合让「受支持但不同契约」按混用判（core §3.1、契约 1 §5；#157，
    // 对等 Python 的 test_declared_contracts_turn_drill_child_into_mixed_reference）。默认基准下
    // dpe1 对象引用 dpex 子 hash 是「不受支持的契约」（同向量 page_child_hash_drill_contract_unsupported）
    let child = [format!("{DRILL_CONTRACT}:{}", "b".repeat(64))];
    assert_err(
        page_hash(&json!({}), &child, "dpe1"),
        ErrorKind::ContractUnsupported,
        "DPE_CONTRACT_UNSUPPORTED",
        "/elements/0",
    );
    assert_err(
        page_hash_with(&json!({}), &child, "dpe1", &declared()),
        ErrorKind::Validation,
        V,
        "/elements/0",
    );
}

#[test]
fn contracts_parameter_threads_through_all_entries() {
    // 四个带子 hash 列表的入口都接受 contracts；合法输入下 hash 不变（对等 Python 的
    // test_contracts_parameter_threads_through_all_entries）。展开视图 document_hashes 的子 hash
    // 全部自算，没有该参数
    let declared = declared();
    let drill_child = [format!("{DRILL_CONTRACT}:{}", "c".repeat(64))];
    let page = json!({"elements": drill_child});
    let results: Vec<ErrorKind> = vec![
        page_hash_with(&json!({}), &drill_child, "dpe1", &declared)
            .expect_err("page_hash_with")
            .kind(),
        doc_hash_with(&json!({"file_type": "md"}), &drill_child, "dpe1", &declared)
            .expect_err("doc_hash_with")
            .kind(),
        object_hash_with(&page, ObjectKind::Page, "dpe1", &declared)
            .expect_err("object_hash_with")
            .kind(),
        children_with(&page, ObjectKind::Page, "dpe1", &declared)
            .expect_err("children_with")
            .kind(),
    ];
    assert!(
        results.iter().all(|kind| *kind == ErrorKind::Validation),
        "{results:?}"
    );
    // 没有混用时，参数不影响结果（含 dpex 对象引用 dpe1 子的既有混用判定）
    let doc = json!({"file_type": "md", "pages": [h()]});
    assert_eq!(
        object_hash_with(&doc, ObjectKind::Document, "dpe1", &declared).unwrap(),
        object_hash(&doc, ObjectKind::Document, "dpe1").unwrap()
    );
    assert_eq!(
        children_with(&doc, ObjectKind::Document, "dpe1", &declared).unwrap(),
        children(&doc, ObjectKind::Document, "dpe1").unwrap()
    );
    assert_eq!(
        page_hash_with(&json!({}), &[h()], "dpe1", &declared).unwrap(),
        page_hash(&json!({}), &[h()], "dpe1").unwrap()
    );
    assert_eq!(
        doc_hash_with(&json!({"file_type": "md"}), &[h()], "dpe1", &declared).unwrap(),
        doc_hash(&json!({"file_type": "md"}), &[h()], "dpe1").unwrap()
    );
    assert_err(
        object_hash_with(
            &json!({"elements": [h()]}),
            ObjectKind::Page,
            DRILL_CONTRACT,
            &declared,
        ),
        ErrorKind::Validation,
        V,
        "/elements/0",
    );
}

#[test]
fn contracts_parameter_rejects_unknown_contracts() {
    // 参数校验与 parse_hash_with 同形：未知契约即拒绝，即使子 hash 列表为空（对等 Python 的
    // test_contracts_parameter_rejects_bad_sets；「单个字符串」那半在 Rust 由类型系统排除）
    let empty: [&str; 0] = [];
    for result in [
        page_hash_with(&json!({}), &empty, "dpe1", &["dpe9"]),
        doc_hash_with(&json!({"file_type": "md"}), &empty, "dpe1", &["dpe9"]),
        object_hash_with(
            &json!({"elements": []}),
            ObjectKind::Page,
            "dpe1",
            &["dpe9"],
        ),
    ] {
        assert_err(
            result,
            ErrorKind::ContractUnsupported,
            "DPE_CONTRACT_UNSUPPORTED",
            "",
        );
    }
    // 页 / 文档自身字段的错误先于 contracts 的校验报出（与 Python 同序：子 hash 列表最后处理）
    assert_err(
        doc_hash_with(&json!({}), &empty, "dpe1", &["dpe9"]),
        ErrorKind::Validation,
        V,
        "",
    );
    assert_err(
        children_with(
            &json!({"elements": []}),
            ObjectKind::Page,
            "dpe1",
            &["dpe9"],
        ),
        ErrorKind::ContractUnsupported,
        "DPE_CONTRACT_UNSUPPORTED",
        "",
    );
}
