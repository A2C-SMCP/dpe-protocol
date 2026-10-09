//! 公开入口的等价性与辅助函数：逐层、线上原像、展开视图三种入口对同一文档结果相等；
//! 类型化结构与未加类型的 JSON 值结果相同。

use std::collections::HashSet;

use dpe_hash::{
    blob_ref, children, content_hash, doc_hash, document_hashes, normalize_file_uri, object_hash,
    page_hash, parse_blob_ref, parse_hash, parse_hash_with, DocumentFields, DocumentObject,
    ElementObject, ErrorKind, ExpandedDocument, ExpandedPage, JsonObject, ObjectKind, PageFields,
    PageObject, CONTRACT, DRILL_CONTRACT, KNOWN_CONTRACTS, SUPPORTED_CONTRACTS,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn object(value: Value) -> JsonObject {
    value.as_object().unwrap().clone()
}

fn elements() -> Vec<ElementObject> {
    vec![
        ElementObject {
            category: "Title".into(),
            text: Some("标题".into()),
            metadata: Some(object(json!({"lang": "zh", "drop": null}))),
            ..Default::default()
        },
        ElementObject {
            category: "Table".into(),
            text: Some("a".into()),
            text_as_html: Some("<table/>".into()),
            ..Default::default()
        },
        ElementObject {
            category: "Image".into(),
            blob: Some(blob_ref(b"png bytes")),
            mime_type: Some("image/png".into()),
            ..Default::default()
        },
    ]
}

fn page_fields() -> PageFields {
    PageFields {
        title: Some("p1".into()),
        page_metadata: Some(object(json!({"page_label": "iv"}))),
    }
}

fn document_fields() -> DocumentFields {
    DocumentFields {
        file_type: "pdf".into(),
        title: Some("季度报告".into()),
        doc_metadata: Some(object(json!({"a": 1}))),
    }
}

fn expanded() -> ExpandedDocument {
    let els = elements();
    let doc = document_fields();
    let page = page_fields();
    ExpandedDocument {
        file_type: doc.file_type,
        title: doc.title,
        doc_metadata: doc.doc_metadata,
        pages: vec![
            ExpandedPage {
                title: page.title,
                page_metadata: page.page_metadata,
                elements: els[..2].to_vec(),
            },
            ExpandedPage {
                elements: vec![els[2].clone(), els[0].clone()],
                ..Default::default()
            },
            ExpandedPage::default(),
        ],
    }
}

#[test]
fn three_entry_points_agree() {
    for contract in [CONTRACT, DRILL_CONTRACT] {
        let expanded = expanded();
        let whole = document_hashes(&expanded, contract).unwrap();
        // 类型化结构与其 JSON 值结果相同
        assert_eq!(
            document_hashes(&serde_json::to_value(&expanded).unwrap(), contract).unwrap(),
            whole
        );

        let mut page_hashes = Vec::new();
        for (page, page_out) in expanded.pages.iter().zip(&whole.pages) {
            let hashes: Vec<String> = page
                .elements
                .iter()
                .map(|el| content_hash(el, contract).unwrap())
                .collect();
            assert_eq!(hashes, page_out.elements);
            let via_object: Vec<String> = page
                .elements
                .iter()
                .map(|el| object_hash(el, ObjectKind::Element, contract).unwrap())
                .collect();
            assert_eq!(via_object, hashes);
            let fields = PageFields {
                title: page.title.clone(),
                page_metadata: page.page_metadata.clone(),
            };
            let ph = page_hash(&fields, &hashes, contract).unwrap();
            let wire = PageObject {
                title: fields.title,
                page_metadata: fields.page_metadata,
                elements: hashes,
            };
            assert_eq!(ph, object_hash(&wire, ObjectKind::Page, contract).unwrap());
            assert_eq!(ph, page_out.page_hash);
            page_hashes.push(ph);
        }

        let dh = doc_hash(&document_fields(), &page_hashes, contract).unwrap();
        let doc = document_fields();
        let wire = DocumentObject {
            file_type: doc.file_type,
            title: doc.title,
            doc_metadata: doc.doc_metadata,
            pages: page_hashes,
        };
        assert_eq!(
            dh,
            object_hash(&wire, ObjectKind::Document, contract).unwrap()
        );
        assert_eq!(dh, whole.doc_hash);
        assert!(dh.starts_with(&format!("{contract}:")));
    }
}

#[test]
fn children_keep_order_and_duplicates() {
    let els = elements();
    let mut hashes: Vec<String> = els
        .iter()
        .map(|el| content_hash(el, CONTRACT).unwrap())
        .collect();
    hashes.push(hashes[0].clone());
    assert_eq!(
        children(&json!({"elements": hashes}), ObjectKind::Page, CONTRACT).unwrap(),
        hashes
    );
    let h = format!("dpe1:{}", "0".repeat(64));
    assert_eq!(
        children(
            &json!({"file_type": "md", "pages": [h]}),
            ObjectKind::Document,
            CONTRACT
        )
        .unwrap(),
        vec![h]
    );
    assert_eq!(
        children(&els[2], ObjectKind::Element, CONTRACT).unwrap(),
        vec![blob_ref(b"png bytes")]
    );
    assert!(children(&els[0], ObjectKind::Element, CONTRACT)
        .unwrap()
        .is_empty());
}

#[test]
fn blob_ref_and_parse() {
    let data = b"\x89PNG";
    let hex = format!("{:x}", Sha256::digest(data));
    let reference = blob_ref(data);
    assert_eq!(reference, format!("sha256:{hex}"));
    assert_eq!(parse_blob_ref(&reference).unwrap(), hex);
}

#[test]
fn parse_hash_defaults_to_supported_contracts() {
    let element = &elements()[0];
    let h = content_hash(element, CONTRACT).unwrap();
    assert_eq!(parse_hash(&h).unwrap(), ("dpe1", &h[5..]));
    let drill = content_hash(element, DRILL_CONTRACT).unwrap();
    // 默认只接受真实契约
    assert_eq!(
        parse_hash(&drill).unwrap_err().kind(),
        ErrorKind::ContractUnsupported
    );
    assert_eq!(
        parse_hash_with(&drill, &["dpe1", DRILL_CONTRACT])
            .unwrap()
            .0,
        DRILL_CONTRACT
    );
}

#[test]
fn known_contracts() {
    assert_eq!(SUPPORTED_CONTRACTS, &["dpe1"]);
    assert!(!SUPPORTED_CONTRACTS.contains(&DRILL_CONTRACT));
    let known: HashSet<&str> = KNOWN_CONTRACTS.iter().copied().collect();
    assert_eq!(known.len(), KNOWN_CONTRACTS.len());
    assert!(SUPPORTED_CONTRACTS
        .iter()
        .chain([&DRILL_CONTRACT])
        .all(|c| known.contains(c)));
    // 认识的契约都能显式用于 parse_hash，不认识的拒绝
    let element = &elements()[0];
    for contract in KNOWN_CONTRACTS {
        let h = content_hash(element, contract).unwrap();
        assert_eq!(parse_hash_with(&h, &[contract]).unwrap().0, *contract);
    }
    let h = content_hash(element, CONTRACT).unwrap();
    assert_eq!(
        parse_hash_with(&h, &["dpe9"]).unwrap_err().kind(),
        ErrorKind::ContractUnsupported
    );
}

/// core.md §2.7：null ≡ 缺省、metadata 缺省 ≡ {}、1.0 ≡ 1；"" 与 null 不等价。
#[test]
fn equivalences() {
    let h = |v: Value| object_hash(&v, ObjectKind::Element, CONTRACT).unwrap();
    let same: HashSet<String> = [
        json!({"category": "NarrativeText", "text": "x"}),
        json!({"category": "NarrativeText", "text": "x", "metadata": null}),
        json!({"category": "NarrativeText", "text": "x", "metadata": {}}),
        json!({"category": "NarrativeText", "text": "x", "metadata": {"k": null}}),
    ]
    .into_iter()
    .map(h)
    .collect();
    assert_eq!(same.len(), 1);
    assert_eq!(
        h(json!({"category": "NarrativeText", "metadata": {"n": 1.0}})),
        h(json!({"category": "NarrativeText", "metadata": {"n": 1}}))
    );
    // 解析得到的字面量同样等价（arbitrary_precision 下按字面文本判定数值）
    let parsed: Value =
        serde_json::from_str(r#"{"category": "NarrativeText", "metadata": {"n": 1.0e0}}"#).unwrap();
    assert_eq!(
        h(parsed),
        h(json!({"category": "NarrativeText", "metadata": {"n": 1}}))
    );
    assert_ne!(
        h(json!({"category": "NarrativeText", "text": ""})),
        h(json!({"category": "NarrativeText"}))
    );
}

/// 类型化结构是封闭 schema：反序列化遇到未定义字段即拒绝，而不是静默丢弃（北极星 P2）。
#[test]
fn typed_structs_reject_unknown_fields() {
    assert!(serde_json::from_value::<ElementObject>(
        json!({"category": "Title", "attributes": {}})
    )
    .is_err());
    assert!(serde_json::from_value::<PageFields>(json!({"elements": []})).is_err());
    assert!(
        serde_json::from_value::<DocumentFields>(json!({"file_type": "md", "pages": []})).is_err()
    );
    let element: ElementObject =
        serde_json::from_value(json!({"category": "Title", "text": null, "metadata": {"n": 1}}))
            .unwrap();
    // None 字段不序列化，往返后 hash 不变
    assert_eq!(
        serde_json::to_value(&element).unwrap(),
        json!({"category": "Title", "metadata": {"n": 1}})
    );
    assert_eq!(
        content_hash(&element, CONTRACT).unwrap(),
        content_hash(
            &json!({"category": "Title", "metadata": {"n": 1}}),
            CONTRACT
        )
        .unwrap()
    );
}

/// 非法输入（无 scheme、非 ASCII、坏百分号三元组）返回 DPE_VALIDATION（core.md §1.1）。
#[test]
fn normalize_file_uri_rejects_with_validation_error() {
    for bad in [
        "a/b",
        "",
        "feishu://doc/季度报告",
        "feishu://doc/%zz",
        "feishu://doc/%4",
    ] {
        let err = normalize_file_uri(bad).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::Validation, "{bad:?}");
        assert_eq!(err.code(), "DPE_VALIDATION");
    }
}

/// 三步变换的结果是不动点（core.md §1.1）：再次规范化不再变化。
#[test]
fn normalize_file_uri_is_idempotent() {
    for uri in [
        "HTTP://EX%43AMPLE.com:8080/A%2Fb?q=%7e#%2F",
        "FEISHU:Doc/A%2fB",
        "https://User@Example.com/%2e%2E",
        "http://[::FFFF:1]:80/X",
    ] {
        let once = normalize_file_uri(uri).unwrap();
        assert_eq!(normalize_file_uri(&once).unwrap(), once, "{uri:?}");
    }
}

#[test]
fn object_kind_round_trips_through_names() {
    for kind in [ObjectKind::Element, ObjectKind::Page, ObjectKind::Document] {
        assert_eq!(kind.as_str().parse::<ObjectKind>().unwrap(), kind);
        assert_eq!(kind.to_string(), kind.as_str());
    }
    assert!("expanded_document".parse::<ObjectKind>().is_err());
}
