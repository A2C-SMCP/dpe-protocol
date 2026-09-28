//! dpe-push/1 的内存版 Robot 服务端，供测试与本地联调使用（**不是**生产实现）。
//!
//! 与 Python SDK 的 `dpe_protocol.testing.FakeRobotServer` 行为一致：缺口按 `file_uri` 限定、
//! 服务端重算 hash、`content_hash` 不符即拒绝、内容不足回 409、幂等键回放首次响应。
//!
//! ```
//! use std::sync::Arc;
//! use dpe_protocol::push::DpePushClient;
//! use dpe_protocol::testing::FakeRobotServer;
//!
//! let server = Arc::new(FakeRobotServer::default());
//! let client = DpePushClient::new("http://robot.test", server.clone());
//! ```

use std::collections::{HashMap, HashSet};
use std::io::Read as _;
use std::sync::{Mutex, MutexGuard};

use async_trait::async_trait;
use flate2::read::GzDecoder;
use serde_json::{json, Value};

use crate::error::DpeError;
use crate::hashing::{compute_hashes, HashStrategyRef};
use crate::push::models::{Manifest, MEDIA_TYPE, WELL_KNOWN_PATH};
use crate::push::{HttpRequest, HttpResponse, Method, Transport};
use crate::schema::{DocElement, DocPage, Document};

pub const NEGOTIATE_PATH: &str = "/v1/memory/dpe:negotiate";
pub const COMMIT_PATH: &str = "/v1/memory/dpe:commit";

#[derive(Debug, Clone)]
pub struct StoredDocument {
    pub document: Document,
    pub doc_hash: String,
    pub hash_strategy_uri: String,
    /// content_hash → element（本文档旧树，缺口计算只在此范围内进行）
    pub contents: HashMap<String, DocElement>,
}

/// 注入的故障：`(http_status, code, headers)`，每个非发现请求消费一个。
pub type Fault = (u16, String, Vec<(String, String)>);

#[derive(Debug, Clone)]
pub struct FakeState {
    pub accepted_strategies: Vec<String>,
    pub max_payload_bytes: u64,
    pub documents: HashMap<String, StoredDocument>,
    pub faults: Vec<Fault>,
    pub requests: Vec<HttpRequest>,
    idempotency: HashMap<String, HttpResponse>,
}

impl Default for FakeState {
    fn default() -> Self {
        Self {
            accepted_strategies: vec![HashStrategyRef::default_v1().to_uri()],
            max_payload_bytes: 50 * 1024 * 1024,
            documents: HashMap::new(),
            faults: Vec::new(),
            requests: Vec::new(),
            idempotency: HashMap::new(),
        }
    }
}

#[derive(Debug, Default)]
pub struct FakeRobotServer {
    state: Mutex<FakeState>,
}

impl FakeRobotServer {
    pub fn state(&self) -> MutexGuard<'_, FakeState> {
        self.state.lock().expect("fake server state lock")
    }

    /// 已接收请求的解压后 JSON 体（按路径后缀过滤）。
    pub fn request_bodies(&self, path_suffix: &str) -> Vec<Value> {
        self.state().requests.iter().filter(|r| r.path().ends_with(path_suffix)).filter_map(read_json).collect()
    }

    pub fn handle(&self, request: HttpRequest) -> HttpResponse {
        let mut state = self.state();
        state.requests.push(request.clone());
        let path = request.path().to_string();
        if path != WELL_KNOWN_PATH && !state.faults.is_empty() {
            let (status, code, headers) = state.faults.remove(0);
            return error(status, &code, "injected fault", Value::Null, headers);
        }
        match (request.method, path.as_str()) {
            (Method::Get, WELL_KNOWN_PATH) => capabilities(&state),
            (Method::Post, NEGOTIATE_PATH) => match read_json(&request).map(serde_json::from_value::<Manifest>) {
                Some(Ok(manifest)) => negotiate(&state, &manifest),
                _ => error(400, "DPE_MANIFEST_INVALID", "bad manifest", Value::Null, vec![]),
            },
            (Method::Post, COMMIT_PATH) => {
                let key = request.header("Idempotency-Key").unwrap_or_default().to_string();
                if let Some(resp) = state.idempotency.get(&key).filter(|_| !key.is_empty()) {
                    return resp.clone();
                }
                let resp = match read_json(&request) {
                    Some(body) => commit(&mut state, &body),
                    None => error(400, "DPE_MANIFEST_INVALID", "bad body", Value::Null, vec![]),
                };
                if !key.is_empty() && resp.status < 400 {
                    state.idempotency.insert(key, resp.clone());
                }
                resp
            }
            _ => error(404, "NOT_FOUND", &path, Value::Null, vec![]),
        }
    }
}

#[async_trait]
impl Transport for FakeRobotServer {
    async fn send(&self, request: HttpRequest) -> Result<HttpResponse, DpeError> {
        Ok(self.handle(request))
    }
}

fn capabilities(state: &FakeState) -> HttpResponse {
    ok(json!({
        "protocol_versions": ["1"],
        "accepted_hash_strategies": state.accepted_strategies.iter()
            .map(|uri| json!({"uri": uri, "status": "stable", "deprecated_at": null})).collect::<Vec<_>>(),
        "default_hash_strategy_uri": state.accepted_strategies.first(),
        "max_payload_bytes": state.max_payload_bytes,
        "content_encodings": ["gzip", "identity"],
        "delta_supported": true,
        "endpoints": {"negotiate": NEGOTIATE_PATH, "commit": COMMIT_PATH},
    }))
}

fn unique_hashes(manifest: &Manifest) -> Vec<String> {
    let mut seen = HashSet::new();
    manifest.content_hashes().filter(|h| seen.insert(*h)).map(str::to_string).collect()
}

fn negotiate(state: &FakeState, manifest: &Manifest) -> HttpResponse {
    let wanted = unique_hashes(manifest);
    let Some(stored) = state.documents.get(&manifest.file_uri) else {
        return ok(json!({"status": "new", "missing_content_hashes": wanted, "server_doc_exists": false}));
    };
    let base_matched = manifest.base_doc_hash.as_ref().map(|b| *b == stored.doc_hash);
    let same_strategy = stored.hash_strategy_uri == manifest.hash_strategy_uri;
    if same_strategy && stored.doc_hash == manifest.doc_hash {
        return ok(json!({"status": "unchanged", "server_doc_exists": true,
                         "server_doc_hash": stored.doc_hash, "base_doc_hash_matched": base_matched}));
    }
    // 跨策略的 hash 不可比：旧树整体视为不可复用
    let missing: Vec<String> =
        wanted.into_iter().filter(|h| !(same_strategy && stored.contents.contains_key(h))).collect();
    ok(json!({"status": "gap", "missing_content_hashes": missing, "server_doc_exists": true,
              "server_doc_hash": stored.doc_hash, "base_doc_hash_matched": base_matched}))
}

fn commit(state: &mut FakeState, body: &Value) -> HttpResponse {
    let Ok(manifest) = serde_json::from_value::<Manifest>(body["manifest"].clone()) else {
        return error(400, "DPE_MANIFEST_INVALID", "bad manifest", Value::Null, vec![]);
    };
    if !state.accepted_strategies.contains(&manifest.hash_strategy_uri) {
        let data = json!({"accepted": state.accepted_strategies});
        return error(400, "DPE_HASH_STRATEGY_UNSUPPORTED", "strategy not accepted", data, vec![]);
    }
    let Ok(strategy) = manifest.hash_strategy_uri.parse::<HashStrategyRef>() else {
        return error(400, "DPE_MANIFEST_INVALID", "bad strategy uri", Value::Null, vec![]);
    };

    let mut supplied: HashMap<String, DocElement> = HashMap::new();
    let empty = Vec::new();
    for (index, item) in body["contents"].as_array().unwrap_or(&empty).iter().enumerate() {
        let Ok(ele) = serde_json::from_value::<DocElement>(item["element"].clone()) else {
            return error(400, "DPE_MANIFEST_INVALID", "bad element", Value::Null, vec![]);
        };
        let actual = element_hash(&ele, &strategy);
        let expected = item["content_hash"].as_str().unwrap_or_default();
        if actual != expected {
            let data = json!({"expected": expected, "actual": actual, "element_index": index});
            return error(400, "DPE_CONTENT_HASH_MISMATCH", "content hash mismatch", data, vec![]);
        }
        supplied.insert(actual, ele);
    }

    let stored = state.documents.get(&manifest.file_uri);
    let mut pool: HashMap<String, DocElement> = stored
        .filter(|s| s.hash_strategy_uri == manifest.hash_strategy_uri)
        .map(|s| s.contents.clone())
        .unwrap_or_default();
    pool.extend(supplied);
    let missing: Vec<String> = unique_hashes(&manifest).into_iter().filter(|h| !pool.contains_key(h)).collect();
    if !missing.is_empty() {
        let data = json!({"missing_content_hashes": missing});
        return error(409, "DPE_INSUFFICIENT_CONTENT", "missing contents", data, vec![]);
    }

    let document = Document {
        file_uri: match manifest.file_uri.parse() {
            Ok(uri) => uri,
            Err(_) => return error(400, "DPE_MANIFEST_INVALID", "bad file_uri", Value::Null, vec![]),
        },
        file_type: match manifest.file_type.parse() {
            Ok(ft) => ft,
            Err(_) => return error(400, "DPE_MANIFEST_INVALID", "bad file_type", Value::Null, vec![]),
        },
        keywords: None,
        doc_metadata: match serde_json::from_value(manifest.doc_metadata.clone()) {
            Ok(meta) => meta,
            Err(_) => return error(400, "DPE_MANIFEST_INVALID", "bad doc_metadata", Value::Null, vec![]),
        },
        pages: manifest
            .pages
            .iter()
            .map(|p| {
                let elements = p.elements.iter().map(|e| pool[&e.content_hash].clone()).collect();
                DocPage::new(p.number, p.title.clone(), elements)
            })
            .collect(),
    };
    // 服务端重算是唯一权威；manifest 中的 page_hash / doc_hash 只作协商提示
    let hashes = compute_hashes(&document, &strategy).expect("strategy already validated");
    let new_hashes: HashSet<String> = hashes.all_content_hashes().into_iter().map(str::to_string).collect();
    let old_hashes: HashSet<String> = stored.map(|s| s.contents.keys().cloned().collect()).unwrap_or_default();
    let status = match stored {
        None => "created",
        Some(s) if s.doc_hash == hashes.doc_hash => "unchanged",
        Some(_) => "updated",
    };
    let counts = json!({
        "elements_added": new_hashes.difference(&old_hashes).count(),
        "elements_removed": old_hashes.difference(&new_hashes).count(),
        "elements_unchanged": new_hashes.intersection(&old_hashes).count(),
    });
    let contents = new_hashes.iter().map(|h| (h.clone(), pool[h].clone())).collect();
    state.documents.insert(
        manifest.file_uri.clone(),
        StoredDocument {
            document,
            doc_hash: hashes.doc_hash.clone(),
            hash_strategy_uri: manifest.hash_strategy_uri.clone(),
            contents,
        },
    );
    ok(json!({
        "status": status,
        "doc_hash": hashes.doc_hash,
        "hash_strategy_uri": manifest.hash_strategy_uri,
        "counts": counts,
    }))
}

fn element_hash(ele: &DocElement, strategy: &HashStrategyRef) -> String {
    let doc = Document::new(
        "dpe://fake/element".parse().expect("static uri"),
        crate::schema::FileType::Unk,
        vec![DocPage::new(0, None, vec![ele.clone()])],
    );
    compute_hashes(&doc, strategy).expect("strategy already validated").pages[0].content_hashes[0].clone()
}

fn read_json(request: &HttpRequest) -> Option<Value> {
    let raw = request.body.as_ref()?;
    let bytes = if request.header("Content-Encoding") == Some("gzip") {
        let mut out = Vec::new();
        GzDecoder::new(raw.as_slice()).read_to_end(&mut out).ok()?;
        out
    } else {
        raw.clone()
    };
    serde_json::from_slice(&bytes).ok()
}

fn ok(payload: Value) -> HttpResponse {
    HttpResponse {
        status: 200,
        headers: vec![("Content-Type".into(), MEDIA_TYPE.into())],
        body: serde_json::to_vec(&payload).expect("json"),
    }
}

fn error(status: u16, code: &str, message: &str, data: Value, headers: Vec<(String, String)>) -> HttpResponse {
    HttpResponse {
        status,
        headers,
        body: serde_json::to_vec(&json!({"code": code, "message": message, "data": data})).expect("json"),
    }
}
