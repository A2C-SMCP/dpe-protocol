//! 一致性向量：dpe-hash 逐字节通过 vectors/ 全部向量，含 dpe2 升级演练、relations 与 preimages。

mod common;

use std::collections::{BTreeSet, HashSet};

use dpe_hash::__private::{document_preimage_of, element_preimage_of, page_preimage_of};
use dpe_hash::{
    children, content_hash, doc_hash, document_hashes, jcs, normalize_file_uri, object_hash,
    page_hash, DocumentHashes, ObjectKind, Result, DRILL_CONTRACT,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use common::{load, parse_strict, read};

fn without(obj: &Value, field: &str) -> Value {
    let mut map: Map<String, Value> = obj.as_object().unwrap().clone();
    map.remove(field);
    Value::Object(map)
}

fn digest(contract: &str, preimage: &str) -> String {
    let salt: &[u8] = if contract == DRILL_CONTRACT {
        b"dpe2"
    } else {
        b""
    };
    let mut hasher = Sha256::new();
    hasher.update(salt);
    hasher.update(preimage.as_bytes());
    format!("{contract}:{:x}", hasher.finalize())
}

/// 按 vectors/README.md 的引用语法取值：`<文档键>.<路径>`。
fn resolve<'a>(computed: &'a Value, reference: &str) -> &'a Value {
    reference
        .split('.')
        .fold(computed, |node, part| match node {
            Value::Array(items) => &items[part.parse::<usize>().unwrap()],
            _ => &node[part],
        })
}

/// 逐层上溯：content_hash → page_hash(页字段, 子 hash) → doc_hash(文档字段, 子 hash)。
fn layered(document: &Value, contract: &str) -> Result<DocumentHashes> {
    let mut pages = Vec::new();
    for page in document["pages"].as_array().unwrap() {
        let hashes = page["elements"]
            .as_array()
            .unwrap()
            .iter()
            .map(|el| content_hash(el, contract))
            .collect::<Result<Vec<_>>>()?;
        let page_hash = page_hash(&without(page, "elements"), &hashes, contract)?;
        pages.push(json!({"page_hash": page_hash, "elements": hashes}));
    }
    let page_hashes: Vec<&str> = pages
        .iter()
        .map(|p| p["page_hash"].as_str().unwrap())
        .collect();
    let doc_hash = doc_hash(&without(document, "pages"), &page_hashes, contract)?;
    Ok(serde_json::from_value(json!({"doc_hash": doc_hash, "pages": pages})).unwrap())
}

/// 线上原像：每层对象带子 hash 列表，经 object_hash 计算。
fn wire(document: &Value, contract: &str) -> Result<DocumentHashes> {
    let mut pages = Vec::new();
    for page in document["pages"].as_array().unwrap() {
        let hashes = page["elements"]
            .as_array()
            .unwrap()
            .iter()
            .map(|el| object_hash(el, ObjectKind::Element, contract))
            .collect::<Result<Vec<_>>>()?;
        let mut wire_page = page.clone();
        wire_page["elements"] = json!(hashes);
        let page_hash = object_hash(&wire_page, ObjectKind::Page, contract)?;
        pages.push(json!({"page_hash": page_hash, "elements": hashes}));
    }
    let mut wire_doc = document.clone();
    wire_doc["pages"] = pages.iter().map(|p| p["page_hash"].clone()).collect();
    let doc_hash = object_hash(&wire_doc, ObjectKind::Document, contract)?;
    Ok(serde_json::from_value(json!({"doc_hash": doc_hash, "pages": pages})).unwrap())
}

/// 向量给出的原像：其摘要等于对应 hash，且本实现规范化出的原像与之逐字节相同。
fn check_preimages(
    document: &Value,
    got: &DocumentHashes,
    preimages: &Value,
    contract: &str,
    label: &str,
) {
    let doc_pre = preimages["document"].as_str().unwrap();
    assert_eq!(digest(contract, doc_pre), got.doc_hash, "{label}");
    let page_hashes: Value = got.pages.iter().map(|p| json!(p.page_hash)).collect();
    assert_eq!(
        document_preimage_of(document, &page_hashes, contract).unwrap(),
        doc_pre,
        "{label}"
    );
    let pages = document["pages"].as_array().unwrap();
    let page_pres = preimages["pages"].as_array().unwrap();
    let element_pres = preimages["elements"].as_array().unwrap();
    assert_eq!(pages.len(), page_pres.len(), "{label}");
    assert_eq!(pages.len(), element_pres.len(), "{label}");
    for (i, page) in pages.iter().enumerate() {
        let page_got = &got.pages[i];
        let page_pre = page_pres[i].as_str().unwrap();
        assert_eq!(
            digest(contract, page_pre),
            page_got.page_hash,
            "{label}/pages/{i}"
        );
        assert_eq!(
            page_preimage_of(page, &json!(page_got.elements), contract).unwrap(),
            page_pre,
            "{label}/pages/{i}"
        );
        let element_pres: Vec<&str> = element_pres[i]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p.as_str().unwrap())
            .collect();
        let digests: Vec<String> = element_pres.iter().map(|p| digest(contract, p)).collect();
        assert_eq!(digests, page_got.elements, "{label}/pages/{i}");
        let ours: Vec<String> = page["elements"]
            .as_array()
            .unwrap()
            .iter()
            .map(|el| element_preimage_of(el).unwrap())
            .collect();
        assert_eq!(ours, element_pres, "{label}/pages/{i}");
    }
}

#[test]
fn document_vectors() {
    let mut checked = 0;
    for vec in load("document") {
        let name = vec["name"].as_str().unwrap();
        let expected_all = vec["expected"].as_object().unwrap();
        let contracts: BTreeSet<&String> = expected_all
            .values()
            .flat_map(|exp| exp.as_object().unwrap().keys())
            .collect();
        for contract in contracts {
            let mut computed = Map::new();
            for (key, document) in vec["documents"].as_object().unwrap() {
                let label = format!("{name}/{key}/{contract}");
                let expected = &expected_all[key][contract];
                let preimages = expected.get("preimages");
                let expected: DocumentHashes =
                    serde_json::from_value(without(expected, "preimages")).unwrap();
                let got =
                    document_hashes(document, contract).unwrap_or_else(|e| panic!("{label}: {e}"));
                assert_eq!(got, expected, "{label}");
                assert_eq!(layered(document, contract).unwrap(), expected, "{label}");
                assert_eq!(wire(document, contract).unwrap(), expected, "{label}");
                if let Some(preimages) = preimages {
                    check_preimages(document, &got, preimages, contract, &label);
                }
                computed.insert(key.clone(), serde_json::to_value(&got).unwrap());
                checked += 1;
            }
            // relations 是向量要证明的规范性质，消费方一并断言（vectors/README.md）
            let computed = Value::Object(computed);
            for rel in vec
                .get("relations")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let (op, refs) = rel.as_object().unwrap().iter().next().unwrap();
                let values: Vec<&Value> = refs
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| resolve(&computed, r.as_str().unwrap()))
                    .collect();
                let distinct: HashSet<String> = values.iter().map(|v| v.to_string()).collect();
                match op.as_str() {
                    "equal" => assert_eq!(distinct.len(), 1, "{name}/{contract}: equal {refs}"),
                    "distinct" => {
                        assert_eq!(
                            distinct.len(),
                            values.len(),
                            "{name}/{contract}: distinct {refs}"
                        )
                    }
                    other => panic!("未知的 relation：{other}"),
                }
            }
        }
    }
    assert!(checked > 0);
}

#[test]
fn vectors_cover_drill_contract() {
    let drills = load("document")
        .into_iter()
        .filter(|v| {
            v["expected"]
                .as_object()
                .unwrap()
                .values()
                .any(|exp| exp.get(DRILL_CONTRACT).is_some())
        })
        .count();
    assert!(drills > 0, "向量应包含 dpe2 升级演练（契约 1 §6）");
}

#[test]
fn jcs_vectors() {
    for name in ["jcs_basic", "jcs_numbers"] {
        let vec = read(&format!("{name}.json"));
        for case in vec["cases"].as_array().unwrap() {
            let canonical = jcs(&case["input"]).unwrap();
            assert_eq!(
                canonical,
                case["canonical"].as_str().unwrap(),
                "{name}: {}",
                case["input"]
            );
            assert_eq!(
                format!("{:x}", Sha256::digest(canonical.as_bytes())),
                case["sha256"].as_str().unwrap()
            );
        }
    }
}

#[test]
fn all_jcs_vectors_listed() {
    let names: BTreeSet<String> = load("jcs")
        .iter()
        .map(|v| v["name"].as_str().unwrap().into())
        .collect();
    assert_eq!(
        names,
        BTreeSet::from(["jcs_basic".into(), "jcs_numbers".into()])
    );
}

/// vectors/ 出现的 kind 是封闭集合，每个都必须有消费方；新增 kind 漏写消费测试时在此失败。
///
/// `pattern`（connector 契约 §4.1.1 的正则子集）目前的消费方在 Python SDK 的 dpe-run
/// （`sdk/python/packages/dpe-sdk/tests/test_run_pattern.py`）；Rust SDK 尚无清单校验实现，
/// 将来实现 connector 时在本文件补消费测试（挂账 #85）。
#[test]
fn all_vector_kinds_have_consumers() {
    let kinds: BTreeSet<String> = common::all_vectors()
        .iter()
        .map(|v| v["kind"].as_str().unwrap().into())
        .collect();
    // 已消费的 kind（本 crate 的测试逐类断言）与显式登记的待办。本断言是「变更探测器」：
    // 新增/删除 kind 会在此失败；Rust 侧补齐 pattern 消费时，请把 "pattern" 从 pending 移入
    // consumed 并同步下一行字面量，否则测试会失败。
    let consumed: BTreeSet<String> = ["document", "jcs", "uri", "invalid"]
        .map(String::from)
        .into();
    let pending: BTreeSet<String> = ["pattern"].map(String::from).into();
    let expected: BTreeSet<String> = consumed.union(&pending).cloned().collect();
    assert_eq!(kinds, expected);
    assert_eq!(pending, ["pattern"].map(String::from).into());
    assert!(
        consumed.is_disjoint(&pending),
        "已消费与待办必须互斥：待办补齐消费后要移出 pending"
    );
}

/// file_uri 语法规范化（core.md §1.1）：逐例输出一致；非法输入被拒且错误码一致；结果幂等。
#[test]
fn uri_vectors() {
    for vec in load("uri") {
        let name = vec["name"].as_str().unwrap();
        for case in vec["cases"].as_array().unwrap() {
            let input = case["input"].as_str().unwrap();
            let normalized =
                normalize_file_uri(input).unwrap_or_else(|e| panic!("{name}: {input:?}: {e}"));
            assert_eq!(
                normalized,
                case["normalized"].as_str().unwrap(),
                "{name}: {input:?}"
            );
            assert_eq!(
                normalize_file_uri(&normalized).unwrap(),
                normalized,
                "{name}: {input:?} 的规范化结果不是不动点"
            );
        }
        for case in vec["invalid_cases"].as_array().unwrap() {
            let input = case["input"].as_str().unwrap();
            let err = normalize_file_uri(input).expect_err(input);
            assert_eq!(
                err.code(),
                case["code"].as_str().unwrap(),
                "{name}: {input:?}"
            );
        }
    }
}

/// 拒绝类向量（core.md §2.8）：每条都被拒绝且错误码一致；声明了 path 的用例，位置也一致。
///
/// `input_json` 用例按 vectors/README.md 以拒绝重复键的严格解析器读取：解析阶段拒绝即视为
/// `DPE_VALIDATION`、位置 `""`（孤立代理项在 Rust 中同样止于解析阶段）。
#[test]
fn invalid_vectors() {
    let mut checked = 0;
    for vec in load("invalid") {
        for case in vec["cases"].as_array().unwrap() {
            let label = format!(
                "{}/{}",
                vec["name"].as_str().unwrap(),
                case["name"].as_str().unwrap()
            );
            let obj = match case.get("input_json") {
                Some(text) => match parse_strict(text.as_str().unwrap()) {
                    Ok(obj) => obj,
                    Err(common::Rejected) => {
                        assert_eq!(case["code"], "DPE_VALIDATION", "{label}");
                        assert_eq!(
                            case.get("path").and_then(Value::as_str).unwrap_or(""),
                            "",
                            "{label}"
                        );
                        checked += 1;
                        continue;
                    }
                },
                None => case["input"].clone(),
            };
            let contract = case["contract"].as_str().unwrap();
            let kind = case["object_kind"].as_str().unwrap();
            let errors = if kind == "expanded_document" {
                vec![document_hashes(&obj, contract)
                    .map(|_| ())
                    .expect_err(&label)]
            } else {
                let kind: ObjectKind = kind.parse().unwrap();
                vec![
                    object_hash(&obj, kind, contract)
                        .map(|_| ())
                        .expect_err(&label),
                    children(&obj, kind, contract)
                        .map(|_| ())
                        .expect_err(&label),
                ]
            };
            for err in errors {
                assert_eq!(err.code(), case["code"].as_str().unwrap(), "{label}: {err}");
                if let Some(path) = case.get("path") {
                    assert_eq!(err.path(), path.as_str().unwrap(), "{label}: {err}");
                }
            }
            checked += 1;
        }
    }
    assert!(checked > 0);
}
