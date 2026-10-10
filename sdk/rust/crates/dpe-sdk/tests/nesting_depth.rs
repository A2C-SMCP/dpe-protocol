//! core §2.8 第 0 步第三项（#94）：嵌套深度上界的文本入口（模型的 `parse` / `from_value`）。
//!
//! 消费 `vectors/nesting_depth.json`：文本入口（严格解析后校验）与对象级入口
//! （dpe-hash 的 `tests/nesting_depth.rs`）对同一条输入同判；元素对象内联在展开文档里时对象根
//! 相对文本根下移 4 层，解析防护上限（64 + 4）不因此拒收合法内容。
//!
//! vector-kind: nesting_depth

mod common;

use dpe_hash::MAX_NESTING_DEPTH;
use dpe_sdk::models::{DocumentObject, ElementObject, ExpandedDocument, PageObject};
use serde_json::Value;

use common::load;

#[test]
fn text_entry_matches_object_entry() {
    let mut checked = 0;
    let mut accepted = 0;
    for vec in load("nesting_depth") {
        assert_eq!(vec["limit"].as_u64().unwrap(), u64::from(MAX_NESTING_DEPTH));
        for case in vec["cases"].as_array().unwrap() {
            let label = case["name"].as_str().unwrap();
            let contract = case["contract"].as_str().unwrap();
            let text = case["input_json"].as_str().unwrap();
            let input = &case["input"];
            let accept = case.get("accept").and_then(Value::as_bool).unwrap_or(false);
            let results = match case["object_kind"].as_str().unwrap() {
                "element" => vec![
                    ElementObject::parse(text, contract).map(|_| ()),
                    ElementObject::from_value(input, contract).map(|_| ()),
                ],
                "page" => vec![
                    PageObject::parse(text, contract).map(|_| ()),
                    PageObject::from_value(input, contract).map(|_| ()),
                ],
                "document" => vec![
                    DocumentObject::parse(text, contract).map(|_| ()),
                    DocumentObject::from_value(input, contract).map(|_| ()),
                ],
                "expanded_document" => vec![
                    ExpandedDocument::parse(text, contract).map(|_| ()),
                    ExpandedDocument::from_value(input, contract).map(|_| ()),
                ],
                other => panic!("未知 object_kind：{other}"),
            };
            if accept {
                for result in results {
                    result.unwrap_or_else(|e| panic!("{label}: 入口不应拒绝：{e}"));
                }
                accepted += 1;
                continue;
            }
            for result in results {
                let e = result.expect_err(label);
                assert_eq!(e.code(), case["code"].as_str().unwrap(), "{label}: {e}");
                if let Some(path) = case.get("path") {
                    assert_eq!(e.path(), path.as_str().unwrap(), "{label}: {e}");
                }
            }
            checked += 1;
        }
    }
    assert!(checked >= 5, "向量应有元素/页/文档/展开视图的拒绝用例");
    assert!(accepted >= 4, "向量应有元素/页/文档/展开视图的接受用例");
}
