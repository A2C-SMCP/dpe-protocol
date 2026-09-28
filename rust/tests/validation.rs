mod common;

use common::{make_doc, make_doc_at};
use dpe_protocol::check_document;
use dpe_protocol::schema::{Document, ElementMetadata};
use serde_json::json;

#[test]
fn check_document_rules() {
    assert!(check_document(&make_doc(&["a"]), None).is_empty());
    assert!(check_document(&make_doc_at("https://acme.com/x", &["a"]), None)[0].starts_with("file_uri must use"));
    assert!(check_document(&make_doc(&["a"]), Some("dpe://acme/other"))[0].contains("differs from expected"));
    let mut doc = make_doc(&["a"]);
    doc.pages[0].elements.last_mut().unwrap().ele_metadata =
        ElementMetadata { image_path: Some("r1/x.png".into()), ..Default::default() };
    assert!(check_document(&doc, None)[0].contains("image_path"));
}

#[test]
fn from_kernel_dump_strips_kernel_fields() {
    let doc = Document::from_kernel_dump(json!({
        "doc_id": 7, "doc_hash": "x", "hash_strategy_uri": "hash-strategy://default?algo_version=v1",
        "file_uri": "dpe://acme/a", "file_type": "txt", "doc_metadata": {"created_at": null},
        "pages": [{"page_id": 1, "page_hash": "y", "number": 0, "title": null,
                   "elements": [{"ele_id": 3, "seq_in_page": 0, "content_hash": "z", "text": "hello"}]}],
    }))
    .unwrap();
    assert_eq!(doc.pages[0].elements[0].text, "hello");
}

#[test]
fn make_dpe_uri_is_reexported() {
    assert_eq!(dpe_protocol::make_dpe_uri("acme", "a b").unwrap().as_str(), "dpe://acme/a%20b");
}
