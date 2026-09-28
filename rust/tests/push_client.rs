mod common;

use common::{make_doc, setup};
use dpe_protocol::push::{CommitStatus, NegotiateStatus, PushOptions};
use dpe_protocol::schema::ElementMetadata;
use dpe_protocol::DpeError;

#[tokio::test]
async fn first_push_creates_document_with_all_contents() {
    let (server, client) = setup();
    let result = client.push(&make_doc(&["欢迎加入。", "第一条"]), None).await.unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!(result.negotiate.as_ref().unwrap().status, NegotiateStatus::New);
    assert_eq!(result.contents_sent, 4);
    assert_eq!(server.state().documents["dpe://acme/handbook"].doc_hash, result.doc_hash);
}

#[tokio::test]
async fn second_push_only_sends_missing_contents() {
    let (server, client) = setup();
    let first = client.push(&make_doc(&["欢迎加入。", "第一条"]), None).await.unwrap();
    let second = client.push(&make_doc(&["欢迎加入。", "第二条"]), Some(first.doc_hash.clone())).await.unwrap();

    assert_eq!(second.status, CommitStatus::Updated);
    assert_eq!(second.contents_sent, 1);
    let commit = server.request_bodies(":commit").pop().unwrap();
    assert_eq!(commit["contents"][0]["element"]["text"], "第二条");
    assert_eq!(commit["manifest"]["base_doc_hash"], first.doc_hash.as_str());
    assert_eq!(server.state().documents["dpe://acme/handbook"].document.pages[0].elements[2].text, "第二条");
}

#[tokio::test]
async fn unchanged_document_stops_after_negotiate() {
    let (server, client) = setup();
    let first = client.push(&make_doc(&["a"]), None).await.unwrap();
    let again = client.push(&make_doc(&["a"]), None).await.unwrap();

    assert_eq!(again.status, CommitStatus::Unchanged);
    assert_eq!(again.doc_hash, first.doc_hash);
    assert_eq!(server.request_bodies(":commit").len(), 1);
}

#[tokio::test]
async fn wire_format_matches_protocol() {
    let (server, client) = setup();
    client.push(&make_doc(&["a"]), None).await.unwrap();
    let state = server.state();
    let request = state.requests.iter().find(|r| r.path().ends_with(":commit")).unwrap();

    assert_eq!(request.header("content-type"), Some("application/vnd.tfrs.dpe.v1+json"));
    assert_eq!(request.header("authorization"), Some("Bearer t0ken"));
    assert_eq!(request.header("x-tfrs-robot-id"), Some("rid-1"));
    assert_eq!(request.header("content-encoding"), Some("gzip"));
    assert!(request.header("idempotency-key").is_some());
    drop(state);

    let body = server.request_bodies(":commit").pop().unwrap();
    for item in body["contents"].as_array().unwrap() {
        let element = item["element"].as_object().unwrap();
        assert!(!element.contains_key("content_hash") && !element.contains_key("seq_in_page"));
    }
    // doc_metadata 必须带全量声明字段（含 null），否则服务端会用 now() 填 created_at
    let meta = body["manifest"]["doc_metadata"].as_object().unwrap();
    assert!(meta.contains_key("created_at") && meta["created_at"].is_null());
}

#[tokio::test]
async fn skip_negotiate_recovers_from_insufficient_content() {
    let (server, client) = setup();
    let result = client.push_with(&make_doc(&["a"]), None, PushOptions { skip_negotiate: true }).await.unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    assert!(server.request_bodies(":negotiate").is_empty());
    let commits = server.request_bodies(":commit");
    assert_eq!(commits[0]["contents"].as_array().unwrap().len(), 0);
    assert_eq!(commits[1]["contents"].as_array().unwrap().len(), 3);
}

#[tokio::test(start_paused = true)]
async fn retries_rate_limit_then_succeeds() {
    let (server, client) = setup();
    server.state().faults = vec![
        (429, "DPE_RATE_LIMITED".into(), vec![("Retry-After".into(), "1".into())]),
        (503, "DPE_ROBOT_UNAVAILABLE".into(), vec![]),
    ];
    let result = client.push(&make_doc(&["a"]), None).await.unwrap();
    assert_eq!(result.status, CommitStatus::Created);
}

#[tokio::test]
async fn non_retryable_error_is_raised_with_code() {
    let (server, client) = setup();
    server.state().faults = vec![(400, "DPE_MANIFEST_INVALID".into(), vec![])];
    let err = client.push(&make_doc(&["a"]), None).await.unwrap_err();
    let push = err.push_error().expect("push error");
    assert_eq!((push.code.as_str(), push.http_status, push.retryable()), ("DPE_MANIFEST_INVALID", 400, false));
}

#[tokio::test]
async fn image_path_rejected_locally() {
    let (server, client) = setup();
    let mut doc = make_doc(&["a"]);
    doc.pages[0].elements.last_mut().unwrap().ele_metadata =
        ElementMetadata { image_path: Some("r1/x.png".into()), ..Default::default() };
    assert!(matches!(client.push(&doc, None).await, Err(DpeError::Validation(_))));
    assert!(server.request_bodies(":negotiate").is_empty());
}

#[tokio::test]
async fn no_compatible_hash_strategy() {
    let (server, client) = setup();
    server.state().accepted_strategies = vec!["hash-strategy://default?algo_version=v9".into()];
    assert!(matches!(client.push(&make_doc(&["a"]), None).await, Err(DpeError::NoCompatibleHashStrategy(_))));
}

#[tokio::test]
async fn payload_too_large_fails_before_sending() {
    let (server, client) = setup();
    server.state().max_payload_bytes = 200;
    let err = client.push(&make_doc(&["a"]), None).await.unwrap_err();
    assert_eq!(err.push_error().unwrap().code, "DPE_PAYLOAD_TOO_LARGE");
    assert!(server.state().requests.iter().all(|r| r.method == dpe_protocol::push::Method::Get));
}

fn big_doc(pages: i64, size: usize, prefix: &str) -> dpe_protocol::schema::Document {
    use dpe_protocol::schema::{DocElement, DocPage, Document, ElementCategory, FileType};
    Document::new(
        "dpe://acme/big".parse().unwrap(),
        FileType::Txt,
        (0..pages)
            .map(|p| {
                let elements = (0..3)
                    .map(|i| {
                        DocElement::new(ElementCategory::NarrativeText, format!("{prefix}{p}-{i}-{}", "x".repeat(size)))
                    })
                    .collect();
                DocPage::new(p, Some(format!("p{p}")), elements)
            })
            .collect(),
    )
}

fn texts(doc: &dpe_protocol::schema::Document) -> Vec<String> {
    doc.iter_elements().map(|(_, e)| e.text.clone()).collect()
}

#[tokio::test]
async fn large_first_push_is_sharded() {
    let (server, client) = setup();
    server.state().max_payload_bytes = 4000;
    let doc = big_doc(6, 400, "");
    let result = client.push(&doc, None).await.unwrap();

    assert!(result.shards > 1);
    assert_eq!(result.contents_sent, 18);
    assert_eq!(texts(&server.state().documents["dpe://acme/big"].document), texts(&doc));
    for body in server.request_bodies(":commit") {
        assert!(serde_json::to_vec(&body).unwrap().len() <= 4000);
        assert_eq!(body["manifest"]["pages"].as_array().unwrap().len(), 6);
    }
}

#[tokio::test]
async fn sharded_update_never_drops_existing_elements() {
    use std::collections::HashSet;

    let (server, client) = setup();
    client.push(&big_doc(6, 400, ""), None).await.unwrap();
    let old: HashSet<String> = server.state().documents["dpe://acme/big"].contents.keys().cloned().collect();
    let commits_before = server.request_bodies(":commit").len();

    server.state().max_payload_bytes = 4000;
    client.discover(true).await.unwrap(); // 能力文档有缓存，服务端调整上限后需刷新
    let mut updated = big_doc(6, 400, "");
    for page in &mut updated.pages {
        let extra = big_doc(1, 400, &format!("new{}", page.number)).pages[0].elements[0].clone();
        page.elements.push(extra);
    }
    let result = client.push(&updated, None).await.unwrap();

    assert!(result.shards > 1);
    assert_eq!(result.contents_sent, 6);
    assert_eq!(result.counts.as_ref().unwrap().elements_removed, 0);
    for body in &server.request_bodies(":commit")[commits_before..] {
        let shard: HashSet<String> = body["manifest"]["pages"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|p| p["elements"].as_array().unwrap().iter())
            .map(|e| e["content_hash"].as_str().unwrap().to_string())
            .collect();
        // 旧元素在每一片中都保留：不会先删后建，也就不会重复传输与重复学习
        assert!(old.is_subset(&shard));
    }
    assert_eq!(texts(&server.state().documents["dpe://acme/big"].document), texts(&updated));
}

#[tokio::test]
async fn single_element_over_budget_cannot_be_sharded() {
    let (server, client) = setup();
    server.state().max_payload_bytes = 4000;
    let err = client.push(&big_doc(1, 5000, ""), None).await.unwrap_err();
    assert_eq!(err.push_error().unwrap().code, "DPE_PAYLOAD_TOO_LARGE");
}
