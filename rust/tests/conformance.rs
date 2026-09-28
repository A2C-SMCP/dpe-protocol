mod common;

use std::collections::HashSet;

use common::{make_doc, make_doc_at};
use dpe_protocol::schema::{ElementMetadata, PyDateTime};
use dpe_protocol::testing::{check_documents, CheckOptions};

fn set(rules: &[&'static str]) -> HashSet<&'static str> {
    rules.iter().copied().collect()
}

#[tokio::test]
async fn deterministic_documents_pass() {
    let options = CheckOptions { expected_file_uri: Some("dpe://acme/handbook"), ..Default::default() };
    check_documents(&make_doc(&["a"]), &make_doc(&["a"]), options).await.assert_ok();
}

#[tokio::test]
async fn nondeterministic_metadata_is_caught() {
    let (mut first, mut second) = (make_doc(&["a"]), make_doc(&["a"]));
    // 典型错误：写入当前时间
    first.doc_metadata.created_at = Some("2026-01-01T00:00:00".parse::<PyDateTime>().unwrap());
    second.doc_metadata.created_at = Some("2026-01-01T00:00:01".parse::<PyDateTime>().unwrap());
    let report = check_documents(&first, &second, CheckOptions::default()).await;
    assert_eq!(report.rules(), set(&["deterministic", "push_roundtrip"]));
}

#[tokio::test]
async fn file_uri_and_static_violations_are_caught() {
    let mut first = make_doc_at("dpe://acme/other", &["a"]);
    first.pages[0].elements.last_mut().unwrap().ele_metadata =
        ElementMetadata { image_path: Some("r1/x.png".into()), ..Default::default() };
    let second = make_doc_at("dpe://acme/third", &["a"]);
    let options = CheckOptions { expected_file_uri: Some("dpe://acme/a"), ..Default::default() };
    let report = check_documents(&first, &second, options).await;
    assert!(report.rules().is_superset(&set(&["static", "file_uri"])));
    assert_eq!(report.issues.iter().filter(|i| i.rule == "file_uri").count(), 2);
}
