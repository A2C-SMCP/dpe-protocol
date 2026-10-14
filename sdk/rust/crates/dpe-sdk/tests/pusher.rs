//! 增量推送器（#23）：`StatefulPusher` 的 I/O 场景，与 Python 侧 `tests/test_pusher.py` 逐条对等。
//!
//! 协议路径经假服务端走**真实** TCP 与 reqwest / tokio 往返（[`MockServer::start_fn`] 按请求
//! 路由应答）。假服务端只模拟网络层，不实现服务端语义：
//!
//! - 筛选读取（`POST heads`、`HEAD documents`）按用例维护的「远端视图」应答，视图就是用例对
//!   服务端当前状态的声明；
//! - 写请求（commit / delete / move / 暂存）按用例逐条给出的脚本应答，可附带「应答后视图变为
//!   …」，用来表达竞态写入与「已生效但响应丢失」。
//!
//! 判定表的逐格覆盖在 `src/pusher.rs` 的纯逻辑单测里；这里覆盖经核心与传输层之后的行为（写
//! 结果标记、重读、意图的持久化时序、并发与保序）。对参考服务端跑同一场景集的跨语言验收归 #25。

#![cfg(feature = "http")]

mod http_common;
mod protocol_common;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::{BlobSource, Precondition};
use dpe_sdk::pusher::{
    MemoryStateStore, PushOp, PushResult, PushStatus, Side, StateSnapshot, StateStore,
    StatefulPusher, StoreError,
};
use dpe_sdk::{ErrorKind, ExpandedDocument};
use serde_json::{json, Value};

use http_common::{capabilities_body, committed, heads_reply, MockServer, Recorded, Reply};
use protocol_common::{doc, doc_hash, percent_decode};

const A: &str = "dpe://docs/a.md";
const B: &str = "dpe://docs/b.md";

// ---------------------------------------------------------------------- 假服务端

/// 写请求的一条脚本应答：回复 `reply`，之后把视图中的条目改为 `then`（表达服务端已生效的
/// 状态，或竞态中他人的写入）。
#[derive(Clone)]
struct Step {
    reply: Reply,
    then: Vec<(String, Option<String>)>,
}

fn reply(reply: Reply) -> Step {
    Step {
        reply,
        then: Vec::new(),
    }
}

fn reply_then(reply: Reply, uri: &str, head: Option<String>) -> Step {
    Step {
        reply,
        then: vec![(uri.to_owned(), head)],
    }
}

/// 写请求的路由键。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum Route {
    Commit(String),
    Delete(String),
    Move(String),
    Negotiate(String),
    Staging,
}

#[derive(Default)]
struct Fake {
    /// 远端视图：file_uri → doc_hash（缺省为不存在）。
    view: Mutex<HashMap<String, String>>,
    /// 写请求的脚本（按路由键排队，耗尽后重复最后一条）。
    writes: Mutex<HashMap<Route, VecDeque<Step>>>,
    /// `HEAD documents` 的覆盖应答（重读失败的用例）。
    head_override: Mutex<Option<Reply>>,
    /// 写请求的处理延迟（并发用例）。
    delay: Mutex<Option<Duration>>,
    /// 按路由追加的处理延迟（让某一条投递的请求在途更久，构造确定的交错）。
    route_delay: Mutex<HashMap<Route, Duration>>,
    /// 已收到的写请求数（commit / delete / move；意图持久化时序的判别点）。
    write_count: AtomicUsize,
    in_flight: AtomicUsize,
    peak: AtomicUsize,
    /// capabilities 的 `max_payload_bytes`（缺省大，走快路径）。
    max_payload_bytes: Mutex<u64>,
    /// 读被拒的 URI（#145）：含其中任一 URI 的批量读整体 403，单篇读该 URI 同样 403。
    denied_reads: Mutex<HashSet<String>>,
    /// 一切批量读都 403（拒绝与具体 URI 无关，#145）。
    reject_all_batches: AtomicBool,
    /// 单篇读得到 400 `DPE_VALIDATION` 的 URI（与 `denied_reads` 的 403 区分来源，#145）。
    invalid_reads: Mutex<HashSet<String>>,
    /// 批量读的覆盖应答（#145）。
    batch_override: Mutex<Option<Reply>>,
    /// 单篇读的处理延迟与并发计数（定位的并发上限，#145）。
    head_delay: Mutex<Option<Duration>>,
    head_in_flight: AtomicUsize,
    head_peak: AtomicUsize,
}

impl Fake {
    fn new() -> Arc<Fake> {
        let fake = Fake::default();
        *fake.max_payload_bytes.lock().unwrap() = 8_388_608;
        Arc::new(fake)
    }

    fn set(&self, uri: &str, head: Option<String>) {
        let mut view = self.view.lock().unwrap();
        match head {
            Some(value) => view.insert(uri.to_owned(), value),
            None => view.remove(uri),
        };
    }

    fn get(&self, uri: &str) -> Option<String> {
        self.view.lock().unwrap().get(uri).cloned()
    }

    fn script(&self, route: Route, steps: Vec<Step>) {
        self.writes.lock().unwrap().insert(route, steps.into());
    }

    fn server(self: &Arc<Self>) -> MockServer {
        let fake = Arc::clone(self);
        MockServer::start_fn(move |request| fake.respond(request))
    }

    fn server_closing_after(self: &Arc<Self>, close_after: usize) -> MockServer {
        let fake = Arc::clone(self);
        MockServer::start_fn_closing_after(move |request| fake.respond(request), close_after)
    }

    fn respond(&self, request: &Recorded) -> Reply {
        let (path, query) = request
            .target
            .split_once('?')
            .unwrap_or((request.target.as_str(), ""));
        let uri = query
            .strip_prefix("uri=")
            .map(percent_decode)
            .unwrap_or_default();
        let route = match (request.method.as_str(), path) {
            ("GET", "/capabilities") => {
                let mut body = capabilities_body();
                body["limits"]["batch_head_max"] = json!(100);
                body["limits"]["max_payload_bytes"] =
                    json!(*self.max_payload_bytes.lock().unwrap());
                return Reply::json(200, body);
            }
            ("POST", "/heads") => {
                let uris = body_json(request)["uris"].clone();
                if let Some(reply) = self.batch_override.lock().unwrap().clone() {
                    return reply;
                }
                let mut denied = self.denied_reads.lock().unwrap().clone();
                denied.extend(self.invalid_reads.lock().unwrap().iter().cloned());
                let rejected = self.reject_all_batches.load(Ordering::SeqCst)
                    || uris.as_array().is_some_and(|list| {
                        list.iter()
                            .any(|uri| denied.contains(uri.as_str().unwrap_or("")))
                    });
                drop(denied);
                if rejected {
                    return problem("DPE_FORBIDDEN", 403);
                }
                let heads: Vec<Option<String>> = uris
                    .as_array()
                    .expect("heads 请求体带 uris")
                    .iter()
                    .map(|uri| self.get(uri.as_str().expect("uri 是字符串")))
                    .collect();
                return heads_reply(&heads);
            }
            ("HEAD", "/documents") => {
                let now = self.head_in_flight.fetch_add(1, Ordering::SeqCst) + 1;
                self.head_peak.fetch_max(now, Ordering::SeqCst);
                if let Some(delay) = *self.head_delay.lock().unwrap() {
                    std::thread::sleep(delay);
                }
                self.head_in_flight.fetch_sub(1, Ordering::SeqCst);
                if let Some(reply) = self.head_override.lock().unwrap().clone() {
                    return reply;
                }
                if self.invalid_reads.lock().unwrap().contains(&uri) {
                    return Reply::Response {
                        status: 400,
                        headers: vec![("DPE-Error-Code".to_owned(), "DPE_VALIDATION".to_owned())],
                        body: Vec::new(),
                    };
                }
                if self.denied_reads.lock().unwrap().contains(&uri) {
                    return Reply::Response {
                        status: 403,
                        headers: vec![("DPE-Error-Code".to_owned(), "DPE_FORBIDDEN".to_owned())],
                        body: Vec::new(),
                    };
                }
                return match self.get(&uri) {
                    Some(value) => Reply::Response {
                        status: 200,
                        headers: vec![
                            ("DPE-Doc-Hash".to_owned(), value.clone()),
                            ("ETag".to_owned(), format!("\"{value}\"")),
                        ],
                        body: Vec::new(),
                    },
                    None => Reply::Response {
                        status: 404,
                        headers: vec![("DPE-Error-Code".to_owned(), "DPE_NOT_FOUND".to_owned())],
                        body: Vec::new(),
                    },
                };
            }
            ("PUT", "/documents") => Route::Commit(uri),
            ("DELETE", "/documents") => Route::Delete(uri),
            ("POST", "/move") => Route::Move(
                body_json(request)["from_uri"]
                    .as_str()
                    .expect("move 请求体带 from_uri")
                    .to_owned(),
            ),
            ("POST", "/negotiate") => Route::Negotiate(
                body_json(request)["file_uri"]
                    .as_str()
                    .expect("negotiate 请求体带 file_uri")
                    .to_owned(),
            ),
            ("PUT", staging) if staging.starts_with("/staging/") => Route::Staging,
            other => panic!("假服务端没有这条路由：{other:?}"),
        };
        if matches!(route, Route::Commit(_) | Route::Delete(_) | Route::Move(_)) {
            self.write_count.fetch_add(1, Ordering::SeqCst);
        }
        let now = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(now, Ordering::SeqCst);
        if let Some(delay) = *self.delay.lock().unwrap() {
            std::thread::sleep(delay);
        }
        let extra = self.route_delay.lock().unwrap().get(&route).copied();
        if let Some(extra) = extra {
            std::thread::sleep(extra);
        }
        self.in_flight.fetch_sub(1, Ordering::SeqCst);
        let step = {
            let mut writes = self.writes.lock().unwrap();
            let queue = writes
                .get_mut(&route)
                .unwrap_or_else(|| panic!("用例没有为 {route:?} 给出应答"));
            if queue.len() > 1 {
                queue.pop_front().expect("非空")
            } else {
                queue.front().expect("非空").clone()
            }
        };
        for (uri, head) in step.then {
            self.set(&uri, head);
        }
        step.reply
    }
}

/// 请求体的 JSON（按需解 gzip）。
fn body_json(request: &Recorded) -> Value {
    if request.header("content-encoding") == Some("gzip") {
        serde_json::from_slice(&request.gunzip()).expect("请求体是 JSON")
    } else {
        request.json()
    }
}

fn client(server: &MockServer) -> DpeClient {
    DpeClient::builder(server.remote())
        .max_resends(1)
        .max_retries(0)
        .timeout(Some(Duration::from_secs(5)))
        .build()
        .expect("客户端")
}

fn pusher<'c>(client: &'c DpeClient, store: &Arc<MemoryStateStore>) -> StatefulPusher<'c> {
    StatefulPusher::with_store(client, Arc::clone(store) as Arc<dyn StateStore>, 4).expect("推送器")
}

fn state(store: &MemoryStateStore, server: &MockServer) -> StateSnapshot {
    store.load(&server.remote()).unwrap().unwrap_or_default()
}

fn text(value: &str) -> ExpandedDocument {
    doc(&[&[value]])
}

fn docs(pairs: &[(&str, &ExpandedDocument)]) -> Vec<(String, ExpandedDocument)> {
    pairs
        .iter()
        .map(|(uri, document)| ((*uri).to_owned(), (*document).clone()))
        .collect()
}

fn none() -> HashMap<String, Option<String>> {
    HashMap::new()
}

fn statuses(results: &[PushResult]) -> Vec<PushStatus> {
    results.iter().map(|result| result.status).collect()
}

fn synced(pairs: &[(&str, String)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(uri, value)| ((*uri).to_owned(), value.clone()))
        .collect()
}

fn intents(pairs: &[(&str, Option<String>)]) -> Intents {
    pairs
        .iter()
        .map(|(uri, value)| ((*uri).to_owned(), value.clone()))
        .collect()
}

fn problem(code: &str, status: u16) -> Reply {
    Reply::problem(code, status, &[])
}

/// 首推 created 的脚本：应答后视图记为该文档。
fn created(uri: &str, document: &ExpandedDocument) -> Step {
    reply_then(
        committed(document, "created", 1, 0, 0),
        uri,
        Some(doc_hash(document)),
    )
}

fn updated(uri: &str, document: &ExpandedDocument) -> Step {
    reply_then(
        committed(document, "updated", 1, 1, 0),
        uri,
        Some(doc_hash(document)),
    )
}

fn moved_reply(base: &str) -> Reply {
    Reply::json_with(200, json!({"doc_hash": base}), &[("DPE-Doc-Hash", base)])
}

fn count(server: &MockServer, method: &str, path: &str) -> usize {
    server
        .requests()
        .iter()
        .filter(|request| request.method == method && request.target.starts_with(path))
        .count()
}

// ---------------------------------------------------------------------- 判定与缓存

#[tokio::test]
async fn push_creates_then_achieved_without_writes() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let hello = text("hello");
    fake.script(Route::Commit(A.into()), vec![created(A, &hello)]);
    let mut pusher = pusher(&client, &store);

    let first = pusher
        .push(&docs(&[(A, &hello)]), &none(), None)
        .await
        .unwrap();
    assert_eq!(statuses(&first), [PushStatus::Created]);
    assert!(first[0].delta.is_some());
    let before = server.count();

    let second = pusher
        .push(&docs(&[(A, &hello)]), &none(), None)
        .await
        .unwrap();
    assert_eq!(statuses(&second), [PushStatus::Achieved]);
    assert_eq!(
        second[0].remote.as_ref().unwrap().doc_hash,
        Some(doc_hash(&hello))
    );
    // 第二遍只有筛选读取（POST heads），没有写入
    let rest: Vec<String> = server.requests()[before..]
        .iter()
        .map(|request| request.method.clone())
        .collect();
    assert_eq!(rest, ["POST"]);
    assert_eq!(count(&server, "PUT", "/documents"), 1);
}

#[tokio::test]
async fn push_without_cache_rerun_is_equivalent_and_sends_nothing() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let hello = text("hello");
    fake.script(Route::Commit(A.into()), vec![created(A, &hello)]);
    let mut cached = pusher(&client, &Arc::new(MemoryStateStore::new()));
    cached
        .push(&docs(&[(A, &hello)]), &none(), None)
        .await
        .unwrap();
    let start = server.count();
    let first_rerun = cached
        .push(&docs(&[(A, &hello)]), &none(), None)
        .await
        .unwrap();

    // 删缓存：换一个空 store 的推送器（同一客户端、同一 remote）
    let mut uncached = pusher(&client, &Arc::new(MemoryStateStore::new()));
    let second_rerun = uncached
        .push(&docs(&[(A, &hello)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(view(&first_rerun), view(&second_rerun));
    assert_eq!(statuses(&second_rerun), [PushStatus::Achieved]);
    let rerun: Vec<(String, String)> = server.requests()[start..]
        .iter()
        .map(|request| (request.method.clone(), request.target.clone()))
        .collect();
    assert_eq!(rerun.len(), 2, "两次重跑各一个 POST heads：{rerun:?}");
    assert!(rerun.iter().all(|(method, _)| method == "POST"));
}

/// 回报的可比较视图（错误按 code 归约）。
fn view(results: &[PushResult]) -> Vec<String> {
    results
        .iter()
        .map(|result| {
            format!(
                "{}|{}|{}|{:?}|{:?}|{:?}|{:?}|{:?}|{:?}|{:?}|{}",
                result.op.as_str(),
                result.file_uri,
                result.status,
                result.precondition,
                result.delta,
                result.remote,
                result.to_uri,
                result.side,
                result
                    .error
                    .as_ref()
                    .map(|error| error.code().map(str::to_owned)),
                result.remote_error.as_ref().map(|error| error.kind()),
                result.unknown,
            )
        })
        .collect()
}

#[tokio::test]
async fn mid_page_insert_uploads_only_new_objects() {
    // 验收②：中间插一页，只上传新页对象与新增元素对象（小 max_payload_bytes 强制走暂存）。
    // 缺失清单由服务端给出（这里按两版文档之差脚本化），判别点是推送器经 deliver 只上传
    // 清单里的对象、引用会话提交，不回退快路径整篇重发。
    let fake = Fake::new();
    *fake.max_payload_bytes.lock().unwrap() = 1500;
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let (a, b, c, d) = (
        "a".repeat(300),
        "b".repeat(300),
        "c".repeat(300),
        "d".repeat(300),
    );
    let new_page = "NEWPAGE".repeat(100);
    let doc1 = doc(&[&[&a, &b], &[&c], &[&d]]);
    let doc2 = doc(&[&[&a, &b], &[&new_page], &[&c], &[&d]]);
    // 首推已完成（L = R = doc1）；只观察第二遍的流量
    fake.set(A, Some(doc_hash(&doc1)));
    store
        .save(
            &server.remote(),
            &StateSnapshot::new(synced(&[(A, doc_hash(&doc1))]), BTreeMap::new()),
        )
        .unwrap();
    let mut pusher = pusher(&client, &store);

    let old = doc1.hashes(dpe_sdk::CONTRACT).unwrap();
    let new = doc2.hashes(dpe_sdk::CONTRACT).unwrap();
    let new_page_hash = new.pages[1].page_hash.clone();
    let new_element = new.pages[1].elements[0].clone();
    assert!(old.pages.iter().all(|page| page.page_hash != new_page_hash));
    fake.script(
        Route::Negotiate(A.into()),
        vec![reply(Reply::json(
            200,
            json!({
                "missing_pages": [new_page_hash],
                "missing_content_hashes": [new_element],
                "staging_session": {"id": "s1", "expires_at": "2026-10-01T00:00:00Z"},
            }),
        ))],
    );
    fake.script(
        Route::Staging,
        vec![reply(Reply::json_with(
            201,
            json!({}),
            &[("DPE-Session-Expires", "2026-10-01T00:00:00Z")],
        ))],
    );
    fake.script(
        Route::Commit(A.into()),
        vec![reply_then(
            committed(&doc2, "updated", 1, 0, 4),
            A,
            Some(doc_hash(&doc2)),
        )],
    );

    let results = pusher
        .push(&docs(&[(A, &doc2)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Updated]);
    let delta = results[0].delta.expect("updated 带 delta");
    assert_eq!((delta.added, delta.removed), (1, 0));
    assert_eq!(
        results[0].precondition,
        Some(Precondition::BaseHash(doc_hash(&doc1)))
    );
    let uploads: Vec<String> = server
        .requests()
        .iter()
        .filter(|request| request.method == "PUT" && request.target.starts_with("/staging/"))
        .map(|request| request.target.clone())
        .collect();
    assert_eq!(
        uploads,
        [
            format!("/staging/s1/pages/{new_page_hash}"),
            format!("/staging/s1/objects/{new_element}"),
        ]
    );
    // 提交引用会话、不内联整篇（快路径会带全部页与元素对象）
    let commit = server
        .requests()
        .into_iter()
        .find(|request| request.method == "PUT" && request.target.starts_with("/documents"))
        .expect("有 commit");
    let body = body_json(&commit);
    assert_eq!(body["staging_session"], json!("s1"));
    assert!(body.get("objects").map_or(true, |objects| objects
        .as_array()
        .map_or(true, Vec::is_empty)));
}

#[tokio::test]
async fn old_contract_state_entries_are_treated_as_missing() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = text("v1");
    let v2 = text("v2");
    fake.set(A, Some(doc_hash(&v1)));
    // 伪造一条旧契约的同步状态：若被当成 L，服务端按值比较必然 412
    let stale = format!("{}:{}", dpe_sdk::DRILL_CONTRACT, "9".repeat(64));
    store
        .save(
            &server.remote(),
            &StateSnapshot::new(synced(&[(A, stale)]), BTreeMap::new()),
        )
        .unwrap();
    fake.script(Route::Commit(A.into()), vec![updated(A, &v2)]);
    let mut reloaded = pusher(&client, &store);

    let changed = reloaded
        .push(&docs(&[(A, &v2)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&changed), [PushStatus::Updated]);
    assert_eq!(
        changed[0].precondition,
        Some(Precondition::BaseHash(doc_hash(&v1)))
    );
}

// ---------------------------------------------------------------------- 冲突

/// 已推 v1（L = R = v1）的推送器与假服务端。
async fn after_v1(fake: &Arc<Fake>, client: &DpeClient, store: &Arc<MemoryStateStore>) {
    let v1 = text("v1");
    fake.script(Route::Commit(A.into()), vec![created(A, &v1)]);
    let mut first = pusher(client, store);
    assert_eq!(
        statuses(&first.push(&docs(&[(A, &v1)]), &none(), None).await.unwrap()),
        [PushStatus::Created]
    );
}

#[tokio::test]
async fn conflict_from_read_is_reported_without_overwriting() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = text("theirs");
    fake.set(A, Some(doc_hash(&theirs))); // 他人改写
    let before = count(&server, "PUT", "/documents");

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v2"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        Some(doc_hash(&theirs))
    );
    assert_eq!(
        (results[0].precondition.clone(), results[0].unknown),
        (None, false)
    );
    assert_eq!(count(&server, "PUT", "/documents"), before, "本轮没有发写");
}

#[tokio::test]
async fn race_conflict_is_detected_and_reread() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = text("theirs");
    // 读后、写前他人抢先：写请求得到 412，视图变为他人的内容
    fake.script(
        Route::Commit(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            Some(doc_hash(&theirs)),
        )],
    );

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v2"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        Some(doc_hash(&theirs))
    );
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_PRECONDITION_FAILED")
    );
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn conflict_when_the_document_was_deleted_by_someone_else() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.set(A, None); // 他人删除

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v2"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, None);
    assert_eq!(results[0].precondition, None);
    assert_eq!(count(&server, "PUT", "/documents"), 1, "未被复活");
}

#[tokio::test]
async fn expected_base_override() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let (theirs, v2, v3) = (text("theirs"), text("v2"), text("v3"));
    fake.set(A, Some(doc_hash(&theirs)));
    fake.script(Route::Commit(A.into()), vec![updated(A, &v2)]);
    let mut pusher = pusher(&client, &store);

    // 以重读到的值作 base 重提（source_wins 等策略归上层）
    let base = HashMap::from([(A.to_owned(), Some(doc_hash(&theirs)))]);
    let resubmitted = pusher.push(&docs(&[(A, &v2)]), &base, None).await.unwrap();
    assert_eq!(statuses(&resubmitted), [PushStatus::Updated]);
    assert_eq!(
        resubmitted[0].precondition,
        Some(Precondition::BaseHash(doc_hash(&theirs)))
    );

    // 声明「不存在」而已存在 → 冲突（不静默覆盖）
    let absent = HashMap::from([(A.to_owned(), None)]);
    let conflicted = pusher
        .push(&docs(&[(A, &v3)]), &absent, None)
        .await
        .unwrap();
    assert_eq!(statuses(&conflicted), [PushStatus::Conflict]);

    // 声明「不存在」但内容已相同：已达成先于矛盾判定
    let achieved = pusher
        .push(&docs(&[(A, &v2)]), &absent, None)
        .await
        .unwrap();
    assert_eq!(statuses(&achieved), [PushStatus::Achieved]);

    // 声明「不存在」且确实不存在 → if_absent 新建
    let fresh = "dpe://docs/fresh.md";
    fake.script(Route::Commit(fresh.into()), vec![created(fresh, &v2)]);
    let fresh_absent = HashMap::from([(fresh.to_owned(), None)]);
    let created = pusher
        .push(&docs(&[(fresh, &v2)]), &fresh_absent, None)
        .await
        .unwrap();
    assert_eq!(statuses(&created), [PushStatus::Created]);
    assert_eq!(created[0].precondition, Some(Precondition::IfAbsent));
}

// ---------------------------------------------------------------------- 意图日志

/// 意图表（file_uri → 预期的 doc_hash 或 `None`＝不存在）。
type Intents = BTreeMap<String, Option<String>>;

/// 记录每次 `save` 时服务端已收到的写请求数与意图（意图持久化时序的判别点）。
struct RecordingStore {
    inner: MemoryStateStore,
    fake: Arc<Fake>,
    saves: Mutex<Vec<(usize, Intents)>>,
}

impl StateStore for RecordingStore {
    fn load(&self, remote: &str) -> Result<Option<StateSnapshot>, StoreError> {
        self.inner.load(remote)
    }

    fn save(&self, remote: &str, snapshot: &StateSnapshot) -> Result<(), StoreError> {
        self.saves.lock().unwrap().push((
            self.fake.write_count.load(Ordering::SeqCst),
            snapshot.intents.clone(),
        ));
        self.inner.save(remote, snapshot)
    }
}

#[tokio::test]
async fn intents_are_saved_before_the_write_is_sent() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let v1 = text("v1");
    fake.script(Route::Commit(A.into()), vec![created(A, &v1)]);
    let store = Arc::new(RecordingStore {
        inner: MemoryStateStore::new(),
        fake: Arc::clone(&fake),
        saves: Mutex::new(Vec::new()),
    });
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();

    pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .unwrap();

    let saves = store.saves.lock().unwrap().clone();
    assert_eq!(
        saves,
        [
            (0, intents(&[(A, Some(doc_hash(&v1)))])), // 写前：意图已持久化、写请求尚未发出
            (1, BTreeMap::new()),                      // 写后：结果确定、意图清除
        ]
    );
}

/// 可开关的 store：`failing` 置位时 `save` 失败（`load` 照常）。
#[derive(Default)]
struct FlakyStore {
    inner: MemoryStateStore,
    failing: std::sync::atomic::AtomicBool,
}

impl StateStore for FlakyStore {
    fn load(&self, remote: &str) -> Result<Option<StateSnapshot>, StoreError> {
        self.inner.load(remote)
    }

    fn save(&self, remote: &str, snapshot: &StateSnapshot) -> Result<(), StoreError> {
        if self.failing.load(Ordering::SeqCst) {
            return Err("disk full".into());
        }
        self.inner.save(remote, snapshot)
    }
}

#[tokio::test]
async fn failed_intent_save_aborts_without_writing_and_keeps_only_the_reconciled_state() {
    // 上一轮 v2 结果未知（意图 A → v2 保留，实际已生效）；本轮 push(v3) 的判定先对账
    // （R == 意图值 → L 推进到 v2，来自真实读到的远端值，保留），再持久化新意图 A → v3 时
    // save 失败：新意图 MUST NOT 进入内存，对账结果不回滚
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(FlakyStore::default());
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();
    let (v1, v2) = (text("v1"), text("v2"));
    fake.script(Route::Commit(A.into()), vec![created(A, &v1)]);
    pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .unwrap();
    fake.script(
        Route::Commit(A.into()),
        vec![reply_then(Reply::Close, A, Some(doc_hash(&v2)))],
    );
    let unknown = pusher
        .push(&docs(&[(A, &v2)]), &none(), None)
        .await
        .unwrap();
    assert!(unknown[0].unknown);
    assert_eq!(
        pusher.snapshot().intents,
        intents(&[(A, Some(doc_hash(&v2)))])
    );
    let writes = fake.write_count.load(Ordering::SeqCst);
    store.failing.store(true, Ordering::SeqCst);

    let error = pusher
        .push(&docs(&[(A, &text("v3"))]), &none(), None)
        .await
        .expect_err("save 失败应整批中止");

    assert_eq!(error.kind(), ErrorKind::StateStore);
    assert!(!error.outcome_unknown(), "写请求确定未发出");
    assert_eq!(
        std::error::Error::source(&error).map(ToString::to_string),
        Some("disk full".to_owned())
    );
    assert_eq!(fake.write_count.load(Ordering::SeqCst), writes, "未发写");
    let snapshot = pusher.snapshot();
    assert_eq!(
        snapshot.intents,
        BTreeMap::new(),
        "本次新增的意图（A → v3）没有进入内存"
    );
    assert_eq!(
        snapshot.synced,
        synced(&[(A, doc_hash(&v2))]),
        "对账结果保留（来自真实读到的远端值）"
    );
}

#[tokio::test]
async fn failed_intent_save_does_not_mask_a_later_foreign_write() {
    // 复现序列：L = R = v1 → push(v2) 时意图持久化失败 → 他人写下同值 v2 → push(v3)
    // 必须判冲突（L = v1 ≠ R = v2）；若内存残留 A → v2 的意图，对账会把 L 推到 v2、以它作
    // base 静默覆盖他人的写入
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(FlakyStore::default());
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();
    let (v1, v2) = (text("v1"), text("v2"));
    fake.script(Route::Commit(A.into()), vec![created(A, &v1)]);
    pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .unwrap();
    let before = pusher.snapshot();
    store.failing.store(true, Ordering::SeqCst);
    pusher
        .push(&docs(&[(A, &v2)]), &none(), None)
        .await
        .expect_err("save 失败");
    // 本轮没有可对账的待定意图：内存状态与失败前完全一致（A → v2 的意图没有留下）
    assert_eq!(pusher.snapshot(), before);
    store.failing.store(false, Ordering::SeqCst);
    fake.set(A, Some(doc_hash(&v2))); // 他人写下同值
    let writes = fake.write_count.load(Ordering::SeqCst);

    let results = pusher
        .push(&docs(&[(A, &text("v3"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        Some(doc_hash(&v2))
    );
    assert_eq!(
        fake.write_count.load(Ordering::SeqCst),
        writes,
        "未覆盖他人写入"
    );
}

#[tokio::test]
async fn unknown_outcome_keeps_the_intent_and_reconciles_next_round() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let (v1, v2, v3) = (text("v1"), text("v2"), text("v3"));
    // 服务端已生效、响应丢失（重发同样丢失）
    fake.script(
        Route::Commit(A.into()),
        vec![reply_then(Reply::Close, A, Some(doc_hash(&v2)))],
    );
    let mut pusher = pusher(&client, &store);

    let unknown = pusher
        .push(&docs(&[(A, &v2)]), &none(), None)
        .await
        .unwrap();
    assert_eq!(statuses(&unknown), [PushStatus::Failed]);
    assert!(unknown[0].unknown);
    let snapshot = state(&store, &server);
    assert_eq!(snapshot.intents, intents(&[(A, Some(doc_hash(&v2)))]));
    assert_eq!(snapshot.synced, synced(&[(A, doc_hash(&v1))]));

    fake.script(Route::Commit(A.into()), vec![updated(A, &v3)]);
    let reconciled = pusher
        .push(&docs(&[(A, &v3)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&reconciled), [PushStatus::Updated]);
    // 判别点：base 是掉响应那次提交的 doc_hash（对账推进后的 L）
    assert_eq!(
        reconciled[0].precondition,
        Some(Precondition::BaseHash(doc_hash(&v2)))
    );
    let snapshot = state(&store, &server);
    assert_eq!(snapshot.intents, BTreeMap::new());
    assert_eq!(snapshot.synced, synced(&[(A, doc_hash(&v3))]));
}

#[tokio::test]
async fn not_sent_failures_are_determined_and_clear_the_intent() {
    // capabilities 与 heads 之后关闭监听：写请求建连失败（确定未发出）
    let fake = Fake::new();
    let server = fake.server_closing_after(2);
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v1"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(
        results[0].error.as_ref().unwrap().kind(),
        ErrorKind::Transport
    );
    assert!(!results[0].unknown, "确定未发出 ≠ 结果未知");
    let snapshot = state(&store, &server);
    assert_eq!(
        (snapshot.intents, snapshot.synced),
        (BTreeMap::new(), BTreeMap::new())
    );
}

#[tokio::test]
async fn first_attempt_lost_then_never_sent_keeps_the_intent() {
    // 首个 commit 送达（可能已生效）后丢响应；其后监听关闭，重发建连失败
    let fake = Fake::new();
    fake.script(Route::Commit(A.into()), vec![reply(Reply::Close)]);
    let server = fake.server_closing_after(3);
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = text("v1");

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert!(
        results[0].unknown,
        "首发可能已生效——不能因最后的建连失败判为确定"
    );
    assert_eq!(
        state(&store, &server).intents,
        intents(&[(A, Some(doc_hash(&v1)))])
    );
}

#[tokio::test]
async fn first_attempt_lost_then_rate_limited_keeps_the_intent() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = text("v1");
    fake.script(
        Route::Commit(A.into()),
        vec![
            reply(Reply::Close),
            reply(Reply::problem(
                "DPE_RATE_LIMITED",
                429,
                &[("Retry-After", "0")],
            )),
        ],
    );

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(
        results[0].error.as_ref().unwrap().kind(),
        ErrorKind::RateLimited
    );
    assert!(results[0].unknown);
    assert_eq!(
        state(&store, &server).intents,
        intents(&[(A, Some(doc_hash(&v1)))])
    );
}

#[tokio::test]
async fn determined_conflict_clears_the_intent_so_later_writes_are_not_masked() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let (theirs, attempted) = (text("theirs"), text("v2"));
    fake.script(
        Route::Commit(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            Some(doc_hash(&theirs)),
        )],
    );
    let mut pusher = pusher(&client, &store);
    assert_eq!(
        statuses(
            &pusher
                .push(&docs(&[(A, &attempted)]), &none(), None)
                .await
                .unwrap()
        ),
        [PushStatus::Conflict]
    );
    assert_eq!(state(&store, &server).intents, BTreeMap::new());

    // 他人随后写下与我们的 v2 逐字节相同的内容；本地再改（v3）必须走正常判定表
    fake.set(A, Some(doc_hash(&attempted)));
    let final_ = pusher
        .push(&docs(&[(A, &text("v3"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&final_), [PushStatus::Conflict]);
    assert_eq!(
        final_[0].remote.as_ref().unwrap().doc_hash,
        Some(doc_hash(&attempted))
    );
}

// ---------------------------------------------------------------------- 错误路径与并发

#[tokio::test]
async fn forbidden_failure_clears_the_intent() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    fake.script(
        Route::Commit(A.into()),
        vec![reply(problem("DPE_FORBIDDEN", 403))],
    );

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v1"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn reread_failure_is_reported_without_masking_the_conflict() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.script(
        Route::Commit(A.into()),
        vec![reply(problem("DPE_PRECONDITION_FAILED", 412))],
    );
    *fake.head_override.lock().unwrap() = Some(Reply::Close);

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("v2"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert!(results[0].remote.is_none(), "未读到");
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_PRECONDITION_FAILED"),
        "裁决冲突的 CAS 错误不丢"
    );
    assert!(results[0].remote_error.is_some(), "重读失败单独带出");
    assert!(!results[0].unknown);
}

#[tokio::test]
async fn concurrency_is_bounded_and_order_is_preserved() {
    for (concurrency, bound) in [(4usize, 4usize), (1, 1)] {
        let fake = Fake::new();
        *fake.delay.lock().unwrap() = Some(Duration::from_millis(60));
        let server = fake.server();
        let client = client(&server);
        let documents: Vec<(String, ExpandedDocument)> = (0..6)
            .map(|index| {
                (
                    format!("dpe://docs/{index}.md"),
                    text(&format!("doc-{index}")),
                )
            })
            .collect();
        for (uri, document) in &documents {
            fake.script(Route::Commit(uri.clone()), vec![created(uri, document)]);
        }
        let mut pusher =
            StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), concurrency)
                .unwrap();

        let results = pusher.push(&documents, &none(), None).await.unwrap();

        assert_eq!(statuses(&results), [PushStatus::Created; 6]);
        let uris: Vec<&str> = results
            .iter()
            .map(|result| result.file_uri.as_str())
            .collect();
        let expected: Vec<&str> = documents.iter().map(|(uri, _)| uri.as_str()).collect();
        assert_eq!(uris, expected, "与输入同序");
        let peak = fake.peak.load(Ordering::SeqCst);
        assert!(peak <= bound, "concurrency={concurrency} 时峰值 {peak}");
        if concurrency > 1 {
            assert!(peak > 1, "并发度大于 1 时写请求应并发执行，峰值 {peak}");
        }
    }
}

#[tokio::test]
async fn local_validation_sends_nothing() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let mut pusher = pusher(&client, &Arc::new(MemoryStateStore::new()));
    let v1 = text("v1");

    let duplicate = docs(&[(A, &v1), ("DPE://docs/a.md", &v1)]);
    let error = pusher
        .push(&duplicate, &none(), None)
        .await
        .expect_err("归一化后重复");
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    let error = pusher
        .delete(&[A.to_owned(), "DPE://docs/a.md".to_owned()], &none())
        .await
        .expect_err("delete 归一化后重复");
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    let outside = HashMap::from([(B.to_owned(), None)]);
    let error = pusher
        .push(&docs(&[(A, &v1)]), &outside, None)
        .await
        .expect_err("expected_base 在批外");
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    assert!(
        StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), 0).is_err(),
        "concurrency 必须是正整数"
    );
    // 只有 capabilities 一个请求（校验在任何读写之前）
    assert_eq!(count(&server, "POST", "/heads"), 0);
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
}

/// 按调用次序失败的 store：第 `fail_on` 次 `save`（从 1 计）失败，`load` 可设为失败。
#[derive(Default)]
struct CountingStore {
    inner: MemoryStateStore,
    saves: AtomicUsize,
    fail_on: AtomicUsize,
    fail_load: std::sync::atomic::AtomicBool,
}

impl StateStore for CountingStore {
    fn load(&self, remote: &str) -> Result<Option<StateSnapshot>, StoreError> {
        if self.fail_load.load(Ordering::SeqCst) {
            return Err("unreadable".into());
        }
        self.inner.load(remote)
    }

    fn save(&self, remote: &str, snapshot: &StateSnapshot) -> Result<(), StoreError> {
        let nth = self.saves.fetch_add(1, Ordering::SeqCst) + 1;
        if nth == self.fail_on.load(Ordering::SeqCst) {
            return Err("disk full".into());
        }
        self.inner.save(remote, snapshot)
    }
}

#[tokio::test]
async fn final_save_failure_is_outcome_unknown_and_the_next_save_catches_up() {
    // 第 1 次 save（写前意图）成功、第 2 次（收尾）失败：写请求已发出 → 结果未知；
    // 同一实例的下一次成功保存把内存整份补上
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(CountingStore::default());
    store.fail_on.store(2, Ordering::SeqCst);
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();
    let (v1, v2) = (text("v1"), text("v2"));
    fake.script(
        Route::Commit(A.into()),
        vec![created(A, &v1), updated(A, &v2)],
    );

    let error = pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .expect_err("收尾 save 失败应整批报错");

    assert_eq!(error.kind(), ErrorKind::StateStore);
    assert!(error.outcome_unknown(), "写请求已发出");
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 1);
    // store 落后于内存：仍是写前的意图；内存已确认 v1
    let behind = store.inner.load(&server.remote()).unwrap().unwrap();
    assert_eq!(behind.intents, intents(&[(A, Some(doc_hash(&v1)))]));
    assert_eq!(pusher.snapshot().synced, synced(&[(A, doc_hash(&v1))]));

    let results = pusher
        .push(&docs(&[(A, &v2)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Updated]);
    assert_eq!(
        store.inner.load(&server.remote()).unwrap().unwrap(),
        pusher.snapshot(),
        "下一次成功保存后 store 与内存一致"
    );
}

#[tokio::test]
async fn final_save_failure_without_writes_is_determined() {
    // 本批全部已达成（未发写）：收尾 save 失败时结果确定未生效
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let v1 = text("v1");
    fake.set(A, Some(doc_hash(&v1)));
    let store = Arc::new(CountingStore::default());
    store.fail_on.store(1, Ordering::SeqCst); // 没有写前保存，第 1 次就是收尾保存
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();

    let error = pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .expect_err("收尾 save 失败应整批报错");

    assert_eq!(error.kind(), ErrorKind::StateStore);
    assert!(!error.outcome_unknown(), "本批未发写请求");
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn load_failure_at_construction_is_a_determined_state_store_error() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(CountingStore::default());
    store.fail_load.store(true, Ordering::SeqCst);

    let error = StatefulPusher::with_store(&client, store as Arc<dyn StateStore>, 4)
        .expect_err("load 失败");

    assert_eq!(error.kind(), ErrorKind::StateStore);
    assert!(!error.outcome_unknown(), "构造阶段不发写请求");
    assert_eq!(server.count(), 0);
}

#[tokio::test]
async fn empty_batches_return_empty_without_reading() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let mut pusher = pusher(&client, &Arc::new(MemoryStateStore::new()));

    assert!(pusher.push(&[], &none(), None).await.unwrap().is_empty());
    assert!(pusher.delete(&[], &none()).await.unwrap().is_empty());
    assert!(pusher
        .move_documents(&[], &none())
        .await
        .unwrap()
        .is_empty());
    assert_eq!(count(&server, "POST", "/heads"), 0);
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
}

// ---------------------------------------------------------------------- blobs / flush / client（#144）

/// 一页一个 Image 元素的文档，blob 引用 `bytes` 的字节（与 Python `_image_doc` 对等）。
fn image_doc(bytes: &[u8]) -> ExpandedDocument {
    ExpandedDocument::from_value(
        &json!({
            "file_type": "md",
            "pages": [{"page_metadata": {}, "elements": [
                {"category": "Image", "blob": dpe_hash::blob_ref(bytes), "mime_type": "image/png"}
            ]}],
        }),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

fn png(seed: u8) -> Vec<u8> {
    let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
    bytes.extend((0..=255u8).map(|i| i.wrapping_add(seed)).cycle().take(1024));
    bytes
}

/// 快路径 commit 缺 blob 的应答（`DPE_MISSING_CONTENT` + 缺失清单；problem 体须是
/// `application/problem+json`，核心才解析清单）。
fn missing_blob(reference: &str) -> Step {
    let body = json!({
        "type": "urn:dpe:error:missing-content", "title": "missing", "status": 400,
        "code": "DPE_MISSING_CONTENT",
        "missing": {"pages": [], "content_hashes": [], "blobs": [reference]},
    });
    reply(Reply::Response {
        status: 400,
        headers: vec![
            (
                "Content-Type".to_owned(),
                "application/problem+json".to_owned(),
            ),
            (
                "DPE-Error-Code".to_owned(),
                "DPE_MISSING_CONTENT".to_owned(),
            ),
        ],
        body: serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    })
}

/// 给 `uri` 脚本化「缺 blob → 开会话 → 上传 → 提交成功」。
fn script_blob_upload(fake: &Fake, uri: &str, document: &ExpandedDocument, reference: &str) {
    fake.script(
        Route::Commit(uri.into()),
        vec![missing_blob(reference), created(uri, document)],
    );
    fake.script(
        Route::Negotiate(uri.into()),
        vec![reply(Reply::json(
            200,
            json!({
                "missing_pages": [], "missing_content_hashes": [],
                "staging_session": {"id": format!("s-{uri}"), "expires_at": "2026-10-01T00:00:00Z"},
            }),
        ))],
    );
    fake.script(
        Route::Staging,
        vec![reply(Reply::json_with(
            201,
            json!({}),
            &[("DPE-Session-Expires", "2026-10-01T00:00:00Z")],
        ))],
    );
}

#[tokio::test]
async fn push_passes_blobs_to_deliver() {
    // blobs 原样交给 deliver：不给时服务端缺的字节由核心如实报 DPE_MISSING_CONTENT（不自行
    // 解引用任何来源），意图随确定的失败清除；给了则经它上传后 commit 成功
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let bytes = png(0);
    let reference = dpe_hash::blob_ref(&bytes);
    let document = image_doc(&bytes);
    // 不给 provider：核心照常开会话补传，取不到字节时如实报缺失。`script` 是替换语义——
    // 这里覆盖 Commit 队列、只留一次缺失应答（Negotiate / Staging 用上面编排的）
    script_blob_upload(&fake, A, &document, &reference);
    fake.script(Route::Commit(A.into()), vec![missing_blob(&reference)]);
    let mut pusher = pusher(&client, &store);

    let missing = pusher
        .push(&docs(&[(A, &document)]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&missing), [PushStatus::Failed]);
    assert_eq!(
        missing[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_MISSING_CONTENT")
    );
    assert!(!missing[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());

    script_blob_upload(&fake, A, &document, &reference);
    let mut asked = Vec::new();
    let mut provider = |requested: &str| {
        asked.push(requested.to_owned());
        Some(BlobSource::from_bytes(bytes.clone()))
    };

    let results = pusher
        .push(&docs(&[(A, &document)]), &none(), Some(&mut provider))
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Created]);
    assert_eq!(asked, std::slice::from_ref(&reference));
    let uploads: Vec<String> = server
        .requests()
        .into_iter()
        .filter(|request| request.target.contains("/blobs/"))
        .map(|request| request.target)
        .collect();
    assert!(!uploads.is_empty() && uploads.iter().all(|target| target.ends_with(&reference)));
    assert_eq!(
        state(&store, &server).synced,
        synced(&[(A, doc_hash(&document))])
    );
}

#[tokio::test]
async fn unchanged_documents_never_call_blobs() {
    // 远端已是本次内容（R == N）：推送器不发写，blobs 回调不发生（来源读取可能很贵）
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let bytes = png(0);
    let document = image_doc(&bytes);
    fake.set(A, Some(doc_hash(&document)));
    let mut calls = 0usize;
    let mut provider = |_: &str| {
        calls += 1;
        Some(BlobSource::from_bytes(bytes.clone()))
    };

    let results = pusher(&client, &Arc::new(MemoryStateStore::new()))
        .push(&docs(&[(A, &document)]), &none(), Some(&mut provider))
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Achieved]);
    assert_eq!(calls, 0);
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn concurrent_pushes_share_one_blob_provider() {
    // 两篇各引用不同 blob 的文档并发投递：同一个 provider 被两条 deliver 共享（单任务内交错，
    // 借用不重叠），各自拿到自己的 blob。B 的 negotiate 在服务端多停留 300 ms，A 调用
    // provider 时 B 必有请求在途——确实在交错中共享
    let fake = Fake::new();
    *fake.delay.lock().unwrap() = Some(Duration::from_millis(30));
    fake.route_delay
        .lock()
        .unwrap()
        .insert(Route::Negotiate(B.into()), Duration::from_millis(300));
    let server = fake.server();
    let client = client(&server);
    let (first, second) = (png(1), png(2));
    let (doc_a, doc_b) = (image_doc(&first), image_doc(&second));
    script_blob_upload(&fake, A, &doc_a, &dpe_hash::blob_ref(&first));
    script_blob_upload(&fake, B, &doc_b, &dpe_hash::blob_ref(&second));
    let sources: HashMap<String, Vec<u8>> = [&first, &second]
        .into_iter()
        .map(|bytes| (dpe_hash::blob_ref(bytes), bytes.clone()))
        .collect();
    let mut asked = Vec::new();
    let mut in_flight_at_call = Vec::new();
    let mut provider = |requested: &str| {
        asked.push(requested.to_owned());
        in_flight_at_call.push(fake.in_flight.load(Ordering::SeqCst));
        sources.get(requested).cloned().map(BlobSource::from_bytes)
    };
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), 2).unwrap();

    let results = pusher
        .push(
            &docs(&[(A, &doc_a), (B, &doc_b)]),
            &none(),
            Some(&mut provider),
        )
        .await
        .unwrap();

    assert_eq!(
        statuses(&results),
        [PushStatus::Created, PushStatus::Created]
    );
    asked.sort();
    let mut expected: Vec<String> = sources.keys().cloned().collect();
    expected.sort();
    assert_eq!(asked, expected);
    assert!(fake.peak.load(Ordering::SeqCst) > 1, "两条投递确实并发");
    assert!(
        in_flight_at_call.iter().any(|&count| count >= 1),
        "至少一次 provider 调用发生在另一条投递有请求在途时：{in_flight_at_call:?}"
    );
}

#[tokio::test]
async fn flush_catches_the_store_up_after_a_failed_save() {
    // 收尾保存失败后 store 落后于内存；flush（同一实例）补存后两者一致。flush 本身失败为
    // StateStore 且确定未生效（它不发写请求）
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(CountingStore::default());
    store.fail_on.store(2, Ordering::SeqCst);
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 4).unwrap();
    let v1 = text("v1");
    fake.script(Route::Commit(A.into()), vec![created(A, &v1)]);
    pusher
        .push(&docs(&[(A, &v1)]), &none(), None)
        .await
        .expect_err("收尾保存失败");
    assert_ne!(
        store.inner.load(&server.remote()).unwrap().unwrap(),
        pusher.snapshot()
    );

    pusher.flush().unwrap();

    assert_eq!(
        store.inner.load(&server.remote()).unwrap().unwrap(),
        pusher.snapshot()
    );
    store
        .fail_on
        .store(store.saves.load(Ordering::SeqCst) + 1, Ordering::SeqCst);
    let error = pusher.flush().expect_err("store 不可写");
    assert_eq!(error.kind(), ErrorKind::StateStore);
    assert!(!error.outcome_unknown(), "flush 不发写请求");
}

#[tokio::test]
async fn client_is_the_instance_given_at_construction() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let pusher = pusher(&client, &Arc::new(MemoryStateStore::new()));
    assert!(std::ptr::eq(pusher.client(), &client));
}

// ---------------------------------------------------------------------- 写后冲突重读即已达成（#140）
//
// 服务端对内容相同的提交本就判成功（core §5.2），所以 commit 的「冲突后已达成」只出现在竞态里：
// 他人先写入不同内容致我方冲突，在我方重读之前他人又写下了与我方相同的内容。推送器只看冲突裁决
// 与重读值：假服务端直接给出冲突应答，并把视图置为重读时的状态（中间的「他人内容」不经视图）。

#[tokio::test]
async fn commit_conflict_whose_reread_equals_the_content_is_achieved() {
    // 三种写后冲突裁决：412（有 L，他人改写）、409（无 L → if_absent，他人先建）、404（有 L，
    // 他人删除——不属于已达成）；重读之前他人都已写下与我方相同的 v2
    for (code, status, has_l) in [
        ("DPE_PRECONDITION_FAILED", 412, true),
        ("DPE_ALREADY_EXISTS", 409, false),
        ("DPE_NOT_FOUND", 404, true),
    ] {
        let fake = Fake::new();
        let server = fake.server();
        let client = client(&server);
        let store = Arc::new(MemoryStateStore::new());
        let (v1, v2) = (text("v1"), text("v2"));
        if has_l {
            fake.set(A, Some(doc_hash(&v1)));
            store
                .save(
                    &server.remote(),
                    &StateSnapshot::new(synced(&[(A, doc_hash(&v1))]), BTreeMap::new()),
                )
                .unwrap();
        }
        fake.script(
            Route::Commit(A.into()),
            vec![reply_then(problem(code, status), A, Some(doc_hash(&v2)))],
        );

        let results = pusher(&client, &store)
            .push(&docs(&[(A, &v2)]), &none(), None)
            .await
            .unwrap();

        assert_eq!(statuses(&results), [PushStatus::Achieved], "{code}");
        assert_eq!(
            results[0].error.as_ref().and_then(|error| error.code()),
            Some(code)
        );
        assert_eq!(
            results[0].remote.as_ref().unwrap().doc_hash,
            Some(doc_hash(&v2))
        );
        assert!(
            results[0].precondition.is_some(),
            "发过写（写前判定的已达成为 None）"
        );
        assert!(!results[0].unknown);
        assert_eq!(fake.write_count.load(Ordering::SeqCst), 1, "没有重提");
        let snapshot = state(&store, &server);
        assert_eq!(snapshot.synced, synced(&[(A, doc_hash(&v2))]), "{code}");
        assert_eq!(snapshot.intents, BTreeMap::new());
    }
}

/// 已推 v1 的状态，delete 写请求先得到 412、重读时视图为 `reread`。
async fn delete_race(
    reread: Option<String>,
    cut_reads: bool,
) -> (Vec<PushResult>, Arc<Fake>, StateSnapshot) {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            reread,
        )],
    );
    if cut_reads {
        *fake.head_override.lock().unwrap() = Some(Reply::Close);
    }
    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();
    let snapshot = state(&store, &server);
    (results, fake, snapshot)
}

#[tokio::test]
async fn delete_conflict_whose_reread_is_absent_is_deleted() {
    // 他人先改写（我方 412），重读之前又删除了它：目标状态已达成
    let (results, fake, snapshot) = delete_race(None, false).await;

    assert_eq!(statuses(&results), [PushStatus::Deleted]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_PRECONDITION_FAILED")
    );
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, None);
    assert_eq!(
        fake.write_count.load(Ordering::SeqCst),
        2,
        "v1 的 commit 与一次 delete，没有重提"
    );
    assert_eq!(snapshot, StateSnapshot::default(), "删条目、清意图");
}

#[tokio::test]
async fn delete_conflict_with_failed_reread_stays_a_conflict() {
    // 重读本身失败：不臆测，仍回报 conflict，条目保留
    let (results, _, snapshot) = delete_race(None, true).await;

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert!(results[0].remote.is_none() && results[0].remote_error.is_some());
    assert_eq!(snapshot.synced, synced(&[(A, doc_hash(&text("v1")))]));
}

#[test]
fn pusher_operation_futures_are_send() {
    // #131：blob 回调为 Send、共享回调用 Mutex，push / delete / move_documents 的 future 都是
    // Send（并发仍在调用方的同一任务内，不经 tokio::spawn）
    fn assert_send<T: Send>(_: T) {}
    let client = DpeClient::new("http://127.0.0.1:1").expect("remote 合法");
    let mut pusher = StatefulPusher::new(&client);
    let documents = docs(&[(A, &text("v1"))]);
    let expected = none();
    let mut asked: Vec<String> = Vec::new();
    let mut provider = |reference: &str| -> Option<BlobSource> {
        asked.push(reference.to_owned());
        None
    };
    assert_send(pusher.push(&documents, &expected, Some(&mut provider)));
    let uris = [A.to_owned()];
    assert_send(pusher.delete(&uris, &expected));
    let pairs = [(A.to_owned(), B.to_owned())];
    assert_send(pusher.move_documents(&pairs, &expected));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn push_runs_inside_tokio_spawn() {
    // 整次操作交给 tokio::spawn：推送器借用客户端，'static 由任务内持有 Arc<DpeClient> 达成
    let fake = Fake::new();
    let server = fake.server();
    let hello = text("hello");
    fake.script(Route::Commit(A.into()), vec![created(A, &hello)]);
    let client = Arc::new(client(&server));
    let store = Arc::new(MemoryStateStore::new());
    let task = tokio::spawn({
        let client = Arc::clone(&client);
        let store = Arc::clone(&store);
        async move {
            let mut pusher = pusher(&client, &store);
            let mut provider = |_: &str| -> Option<BlobSource> { None };
            pusher
                .push(&docs(&[(A, &hello)]), &none(), Some(&mut provider))
                .await
        }
    });
    let results = task.await.expect("任务未 panic").expect("推送成功");
    assert_eq!(statuses(&results), [PushStatus::Created]);
}

// ---------------------------------------------------------------------- delete

#[tokio::test]
async fn delete_success_removes_the_entry() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let v1 = doc_hash(&text("v1"));
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(Reply::new(204, ""), A, None)],
    );

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Deleted]);
    assert_eq!(results[0].op, PushOp::Delete);
    assert_eq!(
        results[0].precondition,
        Some(Precondition::BaseHash(v1.clone()))
    );
    let delete = server
        .requests()
        .into_iter()
        .find(|request| request.method == "DELETE")
        .unwrap();
    assert_eq!(
        delete.header("if-match"),
        Some(format!("\"{v1}\"").as_str())
    );
    assert_eq!(state(&store, &server), StateSnapshot::default());
}

#[tokio::test]
async fn delete_already_absent_is_achieved_without_writes() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.set(A, None);

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Deleted]);
    assert_eq!(results[0].precondition, None);
    assert_eq!(count(&server, "DELETE", "/documents"), 0);
    assert_eq!(state(&store, &server).synced, BTreeMap::new());
}

#[tokio::test]
async fn delete_conflict_is_reported_without_writing() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.set(A, Some(theirs.clone()));

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, Some(theirs));
    assert_eq!(count(&server, "DELETE", "/documents"), 0);
}

#[tokio::test]
async fn delete_race_conflict_is_detected_and_reread() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            Some(theirs.clone()),
        )],
    );

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, Some(theirs));
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn delete_race_deleted_first_is_achieved() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(problem("DPE_NOT_FOUND", 404), A, None)],
    );

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Deleted]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_NOT_FOUND")
    );
    assert_eq!(state(&store, &server), StateSnapshot::default());
}

#[tokio::test]
async fn delete_expected_base_override() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.set(A, Some(theirs.clone()));
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(Reply::new(204, ""), A, None)],
    );
    let mut pusher = pusher(&client, &store);

    let absent = HashMap::from([(A.to_owned(), None)]);
    let conflicted = pusher.delete(&[A.to_owned()], &absent).await.unwrap();
    assert_eq!(statuses(&conflicted), [PushStatus::Conflict]);

    let base = HashMap::from([(A.to_owned(), Some(theirs.clone()))]);
    let deleted = pusher.delete(&[A.to_owned()], &base).await.unwrap();
    assert_eq!(statuses(&deleted), [PushStatus::Deleted]);
    assert_eq!(
        deleted[0].precondition,
        Some(Precondition::BaseHash(theirs))
    );
}

#[tokio::test]
async fn delete_intent_reconciles_after_lost_response() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(Reply::Close, A, None)],
    );
    let mut pusher = pusher(&client, &store);

    let unknown = pusher.delete(&[A.to_owned()], &none()).await.unwrap();
    assert_eq!(statuses(&unknown), [PushStatus::Failed]);
    assert!(unknown[0].unknown);
    assert_eq!(state(&store, &server).intents, intents(&[(A, None)]));

    // 下一轮：R 不存在 == 意图「不存在」→ 对账删条目，判已达成、不发写
    let before = count(&server, "DELETE", "/documents");
    let reconciled = pusher.delete(&[A.to_owned()], &none()).await.unwrap();
    assert_eq!(statuses(&reconciled), [PushStatus::Deleted]);
    assert_eq!(count(&server, "DELETE", "/documents"), before);
    assert_eq!(state(&store, &server), StateSnapshot::default());
}

#[tokio::test]
async fn delete_not_sent_failure_clears_the_intent() {
    let fake = Fake::new();
    let v1 = doc_hash(&text("v1"));
    fake.set(A, Some(v1));
    let server = fake.server_closing_after(2);
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn delete_forbidden_failure_is_reported_with_code() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    fake.set(A, Some(doc_hash(&text("v1"))));
    fake.script(
        Route::Delete(A.into()),
        vec![reply(problem("DPE_FORBIDDEN", 403))],
    );

    let results = pusher(&client, &store)
        .delete(&[A.to_owned()], &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

// ---------------------------------------------------------------------- move

fn moves(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(from, to)| ((*from).to_owned(), (*to).to_owned()))
        .collect()
}

/// 成功的 move 脚本：应答后视图从源迁到目标。
fn move_ok(from: &str, to: &str, base: &str) -> Step {
    Step {
        reply: moved_reply(base),
        then: vec![
            (from.to_owned(), None),
            (to.to_owned(), Some(base.to_owned())),
        ],
    }
}

#[tokio::test]
async fn move_success_migrates_the_entry() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let v1 = doc_hash(&text("v1"));
    fake.script(Route::Move(A.into()), vec![move_ok(A, B, &v1)]);

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(
        (results[0].op, results[0].to_uri.as_deref()),
        (PushOp::Move, Some(B))
    );
    assert_eq!(
        results[0].precondition,
        Some(Precondition::BaseHash(v1.clone()))
    );
    let request = server
        .requests()
        .into_iter()
        .find(|request| request.target == "/move")
        .unwrap();
    assert_eq!(
        body_json(&request),
        json!({"from_uri": A, "to_uri": B, "base_hash": v1})
    );
    let snapshot = state(&store, &server);
    assert_eq!(
        (snapshot.synced, snapshot.intents),
        (synced(&[(B, v1)]), BTreeMap::new())
    );
}

#[tokio::test]
async fn move_replay_after_own_success_is_achieved() {
    // B1：成功后重放（同一推送器、读同一 store 的新推送器）都判 moved，且不发 move 写请求
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let v1 = doc_hash(&text("v1"));
    fake.script(Route::Move(A.into()), vec![move_ok(A, B, &v1)]);
    let mut first = pusher(&client, &store);
    first
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    assert_eq!(count(&server, "POST", "/move"), 1);

    let same = first
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    let fresh = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    for replay in [&same, &fresh] {
        assert_eq!(statuses(replay), [PushStatus::Moved]);
        assert_eq!(replay[0].precondition, None);
    }
    assert_eq!(count(&server, "POST", "/move"), 1, "重放不发 move 写请求");
    assert_eq!(state(&store, &server).synced, synced(&[(B, v1)]));
}

#[tokio::test]
async fn move_already_achieved_without_writes() {
    // 他人已完成同一 move：源不存在、目标等于 base（L）→ 已达成，迁条目、不发写
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let v1 = doc_hash(&text("v1"));
    fake.set(A, None);
    fake.set(B, Some(v1.clone()));

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(count(&server, "POST", "/move"), 0);
    assert_eq!(state(&store, &server).synced, synced(&[(B, v1)]));
}

#[tokio::test]
async fn move_source_gone_and_target_differs_is_failed_side_from() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.set(A, None);
    fake.set(B, Some(doc_hash(&text("other"))));

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(results[0].side, Some(Side::From));
    assert_eq!(results[0].to_uri.as_deref(), Some(B));
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        None,
        "读到的源不存在"
    );
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_NOT_FOUND")
    );
    assert!(!results[0].unknown);
    assert_eq!(count(&server, "POST", "/move"), 0);
}

#[tokio::test]
async fn move_source_rewritten_is_conflict_side_from() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.set(A, Some(doc_hash(&text("theirs"))));

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(
        (results[0].side, results[0].to_uri.as_deref()),
        (Some(Side::From), Some(B))
    );
    assert_eq!(count(&server, "POST", "/move"), 0);
}

#[tokio::test]
async fn move_target_occupied_is_failed_side_to() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let occupant = doc_hash(&text("occupant"));
    fake.set(B, Some(occupant.clone()));
    fake.script(
        Route::Move(A.into()),
        vec![reply(problem("DPE_ALREADY_EXISTS", 409))],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(results[0].side, Some(Side::To));
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        Some(occupant),
        "重读目标侧"
    );
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_ALREADY_EXISTS")
    );
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

// ---------------------------------------------------------------------- 目标已持有同一内容（#165）

/// 源 A 为 v1（有 L），目标 B 也已是 v1：判定为删源路径。
async fn same_content_on_both_sides(
    fake: &Arc<Fake>,
    client: &DpeClient,
    store: &Arc<MemoryStateStore>,
) -> String {
    after_v1(fake, client, store).await;
    let v1 = doc_hash(&text("v1"));
    fake.set(B, Some(v1.clone()));
    v1
}

#[tokio::test]
async fn move_onto_target_holding_same_content_deletes_source() {
    // 不发 move（会得 ALREADY_EXISTS、重放永不收敛），以 base 删除源 → Moved，条目迁到目标
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = same_content_on_both_sides(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(Reply::new(204, ""), A, None)],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert!(results[0].error.is_none());
    assert_eq!(results[0].to_uri.as_deref(), Some(B));
    assert_eq!(
        results[0].remote.as_ref().unwrap().doc_hash,
        Some(v1.clone()),
        "带读到的源侧 R"
    );
    assert_eq!(
        results[0].precondition,
        Some(Precondition::BaseHash(v1.clone()))
    );
    assert_eq!(count(&server, "POST", "/move"), 0, "不发 move");
    assert_eq!(count(&server, "DELETE", "/documents"), 1, "只删源");
    let delete = server
        .requests()
        .into_iter()
        .find(|request| request.method == "DELETE")
        .unwrap();
    assert_eq!(
        delete.header("if-match"),
        Some(format!("\"{v1}\"").as_str()),
        "删源的 CAS 用 base"
    );
    let snapshot = state(&store, &server);
    assert_eq!(snapshot.synced, synced(&[(B, v1)]));
    assert_eq!(snapshot.intents, BTreeMap::new());
}

#[tokio::test]
async fn move_onto_same_content_without_sync_state_deletes_source() {
    // 同步状态缺失（无 L）：以读到的源 R 为 base，目标同内容同样删源
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let v1 = doc_hash(&text("v1"));
    fake.set(A, Some(v1.clone()));
    fake.set(B, Some(v1.clone()));
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(Reply::new(204, ""), A, None)],
    );
    let store = Arc::new(MemoryStateStore::new());

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(count(&server, "POST", "/move"), 0);
    assert_eq!(state(&store, &server).synced, synced(&[(B, v1)]));
}

#[tokio::test]
async fn move_onto_same_content_source_rewritten_is_conflict_side_from() {
    // 删源遇 412（源被他人改写）→ 冲突、问题在源侧，带重读到的源侧值
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    same_content_on_both_sides(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            Some(theirs.clone()),
        )],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].side, Some(Side::From));
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, Some(theirs));
    assert_eq!(count(&server, "POST", "/move"), 0);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn move_onto_same_content_source_already_gone_is_moved() {
    // 删源遇「不存在」→ move 的目标状态（源不存在、目标为 base）已达成 → Moved
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = same_content_on_both_sides(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(problem("DPE_NOT_FOUND", 404), A, None)],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_NOT_FOUND"),
        "原错误一并带上，同 delete 的已达成口径"
    );
    let snapshot = state(&store, &server);
    assert_eq!(snapshot.synced, synced(&[(B, v1)]));
    assert_eq!(snapshot.intents, BTreeMap::new());
}

#[tokio::test]
async fn move_onto_same_content_conflict_whose_reread_is_absent_is_moved() {
    // 删源遇 412、重读源已不存在（他人改写后又删除）→ move 的目标状态已达成 → Moved
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = same_content_on_both_sides(&fake, &client, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply_then(problem("DPE_PRECONDITION_FAILED", 412), A, None)],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_PRECONDITION_FAILED")
    );
    assert_eq!(state(&store, &server).synced, synced(&[(B, v1)]));
}

#[tokio::test]
async fn move_onto_same_content_other_delete_errors_carry_no_side() {
    // 删源时 412 / 404 以外的错误按 delete 的常规归类、不带侧别（侧别专指 §7.2 的 move
    // 条目失败）：结果未知保留两侧意图，已确定的失败清除意图
    let fake = Fake::new();
    let server = fake.server();
    let dropping = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let v1 = same_content_on_both_sides(&fake, &dropping, &store).await;
    fake.script(Route::Delete(A.into()), vec![reply(Reply::Close)]);
    let results = pusher(&dropping, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!((results[0].side, results[0].unknown), (None, true));
    assert_eq!(
        state(&store, &server).intents,
        intents(&[(A, None), (B, Some(v1.clone()))])
    );

    let fake = Fake::new();
    let server = fake.server();
    let rejecting = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    same_content_on_both_sides(&fake, &rejecting, &store).await;
    fake.script(
        Route::Delete(A.into()),
        vec![reply(problem("DPE_FORBIDDEN", 403))],
    );
    let results = pusher(&rejecting, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!((results[0].side, results[0].unknown), (None, false));
    assert_eq!(
        results[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn move_race_conflict_rereads_source() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.script(
        Route::Move(A.into()),
        vec![reply_then(
            problem("DPE_PRECONDITION_FAILED", 412),
            A,
            Some(theirs.clone()),
        )],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].side, Some(Side::From));
    assert_eq!(results[0].remote.as_ref().unwrap().doc_hash, Some(theirs));
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn move_intents_record_both_sides_and_reconcile_after_lost_response() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let v1 = doc_hash(&text("v1"));
    fake.script(
        Route::Move(A.into()),
        vec![Step {
            reply: Reply::Close,
            then: vec![(A.to_owned(), None), (B.to_owned(), Some(v1.clone()))],
        }],
    );
    let mut pusher = pusher(&client, &store);

    let unknown = pusher
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    assert_eq!(statuses(&unknown), [PushStatus::Failed]);
    assert!(unknown[0].unknown);
    assert_eq!(
        state(&store, &server).intents,
        intents(&[(A, None), (B, Some(v1.clone()))])
    );

    // 下一轮：两侧都对上意图 → 并入同步状态；源不存在、目标等于 base → 已达成、不发写
    let before = count(&server, "POST", "/move");
    let reconciled = pusher
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();
    assert_eq!(statuses(&reconciled), [PushStatus::Moved]);
    assert_eq!(count(&server, "POST", "/move"), before);
    let snapshot = state(&store, &server);
    assert_eq!(
        (snapshot.synced, snapshot.intents),
        (synced(&[(B, v1)]), BTreeMap::new())
    );
}

#[tokio::test]
async fn move_expected_base_override() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    let theirs = doc_hash(&text("theirs"));
    fake.set(A, Some(theirs.clone()));
    fake.script(Route::Move(A.into()), vec![move_ok(A, B, &theirs)]);

    let base = HashMap::from([(A.to_owned(), Some(theirs.clone()))]);
    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &base)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Moved]);
    assert_eq!(
        results[0].precondition,
        Some(Precondition::BaseHash(theirs))
    );
}

#[tokio::test]
async fn move_declared_absent_but_present_is_conflict() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    fake.set(A, Some(doc_hash(&text("v1"))));

    let absent = HashMap::from([(A.to_owned(), None)]);
    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &absent)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Conflict]);
    assert_eq!(results[0].side, Some(Side::From));
    assert_eq!(count(&server, "POST", "/move"), 0);
}

#[tokio::test]
async fn move_post_write_not_found_is_failed_side_from() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    after_v1(&fake, &client, &store).await;
    fake.script(
        Route::Move(A.into()),
        vec![reply_then(problem("DPE_NOT_FOUND", 404), A, None)],
    );

    let results = pusher(&client, &store)
        .move_documents(&moves(&[(A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed]);
    assert_eq!(results[0].side, Some(Side::From));
    assert!(!results[0].unknown);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn overlapping_moves_are_rejected_before_any_request() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let mut pusher = pusher(&client, &Arc::new(MemoryStateStore::new()));
    let x = "dpe://docs/x.md";

    for batch in [
        moves(&[(A, B), (B, x)]),                 // 链式
        moves(&[(A, x), (B, x)]),                 // 共用目标
        moves(&[(A, B), (A, x)]),                 // 重复源
        moves(&[(A, x), ("DPE://docs/b.md", B)]), // 归一化后才重叠（scheme 大小写）
    ] {
        let error = pusher
            .move_documents(&batch, &none())
            .await
            .expect_err("整批拒绝");
        assert_eq!(error.kind(), ErrorKind::InvalidArgument, "{batch:?}");
    }
    assert_eq!(count(&server, "POST", "/heads"), 0);
}

#[tokio::test]
async fn parity_scenario_runs_commit_delete_and_move_end_to_end() {
    // 与 Python `test_sync_and_async_agree_against_the_same_server` 的场景序列同形：
    // 推送 → 更新 → move → move 重放（moved、无前置条件）→ delete
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    let (v1, v2) = (text("v1"), text("v2"));
    fake.script(
        Route::Commit(A.into()),
        vec![created(A, &v1), updated(A, &v2)],
    );
    fake.script(Route::Move(A.into()), vec![move_ok(A, B, &doc_hash(&v2))]);
    fake.script(
        Route::Delete(B.into()),
        vec![reply_then(Reply::new(204, ""), B, None)],
    );
    let mut pusher = pusher(&client, &store);

    let mut trail = Vec::new();
    trail.extend(
        pusher
            .push(&docs(&[(A, &v1)]), &none(), None)
            .await
            .unwrap(),
    );
    trail.extend(
        pusher
            .push(&docs(&[(A, &v2)]), &none(), None)
            .await
            .unwrap(),
    );
    trail.extend(
        pusher
            .move_documents(&moves(&[(A, B)]), &none())
            .await
            .unwrap(),
    );
    trail.extend(
        pusher
            .move_documents(&moves(&[(A, B)]), &none())
            .await
            .unwrap(),
    );
    trail.extend(pusher.delete(&[B.to_owned()], &none()).await.unwrap());

    let summary: Vec<(PushOp, PushStatus, Option<Precondition>)> = trail
        .iter()
        .map(|result| (result.op, result.status, result.precondition.clone()))
        .collect();
    assert_eq!(
        summary,
        [
            (
                PushOp::Commit,
                PushStatus::Created,
                Some(Precondition::IfAbsent)
            ),
            (
                PushOp::Commit,
                PushStatus::Updated,
                Some(Precondition::BaseHash(doc_hash(&v1)))
            ),
            (
                PushOp::Move,
                PushStatus::Moved,
                Some(Precondition::BaseHash(doc_hash(&v2)))
            ),
            (PushOp::Move, PushStatus::Moved, None),
            (
                PushOp::Delete,
                PushStatus::Deleted,
                Some(Precondition::BaseHash(doc_hash(&v2)))
            ),
        ]
    );
    assert_eq!(state(&store, &server), StateSnapshot::default());
}

// ---------------------------------------------------------------------- 批量读被拒的逐条定位（#145）

const SECRET: &str = "dpe://secret/b.md";

fn deny(fake: &Fake, uris: &[&str]) {
    let mut denied = fake.denied_reads.lock().unwrap();
    denied.extend(uris.iter().map(|uri| (*uri).to_owned()));
}

#[tokio::test]
async fn batch_read_rejection_fails_only_the_offending_entry() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    deny(&fake, &[SECRET]);
    fake.script(Route::Commit(A.into()), vec![created(A, &text("A"))]);
    fake.script(Route::Commit(B.into()), vec![created(B, &text("B"))]);

    let results = pusher(&client, &store)
        .push(
            &docs(&[(A, &text("A")), (SECRET, &text("S")), (B, &text("B"))]),
            &none(),
            None,
        )
        .await
        .unwrap();

    assert_eq!(
        statuses(&results),
        [PushStatus::Created, PushStatus::Failed, PushStatus::Created]
    );
    assert_eq!(results[1].file_uri, SECRET);
    assert_eq!(
        results[1].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
    assert_eq!((results[1].side, results[1].unknown), (None, false));
    assert_eq!(count(&server, "POST", "/heads"), 1);
    assert_eq!(count(&server, "HEAD", "/documents"), 3);
    let snapshot = state(&store, &server);
    assert_eq!(
        snapshot.synced.keys().cloned().collect::<Vec<_>>(),
        [A.to_owned(), B.to_owned()]
    );
    assert_eq!(snapshot.intents, BTreeMap::new());
}

#[tokio::test]
async fn unlocalizable_batch_rejection_fails_every_entry() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    fake.reject_all_batches.store(true, Ordering::SeqCst);

    let results = pusher(&client, &store)
        .push(&docs(&[(A, &text("A")), (B, &text("B"))]), &none(), None)
        .await
        .unwrap();

    assert_eq!(statuses(&results), [PushStatus::Failed, PushStatus::Failed]);
    for result in &results {
        assert_eq!(
            result.error.as_ref().and_then(|error| error.code()),
            Some("DPE_FORBIDDEN")
        );
        assert_eq!(
            result.error.as_ref().map(|error| error.is_retryable()),
            Some(false)
        );
    }
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn network_failure_while_locating_fails_the_whole_batch() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    deny(&fake, &[SECRET]);
    *fake.head_override.lock().unwrap() = Some(Reply::Close);

    let error = pusher(&client, &store)
        .push(
            &docs(&[(A, &text("A")), (SECRET, &text("S"))]),
            &none(),
            None,
        )
        .await
        .unwrap_err();

    assert!(error.code().is_none(), "{error}");
    assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
    assert_eq!(state(&store, &server).intents, BTreeMap::new());
}

#[tokio::test]
async fn delete_and_move_read_rejection_is_located() {
    const D1: &str = "dpe://docs/d1.md";
    const D2: &str = "dpe://docs/d2.md";
    const M1: &str = "dpe://docs/m1.md";
    const M2: &str = "dpe://docs/m2.md";
    const N2: &str = "dpe://docs/n2.md";
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    for uri in [D1, D2, M1, M2] {
        fake.set(uri, Some(doc_hash(&text(uri))));
    }
    deny(&fake, &[D2, SECRET]);
    fake.script(
        Route::Delete(D1.into()),
        vec![reply_then(Reply::new(204, ""), D1, None)],
    );
    fake.script(
        Route::Move(M2.into()),
        vec![move_ok(M2, N2, &doc_hash(&text(M2)))],
    );
    let mut pusher = pusher(&client, &store);

    let deleted = pusher
        .delete(&[D1.to_owned(), D2.to_owned()], &none())
        .await
        .unwrap();
    assert_eq!(
        statuses(&deleted),
        [PushStatus::Deleted, PushStatus::Failed]
    );
    assert_eq!(
        deleted[1].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );

    let moved = pusher
        .move_documents(&moves(&[(M1, SECRET), (M2, N2)]), &none())
        .await
        .unwrap();
    assert_eq!(statuses(&moved), [PushStatus::Failed, PushStatus::Moved]);
    assert_eq!(moved[0].to_uri.as_deref(), Some(SECRET));
    assert_eq!(moved[0].side, None);
    assert_eq!(
        moved[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
}

#[tokio::test]
async fn locating_respects_the_concurrency_limit() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    deny(&fake, &[SECRET]);
    *fake.head_delay.lock().unwrap() = Some(Duration::from_millis(20));
    let uris: Vec<String> = (0..7).map(|i| format!("dpe://docs/{i}.md")).collect();
    let mut documents: Vec<(String, ExpandedDocument)> =
        uris.iter().map(|uri| (uri.clone(), text(uri))).collect();
    documents.push((SECRET.to_owned(), text("S")));
    for uri in &uris {
        fake.script(Route::Commit(uri.clone()), vec![created(uri, &text(uri))]);
    }

    let results = StatefulPusher::with_store(&client, Arc::clone(&store) as Arc<dyn StateStore>, 2)
        .unwrap()
        .push(&documents, &none(), None)
        .await
        .unwrap();

    assert_eq!(
        results
            .iter()
            .filter(|r| r.status == PushStatus::Failed)
            .count(),
        1
    );
    assert_eq!(count(&server, "HEAD", "/documents"), 8);
    let peak = fake.head_peak.load(Ordering::SeqCst);
    assert_eq!(peak, 2, "峰值并发应恰为 concurrency");
}

#[tokio::test]
async fn round_level_failures_while_locating_fail_the_whole_batch() {
    for (reply, expected_code) in [
        (
            Reply::Response {
                status: 503,
                headers: vec![
                    ("DPE-Error-Code".to_owned(), "DPE_UNAVAILABLE".to_owned()),
                    ("Retry-After".to_owned(), "0".to_owned()),
                ],
                body: Vec::new(),
            },
            Some("DPE_UNAVAILABLE"),
        ),
        (Reply::new(502, "bad gateway"), None),
    ] {
        let fake = Fake::new();
        let server = fake.server();
        let client = client(&server);
        let store = Arc::new(MemoryStateStore::new());
        deny(&fake, &[SECRET]);
        *fake.head_override.lock().unwrap() = Some(reply);

        let error = pusher(&client, &store)
            .push(
                &docs(&[(A, &text("A")), (SECRET, &text("S"))]),
                &none(),
                None,
            )
            .await
            .unwrap_err();

        assert_eq!(error.code(), expected_code, "{error}");
        assert_eq!(fake.write_count.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn contract_rejection_is_not_located() {
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    *fake.batch_override.lock().unwrap() = Some(problem("DPE_CONTRACT_UNSUPPORTED", 400));

    let error = pusher(&client, &store)
        .push(&docs(&[(A, &text("A")), (B, &text("B"))]), &none(), None)
        .await
        .unwrap_err();

    assert_eq!(error.kind(), ErrorKind::ContractUnsupported);
    assert_eq!(count(&server, "HEAD", "/documents"), 0);
}

#[tokio::test]
async fn move_with_both_sides_denied_reports_the_source_error() {
    const TARGET: &str = "dpe://secret/t.md";
    let fake = Fake::new();
    let server = fake.server();
    let client = client(&server);
    let store = Arc::new(MemoryStateStore::new());
    fake.set(SECRET, Some(doc_hash(&text("S"))));
    deny(&fake, &[SECRET]);
    fake.invalid_reads.lock().unwrap().insert(TARGET.to_owned());

    let moved = pusher(&client, &store)
        .move_documents(&moves(&[(SECRET, TARGET), (A, B)]), &none())
        .await
        .unwrap();

    assert_eq!(moved[0].status, PushStatus::Failed);
    // 源侧 403，而不是目标侧的 400
    assert_eq!(
        moved[0].error.as_ref().and_then(|error| error.code()),
        Some("DPE_FORBIDDEN")
    );
}
