//! HTTP 传输适配（#22）：`DpeClient` 的行为测试。
//!
//! 协议路径经本文件的假服务端走**真实** TCP 与 reqwest / tokio 往返（零外部依赖、零网络），
//! 与 Python 侧 `tests/test_transport.py` 的场景逐条对等；「对参考服务端的集成测试」随 #25。
//!
//! 覆盖 Issue #22 的交付面：三类自行重发（`retry_safe` 网络失败、确定未发出的失败、按 code
//! 的可重试错误）、`Retry-After` 的等待与上限、gzip 的四个条件、凭证的每轮重新取、remote
//! 校验、capabilities 每实例一次、异常原样抛出与「确定未发出」标记、缺省（超时、不跟随
//! 重定向、大 blob 源在工作线程）。

#![cfg(feature = "http")]

mod http_common;

use std::io;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::{
    BlobRange, BlobReader, BlobSource, Body, Headers, Method, NotSent, Precondition, Request,
};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{ErrorKind, ExpandedDocument};
use serde_json::{json, Value};

use http_common::{
    capabilities_reply, committed, hash, heads_reply, http_date_in, MockServer, Recorded, Reply,
};

/// 一页、每段文本一个 `NarrativeText` 元素的文档（元素总数 = 文本数）。
fn doc(texts: &[&str]) -> ExpandedDocument {
    let elements: Vec<Value> = texts
        .iter()
        .map(|text| json!({"category": "NarrativeText", "text": text}))
        .collect();
    ExpandedDocument::parse(
        &json!({"file_type": "md", "pages": [{"elements": elements}]}).to_string(),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 不经核心的合成请求（验证传输层自身的判定时用）。
fn synthetic_request(retry_safe: bool) -> Request {
    Request {
        method: Method::Put,
        target: "documents?uri=s3%3A%2F%2Fbucket%2Fa.md".to_owned(),
        headers: Headers::new(),
        body: Body::Bytes(b"{}".to_vec()),
        retry_safe,
    }
}

/// 一个**建连必失败**的 remote：端口 0 永远不会有监听者（内核分配的临时端口不可能是 0），
/// 连接在连接阶段失败、不会碰到任何真实服务端——并行用例互不干扰（曾用「bind(0) 探端口再
/// 释放」，释放的端口会被并行用例的假服务端抢去，造成串扰）。
fn refused_remote() -> String {
    "http://127.0.0.1:0".to_owned()
}

fn puts(server: &MockServer) -> Vec<Recorded> {
    server
        .requests()
        .into_iter()
        .filter(|request| request.method == "PUT")
        .collect()
}

// ---------------------------------------------------------------------- remote 校验

#[test]
fn remote_must_be_a_plain_http_base_url() {
    for remote in [
        "ftp://example.com/r",
        "http://",
        "https://example.com/r?a=1",
        "https://example.com/r#f",
        // 空定界符：解析后与「没有」不可区分，但原始串拼接会让首个请求错位，必须同样拒绝
        "https://example.com/r?",
        "https://example.com/r#",
        "https://user:pw@example.com/r",
        "not-a-url",
    ] {
        let error = DpeClient::new(remote).expect_err("remote 必须被拒绝");
        assert_eq!(error.kind(), ErrorKind::InvalidArgument, "{remote}");
    }
}

#[tokio::test]
async fn remote_trailing_slash_is_stripped_and_the_first_target_follows_it() {
    let server = MockServer::start(vec![capabilities_reply(&["gzip"])]);
    let client = DpeClient::new(format!("{}/", server.remote())).expect("remote 合法");
    assert_eq!(client.remote(), server.remote());
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].target, "/capabilities");
}

// ---------------------------------------------------------------------- capabilities 快照

#[tokio::test]
async fn capabilities_are_snapshotted_once() {
    let server = MockServer::start(vec![capabilities_reply(&["gzip"])]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let first = client.capabilities().await.expect("能力").clone();
    assert_eq!(*client.capabilities().await.expect("能力"), first);
    assert_eq!(*client.core().await.expect("核心").capabilities(), first);
    assert_eq!(server.count(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_first_use_negotiates_capabilities_once() {
    let server = MockServer::start(vec![capabilities_reply(&["gzip"])]);
    let client = Arc::new(DpeClient::new(server.remote()).expect("remote 合法"));
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let client = Arc::clone(&client);
        tasks.push(tokio::spawn(async move {
            client
                .capabilities()
                .await
                .map(|capabilities| capabilities.protocol.clone())
        }));
    }
    for task in tasks {
        assert_eq!(task.await.expect("任务不 panic").expect("能力"), "dpe/1");
    }
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn an_incompatible_server_protocol_is_rejected() {
    let mut body = http_common::capabilities_body();
    body["protocol"] = json!("dpe/2");
    let server = MockServer::start(vec![Reply::json(200, body)]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let error = client.capabilities().await.expect_err("协议版本不符");
    assert_eq!(error.kind(), ErrorKind::IncompatibleServer);
}

// ---------------------------------------------------------------------- gzip

#[tokio::test]
async fn large_json_requests_are_gzipped() {
    let document = doc(&[&"x".repeat(4096)]);
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let result = client
        .commit(
            "s3://bucket/big.md",
            &document,
            Precondition::IfAbsent,
            None,
        )
        .await
        .expect("提交成功");
    assert_eq!(result.status, CommitStatus::Created);
    // capabilities 请求没有体，也不需要压缩
    assert!(server.requests()[0].header("content-encoding").is_none());
    let put = &puts(&server)[0];
    assert_eq!(put.header("content-encoding"), Some("gzip"));
    let payload: Value = serde_json::from_slice(&put.gunzip()).expect("解压后是 JSON");
    assert_eq!(payload["document"]["file_type"], "md");
}

#[tokio::test]
async fn small_json_requests_stay_identity() {
    let document = doc(&["hi"]);
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    client
        .commit(
            "s3://bucket/small.md",
            &document,
            Precondition::IfAbsent,
            None,
        )
        .await
        .expect("提交成功");
    assert!(server
        .requests()
        .iter()
        .all(|request| request.header("content-encoding").is_none()));
}

#[tokio::test]
async fn no_gzip_when_the_server_does_not_offer_it() {
    // 服务端不声明 gzip 时客户端必须用 identity：错误压缩会被服务端 415 拒绝
    let document = doc(&[&"x".repeat(4096)]);
    let server = MockServer::start(vec![
        capabilities_reply(&[]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    client
        .commit(
            "s3://bucket/big.md",
            &document,
            Precondition::IfAbsent,
            None,
        )
        .await
        .expect("提交成功");
    assert!(server
        .requests()
        .iter()
        .all(|request| request.header("content-encoding").is_none()));
}

#[tokio::test]
async fn chunked_requests_are_never_gzipped() {
    // 带 Content-Range 的请求体按原始字节处理（§7）：不走解码路径，MUST NOT 压缩
    let server = MockServer::start(vec![capabilities_reply(&["gzip"]), Reply::new(201, "")]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    client.capabilities().await.expect("能力"); // 建立核心：content_encodings 含 gzip
    let mut headers = Headers::new();
    headers.insert("Content-Type", "application/json");
    headers.insert("Content-Range", "bytes 0-1023/4096");
    let request = Request {
        method: Method::Put,
        target: "staging/st-1/blobs/sha256:00".to_owned(),
        headers,
        body: Body::Bytes(vec![b'x'; 4096]),
        retry_safe: false,
    };
    assert_eq!(client.send(request).await.expect("响应").status(), 201);
    let chunk = &server.requests()[1];
    assert!(chunk.header("content-encoding").is_none());
    assert_eq!(chunk.body.len(), 4096);
}

// ---------------------------------------------------------------------- 凭证

#[derive(Default)]
struct CountingProvider {
    calls: AtomicUsize,
    value: Option<String>,
}

impl dpe_sdk::credentials::CredentialProvider for CountingProvider {
    fn authorization(&self) -> Option<String> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.value.clone()
    }
}

struct RotatingProvider {
    calls: AtomicUsize,
}

impl dpe_sdk::credentials::CredentialProvider for RotatingProvider {
    fn authorization(&self) -> Option<String> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        Some(format!("Bearer token-{call}"))
    }
}

#[tokio::test]
async fn credential_is_injected_into_every_request() {
    let document = doc(&["hello"]);
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let provider = Arc::new(CountingProvider {
        calls: AtomicUsize::new(0),
        value: Some("Bearer full".to_owned()),
    });
    let client = DpeClient::builder(server.remote())
        .credential(provider.clone())
        .build()
        .expect("remote 合法");
    client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect("提交成功");
    let requests = server.requests();
    assert_eq!(provider.calls.load(Ordering::SeqCst), requests.len());
    assert!(requests
        .iter()
        .all(|request| request.header("authorization") == Some("Bearer full")));
}

#[tokio::test]
async fn a_provider_returning_none_sends_no_authorization() {
    let server = MockServer::start(vec![capabilities_reply(&["gzip"])]);
    let provider = Arc::new(CountingProvider::default());
    let client = DpeClient::builder(server.remote())
        .credential(provider.clone())
        .build()
        .expect("remote 合法");
    client.capabilities().await.expect("能力");
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
    assert!(server.requests()[0].header("authorization").is_none());
}

#[tokio::test]
async fn credential_is_refreshed_before_every_retry() {
    // 429 之后的重发同样算一次发送：重新取凭证，轮换中的短时凭证在等待后仍可用
    let server = MockServer::start(vec![
        Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "0")]),
        capabilities_reply(&["gzip"]),
    ]);
    let client = DpeClient::builder(server.remote())
        .credential(Arc::new(RotatingProvider {
            calls: AtomicUsize::new(0),
        }))
        .build()
        .expect("remote 合法");
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].header("authorization"), Some("Bearer token-1"));
    assert_eq!(requests[1].header("authorization"), Some("Bearer token-2"));
}

// ---------------------------------------------------------------------- 重试

#[tokio::test(start_paused = true)]
async fn retryable_errors_wait_for_retry_after_delta_seconds() {
    let server = MockServer::start(vec![
        Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "7")]),
        capabilities_reply(&["gzip"]),
    ]);
    let client = DpeClient::builder(server.remote())
        .timeout(None)
        .build()
        .expect("remote 合法");
    let started = tokio::time::Instant::now();
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2);
    assert!(
        started.elapsed() >= Duration::from_secs(7),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test(start_paused = true)]
async fn retryable_errors_wait_for_retry_after_http_date() {
    let when = http_date_in(30);
    let server = MockServer::start(vec![
        Reply::problem("DPE_UNAVAILABLE", 503, &[("Retry-After", &when)]),
        capabilities_reply(&["gzip"]),
    ]);
    let client = DpeClient::builder(server.remote())
        .timeout(None)
        .build()
        .expect("remote 合法");
    let started = tokio::time::Instant::now();
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2);
    // HTTP-date 只有秒级分辨率：等到的应在 25–30 秒之间
    assert!(
        started.elapsed() >= Duration::from_secs(20),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test(start_paused = true)]
async fn retryable_error_without_retry_after_uses_the_spec_default() {
    let server = MockServer::start(vec![
        Reply::problem("DPE_RATE_LIMITED", 429, &[]),
        capabilities_reply(&["gzip"]),
    ]);
    let client = DpeClient::builder(server.remote())
        .timeout(None)
        .build()
        .expect("remote 合法");
    let started = tokio::time::Instant::now();
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2);
    assert!(
        started.elapsed() >= Duration::from_secs(1),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test(start_paused = true)]
async fn waiting_at_the_ceiling_is_still_retried() {
    let server = MockServer::start(vec![
        Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "300")]),
        capabilities_reply(&["gzip"]),
    ]);
    let client = DpeClient::builder(server.remote())
        .timeout(None)
        .build()
        .expect("remote 合法");
    let started = tokio::time::Instant::now();
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2);
    assert!(
        started.elapsed() >= Duration::from_secs(300),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn retryable_error_budget_exhausted_is_reported() {
    let server = MockServer::start(vec![Reply::problem(
        "DPE_RATE_LIMITED",
        429,
        &[("Retry-After", "0")],
    )]);
    let client = DpeClient::builder(server.remote())
        .max_retries(2)
        .build()
        .expect("remote 合法");
    let error = client.capabilities().await.expect_err("预算耗尽");
    assert_eq!(error.kind(), ErrorKind::RateLimited);
    assert_eq!(
        (server.count(), error.is_retryable(), error.retry_after()),
        (3, true, Some(0.0))
    );
}

#[tokio::test(start_paused = true)]
async fn an_absurd_retry_after_is_not_retried() {
    // 超过等待上限的 Retry-After（含大到无法表示的 inf）不吸收：如实抛出且值不丢
    for (header, expected) in [
        (
            "9".repeat(30),
            "9".repeat(30).parse::<f64>().expect("有限值"),
        ),
        ("9".repeat(5000), f64::INFINITY),
    ] {
        let server = MockServer::start(vec![Reply::problem(
            "DPE_RATE_LIMITED",
            429,
            &[("Retry-After", &header)],
        )]);
        let client = DpeClient::builder(server.remote())
            .timeout(None)
            .build()
            .expect("remote 合法");
        let started = tokio::time::Instant::now();
        let error = client.capabilities().await.expect_err("不重试");
        assert_eq!(server.count(), 1);
        assert_eq!(error.retry_after(), Some(expected));
        assert_eq!(started.elapsed(), Duration::ZERO, "没有等待");
    }
}

#[tokio::test]
async fn read_requests_are_resent_on_network_failures() {
    let server = MockServer::start(vec![Reply::Stall, capabilities_reply(&["gzip"])]);
    let client = DpeClient::builder(server.remote())
        .timeout(Some(Duration::from_millis(150)))
        .build()
        .expect("remote 合法");
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2); // 首发读超时 + 重发成功
}

#[tokio::test]
async fn read_requests_are_resent_when_the_response_is_lost() {
    let server = MockServer::start(vec![Reply::Close, capabilities_reply(&["gzip"])]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    assert_eq!(server.count(), 2);
}

#[tokio::test]
async fn the_resend_budget_caps_network_failure_retries() {
    // 网络失败的重发同样受 max_retries 约束（首发 + 2 次重发）：等不到响应就放弃、如实抛出。
    // 精确的尝试次数断言交给 `retryable_error_budget_exhausted_is_reported`（按应答计次，
    // 确定）；这里因为是超时路径，只断言「确实重发过」——客户端复用刚超时的那条连接时，
    // 该次尝试会立刻失败、不新开连接（服务端只见到 2 个请求），精确计数会随竞态抖动。
    let server = MockServer::start(vec![Reply::Stall]);
    let client = DpeClient::builder(server.remote())
        .timeout(Some(Duration::from_millis(150)))
        .max_retries(2)
        .build()
        .expect("remote 合法");
    let started = std::time::Instant::now();
    let error = client
        .send(synthetic_request(true))
        .await
        .expect_err("预算耗尽");
    assert!(error.downcast_ref::<NotSent>().is_none());
    // 至少两次超时（若首发即弃，只会等一次）；服务端至少见到两次请求
    assert!(
        started.elapsed() >= Duration::from_millis(250),
        "{:?}",
        started.elapsed()
    );
    let seen = server.count();
    assert!((2..=3).contains(&seen), "服务端见到 {seen} 个请求");
}

/// 客户端可跨任务共享，常见操作的 future 是 `Send`（可交给 `tokio::spawn`）。
#[test]
fn the_client_and_its_operation_futures_are_send() {
    fn assert_send<T: Send>(_: T) {}
    fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<DpeClient>();

    let client = DpeClient::new("http://127.0.0.1:1").expect("remote 合法");
    let document = doc(&["x"]);
    assert_send(client.head("s3://bucket/a.md"));
    assert_send(client.get_skeleton("s3://bucket/a.md"));
    assert_send(client.list_page("s3://bucket/", None, None));
    assert_send(client.commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None));
    assert_send(client.delete("s3://bucket/a.md", &hash('1')));
}

#[tokio::test]
async fn batch_head_is_retry_safe_despite_being_a_post() {
    // 重发安全性来自 retry_safe，不按 HTTP 方法推断：batch_head 走 POST 但只读
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        Reply::Stall,
        heads_reply(&[Some(hash('1'))]),
    ]);
    let client = DpeClient::builder(server.remote())
        .timeout(Some(Duration::from_millis(150)))
        .build()
        .expect("remote 合法");
    let heads = client
        .batch_head(&["s3://bucket/a.md".to_owned()])
        .await
        .expect("批查询成功");
    assert_eq!(heads, vec![Some(hash('1'))]);
    let posts: Vec<Recorded> = server
        .requests()
        .into_iter()
        .filter(|request| request.target == "/heads")
        .collect();
    assert_eq!(posts.len(), 2);
}

#[tokio::test]
async fn retryable_errors_are_resent_on_writes_too() {
    // 写请求（retry_safe=false）遇到按 code 判定的可重试错误时原样重发——这是写路径上
    // 唯一允许传输层自行重发的情形（core §6：retryable 即请求未被处理）
    let document = doc(&["hello"]);
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "0")]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let result = client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect("提交成功");
    assert_eq!(result.status, CommitStatus::Created);
    let puts = puts(&server);
    assert_eq!(puts.len(), 2);
    assert_eq!(puts[0].body, puts[1].body); // 原样重发：两次的请求体逐字节相同
}

#[tokio::test]
async fn compressed_bodies_are_identical_across_resends() {
    // 原样重发对压缩体同样成立：确定性压缩（mtime=0）保证逐字节相同
    let document = doc(&[&"x".repeat(4096)]);
    let server = MockServer::start(vec![
        capabilities_reply(&["gzip"]),
        Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "0")]),
        committed(&document, "created", 1, 0, 0),
    ]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    client
        .commit(
            "s3://bucket/big.md",
            &document,
            Precondition::IfAbsent,
            None,
        )
        .await
        .expect("提交成功");
    let puts = puts(&server);
    assert_eq!(puts.len(), 2);
    assert_eq!(puts[0].header("content-encoding"), Some("gzip"));
    assert_eq!(puts[0].body, puts[1].body);
}

#[tokio::test]
async fn a_bare_429_is_not_retried_and_not_guessed() {
    // 无 problem 体、无 DPE-Error-Code 的 429 不是 DPE 错误：不猜 code、也不重试（§5）
    let server = MockServer::start(vec![Reply::new(429, "")]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let error = client.capabilities().await.expect_err("非协议错误");
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(429));
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn a_non_2xx_response_is_returned_to_the_caller_verbatim() {
    // 无码 503 不是可重试的 DPE 错误：响应原样交回调用方（不猜 code、不重发）
    let server = MockServer::start(vec![Reply::new(503, b"<h1>gateway</h1>".to_vec())]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let response = client
        .send(synthetic_request(false))
        .await
        .expect("响应原样返回");
    assert_eq!(response.status(), 503);
    assert_eq!(response.body(), b"<h1>gateway</h1>");
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn write_requests_are_not_resent_on_network_failures() {
    // 写请求可能已发出后的读超时不被传输层重发：原样抛回，由核心按 core §5.2 处理
    let server = MockServer::start(vec![Reply::Stall]);
    let client = DpeClient::builder(server.remote())
        .timeout(Some(Duration::from_millis(150)))
        .build()
        .expect("remote 合法");
    let error = client
        .send(synthetic_request(false))
        .await
        .expect_err("原样抛出");
    assert!(error.downcast_ref::<NotSent>().is_none());
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn write_timeouts_are_left_to_the_core_to_handle() {
    // 写请求的响应丢失：传输层不重发，核心按 core §5.2 原样重发至 max_resends 次
    let document = doc(&["hello"]);
    let server = MockServer::start(vec![capabilities_reply(&["gzip"]), Reply::Close]);
    let client = DpeClient::builder(server.remote())
        .max_resends(1)
        .build()
        .expect("remote 合法");
    let error = client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect_err("响应丢失");
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert_eq!(puts(&server).len(), 2); // 首发 + 核心的 1 次重发（传输层没有自行翻倍）
}

// ---------------------------------------------- 「确定未发出」与写操作的结果判定

#[tokio::test]
async fn not_sent_is_marked_on_connect_failures() {
    // 公开的判定：建连失败标「确定未发出」，原始异常类型不变、可取回
    let client = DpeClient::builder(refused_remote())
        .timeout(Some(Duration::from_secs(1)))
        .build()
        .expect("remote 合法");
    let error = client
        .send(synthetic_request(false))
        .await
        .expect_err("建连失败");
    let marker = error
        .downcast_ref::<NotSent>()
        .expect("建连失败带「确定未发出」标记");
    assert!(marker.error().downcast_ref::<reqwest::Error>().is_some());
}

#[tokio::test]
async fn not_sent_is_not_marked_on_lost_responses_or_timeouts() {
    for reply in [Reply::Stall, Reply::Close] {
        let server = MockServer::start(vec![reply]);
        let client = DpeClient::builder(server.remote())
            .timeout(Some(Duration::from_millis(150)))
            .max_retries(0)
            .build()
            .expect("remote 合法");
        let error = client
            .send(synthetic_request(false))
            .await
            .expect_err("可能已发出");
        assert!(error.downcast_ref::<NotSent>().is_none());
        // 异常不包装：reqwest 的错误类型原样可取回，调用方按类型区分超时与响应丢失
        assert!(error.downcast_ref::<reqwest::Error>().is_some());
    }
}

#[tokio::test]
async fn a_write_is_determined_when_every_attempt_is_not_sent() {
    // 全链路：capabilities 之后服务端关闭监听，写请求的每个尝试（传输层与核心两级重发）
    // 都确定未发出 → 结果确定「未生效」（outcome_unknown = false）
    let document = doc(&["x"]);
    let server = MockServer::start_closing_after(vec![capabilities_reply(&["gzip"])], 1);
    let client = DpeClient::builder(server.remote())
        .max_retries(1)
        .max_resends(1)
        .build()
        .expect("remote 合法");
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    let error = client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect_err("建连失败");
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(!error.outcome_unknown(), "每个尝试都确定未发出");
}

#[tokio::test]
async fn a_coded_retry_then_a_connect_failure_is_determined() {
    // 服务端明确裁决「请求未被处理」（带码 429）之后的重发又全部确定未发出 → 整次写入确定
    // 「未生效」。这是标记按**单次尝试的异常**判定的判别性用例：若按「整次 send 的尝试全部
    // 未发出」的累计语义，这里会判成「结果未知」（与 Python 侧分叉）——两侧都按前者。
    let document = doc(&["x"]);
    let server = MockServer::start_closing_after(
        vec![
            capabilities_reply(&["gzip"]),
            Reply::problem("DPE_RATE_LIMITED", 429, &[("Retry-After", "0")]),
        ],
        2, // capabilities 之后、429 之后关闭监听：重发即建连失败
    );
    let client = DpeClient::builder(server.remote())
        .max_retries(1)
        .max_resends(1)
        .build()
        .expect("remote 合法");
    assert_eq!(client.capabilities().await.expect("能力").protocol, "dpe/1");
    let error = client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect_err("建连失败");
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(
        !error.outcome_unknown(),
        "带码 429 是服务端「未处理」的裁决，其后的尝试全部未发出"
    );
}

#[tokio::test]
async fn a_write_is_unknown_when_the_first_response_was_lost() {
    // 对照：首发响应丢失（可能已生效）之后的失败一律按「结果未知」处理
    let document = doc(&["x"]);
    let server = MockServer::start(vec![capabilities_reply(&["gzip"]), Reply::Close]);
    let client = DpeClient::builder(server.remote())
        .max_retries(0)
        .max_resends(1)
        .build()
        .expect("remote 合法");
    let error = client
        .commit("s3://bucket/a.md", &document, Precondition::IfAbsent, None)
        .await
        .expect_err("响应丢失");
    assert!(error.outcome_unknown(), "首发可能已生效");
}

// ---------------------------------------------------------------------- 客户端缺省

#[tokio::test]
async fn the_configured_timeout_bounds_the_request() {
    let server = MockServer::start(vec![Reply::Stall]);
    let client = DpeClient::builder(server.remote())
        .timeout(Some(Duration::from_millis(150)))
        .max_retries(0)
        .build()
        .expect("remote 合法");
    let started = std::time::Instant::now();
    let error = client
        .send(synthetic_request(true))
        .await
        .expect_err("读超时");
    assert!(error
        .downcast_ref::<reqwest::Error>()
        .expect("reqwest 错误")
        .is_timeout());
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[tokio::test]
async fn redirects_are_not_followed() {
    let server = MockServer::start(vec![Reply::Response {
        status: 302,
        headers: vec![("Location".to_owned(), "/elsewhere".to_owned())],
        body: Vec::new(),
    }]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let response = client
        .send(synthetic_request(true))
        .await
        .expect("3xx 原样返回");
    assert_eq!(response.status(), 302);
    assert_eq!(server.count(), 1); // 没有跟随
}

struct RecordingReader {
    data: Vec<u8>,
    read_on: Arc<std::sync::Mutex<Option<std::thread::ThreadId>>>,
}

impl BlobReader for RecordingReader {
    fn read(&self, offset: u64, length: u64) -> io::Result<Vec<u8>> {
        *self.read_on.lock().expect("线程记录锁") = Some(std::thread::current().id());
        Ok(self.data[offset as usize..(offset + length) as usize].to_vec())
    }
}

#[tokio::test]
async fn blob_ranges_are_read_on_a_worker_thread() {
    // BlobRange 是阻塞的源读取：异步层放进工作线程，不在执行器线程里读源
    let server = MockServer::start(vec![Reply::new(201, "")]);
    let client = DpeClient::new(server.remote()).expect("remote 合法");
    let read_on = Arc::new(std::sync::Mutex::new(None));
    let source = BlobSource::new(
        "sha256:00",
        512,
        Arc::new(RecordingReader {
            data: vec![7u8; 512],
            read_on: Arc::clone(&read_on),
        }),
    );
    let request = Request {
        method: Method::Put,
        target: "staging/st-1/blobs/sha256:00".to_owned(),
        headers: Headers::new(),
        body: Body::BlobRange(BlobRange {
            source,
            offset: 0,
            length: 512,
        }),
        retry_safe: false,
    };
    assert_eq!(client.send(request).await.expect("响应").status(), 201);
    assert_eq!(server.requests()[0].body, vec![7u8; 512]);
    let reader_thread = read_on.lock().expect("线程记录锁").expect("读过源");
    assert_ne!(reader_thread, std::thread::current().id());
}
