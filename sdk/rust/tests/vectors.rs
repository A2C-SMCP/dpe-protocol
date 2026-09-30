//! 向量一致性：dpe-hash 必须逐字节通过 vectors/ 全部向量（含 dpe2 升级演练）。

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

#[test]
fn document_vectors() {
    let mut count = 0;
    for vec in load_vectors() {
        if vec["kind"] != "document" {
            continue;
        }
        let name = vec["name"].as_str().unwrap();
        for (key, document) in vec["documents"].as_object().unwrap() {
            for (contract, expected) in vec["expected"][key].as_object().unwrap() {
                let got = dpe_hash::document_hashes(document, contract)
                    .unwrap_or_else(|e| panic!("{name}/{key}/{contract}: {e}"));
                assert_eq!(&got, expected, "{name}/{key}/{contract}");
                count += 1;
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
fn manifest_reserved_keys_match() {
    // manifest 导出的保留键集合必须与 SDK 常量一致（#3 S1）
    let manifest: Value =
        serde_json::from_str(&fs::read_to_string(vectors_dir().join("manifest.json")).unwrap())
            .unwrap();
    let from_manifest: Vec<&str> = manifest["reserved_metadata_keys"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(from_manifest, dpe_hash::RESERVED_METADATA_KEYS);
}
