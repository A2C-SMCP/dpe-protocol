//! 消费向量 kind：HTTP 绑定 §1（remote URL 的文法）的 `vectors/remote_url.json`，`valid` 与
//! `invalid` 两组逐条经三个入口断言：[`check_remote_url`] 本身、`DpeClient` 构造（传输层，随
//! feature `http`）、实例定义的 `remote.url`（connector 契约 §4.4）。三者是同一个判定，与
//! Python SDK 的 `tests/test_remote_url.py` 对等。
//!
//! 跨 crate 登记标记（`sdk/rust/vector-consumers.json` → `dpe-hash/tests/vectors.rs` 的
//! `all_vector_kinds_have_consumers` 核验本文件存在且带下行标记）：
//!
//! vector-kind: remote_url

mod common;

use serde_json::{json, Value};

use dpe_sdk::remote_url::check_remote_url;
use dpe_sdk::run::definition::parse_definition;

fn vector() -> Value {
    let vector = common::read("remote_url.json");
    assert_eq!(vector["kind"], "remote_url");
    vector
}

fn valid() -> Vec<String> {
    let vector = vector();
    let cases: Vec<String> = vector["valid"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| case.as_str().unwrap().to_owned())
        .collect();
    assert!(!cases.is_empty(), "向量不应为空");
    cases
}

fn invalid() -> Vec<String> {
    let vector = vector();
    let cases: Vec<String> = vector["invalid"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| case["input"].as_str().unwrap().to_owned())
        .collect();
    assert!(!cases.is_empty(), "向量不应为空");
    cases
}

fn definition(url: &str) -> Value {
    json!({
        "definition_version": 1,
        "id": "acme-repo",
        "remote": {"url": url},
        "uri_prefix": "git://acme/repo/",
        "plugin": {"manifest": "plugins/git/dpe-connector.json", "command": ["dpe-git-connector"]},
        "config": {},
    })
}

#[test]
fn valid_remote_urls_are_accepted() {
    for url in valid() {
        assert_eq!(check_remote_url(&url), Ok(()), "{url}");
    }
}

#[test]
fn invalid_remote_urls_are_rejected_without_echo() {
    for url in invalid() {
        let error = check_remote_url(&url).expect_err(&url);
        // 消息不回显 URL（userinfo 里可能是凭证）
        assert!(url.is_empty() || !error.to_string().contains(&url), "{url}");
    }
}

#[test]
fn definition_follows_the_grammar() {
    let base = std::env::temp_dir();
    for url in valid() {
        let parsed = parse_definition(&definition(&url), &base).expect(&url);
        assert_eq!(parsed.remote().url(), url);
    }
    for url in invalid() {
        let failure = parse_definition(&definition(&url), &base).expect_err(&url);
        assert_eq!(failure.code(), "definition_invalid", "{url}");
        assert!(
            url.is_empty() || !failure.to_string().contains(&url),
            "{url}"
        );
    }
}

#[cfg(feature = "http")]
#[test]
fn client_follows_the_grammar() {
    use dpe_sdk::http::DpeClient;
    use dpe_sdk::ErrorKind;

    for url in valid() {
        let client = DpeClient::new(url.as_str()).expect(&url);
        assert_eq!(client.remote(), url);
    }
    for url in invalid() {
        let error = DpeClient::new(url.as_str()).expect_err(&url);
        assert_eq!(error.kind(), ErrorKind::InvalidArgument, "{url}");
    }
}
