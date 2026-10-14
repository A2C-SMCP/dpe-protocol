//! HTTP 绑定 §1：remote URL 的文法。消费 `vectors/remote_url.json`，`valid` 与 `invalid` 两组
//! 逐条经 [`check_remote_url`] 断言（与 Python dpe-hash 的 `tests/test_remote_url.py` 对等）；
//! dpe-sdk 的客户端构造与实例定义入口见 `dpe-sdk/tests/remote_url_vectors.rs`。
//!
//! vector-kind: remote_url

mod common;

use dpe_hash::check_remote_url;

fn cases(group: &str) -> Vec<String> {
    let vector = common::read("remote_url.json");
    assert_eq!(vector["kind"], "remote_url");
    let cases: Vec<String> = vector[group]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| {
            case.as_str()
                .or_else(|| case["input"].as_str())
                .unwrap()
                .to_owned()
        })
        .collect();
    assert!(!cases.is_empty(), "向量不应为空");
    cases
}

#[test]
fn valid_remote_urls_are_accepted() {
    for url in cases("valid") {
        assert_eq!(check_remote_url(&url), Ok(()), "{url}");
    }
}

#[test]
fn invalid_remote_urls_are_rejected_without_echo() {
    for url in cases("invalid") {
        let error = check_remote_url(&url).expect_err(&url);
        // 消息不回显 URL（userinfo 里可能是凭证）
        assert!(url.is_empty() || !error.to_string().contains(&url), "{url}");
    }
}
