//! 暂存路径对参考服务端（#25）：与 Python `tests/test_protocol_staging_http.py`（#49 / #50）
//! 逐条对等。
//!
//! Python 用进程内 `FakeClock` 拨钟的场景，这里经参考服务端的 `clock/advance` 钩子拨（需
//! `--hooks`；`--staging-ttl-seconds` 不得低于 3600，拨钟秒数取同值）；丢响应、发出前拨钟在
//! 客户端驱动循环里注入（`reference_common::drive`）。

#![cfg(feature = "http")]

mod reference_common;

use std::sync::Arc;

use dpe_sdk::protocol::{BlobReader, BlobSource, Precondition};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{ErrorKind, ExpandedDocument, PageObject};
use serde_json::json;

use reference_common::{
    body_json, chunk_spans, doc_hash, drive, from_value, image_bytes, is_blob_chunk_from,
    session_ids, Injection, ReferenceServer, SMALL_LIMITS,
};

const URI: &str = "test://docs/staged";
const TARGET: &str = "documents?uri=test%3A%2F%2Fdocs%2Fstaged";

fn image() -> Vec<u8> {
    image_bytes() // 250 字节
}

fn reference() -> String {
    dpe_hash::blob_ref(&image())
}

fn image_doc() -> ExpandedDocument {
    from_value(json!({
        "file_type": "png",
        "title": "图片",
        "pages": [{"elements": [
            {"category": "Image", "blob": reference(), "mime_type": "image/png"}
        ]}],
    }))
}

/// 一页多元素：页对象超过 `max_payload_bytes` 而每个元素不超（走页分块）。
fn many_elements_doc(count: usize) -> ExpandedDocument {
    let elements: Vec<_> = (0..count)
        .map(|i| json!({"category": "NarrativeText", "text": format!("sentence {i} ").repeat(4)}))
        .collect();
    from_value(json!({"file_type": "md", "pages": [{"elements": elements}]}))
}

fn args(extra: &[&'static str]) -> Vec<&'static str> {
    let mut all = vec!["--prefix", "/r/1"];
    all.extend_from_slice(SMALL_LIMITS);
    all.extend_from_slice(extra);
    all
}

/// 以 TTL 3600 + 钩子启动（拨钟场景）。
fn clocked() -> Vec<&'static str> {
    args(&["--staging-ttl-seconds", "3600", "--hooks", "/__hooks__"])
}

fn image_provider() -> impl FnMut(&str) -> Option<BlobSource> {
    |_: &str| Some(BlobSource::from_bytes(image()))
}

#[tokio::test]
async fn deliver_chunks_a_large_page_against_the_reference_server() {
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = many_elements_doc(10);
    let mut recorder = Injection::new();

    let result = drive(
        &server,
        &client,
        core.deliver(URI, &document, Precondition::IfAbsent, None, None)
            .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!((result.delta.added, result.delta.removed), (10, 0));
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
    assert_eq!(
        (skeleton.pages.len(), skeleton.pages[0].elements.len()),
        (1, 10)
    );
    let spans = chunk_spans(&recorder.sent);
    assert!(!spans.is_empty(), "页对象应当分块上传");
    let total = spans[0].2;
    assert!(spans.iter().all(|span| span.2 == total));
    assert_eq!((spans[0].0, spans.last().unwrap().1 + 1), (0, total));
    assert!(spans.windows(2).all(|pair| pair[1].0 == pair[0].1 + 1));
    for request in &recorder.sent {
        if request.headers.get("Content-Range").is_some() {
            let body = request.body_bytes().unwrap().expect("分块请求带体");
            assert!(body.len() <= 100, "单块不超过 blob_chunk_bytes");
        }
    }
}

fn is_second_blob_chunk(request: &dpe_sdk::Request) -> bool {
    is_blob_chunk_from(request, 100)
}

#[tokio::test]
async fn deliver_resumes_a_lost_blob_chunk_against_the_reference_server() {
    // 某一块的响应丢失：先断点查询、按已收偏移续传（core §3.4 的客户端义务）
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = image_doc();
    let mut provider = image_provider();
    let mut lossy = Injection::new().lose(is_second_blob_chunk);

    let result = drive(
        &server,
        &client,
        core.deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap(),
        &mut lossy,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!(lossy.dropped, 1);
    let dropped_at = lossy
        .sent
        .iter()
        .position(is_second_blob_chunk)
        .expect("丢弃的那一块");
    let head = &lossy.sent[dropped_at + 1];
    assert_eq!(
        (head.method.as_str(), head.target.as_str()),
        ("HEAD", lossy.sent[dropped_at].target.as_str())
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
}

#[tokio::test]
async fn deliver_renegotiates_when_the_session_expires_mid_flow() {
    let Some(server) = ReferenceServer::spawn(&clocked()) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = many_elements_doc(10);
    let mut expiring = Injection::new().expire_before(
        |request| request.method.as_str() == "PUT" && request.target.contains("/pages/"),
        3601,
    );

    let result = drive(
        &server,
        &client,
        core.deliver(URI, &document, Precondition::IfAbsent, None, None)
            .unwrap(),
        &mut expiring,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!(expiring.expired, 1);
    let sessions = session_ids(&expiring.sent);
    assert_eq!(
        sessions.len(),
        2,
        "过期后重开：旧会话的请求被拒、新会话完成投递"
    );
    let last = body_json(expiring.sent.last().unwrap());
    assert!(sessions.contains(last["staging_session"].as_str().expect("引用会话")));
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
}

/// 声明正确 ref、给出错误字节的读取器。
struct Wrong;

impl BlobReader for Wrong {
    fn read(&self, _offset: u64, length: u64) -> std::io::Result<Vec<u8>> {
        Ok(vec![b'y'; length as usize])
    }
}

#[tokio::test]
async fn deliver_reports_a_server_side_blob_hash_mismatch() {
    // 声明了正确的 ref 但字节不符：服务端到齐校验失败，如实上报（带会话句柄）
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let mut provider = |_: &str| Some(BlobSource::new(reference(), 250, Arc::new(Wrong)));

    let error = client
        .deliver(
            URI,
            &image_doc(),
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .await
        .expect_err("字节不符");

    assert_eq!(error.kind(), ErrorKind::HashMismatch);
    assert!(error.session().is_some());
}

#[tokio::test]
async fn deliver_recovers_a_missing_blob_via_a_session() {
    // 快路径缺 blob（blob 永远不能内联）：开会话补传后以「内联 + 会话」重提（core §6）
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = image_doc();
    let mut provider = image_provider();
    let mut recorder = Injection::new();

    let result = drive(
        &server,
        &client,
        core.deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    let sent = recorder.methods_and_targets();
    assert_eq!(sent[0], ("PUT".to_owned(), TARGET.to_owned()), "先快路径");
    assert_eq!(sent[1], ("POST".to_owned(), "negotiate".to_owned()));
    assert!(sent.iter().any(|(_, target)| target.contains("/blobs/")));
    let body = body_json(recorder.sent.last().unwrap());
    assert!(
        body["staging_session"].is_string()
            && body["pages"]
                .as_array()
                .is_some_and(|pages| !pages.is_empty())
            && body["objects"]
                .as_array()
                .is_some_and(|objects| !objects.is_empty()),
        "内联 + 会话：{body}"
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
}

#[tokio::test]
async fn deliver_reuses_the_session_after_a_cas_failure() {
    // CAS 冲突后以新前置条件引用同一会话重提，已上传内容不重传（core §3.4）
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let (document, changed) = (many_elements_doc(10), many_elements_doc(9));
    let created = client
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    assert_eq!(created.status, CommitStatus::Created);

    // 前置条件不满足（如他人已写入）：冲突原样上报，错误带仍可复用的会话
    let error = client
        .deliver(
            URI,
            &changed,
            Precondition::BaseHash(doc_hash(&changed)),
            None,
            None,
        )
        .await
        .expect_err("前置条件不符");
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    let session = error.session().expect("带会话").clone();

    // 重新读取后引用同一会话重提：只 commit，不再 negotiate、不重传页与元素
    let mut recorder = Injection::new();
    let result = drive(
        &server,
        &client,
        core.deliver(
            URI,
            &changed,
            Precondition::BaseHash(doc_hash(&document)),
            None,
            Some(session),
        )
        .unwrap(),
        &mut recorder,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Updated);
    let methods: Vec<&str> = recorder
        .sent
        .iter()
        .map(|request| request.method.as_str())
        .collect();
    assert_eq!(methods, ["PUT"]);
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&changed));
}

/// V1 → V2：第一页追加一个文本与一个带 blob 的图片元素，第二页不动（未变的层不应重传）。
fn incremental_docs() -> (ExpandedDocument, ExpandedDocument) {
    let texts = json!([
        {"category": "NarrativeText", "text": "x"},
        {"category": "NarrativeText", "text": "a"},
    ]);
    let second = json!({"elements": [{"category": "NarrativeText", "text": "b"}]});
    let v1 = from_value(json!({"file_type": "md", "pages": [{"elements": texts}, second]}));
    let mut first = texts.as_array().unwrap().clone();
    first.push(json!({"category": "NarrativeText", "text": "c"}));
    first.push(json!({"category": "Image", "blob": reference(), "mime_type": "image/png"}));
    let v2 = from_value(json!({"file_type": "md", "pages": [{"elements": first}, second]}));
    (v1, v2)
}

#[tokio::test]
async fn deliver_uploads_only_the_changed_layers_on_an_incremental_update() {
    // 增量投递：去重范围固定为本文档（core §3.3），只传变更的页、元素与 blob。
    // max_payload_bytes=240：negotiate 与 commit 请求体放得下，快路径内联体放不下（必走暂存）、
    // 变更页（302 字节）与 blob（250 字节）都放不下（必分块）；会话 id 用确定性的短值
    let mut extra = vec!["--prefix", "/r/1"];
    extra.extend_from_slice(SMALL_LIMITS);
    extra.extend_from_slice(&["--max-payload-bytes", "240", "--session-id-prefix", "st-"]);
    let Some(server) = ReferenceServer::spawn(&extra) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let (v1, v2) = incremental_docs();
    let hashes = v2.hashes(dpe_sdk::CONTRACT).unwrap();
    let changed_page = hashes.pages[0].page_hash.clone();
    let (new_text, new_image) = (
        hashes.pages[0].elements[2].clone(),
        hashes.pages[0].elements[3].clone(),
    );
    let first = client
        .deliver(URI, &v1, Precondition::IfAbsent, None, None)
        .await
        .unwrap();
    assert_eq!(first.status, CommitStatus::Created);
    let mut provider = image_provider();
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
        (
            result.status,
            result.delta.added,
            result.delta.removed,
            result.delta.retained
        ),
        (CommitStatus::Updated, 2, 0, 3)
    );
    let sent = &recorder.sent;
    let sessions = session_ids(sent);
    assert_eq!(sessions.len(), 1, "全程一个会话：{sessions:?}");
    let sid = sessions.into_iter().next().unwrap();
    let targets = |part: &str| -> std::collections::BTreeSet<String> {
        sent.iter()
            .filter(|request| request.target.contains(part))
            .map(|request| request.target.clone())
            .collect()
    };
    // 只有变更的页被上传（未变的第二页不重传），页对象按分块走完
    assert_eq!(
        targets("/pages/"),
        [format!("staging/{sid}/pages/{changed_page}")].into()
    );
    let pages: Vec<_> = sent
        .iter()
        .filter(|request| request.target.contains("/pages/"))
        .cloned()
        .collect();
    let spans = chunk_spans(&pages);
    assert_eq!((spans[0].0, spans.last().unwrap().1 + 1), (0, spans[0].2));
    assert!(spans.windows(2).all(|pair| pair[1].0 == pair[0].1 + 1));
    // 只有两个新元素被上传（未变元素不重传；顺序无规范约束，按集合断言）
    assert_eq!(
        targets("/objects/"),
        [
            format!("staging/{sid}/objects/{new_text}"),
            format!("staging/{sid}/objects/{new_image}"),
        ]
        .into()
    );
    assert_eq!(
        targets("/blobs/"),
        [format!("staging/{sid}/blobs/{}", reference())].into()
    );
    let blobs: Vec<_> = sent
        .iter()
        .filter(|request| request.target.contains("/blobs/"))
        .cloned()
        .collect();
    let blob_spans = chunk_spans(&blobs);
    assert!(blob_spans.iter().all(|span| span.2 == 250));
    assert_eq!(
        (blob_spans[0].0, blob_spans.last().unwrap().1 + 1),
        (0, 250)
    );
    // 最后的 commit 引用会话，不内联页与元素
    let commit = sent.last().unwrap();
    assert_eq!(
        (commit.method.as_str(), commit.target.as_str()),
        ("PUT", TARGET)
    );
    let body = body_json(commit);
    assert_eq!(body["staging_session"], json!(sid));
    assert!(body.get("pages").is_none() && body.get("objects").is_none());
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&v2));
    assert_eq!(skeleton.pages.len(), 2);
}

#[tokio::test]
async fn deliver_renews_the_session_past_the_ttl() {
    // 会话续期（core §3.4）：每次成功上传把过期时间推后——总耗时超过 TTL 的长流程，仍以
    // 同一个会话完成，不重开 negotiate
    let Some(server) = ReferenceServer::spawn(&clocked()) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = many_elements_doc(10);
    // 每个请求前拨 1000 秒：单步间隔小于 TTL，但流程总耗时远大于 TTL
    let mut tick = Injection::new().tick(1000);

    let result = drive(
        &server,
        &client,
        core.deliver(URI, &document, Precondition::IfAbsent, None, None)
            .unwrap(),
        &mut tick,
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert!(tick.advanced > 3600, "流程总耗时 {} 秒", tick.advanced);
    assert_eq!(session_ids(&tick.sent).len(), 1);
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
}

#[tokio::test]
async fn blind_resend_of_an_applied_middle_chunk_is_rejected_with_the_offset() {
    // #73（HTTP 绑定 §4.7）：中间块已被接受后盲目重发同一块 → 400 + DPE-Upload-Offset，按该
    // 偏移续传成功；断点查询（HEAD，只读、不续期）给出同一偏移
    let Some(server) = ReferenceServer::spawn(&clocked()) else {
        return;
    };
    let client = server.client();
    let negotiated = client.negotiate(URI, &image_doc()).await.unwrap();
    let base = format!("staging/{}/blobs/{}", negotiated.session.id, reference());
    let bytes = image();
    let put = |range: &'static str, body: Vec<u8>| {
        let base = base.clone();
        let server = &server;
        async move {
            server
                .raw("PUT", &base, &[("Content-Range", range)], body)
                .await
        }
    };

    // 第一块已被接受（202 的响应在客户端看来「丢失」）
    let first = put("bytes 0-99/250", bytes[..100].to_vec()).await;
    assert_eq!(
        (first.status, first.header("DPE-Upload-Offset")),
        (202, Some("100"))
    );
    // 盲目重发同一块：不命中「已完成」的 200，而是 400 + 本会话已收字节数
    let blind = put("bytes 0-99/250", bytes[..100].to_vec()).await;
    assert_eq!(
        (blind.status, blind.code()),
        (400, "DPE_VALIDATION".to_owned())
    );
    assert_eq!(blind.header("DPE-Upload-Offset"), Some("100"));
    // 断点查询给出同一偏移，且不续期（拨钟后过期时间不变）
    server.advance_clock(60).await;
    let head = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(
        (head.status, head.header("DPE-Upload-Offset")),
        (200, Some("100"))
    );
    let expires = first
        .header("DPE-Session-Expires")
        .expect("202 带 DPE-Session-Expires");
    assert_eq!(head.header("DPE-Session-Expires"), Some(expires));
    // 按已收偏移续传剩余两块
    let middle = put("bytes 100-199/250", bytes[100..200].to_vec()).await;
    assert_eq!(
        (middle.status, middle.header("DPE-Upload-Offset")),
        (202, Some("200"))
    );
    let last = put("bytes 200-249/250", bytes[200..].to_vec()).await;
    assert_eq!(last.status, 201);
    let done = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(done.header("DPE-Upload-Offset"), Some("250"));
}

#[tokio::test]
async fn blob_upload_resyncs_from_the_offset_in_a_400() {
    // #73 客户端侧（HTTP 绑定 §4.7）：本会话已有进度时，核心的上传从 0 开始，收到
    // 400 + DPE-Upload-Offset 后直接按该偏移续传（不回退重发、不重开会话），完成后可提交
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = image_doc();
    let mut negotiated = client.negotiate(URI, &document).await.unwrap();
    let base = format!("staging/{}/blobs/{}", negotiated.session.id, reference());
    let bytes = image();
    // 服务端已有第一块（对客户端是「别处传来的既成进度」）：核心上传前并不知道
    let first = server
        .raw(
            "PUT",
            &base,
            &[("Content-Range", "bytes 0-99/250")],
            bytes[..100].to_vec(),
        )
        .await;
    assert_eq!(first.status, 202);

    let mut recorded = Injection::new();
    drive(
        &server,
        &client,
        core.upload_blob(
            &mut negotiated.session,
            BlobSource::from_bytes(bytes.clone()),
            None,
        )
        .unwrap(),
        &mut recorded,
    )
    .await
    .unwrap();

    // 第一条 PUT 从 0 开始并收到 400 + 偏移 100；第二条直接从 100 继续（不重发 0-99）
    let seen = recorded.responses[0].clone().expect("有响应");
    assert_eq!(
        (seen.status, seen.code.as_deref(), seen.offset.as_deref()),
        (400, Some("DPE_VALIDATION"), Some("100"))
    );
    assert_eq!(
        chunk_spans(&recorded.sent),
        [(0, 99, 250), (100, 199, 250), (200, 249, 250)]
    );
    // 全程一个会话，不开新会话
    assert_eq!(
        session_ids(&recorded.sent),
        [negotiated.session.id.clone()].into()
    );
    // 完成后偏移等于总字节数；补齐文档其余层后，引用同一会话提交成功、内容一致
    let done = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(done.header("DPE-Upload-Offset"), Some("250"));
    let hashes = document.hashes(dpe_sdk::CONTRACT).unwrap();
    let page = PageObject::from_value(
        &json!({"elements": hashes.pages[0].elements}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    client
        .upload_page(&mut negotiated.session, &page)
        .await
        .unwrap();
    client
        .upload_element(&mut negotiated.session, &document.pages[0].elements[0])
        .await
        .unwrap();
    let result = client
        .commit(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&negotiated.session),
        )
        .await
        .unwrap();
    assert_eq!(
        (result.status, result.doc_hash),
        (CommitStatus::Created, doc_hash(&document))
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
}

#[tokio::test]
async fn resume_query_reports_the_three_states_and_resets_after_discard() {
    // #73（HTTP 绑定 §4.7）：断点查询三状态——本会话无进度 → 200 + 偏移 0（不得 404）、
    // 已完成 → 该对象总字节数、到齐校验失败被丢弃 → 归 0
    let Some(server) = ReferenceServer::spawn(&args(&[])) else {
        return;
    };
    let client = server.client();
    let negotiated = client.negotiate(URI, &image_doc()).await.unwrap();
    let base = format!("staging/{}/blobs/{}", negotiated.session.id, reference());
    let bytes = image();

    // ① 无进度：200 + 偏移 0（绝不是 404）
    let empty = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(
        (empty.status, empty.header("DPE-Upload-Offset")),
        (200, Some("0"))
    );
    // ② 到齐校验失败被丢弃：已收的中间态先有偏移，失败后归 0
    let part = server
        .raw(
            "PUT",
            &base,
            &[("Content-Range", "bytes 0-99/250")],
            bytes[..100].to_vec(),
        )
        .await;
    assert_eq!(part.status, 202);
    let progress = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(progress.header("DPE-Upload-Offset"), Some("100"));
    let second = server
        .raw(
            "PUT",
            &base,
            &[("Content-Range", "bytes 100-199/250")],
            vec![b'y'; 100],
        )
        .await;
    assert_eq!(second.status, 202);
    let wrong = server
        .raw(
            "PUT",
            &base,
            &[("Content-Range", "bytes 200-249/250")],
            vec![b'y'; 50],
        )
        .await;
    assert_eq!(
        (wrong.status, wrong.code()),
        (400, "DPE_HASH_MISMATCH".to_owned())
    );
    let discarded = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(discarded.header("DPE-Upload-Offset"), Some("0"));
    // ③ 完成：偏移 = 该对象总字节数
    let mut statuses = Vec::new();
    for start in (0..250).step_by(100) {
        let end = (start + 100).min(250);
        let range = format!("bytes {start}-{}/250", end - 1);
        let response = server
            .raw(
                "PUT",
                &base,
                &[("Content-Range", range.as_str())],
                bytes[start..end].to_vec(),
            )
            .await;
        statuses.push(response.status);
    }
    assert_eq!(statuses.last(), Some(&201));
    let done = server.raw("HEAD", &base, &[], Vec::new()).await;
    assert_eq!(done.header("DPE-Upload-Offset"), Some("250"));
}
