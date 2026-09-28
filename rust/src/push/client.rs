//! dpe-push/1 异步客户端：能力发现 → 协商 → 投递。
//!
//! ```no_run
//! # async fn demo(doc: dpe_protocol::schema::Document) -> dpe_protocol::Result<()> {
//! use dpe_protocol::push::DpePushClient;
//!
//! let client = DpePushClient::with_reqwest("https://robot.example.com")?.with_token("<JWT>");
//! let result = client.push(&doc, None).await?;
//! // result.doc_hash 持久化，作为下次投递的 base_doc_hash
//! # Ok(()) }
//! ```

use std::collections::HashSet;
use std::io::Write as _;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use flate2::write::GzEncoder;
use flate2::Compression;
use futures::future::BoxFuture;
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;
use tokio::time::Instant;

use super::models::{
    Capabilities, CommitCounts, CommitRequest, CommitResponse, CommitStatus, ContentItem, ErrorEnvelope, Manifest,
    NegotiateResponse, NegotiateStatus, MEDIA_TYPE, PROTOCOL_VERSION, WELL_KNOWN_PATH,
};
use super::transport::{HttpRequest, HttpResponse, Method, Transport};
use crate::error::{codes, DpeError, PushError, Result};
use crate::hashing::{compute_hashes, supported_hash_strategies, DocumentHashes, HashStrategyRef};
use crate::schema::{DocElement, Document};

type TokenFn = dyn Fn() -> BoxFuture<'static, Result<String>> + Send + Sync;

/// Bearer token 来源：静态值，或每次请求前调用的异步函数（用于刷新）。
#[derive(Clone)]
pub enum TokenProvider {
    Static(String),
    Dynamic(Arc<TokenFn>),
}

impl<S: Into<String>> From<S> for TokenProvider {
    fn from(value: S) -> Self {
        Self::Static(value.into())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PushResult {
    pub file_uri: String,
    /// `Unchanged` 可能来自协商（未进入 Phase 2）或 commit
    pub status: CommitStatus,
    /// 服务端权威 doc_hash，应持久化为下次投递的 `base_doc_hash`
    pub doc_hash: String,
    pub hash_strategy_uri: String,
    pub counts: Option<CommitCounts>,
    pub negotiate: Option<NegotiateResponse>,
    pub contents_sent: usize,
    /// 提交次数；大于 1 表示触发了文档层分片
    pub shards: usize,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct PushOptions {
    /// 快速路径：确信服务端已持有全部内容时跳过协商；
    /// 若服务端回 `DPE_INSUFFICIENT_CONTENT`，自动补传一次。
    pub skip_negotiate: bool,
}

/// dpe-push/1 v1 不支持的内容（push-protocol-v1 §10.4），在本地提前拦截。
pub fn check_pushable(doc: &Document) -> Vec<String> {
    doc.iter_elements()
        .filter(|(_, ele)| ele.ele_metadata.image_path.as_deref().is_some_and(|p| !p.is_empty()))
        .map(|(page, ele)| {
            format!(
                "page {} element {}: image_path is not allowed in dpe-push/1, use image_base64 or image_url",
                page.number, ele.element_id
            )
        })
        .collect()
}

pub struct DpePushClient<T: Transport> {
    base_url: String,
    token: Option<TokenProvider>,
    robot_id: Option<String>,
    transport: T,
    max_retries: u32,
    max_retry_after: Duration,
    capabilities_ttl: Duration,
    capabilities: Mutex<Option<(Instant, Capabilities)>>,
}

#[cfg(feature = "reqwest")]
impl DpePushClient<super::transport::ReqwestTransport> {
    /// 使用 reqwest 传输（默认 30 秒超时）。
    pub fn with_reqwest(base_url: impl Into<String>) -> Result<Self> {
        Ok(Self::new(base_url, super::transport::ReqwestTransport::new(Duration::from_secs(30))?))
    }
}

impl<T: Transport> DpePushClient<T> {
    pub fn new(base_url: impl Into<String>, transport: T) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            token: None,
            robot_id: None,
            transport,
            max_retries: 3,
            max_retry_after: Duration::from_secs(60),
            capabilities_ttl: Duration::from_secs(300),
            capabilities: Mutex::new(None),
        }
    }

    pub fn with_token(mut self, token: impl Into<TokenProvider>) -> Self {
        self.token = Some(token.into());
        self
    }

    /// 仅用于服务端防误路由校验，不是路由键。
    pub fn with_robot_id(mut self, robot_id: impl Into<String>) -> Self {
        self.robot_id = Some(robot_id.into());
        self
    }

    pub fn with_max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = max_retries;
        self
    }

    pub fn with_max_retry_after(mut self, max_retry_after: Duration) -> Self {
        self.max_retry_after = max_retry_after;
        self
    }

    pub fn with_capabilities_ttl(mut self, ttl: Duration) -> Self {
        self.capabilities_ttl = ttl;
        self
    }

    pub fn transport(&self) -> &T {
        &self.transport
    }

    // ------------------------------------------------------------ Phase 0

    /// 读取能力文档（带 TTL 缓存）。它只是协商提示，服务端写入路径会重新校验。
    pub async fn discover(&self, refresh: bool) -> Result<Capabilities> {
        if !refresh {
            if let Some((at, caps)) = self.capabilities.lock().expect("capabilities lock").as_ref() {
                if at.elapsed() < self.capabilities_ttl {
                    return Ok(caps.clone());
                }
            }
        }
        let resp = self.send(Method::Get, WELL_KNOWN_PATH, None::<&()>, None, false, &[]).await?;
        let caps: Capabilities = parse(&resp)?;
        *self.capabilities.lock().expect("capabilities lock") = Some((Instant::now(), caps.clone()));
        Ok(caps)
    }

    /// 在 SDK 已实现策略与服务端 allowlist 的交集中选择，优先服务端默认、其次未废弃者。
    pub async fn select_strategy(&self) -> Result<HashStrategyRef> {
        let caps = self.discover(false).await?;
        let supported = supported_hash_strategies();
        let mut candidates: Vec<(bool, HashStrategyRef)> = Vec::new();
        for accepted in &caps.accepted_hash_strategies {
            let Ok(strategy) = accepted.uri.parse::<HashStrategyRef>() else { continue };
            if supported.contains(&strategy) {
                candidates.push((accepted.deprecated_at.is_some(), strategy));
            }
        }
        if let Some(default) = caps.default_hash_strategy_uri.as_deref().and_then(|u| u.parse().ok()) {
            if candidates.iter().any(|(_, s)| *s == default) {
                return Ok(default);
            }
        }
        candidates.sort();
        candidates.into_iter().next().map(|(_, s)| s).ok_or_else(|| {
            let accepted: Vec<&str> = caps.accepted_hash_strategies.iter().map(|s| s.uri.as_str()).collect();
            let supported: Vec<String> = supported.iter().map(HashStrategyRef::to_uri).collect();
            DpeError::NoCompatibleHashStrategy(format!("server accepts {accepted:?}, SDK supports {supported:?}"))
        })
    }

    // ------------------------------------------------------------ Phase 1 / 2

    pub async fn negotiate(&self, manifest: &Manifest) -> Result<NegotiateResponse> {
        let caps = self.discover(false).await?;
        let resp = self.send(Method::Post, &caps.endpoints.negotiate, Some(manifest), Some(&caps), true, &[]).await?;
        parse(&resp)
    }

    pub async fn commit(&self, request: &CommitRequest, idempotency_key: &str) -> Result<CommitResponse> {
        let caps = self.discover(false).await?;
        let headers = [("Idempotency-Key", idempotency_key)];
        let resp = self.send(Method::Post, &caps.endpoints.commit, Some(request), Some(&caps), true, &headers).await?;
        parse(&resp)
    }

    /// 完整投递一个文档（永远是全貌，不支持局部投递）。
    ///
    /// `base_doc_hash` 为上次投递成功时服务端返回的 `doc_hash`，仅用于服务端诊断。
    pub async fn push(&self, doc: &Document, base_doc_hash: Option<String>) -> Result<PushResult> {
        self.push_with(doc, base_doc_hash, PushOptions::default()).await
    }

    pub async fn push_with(
        &self,
        doc: &Document,
        base_doc_hash: Option<String>,
        options: PushOptions,
    ) -> Result<PushResult> {
        let caps = self.discover(false).await?;
        if !caps.protocol_versions.iter().any(|v| v == PROTOCOL_VERSION) {
            return Err(DpeError::Protocol(format!(
                "server supports dpe-push {:?}, SDK speaks {PROTOCOL_VERSION}",
                caps.protocol_versions
            )));
        }
        doc.validate()?;
        let violations = check_pushable(doc);
        if !violations.is_empty() {
            return Err(DpeError::Validation(violations));
        }

        let hashes = compute_hashes(doc, &self.select_strategy().await?)?;
        let manifest = Manifest::build(doc, &hashes, base_doc_hash);
        let elements = index_elements(doc, &hashes);

        let mut negotiated = None;
        let mut missing: HashSet<String> = HashSet::new();
        if !options.skip_negotiate {
            let resp = self.negotiate(&manifest).await?;
            match resp.status {
                NegotiateStatus::Unchanged => {
                    return Ok(PushResult {
                        file_uri: manifest.file_uri.clone(),
                        status: CommitStatus::Unchanged,
                        doc_hash: resp.server_doc_hash.clone().unwrap_or_else(|| manifest.doc_hash.clone()),
                        hash_strategy_uri: manifest.hash_strategy_uri.clone(),
                        counts: None,
                        negotiate: Some(resp),
                        contents_sent: 0,
                        shards: 0,
                    });
                }
                NegotiateStatus::New => missing.extend(elements.iter().map(|(h, _)| h.clone())),
                NegotiateStatus::Gap => missing.extend(resp.missing_content_hashes.iter().cloned()),
            }
            negotiated = Some(resp);
        }

        // 至多两轮：首轮按协商结果补传；若服务端判定内容不足（快速路径或并发变更），按其清单再补一轮。
        for attempt in 0..2 {
            match self.commit_missing(doc, &hashes, &manifest, &elements, &missing).await {
                Ok((result, contents_sent, shards)) => {
                    return Ok(PushResult {
                        file_uri: manifest.file_uri.clone(),
                        status: result.status,
                        doc_hash: result.doc_hash,
                        hash_strategy_uri: result.hash_strategy_uri,
                        counts: Some(result.counts),
                        negotiate: negotiated,
                        contents_sent,
                        shards,
                    });
                }
                Err(DpeError::Push(err)) if err.code == codes::INSUFFICIENT_CONTENT && attempt == 0 => {
                    if let Some(extra) = err.data.get("missing_content_hashes").and_then(Value::as_array) {
                        missing.extend(extra.iter().filter_map(Value::as_str).map(str::to_string));
                    }
                }
                Err(err) => return Err(err),
            }
        }
        unreachable!("commit loop always returns")
    }

    /// 提交缺口内容；请求体超过 `max_payload_bytes` 时自动做文档层分片。返回 (结果, 发送内容数, 分片数)。
    ///
    /// 分片按「缺失内容」切批，而不是按页前缀（push-protocol-v1 §10.3 的前缀方案）：
    /// 第 i 片提交的是**完整文档结构**，但只保留「服务端已有 + 前 i 批」的 element。
    /// 每一片都是合法的完整 manifest，且从不删除最终文档中仍存在的 element——
    /// 对更新已有文档同样适用，不会因前缀截断导致尾部页面先删后建、重复传输与重复学习。
    async fn commit_missing(
        &self,
        doc: &Document,
        hashes: &DocumentHashes,
        manifest: &Manifest,
        elements: &[(String, &DocElement)],
        missing: &HashSet<String>,
    ) -> Result<(CommitResponse, usize, usize)> {
        let caps = self.discover(false).await?;
        let request = CommitRequest { manifest: manifest.clone(), contents: build_contents(elements, missing)? };
        let contents_sent = request.contents.len();
        let limit = match caps.max_payload_bytes {
            Some(limit) if json_size(&request)? > limit => limit,
            // 请求体不同必须换新的幂等键，否则服务端会回放首次响应
            _ => return Ok((self.commit(&request, &new_key()).await?, contents_sent, 1)),
        };

        let budget = limit.saturating_sub(json_size(manifest)? + ENVELOPE_OVERHEAD);
        let batches = plan_batches(request.contents, budget)?;
        let shards = batches.len();
        let mut available: HashSet<String> =
            elements.iter().map(|(h, _)| h.clone()).filter(|h| !missing.contains(h)).collect();
        let mut base_doc_hash = manifest.base_doc_hash.clone();
        let mut last = None;
        for (index, batch) in batches.into_iter().enumerate() {
            available.extend(batch.iter().map(|c| c.content_hash.clone()));
            let shard_manifest = if index + 1 == shards {
                Manifest { base_doc_hash: base_doc_hash.clone(), ..manifest.clone() }
            } else {
                let shard_doc = filter_elements(doc, hashes, &available);
                Manifest::build(&shard_doc, &compute_hashes(&shard_doc, &hashes.strategy)?, base_doc_hash.clone())
            };
            let result = self.commit(&CommitRequest { manifest: shard_manifest, contents: batch }, &new_key()).await?;
            base_doc_hash = Some(result.doc_hash.clone());
            last = Some(result);
        }
        Ok((last.expect("at least one batch"), contents_sent, shards))
    }

    // ------------------------------------------------------------ transport

    async fn headers(&self, authenticated: bool) -> Result<Vec<(String, String)>> {
        let mut headers = vec![("Accept".to_string(), MEDIA_TYPE.to_string())];
        if authenticated {
            if let Some(token) = &self.token {
                let token = match token {
                    TokenProvider::Static(t) => t.clone(),
                    TokenProvider::Dynamic(f) => f().await?,
                };
                headers.push(("Authorization".into(), format!("Bearer {token}")));
            }
            if let Some(rid) = &self.robot_id {
                headers.push(("X-TFRS-Robot-Id".into(), rid.clone()));
            }
        }
        Ok(headers)
    }

    async fn send<B: Serialize + ?Sized>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
        caps: Option<&Capabilities>,
        authenticated: bool,
        extra_headers: &[(&str, &str)],
    ) -> Result<HttpResponse> {
        let mut headers = self.headers(authenticated).await?;
        headers.extend(extra_headers.iter().map(|(k, v)| (k.to_string(), v.to_string())));
        let mut content = None;
        if let Some(body) = body {
            let mut bytes = serde_json::to_vec(body)?;
            // 按解压后大小比较：服务端需按解压后体积设限才能防御压缩炸弹
            if let Some(max) = caps.and_then(|c| c.max_payload_bytes) {
                if bytes.len() as u64 > max {
                    return Err(DpeError::Push(PushError {
                        code: codes::PAYLOAD_TOO_LARGE.into(),
                        message: format!(
                            "request body {} bytes exceeds max_payload_bytes {max}; \
                             split the document into growing prefixes (push-protocol-v1 §10.3)",
                            bytes.len()
                        ),
                        http_status: 413,
                        data: serde_json::json!({ "max_payload_bytes": max }),
                        retry_after: None,
                    }));
                }
            }
            headers.push(("Content-Type".into(), MEDIA_TYPE.into()));
            if caps.is_some_and(|c| c.content_encodings.iter().any(|e| e == "gzip")) {
                let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
                encoder.write_all(&bytes)?;
                bytes = encoder.finish()?;
                headers.push(("Content-Encoding".into(), "gzip".into()));
            }
            content = Some(bytes);
        }

        let request = HttpRequest { method, url: format!("{}{path}", self.base_url), headers, body: content };
        let mut attempt = 0;
        loop {
            let last_attempt = attempt >= self.max_retries;
            match self.transport.send(request.clone()).await {
                // negotiate 只读、commit 有幂等键，网络层失败均可安全重试
                Err(DpeError::Transport(_)) if !last_attempt => {
                    tokio::time::sleep(self.backoff(attempt, None)).await;
                }
                Err(err) => return Err(err),
                Ok(resp) if resp.status < 400 => return Ok(resp),
                Ok(resp) => {
                    let err = parse_error(&resp);
                    if matches!(resp.status, 429 | 503) && !last_attempt {
                        tokio::time::sleep(self.backoff(attempt, err.retry_after)).await;
                    } else {
                        return Err(DpeError::Push(err));
                    }
                }
            }
            attempt += 1;
        }
    }

    fn backoff(&self, attempt: u32, retry_after: Option<f64>) -> Duration {
        let delay = retry_after.map_or_else(
            || Duration::from_millis(500 * 2u64.pow(attempt.min(16))),
            |s| Duration::from_secs_f64(s.max(0.0)),
        );
        delay.min(self.max_retry_after)
    }
}

fn parse<M: DeserializeOwned>(resp: &HttpResponse) -> Result<M> {
    serde_json::from_slice(&resp.body)
        .map_err(|e| DpeError::Protocol(format!("malformed {} from server: {e}", std::any::type_name::<M>())))
}

fn parse_error(resp: &HttpResponse) -> PushError {
    let retry_after = resp.header("Retry-After").and_then(|v| v.trim().parse::<f64>().ok());
    match serde_json::from_slice::<ErrorEnvelope>(&resp.body) {
        Ok(envelope) => PushError {
            code: envelope.code_string(),
            message: envelope.message,
            http_status: resp.status,
            data: envelope.data,
            retry_after,
        },
        Err(_) => PushError {
            code: format!("HTTP_{}", resp.status),
            message: String::from_utf8_lossy(&resp.body).chars().take(500).collect(),
            http_status: resp.status,
            data: Value::Null,
            retry_after,
        },
    }
}

/// content_hash → element，保持文档顺序。相同内容的 element 共享同一 hash，传一次即可。
fn index_elements<'a>(doc: &'a Document, hashes: &DocumentHashes) -> Vec<(String, &'a DocElement)> {
    let mut seen = HashSet::new();
    let mut index = Vec::new();
    for (page, page_hashes) in doc.pages.iter().zip(&hashes.pages) {
        for (ele, hash) in page.elements.iter().zip(&page_hashes.content_hashes) {
            if seen.insert(hash.clone()) {
                index.push((hash.clone(), ele));
            }
        }
    }
    index
}

fn build_contents(elements: &[(String, &DocElement)], missing: &HashSet<String>) -> Result<Vec<ContentItem>> {
    let known: HashSet<&str> = elements.iter().map(|(h, _)| h.as_str()).collect();
    let mut unknown: Vec<&str> = missing.iter().map(String::as_str).filter(|h| !known.contains(h)).collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(DpeError::Protocol(format!(
            "server requested content hashes not present in this document: {unknown:?}"
        )));
    }
    Ok(elements.iter().filter(|(h, _)| missing.contains(h)).map(|(h, ele)| ContentItem::build(h, ele)).collect())
}

/// CommitRequest 外层 JSON 结构与分隔符的预留字节
const ENVELOPE_OVERHEAD: u64 = 64;

fn new_key() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

fn json_size<T: Serialize>(value: &T) -> Result<u64> {
    Ok(serde_json::to_vec(value)?.len() as u64)
}

/// 按文档顺序贪心切批，每批序列化后不超过 `budget`。
fn plan_batches(contents: Vec<ContentItem>, budget: u64) -> Result<Vec<Vec<ContentItem>>> {
    let mut batches: Vec<Vec<ContentItem>> = vec![Vec::new()];
    let mut used = 0;
    for item in contents {
        let size = json_size(&item)? + 1; // 逗号
        if size > budget {
            return Err(DpeError::Push(PushError {
                code: codes::PAYLOAD_TOO_LARGE.into(),
                message: format!(
                    "element {} alone ({size} bytes) exceeds the per-request budget ({budget} bytes); \
                     it cannot be sharded at document level (needs content staging, push-protocol-v1 §10.3 v2)",
                    item.content_hash
                ),
                http_status: 413,
                data: Value::Null,
                retry_after: None,
            }));
        }
        if used + size > budget && !batches.last().expect("non-empty").is_empty() {
            batches.push(Vec::new());
            used = 0;
        }
        batches.last_mut().expect("non-empty").push(item);
        used += size;
    }
    Ok(batches)
}

/// 保留全部页，但每页只保留 content_hash 在 `keep` 中的 element（顺序不变）。
fn filter_elements(doc: &Document, hashes: &DocumentHashes, keep: &HashSet<String>) -> Document {
    let mut filtered = doc.clone();
    for (page, page_hashes) in filtered.pages.iter_mut().zip(&hashes.pages) {
        let mut hashes = page_hashes.content_hashes.iter();
        page.elements.retain(|_| hashes.next().is_some_and(|h| keep.contains(h)));
    }
    filtered
}
