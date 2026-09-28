//! hash-contract-v1 一致性向量：期望值由 TFRobot 内核生成（`scripts/gen_vectors.py`），
//! 与 Python SDK 共享仓库根目录的 `vectors/v1`。

use std::collections::BTreeMap;
use std::path::PathBuf;

use dpe_protocol::hashing::{compute_hashes, HashStrategyRef};
use dpe_protocol::schema::Document;
use serde_json::Value;

fn vector_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../vectors/v1")
}

fn load(name: &str) -> Value {
    let raw = std::fs::read_to_string(vector_dir().join(format!("{name}.json"))).unwrap();
    serde_json::from_str(&raw).unwrap()
}

#[test]
fn all_vectors_match_kernel() {
    let manifest = load("manifest");
    let cases = manifest["cases"].as_array().unwrap();
    assert!(!cases.is_empty());
    let mut failures = Vec::new();
    for case in cases {
        let case = case.as_str().unwrap();
        let vector = load(case);
        let doc = Document::from_json(vector["input"].clone()).unwrap_or_else(|e| panic!("{case}: {e}"));
        let strategy: HashStrategyRef = vector["hash_strategy_uri"].as_str().unwrap().parse().unwrap();
        let hashes = compute_hashes(&doc, &strategy).unwrap();

        let expect = &vector["expect"];
        let content: Vec<Vec<String>> = hashes.pages.iter().map(|p| p.content_hashes.clone()).collect();
        let pages: BTreeMap<String, String> =
            hashes.pages.iter().map(|p| (p.number.to_string(), p.page_hash.clone())).collect();
        let expected_content: Vec<Vec<String>> = serde_json::from_value(expect["content_hashes"].clone()).unwrap();
        let expected_pages: BTreeMap<String, String> = serde_json::from_value(expect["page_hashes"].clone()).unwrap();
        if content != expected_content || pages != expected_pages || hashes.doc_hash != expect["doc_hash"] {
            failures.push(format!("{case}: doc_hash {} != {}", hashes.doc_hash, expect["doc_hash"]));
        }
    }
    assert!(failures.is_empty(), "vector mismatches:\n{}", failures.join("\n"));
}

#[test]
fn negative_pairs_differ() {
    let manifest = load("manifest");
    for pair in manifest["distinct_pairs"].as_array().unwrap() {
        let (a, b) = (pair[0].as_str().unwrap(), pair[1].as_str().unwrap());
        assert_ne!(load(a)["expect"]["doc_hash"], load(b)["expect"]["doc_hash"], "{a} vs {b}");
    }
}

#[test]
fn manifest_records_kernel_provenance() {
    // 向量必须能追溯到生成它的内核版本，否则无法判断是否落后于内核
    let provenance = &load("manifest")["provenance"];
    for key in ["tfrobot_version", "pydantic_version", "generated_at"] {
        assert!(provenance[key].as_str().is_some_and(|v| !v.is_empty()), "missing provenance.{key}");
    }
}
