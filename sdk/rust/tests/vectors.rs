//! 向量一致性：dpe-hash 必须逐字节通过 vectors/ 全部向量（含 dpe2 升级演练与 relations）。

use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;

use serde_json::Value;
use sha2::{Digest, Sha256};

fn vectors_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../vectors")
}

fn load_vectors() -> Vec<Value> {
    let mut entries: Vec<PathBuf> = fs::read_dir(vectors_dir())
        .expect("vectors dir")
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .filter(|p| p.file_name().is_some_and(|n| n != "manifest.json"))
        .collect();
    entries.sort();
    assert!(!entries.is_empty(), "no vectors found");
    entries
        .iter()
        .map(|p| serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap())
        .collect()
}

/// 按 vectors/README.md 的引用语法取值：`<文档键>.<路径>`。
fn resolve<'a>(computed: &'a Value, reference: &str) -> &'a Value {
    let mut node = computed;
    for part in reference.split('.') {
        node = match part.parse::<usize>() {
            Ok(i) => &node[i],
            Err(_) => &node[part],
        };
    }
    node
}

#[test]
fn document_vectors() {
    let mut count = 0;
    for vec in load_vectors() {
        if vec["kind"] != "document" {
            continue;
        }
        let name = vec["name"].as_str().unwrap();
        let first_key = vec["documents"].as_object().unwrap().keys().next().unwrap();
        let contracts: Vec<String> = vec["expected"][first_key]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        for contract in &contracts {
            let mut computed = serde_json::Map::new();
            for (key, document) in vec["documents"].as_object().unwrap() {
                let got = dpe_hash::document_hashes(document, contract)
                    .unwrap_or_else(|e| panic!("{name}/{key}/{contract}: {e}"));
                let mut expected = vec["expected"][key][contract].clone();
                // preimages 是调试字段，不属于 SDK 输出；其 SHA-256 必须等于对应 hash（契约 1 §3.1）
                if let Some(preimages) = expected.as_object_mut().unwrap().remove("preimages") {
                    let digest = |s: &str| {
                        let mut hasher = Sha256::new();
                        if *contract == "dpe2" {
                            hasher.update(b"dpe2");
                        }
                        hasher.update(s.as_bytes());
                        format!("{contract}:{:x}", hasher.finalize())
                    };
                    assert_eq!(
                        digest(preimages["root"].as_str().unwrap()),
                        got["doc_hash"].as_str().unwrap()
                    );
                    for (i, page) in got["pages"].as_array().unwrap().iter().enumerate() {
                        assert_eq!(
                            digest(preimages["pages"][i].as_str().unwrap()),
                            page["page_hash"].as_str().unwrap()
                        );
                        for (j, eh) in page["elements"].as_array().unwrap().iter().enumerate() {
                            assert_eq!(
                                digest(preimages["elements"][i][j].as_str().unwrap()),
                                eh.as_str().unwrap()
                            );
                        }
                    }
                }
                assert_eq!(&got, &expected, "{name}/{key}/{contract}");
                computed.insert(key.clone(), got);
                count += 1;
            }
            // relations 是向量要证明的规范性质，消费方一并断言（vectors/README.md）
            let computed = Value::Object(computed);
            for rel in vec["relations"].as_array().unwrap_or(&Vec::new()) {
                if let Some(refs) = rel["equal"].as_array() {
                    let values: Vec<&Value> = refs
                        .iter()
                        .map(|r| resolve(&computed, r.as_str().unwrap()))
                        .collect();
                    assert!(
                        values.windows(2).all(|w| w[0] == w[1]),
                        "{name}/{contract}: equal 不成立 {refs:?}"
                    );
                } else if let Some(refs) = rel["distinct"].as_array() {
                    let values: Vec<String> = refs
                        .iter()
                        .map(|r| resolve(&computed, r.as_str().unwrap()).to_string())
                        .collect();
                    let unique: HashSet<&String> = values.iter().collect();
                    assert_eq!(
                        unique.len(),
                        values.len(),
                        "{name}/{contract}: distinct 不成立 {refs:?}"
                    );
                }
            }
        }
    }
    assert!(count > 0);
}

#[test]
fn jcs_vectors() {
    let mut count = 0;
    for vec in load_vectors() {
        if vec["kind"] != "jcs" {
            continue;
        }
        let name = vec["name"].as_str().unwrap();
        for case in vec["cases"].as_array().unwrap() {
            let canonical = dpe_hash::jcs(&case["input"]).unwrap_or_else(|e| panic!("{name}: {e}"));
            assert_eq!(canonical, case["canonical"].as_str().unwrap(), "{name}");
            let digest = format!("{:x}", Sha256::digest(canonical.as_bytes()));
            assert_eq!(
                digest,
                case["sha256"].as_str().unwrap(),
                "{name}: {canonical:?}"
            );
            count += 1;
        }
    }
    assert!(count > 0);
}

#[test]
fn manifest_constants_match() {
    // manifest 导出的契约常量必须与 SDK 常量一致（#3 S1）；manifest 按规范声明顺序，比较集合
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(vectors_dir().join("manifest.json")).unwrap())
            .unwrap();
    let from_manifest: HashSet<&str> = manifest["file_types"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    let from_sdk: HashSet<&str> = dpe_hash::FILE_TYPES.iter().copied().collect();
    assert_eq!(from_manifest, from_sdk);
}
