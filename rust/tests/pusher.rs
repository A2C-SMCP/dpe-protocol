mod common;

use common::{make_doc_at, setup};
use dpe_protocol::push::CommitStatus;
use dpe_protocol::state::{JsonFileStateStore, MemoryStateStore, SyncStateStore};
use dpe_protocol::StatefulPusher;

#[tokio::test]
async fn pusher_tracks_base_doc_hash_and_fingerprint() {
    let dir = tempfile::tempdir().unwrap();
    let (server, client) = setup();
    let state = JsonFileStateStore::new(dir.path().join("state.json"));
    let pusher = StatefulPusher::new(&client, &state);
    let uri = "dpe://acme/a";

    assert!(pusher.needs_push(uri, Some("rev-1")).await.unwrap());
    let first = pusher.push(&make_doc_at(uri, &["一"]), Some("rev-1".into())).await.unwrap();
    assert_eq!(first.status, CommitStatus::Created);
    assert!(!pusher.needs_push(uri, Some("rev-1")).await.unwrap());
    assert!(pusher.needs_push(uri, Some("rev-2")).await.unwrap());
    assert!(pusher.needs_push(uri, None).await.unwrap());

    let second = pusher.push(&make_doc_at(uri, &["一", "二"]), Some("rev-2".into())).await.unwrap();
    assert_eq!(second.status, CommitStatus::Updated);
    assert_eq!(second.contents_sent, 1);
    // 第二次协商自动带上第一次的 doc_hash
    let negotiate = server.request_bodies(":negotiate").pop().unwrap();
    assert_eq!(negotiate["base_doc_hash"], first.doc_hash.as_str());

    let record = JsonFileStateStore::new(dir.path().join("state.json")).get(uri).await.unwrap().unwrap();
    assert_eq!(record.doc_hash, server.state().documents[uri].doc_hash);
    assert_eq!(record.source_fingerprint.as_deref(), Some("rev-2"));
}

#[tokio::test]
async fn forget_clears_local_state() {
    let (_server, client) = setup();
    let state = MemoryStateStore::default();
    let pusher = StatefulPusher::new(&client, &state);
    pusher.push(&make_doc_at("dpe://acme/a", &["a"]), Some("rev-1".into())).await.unwrap();
    pusher.forget("dpe://acme/a").await.unwrap();
    assert!(pusher.needs_push("dpe://acme/a", Some("rev-1")).await.unwrap());
}
