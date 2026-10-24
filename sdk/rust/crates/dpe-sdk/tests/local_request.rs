//! 请求构造阶段的本地失败（#207）：非法 `xn--` 主机与非法凭证头值。
//!
//! 两侧对等口径：这类失败发生在请求发出之前、由本地判定，客户端 MUST 以 SDK 自己的错误类型
//! 上报、显式标记「确定未发出」、不重发，且消息不回显 URL / 主机 / 凭证。

#![cfg(feature = "http")]

mod http_common;

use std::error::Error as _;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use dpe_sdk::credentials::CredentialProvider;
use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::{BlobSource, Precondition, ProtocolCore, Session};
use dpe_sdk::{ErrorKind, ExpandedDocument, CONTRACT};
use serde_json::json;

use http_common::{capabilities, capabilities_reply, hash, CountingProvider, MockServer, Reply};

/// 两侧 URL 库都会拒绝的主机（各自拒绝集合更大，按「不复刻库怪癖」不对齐，见 #207）。
const INVALID_HOSTS: [&str; 3] = ["xn--zz.example", "xn--a", "xn--.example"];

/// RFC 9110 field-value 不允许的控制字符（HTAB 除外）。
const INVALID_CREDENTIALS: [&str; 3] = ["Bearer abc\ndef", "Bearer abc\0def", "Bearer abc\rdef"];

fn h1() -> String {
    format!("dpe1:{}", "1".repeat(64))
}

fn doc() -> ExpandedDocument {
    ExpandedDocument::parse(
        &json!({"file_type": "md", "pages": [{"elements": [{"category": "NarrativeText", "text": "hi"}]}]})
            .to_string(),
        CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 前 `valid_calls` 次给合法凭证，之后给含 LF 的非法凭证。
struct RotatingProvider {
    calls: AtomicUsize,
    valid_calls: usize,
}

impl RotatingProvider {
    fn new(valid_calls: usize) -> Self {
        RotatingProvider {
            calls: AtomicUsize::new(0),
            valid_calls,
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl CredentialProvider for RotatingProvider {
    fn authorization(&self) -> Option<String> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        Some(if call <= self.valid_calls {
            "Bearer token".to_owned()
        } else {
            "Bearer bad\nvalue".to_owned()
        })
    }
}

fn client(remote: impl Into<String>, provider: &Arc<CountingProvider>) -> DpeClient {
    let provider: Arc<dyn CredentialProvider> = provider.clone();
    DpeClient::builder(remote)
        .credential(provider)
        .build()
        .expect("remote 文法接受 xn-- 主机")
}

fn assert_local_request(error: &dpe_sdk::Error, forbidden: &str) {
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(!error.outcome_unknown(), "确定未发出");
    assert!(!error.is_retryable(), "本地构造失败不可原样重试");
    assert!(
        !error.to_string().contains(forbidden),
        "消息不得回显输入：{error}"
    );
    assert!(error.source().is_some(), "原始库错误经 source() 保留");
}

#[tokio::test]
async fn invalid_xn_host_is_a_local_request_error_for_reads() {
    for host in INVALID_HOSTS {
        let provider = Arc::new(CountingProvider::new("Bearer token"));
        let client = client(format!("http://{host}/r"), &provider);
        let error = client
            .head("s3://bucket/a.md")
            .await
            .expect_err("非法 A-label 必须在发出前失败");
        assert_local_request(&error, host);
        assert_eq!(provider.calls(), 1, "{host}：只取一次凭证（没有重发）");
    }
}

#[tokio::test]
async fn invalid_xn_host_is_a_local_request_error_for_writes() {
    for host in INVALID_HOSTS {
        let provider = Arc::new(CountingProvider::new("Bearer token"));
        let client = client(format!("http://{host}/r"), &provider);
        let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
        let error = client
            .drive(
                core.delete("s3://bucket/a.md", &h1())
                    .expect("本地参数合法"),
            )
            .await
            .expect_err("非法 A-label 必须在发出前失败");
        assert_local_request(&error, host);
        assert_eq!(
            provider.calls(),
            1,
            "{host}：确定未发出，不得按响应丢失重发"
        );
    }
}

#[tokio::test]
async fn invalid_credential_header_value_is_a_local_request_error() {
    for value in INVALID_CREDENTIALS {
        let server = MockServer::start(vec![capabilities_reply(&[])]);
        let provider = Arc::new(CountingProvider::new(value));
        let client = client(server.remote(), &provider);
        let error = client
            .head("s3://bucket/a.md")
            .await
            .expect_err("非法头字段值必须在发出前失败");
        assert_local_request(&error, "abc");
        assert_eq!(provider.calls(), 1, "{value:?}：只取一次凭证（没有重发）");
        assert_eq!(server.count(), 0, "{value:?}：不应发出任何请求");
    }
}

#[tokio::test]
async fn invalid_credential_header_value_is_not_resent_for_writes() {
    for value in INVALID_CREDENTIALS {
        let server = MockServer::start(vec![capabilities_reply(&[])]);
        let provider = Arc::new(CountingProvider::new(value));
        let client = client(server.remote(), &provider);
        let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
        let error = client
            .drive(
                core.delete("s3://bucket/a.md", &h1())
                    .expect("本地参数合法"),
            )
            .await
            .expect_err("非法头字段值必须在发出前失败");
        assert_local_request(&error, "abc");
        assert_eq!(provider.calls(), 1, "{value:?}：确定未发出，不得重发");
        assert_eq!(server.count(), 0, "{value:?}：不应发出任何请求");
    }
}

#[tokio::test]
async fn local_failure_after_an_uncertain_attempt_keeps_outcome_unknown() {
    // 首次提交的响应丢失（结果不确定），核心按 §5.2 重发；第二次尝试在构造阶段失败
    // （轮换出的凭证含 LF）。整体结果必须仍是「可能已生效」，不能因本次尝试未发出而降级。
    let server = MockServer::start(vec![Reply::Close]);
    let provider = Arc::new(RotatingProvider::new(1));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.delete("s3://bucket/a.md", &h1())
                .expect("本地参数合法"),
        )
        .await
        .expect_err("本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(error.outcome_unknown(), "先前的不确定尝试可能已生效");
    assert!(!error.to_string().contains("bad"));
    assert_eq!(provider.calls(), 2);
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn bare_412_head_local_failure_is_still_unknown() {
    // 裸 412 的 head 判定本身在本地构造阶段失败：未化解的 412 仍可能已生效。
    let server = MockServer::start(vec![Reply::new(412, "precondition")]);
    let provider = Arc::new(RotatingProvider::new(1));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.commit(
                "s3://bucket/a.md",
                &doc(),
                Precondition::BaseHash(h1()),
                None,
            )
            .expect("本地参数合法"),
        )
        .await
        .expect_err("head 本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(error.outcome_unknown(), "裸 412 未化解：可能已生效");
    assert_eq!(provider.calls(), 2);
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn force_confirm_head_local_failure_keeps_force_not_confirmed() {
    // force 提交丢失后，确认用的 head 在本地构造阶段失败：保留 ForceNotConfirmed 载荷。
    let server = MockServer::start(vec![Reply::new(504, "gateway")]);
    let provider = Arc::new(RotatingProvider::new(1));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.commit("s3://bucket/a.md", &doc(), Precondition::Force, None)
                .expect("本地参数合法"),
        )
        .await
        .expect_err("head 本地构造失败");
    assert_eq!(error.kind(), ErrorKind::ForceNotConfirmed);
    assert!(error.outcome_unknown(), "force 提交可能已生效");
    assert_eq!(provider.calls(), 2);
    assert_eq!(server.count(), 1);
    let detail = error.force_not_confirmed().expect("ForceNotConfirmed 载荷");
    assert!(!detail.checked);
}

#[tokio::test]
async fn bare_412_resolved_after_a_lost_head_is_determined() {
    // head 的一次可重试丢失不得污染结果：重发后化解 412（读到不同 doc_hash）仍为「确定」。
    let head = Reply::Response {
        status: 200,
        headers: vec![("DPE-Doc-Hash".to_owned(), hash('2'))],
        body: Vec::new(),
    };
    let server = MockServer::start(vec![Reply::new(412, "precondition"), Reply::Close, head]);
    let client = DpeClient::builder(server.remote())
        .max_retries(0)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.commit(
                "s3://bucket/a.md",
                &doc(),
                Precondition::BaseHash(h1()),
                None,
            )
            .expect("本地参数合法"),
        )
        .await
        .expect_err("head 化解后应报 CAS 失败");
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    assert!(!error.outcome_unknown(), "head 已化解：结果确定");
    assert_eq!(server.count(), 3, "PUT + 两次 HEAD");
}

#[tokio::test]
async fn delete_bare_412_head_local_failure_is_still_unknown() {
    let server = MockServer::start(vec![Reply::new(412, "precondition")]);
    let provider = Arc::new(RotatingProvider::new(1));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.delete("s3://bucket/a.md", &h1())
                .expect("本地参数合法"),
        )
        .await
        .expect_err("head 本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(error.outcome_unknown(), "裸 412 未化解：可能已生效");
    assert_eq!(provider.calls(), 2);
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn upload_runner_stops_on_local_request_error() {
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let provider = Arc::new(CountingProvider::new("Bearer bad\nvalue"));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let mut session = Session {
        id: "session-1".to_owned(),
        uri: "s3://bucket/a.md".to_owned(),
        expires_at: "2026-10-01T00:00:00Z".to_owned(),
    };
    let error = client
        .drive(
            core.upload_blob(&mut session, BlobSource::from_bytes(b"hi".to_vec()), None)
                .expect("本地参数合法"),
        )
        .await
        .expect_err("本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(!error.outcome_unknown());
    assert_eq!(provider.calls(), 1);
    assert_eq!(server.count(), 0);
}

#[tokio::test]
async fn move_stops_on_local_request_error() {
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let provider = Arc::new(CountingProvider::new("Bearer bad\nvalue"));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.move_document("s3://bucket/a.md", "s3://bucket/b.md", &h1())
                .expect("本地参数合法"),
        )
        .await
        .expect_err("本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(!error.outcome_unknown());
    assert_eq!(provider.calls(), 1);
    assert_eq!(server.count(), 0);
}

#[tokio::test]
async fn deliver_bare_412_head_local_failure_is_still_unknown() {
    // deliver 包装 CommitOp 时，裸 412 未化解的「可能已生效」不能被 settle 覆写为确定。
    let server = MockServer::start(vec![
        capabilities_reply(&[]),
        Reply::new(412, "precondition"),
    ]);
    let provider = Arc::new(RotatingProvider::new(2)); // capabilities 与 PUT 用合法凭证
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let error = client
        .deliver(
            "s3://bucket/a.md",
            &doc(),
            Precondition::BaseHash(h1()),
            None,
            None,
        )
        .await
        .expect_err("head 本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(error.outcome_unknown(), "裸 412 未化解：可能已生效");
    assert_eq!(provider.calls(), 3);
    assert_eq!(server.count(), 2, "capabilities + PUT");
}

#[tokio::test]
async fn force_first_attempt_local_failure_is_a_local_request_error() {
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let provider = Arc::new(CountingProvider::new("Bearer bad\nvalue"));
    let erased: Arc<dyn CredentialProvider> = provider.clone();
    let client = DpeClient::builder(server.remote())
        .credential(erased)
        .build()
        .expect("remote 合法");
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let error = client
        .drive(
            core.commit("s3://bucket/a.md", &doc(), Precondition::Force, None)
                .expect("本地参数合法"),
        )
        .await
        .expect_err("本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(!error.outcome_unknown());
    assert_eq!(provider.calls(), 1);
    assert_eq!(server.count(), 0);
}

#[test]
fn valid_alabel_host_is_not_rejected() {
    // 合法 A-label 的 wire 级发送由 Python 侧的同名用例覆盖；这里锁住同一个预解析器不误伤它
    // （send 里用的就是 reqwest::Url::parse）。
    let url = "https://xn--fsqu00a.example/r";
    assert!(
        reqwest::Url::parse(url).is_ok(),
        "合法 A-label 必须能预解析"
    );
    assert!(
        DpeClient::builder(url).build().is_ok(),
        "合法 A-label 的 remote 照常接受"
    );
}

#[test]
fn free_drive_stops_on_local_request_error() {
    let core = ProtocolCore::new(capabilities(), CONTRACT, 2).expect("能力与服务端兼容");
    let calls = AtomicUsize::new(0);
    let error = dpe_sdk::drive(core.head("s3://bucket/a.md"), |_| {
        calls.fetch_add(1, Ordering::SeqCst);
        Err(dpe_sdk::local_request_error(std::io::Error::other("boom")))
    })
    .expect_err("本地构造失败");
    assert_eq!(error.kind(), ErrorKind::LocalRequest);
    assert!(!error.outcome_unknown());
    assert_eq!(calls.load(Ordering::SeqCst), 1, "不重发");
}
