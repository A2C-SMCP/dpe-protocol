//! 更新已存在的文档时只传缺失（#212，core §3.2）对参考服务端：与 Python
//! `tests/test_deliver_delta_http.py` 逐条对等。
//!
//! 选路（两个 SDK 一致）：新建与小文档（整篇内联不超过 `SMALL_UPDATE_BYTES`）的更新走快路径；
//! 更新较大的文档走差量路径，只传缺失、往返数有上限——`negotiate` 只交文档对象得到缺失的页；
//! 有缺失的页时再 `negotiate` 一次、只附带这些页（存入会话），得到其中缺失的元素；`commit`
//! 引用会话并只内联缺失的元素。缺失的 blob 由 commit 的缺失清单给出，补传后重提。已有的页与
//! 元素一次都不出现在请求里。

#![cfg(feature = "http")]

mod reference_common;

use std::collections::BTreeSet;

use dpe_sdk::models::{ElementObject, PageObject};
use dpe_sdk::protocol::{BlobSource, Precondition, SMALL_UPDATE_BYTES};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{ExpandedDocument, Request};
use serde_json::{json, Value};

use reference_common::{body_json, doc_hash, drive, from_value, Injection, ReferenceServer};

const URI: &str = "test://docs/delta";

fn image() -> Vec<u8> {
    let mut bytes = b"\x89PNG".to_vec();
    bytes.extend(std::iter::repeat(b'y').take(96));
    bytes
}

fn element(tag: &str) -> Value {
    json!({"category": "NarrativeText", "text": format!("{tag} {}", "lorem ipsum dolor sit amet ".repeat(3))})
}

fn page(tag: &str, count: usize) -> Value {
    let elements: Vec<Value> = (0..count).map(|i| element(&format!("{tag}-{i}"))).collect();
    json!({"elements": elements})
}

fn document(pages: Vec<Value>) -> ExpandedDocument {
    from_value(json!({"file_type": "md", "pages": pages}))
}

/// 三页、整篇内联超过 `SMALL_UPDATE_BYTES` 的文档；`extra` 插入到第 1 页之前。
fn large(extra: Vec<Value>) -> ExpandedDocument {
    let mut pages = vec![page("a", 250), page("b", 250), page("c", 250)];
    for (offset, inserted) in extra.into_iter().enumerate() {
        pages.insert(1 + offset, inserted);
    }
    document(pages)
}

fn negotiates(sent: &[Request]) -> Vec<&Request> {
    sent.iter().filter(|r| r.target == "negotiate").collect()
}

fn commits(sent: &[Request]) -> Vec<&Request> {
    sent.iter()
        .filter(|r| r.target.starts_with("documents?"))
        .collect()
}

fn inline_hashes(body: &Value) -> Vec<String> {
    let mut hashes: Vec<String> = body["objects"]
        .as_array()
        .map(|objects| {
            objects
                .iter()
                .map(|object| {
                    ElementObject::parse(&object.to_string(), dpe_sdk::CONTRACT)
                        .and_then(|element| element.content_hash(dpe_sdk::CONTRACT))
                        .expect("合法元素")
                })
                .collect()
        })
        .unwrap_or_default();
    hashes.sort();
    hashes
}

/// 请求里出现过的全部页对象（negotiate 附带、commit 内联与暂存上传）的 page_hash。
fn sent_pages(sent: &[Request]) -> BTreeSet<String> {
    let mut hashes = BTreeSet::new();
    for request in sent {
        if request.target == "negotiate" || request.target.starts_with("documents?") {
            for page in body_json(request)["pages"].as_array().into_iter().flatten() {
                hashes.insert(
                    PageObject::parse(&page.to_string(), dpe_sdk::CONTRACT)
                        .and_then(|page| page.page_hash(dpe_sdk::CONTRACT))
                        .expect("合法页"),
                );
            }
        } else if request.target.contains("/pages/") {
            hashes.insert(request.target.rsplit('/').next().unwrap().to_owned());
        }
    }
    hashes
}

fn page_hashes(document: &ExpandedDocument) -> BTreeSet<String> {
    document
        .hashes(dpe_sdk::CONTRACT)
        .unwrap()
        .pages
        .into_iter()
        .map(|page| page.page_hash)
        .collect()
}

fn page_elements(document: &ExpandedDocument, index: usize) -> Vec<String> {
    let mut hashes = document.hashes(dpe_sdk::CONTRACT).unwrap().pages[index]
        .elements
        .clone();
    hashes.sort();
    hashes
}

fn spawn(extra: &[&'static str]) -> Option<ReferenceServer> {
    let mut args = vec!["--prefix", "/r/1"];
    args.extend_from_slice(extra);
    ReferenceServer::spawn(&args)
}

#[tokio::test]
async fn inserting_a_middle_page_sends_only_the_new_objects() {
    // 北极星验收：中间插入一页，只传新增的内容对象
    let Some(server) = spawn(&["--session-id-prefix", "st-"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = large(vec![]);
    let v2 = large(vec![page("new", 3)]);
    // 整篇内联超过阈值：走差量路径
    assert!(serde_json::to_vec(&v2).unwrap().len() as u64 > SMALL_UPDATE_BYTES);
    let created = client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    assert_eq!(created.status, CommitStatus::Created);
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added, result.delta.removed),
        (CommitStatus::Updated, 3, 0)
    );
    // 三次往返：只交文档对象 → 只附带新页 → commit
    let targets: Vec<&str> = recorder
        .sent
        .iter()
        .map(|r| r.target.split('?').next().unwrap())
        .collect();
    assert_eq!(targets, ["negotiate", "negotiate", "documents"]);
    let negotiate = negotiates(&recorder.sent);
    assert!(body_json(negotiate[0]).get("pages").is_none());
    let new_page = v2.hashes(dpe_sdk::CONTRACT).unwrap().pages[1]
        .page_hash
        .clone();
    assert_eq!(sent_pages(&recorder.sent), BTreeSet::from([new_page]));
    let body = body_json(commits(&recorder.sent)[0]);
    assert!(body["staging_session"].as_str().is_some());
    assert!(body.get("pages").is_none()); // 新页已随第二次 negotiate 存入会话，不再内联
    assert_eq!(inline_hashes(&body), page_elements(&v2, 1));
    assert!(sent_pages(&recorder.sent).is_disjoint(&page_hashes(&v1))); // 旧页对象一次都不出现
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&v2));
}

#[tokio::test]
async fn a_new_blob_is_uploaded_from_the_commit_manifest() {
    // blob 不能内联：由 commit 的缺失清单给出、补传到同一会话后重提；两次 commit 都只内联新元素
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let reference = dpe_hash::blob_ref(&image());
    let v1 = large(vec![]);
    let v2 = large(vec![json!({"elements": [
        {"category": "Image", "blob": reference, "mime_type": "image/png"}
    ]})]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut provider = |_: &str| Some(BlobSource::from_bytes(image()));
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(
            URI,
            &v2,
            Precondition::BaseHash(doc_hash(&v1)),
            Some(&mut provider),
            None,
        )
        .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 1)
    );
    let commits = commits(&recorder.sent);
    assert_eq!(commits.len(), 2);
    for commit in &commits {
        assert_eq!(inline_hashes(&body_json(commit)), page_elements(&v2, 1));
    }
    let blobs: Vec<&String> = recorder
        .sent
        .iter()
        .map(|r| &r.target)
        .filter(|target| target.contains("/blobs/"))
        .collect();
    assert_eq!(blobs.len(), 1);
    assert!(sent_pages(&recorder.sent).is_disjoint(&page_hashes(&v1)));
}

#[tokio::test]
async fn force_update_of_a_large_document_also_sends_only_missing() {
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = large(vec![]);
    let v2 = large(vec![page("new", 2)]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::Force, None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 2)
    );
    let body = body_json(commits(&recorder.sent)[0]);
    assert_eq!(body["force"], json!(true));
    assert_eq!(inline_hashes(&body), page_elements(&v2, 1));
}

#[tokio::test]
async fn creating_a_large_document_is_still_a_single_commit() {
    // 新建：内容必然全部缺失，整篇内联放得下就走快路径（一次往返）
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &large(vec![]), Precondition::IfAbsent, None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!(recorder.sent.len(), 1);
    assert_eq!(commits(&recorder.sent).len(), 1);
}

#[tokio::test]
async fn updating_a_small_document_is_still_a_single_commit() {
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = document(vec![page("a", 3)]);
    let v2 = document(vec![page("a", 3), page("b", 1)]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(result.status, CommitStatus::Updated);
    assert_eq!(recorder.sent.len(), 1);
    assert_eq!(commits(&recorder.sent).len(), 1);
}

#[tokio::test]
async fn a_delta_that_does_not_fit_falls_back_to_layered_uploads() {
    // 差量请求体放不下 max_payload_bytes：按 negotiate 的清单逐个上传缺失元素，commit 只引用会话
    let Some(server) = spawn(&["--max-payload-bytes", "2000"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let big =
        |i: &str| json!({"category": "NarrativeText", "text": format!("{i} {}", "x".repeat(1200))});
    let v1 = document(vec![page("a", 1), page("b", 1)]);
    let v2 = document(vec![
        page("a", 1),
        json!({"elements": [big("1"), big("2")]}),
        page("b", 1),
    ]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 2)
    );
    let negotiate = negotiates(&recorder.sent);
    assert_eq!(negotiate.len(), 2);
    assert!(body_json(negotiate[0]).get("pages").is_none());
    assert_eq!(
        body_json(negotiate[1])["pages"].as_array().map(Vec::len),
        Some(1)
    );
    let uploads = recorder
        .sent
        .iter()
        .filter(|r| r.target.contains("/objects/"))
        .count();
    assert_eq!(uploads, 2);
    let body = body_json(commits(&recorder.sent)[0]);
    assert!(body.get("objects").is_none() && body.get("pages").is_none());
}

#[tokio::test]
async fn an_expired_delta_session_is_renegotiated_with_pages() {
    // 差量路径的会话在 commit 前过期：重走两次 negotiate，重算只含缺失对象的请求体
    let Some(server) = spawn(&["--staging-ttl-seconds", "3600", "--hooks", "/__hooks__"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = large(vec![]);
    let v2 = large(vec![page("new", 2)]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder =
        Injection::new().expire_before(|request| request.target.starts_with("documents?"), 3601);
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 2)
    );
    assert_eq!(recorder.expired, 1);
    let carried: Vec<usize> = negotiates(&recorder.sent)
        .iter()
        .map(|n| body_json(n)["pages"].as_array().map_or(0, Vec::len))
        .collect();
    assert_eq!(carried, [0, 1, 0, 1]);
    let commits = commits(&recorder.sent);
    assert_eq!(commits.len(), 2);
    assert_ne!(
        body_json(commits[0])["staging_session"],
        body_json(commits[1])["staging_session"]
    );
    assert_eq!(inline_hashes(&body_json(commits[1])), page_elements(&v2, 1));
    assert!(sent_pages(&recorder.sent).is_disjoint(&page_hashes(&v1)));
}

#[tokio::test]
async fn appending_one_element_sends_only_that_element() {
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = large(vec![]);
    let mut last = page("c", 250);
    last["elements"]
        .as_array_mut()
        .unwrap()
        .push(element("tail"));
    let v2 = document(vec![page("a", 250), page("b", 250), last]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 1)
    );
    let tail = page_elements(&v2, 2)
        .into_iter()
        .find(|hash| !page_elements(&v1, 2).contains(hash))
        .unwrap();
    assert_eq!(
        inline_hashes(&body_json(commits(&recorder.sent)[0])),
        [tail]
    );
    let changed = v2.hashes(dpe_sdk::CONTRACT).unwrap().pages[2]
        .page_hash
        .clone();
    assert_eq!(sent_pages(&recorder.sent), BTreeSet::from([changed]));
}

#[tokio::test]
async fn reordering_pages_needs_no_objects() {
    // 只调整页序：没有缺失的页，negotiate 只交文档对象后直接 commit 引用会话（两次往返）
    let Some(server) = spawn(&[]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let v1 = large(vec![]);
    let v2 = document(vec![page("c", 250), page("a", 250), page("b", 250)]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added, result.delta.removed),
        (CommitStatus::Updated, 0, 0)
    );
    let targets: Vec<&str> = recorder
        .sent
        .iter()
        .map(|r| r.target.split('?').next().unwrap())
        .collect();
    assert_eq!(targets, ["negotiate", "documents"]);
    let body = body_json(commits(&recorder.sent)[0]);
    assert!(body.get("objects").is_none() && body.get("pages").is_none());
    assert!(sent_pages(&recorder.sent).is_empty());
}

#[tokio::test]
async fn a_new_page_too_large_to_attach_is_uploaded_by_layers() {
    // 缺失的页连附带进第二次 negotiate 都放不下：按第一次 negotiate 的清单逐层上传——新页经
    // 暂存上传（响应给出其中缺失的元素），旧页一次都不出现
    let Some(server) = spawn(&["--max-payload-bytes", "2000"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let wide: Vec<Value> = (0..40)
        .map(|i| json!({"category": "NarrativeText", "text": format!("w{i}")}))
        .collect();
    let v1 = document(vec![page("a", 1), page("b", 1)]);
    let v2 = document(vec![page("a", 1), json!({"elements": wide}), page("b", 1)]);
    client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(URI, &v2, Precondition::BaseHash(doc_hash(&v1)), None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();
    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Updated, 40)
    );
    let negotiate = negotiates(&recorder.sent);
    assert_eq!(negotiate.len(), 1);
    assert!(body_json(negotiate[0]).get("pages").is_none());
    let new_page = v2.hashes(dpe_sdk::CONTRACT).unwrap().pages[1]
        .page_hash
        .clone();
    assert_eq!(sent_pages(&recorder.sent), BTreeSet::from([new_page]));
    let uploaded: BTreeSet<String> = recorder
        .sent
        .iter()
        .filter(|r| r.target.contains("/objects/"))
        .map(|r| r.target.rsplit('/').next().unwrap().to_owned())
        .collect();
    assert_eq!(uploaded, page_elements(&v2, 1).into_iter().collect());
    // 分块上传的页的缺失清单可能截断，余下的由 commit 的缺失清单逐轮给出（core §3.3）
    assert!(commits(&recorder.sent).iter().all(|commit| {
        let body = body_json(commit);
        body.get("objects").is_none() && body.get("pages").is_none()
    }));
}
