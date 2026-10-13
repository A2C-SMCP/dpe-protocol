//! 传输适配对参考服务端（#25）：与 Python `tests/test_transport_server.py`（#13）逐条对等。
//!
//! 服务端参数同 Python 的 `SERVER_ARGS`：多身份按 URI 前缀授权、blob 分块上限 16 KiB。Python 的
//! `test_sync_and_async_agree_against_the_same_server` 是同步 / 异步两套接口的自我对照；Rust
//! 只有异步客户端，这里对同一场景逐步断言具体值（两份 Python 结果应相等的那一组）。

#![cfg(feature = "http")]

mod reference_common;

use dpe_sdk::protocol::{BlobSource, Precondition};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::ErrorKind;
use serde_json::json;

use reference_common::{doc, doc_hash, from_value, ReferenceServer};

const SERVER_ARGS: &[&str] = &[
    "--prefix",
    "/r/1",
    "--max-payload-bytes",
    "65536",
    "--blob-chunk-bytes",
    "16384",
    "--grant",
    "Bearer limited",
    "test://scope/",
    "--grant",
    "Bearer full",
    "*",
];

const FULL: &str = "Bearer full";
const H1: &str = "dpe1:1111111111111111111111111111111111111111111111111111111111111111";

#[tokio::test]
async fn client_full_flow_over_tcp() {
    let Some(server) = ReferenceServer::spawn(SERVER_ARGS) else {
        return;
    };
    let client = server.client_as(FULL);
    let uri = "test://sync-flow/round.md";
    let document = doc(&["hello", "world"]);

    let created = client
        .commit(uri, &document, Precondition::IfAbsent, None)
        .await
        .unwrap();

    assert_eq!(
        (created.status, created.doc_hash.clone()),
        (CommitStatus::Created, doc_hash(&document))
    );
    assert_eq!(client.head(uri).await.unwrap(), Some(doc_hash(&document)));
    // 线上页对象的 elements 是 content_hash 列表（展开视图才内联元素）
    let skeleton = client.get_skeleton(uri).await.unwrap().unwrap();
    let counts: Vec<usize> = skeleton
        .pages
        .iter()
        .map(|page| page.elements.len())
        .collect();
    assert_eq!(counts, [2]);
    let error = client
        .commit(
            uri,
            &doc(&["other"]),
            Precondition::BaseHash(H1.to_owned()),
            None,
        )
        .await
        .expect_err("前置条件不符");
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    let page = client
        .list_page("test://sync-flow/", None, None)
        .await
        .unwrap();
    let uris: Vec<&str> = page
        .documents
        .iter()
        .map(|entry| entry.file_uri.as_str())
        .collect();
    assert_eq!(uris, [uri]);
    client.delete(uri, &created.doc_hash).await.unwrap();
    assert_eq!(client.head(uri).await.unwrap(), None);
}

#[tokio::test]
async fn client_uploads_a_large_blob_in_chunks() {
    // blob 超过 max_payload_bytes：经分块与断点续传协议（HTTP 绑定 §4.7）跑通
    let Some(server) = ReferenceServer::spawn(SERVER_ARGS) else {
        return;
    };
    let client = server.client_as(FULL);
    let data: Vec<u8> = (0..=255u8).cycle().take(256 * 400).collect(); // 102400 字节
    let reference = dpe_hash::blob_ref(&data);
    let document = from_value(json!({
        "file_type": "png",
        "pages": [{"elements": [
            {"category": "Image", "blob": reference, "mime_type": "image/png"}
        ]}],
    }));
    let uri = "test://sync-flow/image.png";
    let mut provider = |_: &str| Some(BlobSource::from_bytes(data.clone()));

    let result = client
        .deliver(
            uri,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .await
        .unwrap();

    assert_eq!(
        (result.status, result.doc_hash),
        (CommitStatus::Created, doc_hash(&document))
    );
    assert_eq!(client.head(uri).await.unwrap(), Some(doc_hash(&document)));
}

#[tokio::test]
async fn credential_scoping_over_tcp() {
    // 凭据可插拔：同一 remote 上，不同 CredentialProvider 得到不同的授权结果（#45 多身份）
    let Some(server) = ReferenceServer::spawn(SERVER_ARGS) else {
        return;
    };
    let x = doc(&["x"]);
    let limited = server.client_as("Bearer limited");
    let ok = limited
        .commit("test://scope/ok.md", &x, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(ok.status, CommitStatus::Created);
    let denied = limited
        .commit("test://other/denied.md", &x, Precondition::IfAbsent, None)
        .await
        .expect_err("前缀外");
    assert_eq!(denied.kind(), ErrorKind::Forbidden);
    let allowed = server
        .client_as(FULL)
        .commit("test://other/allowed.md", &x, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(allowed.status, CommitStatus::Created);
    // 无凭证：不在授权表内
    let anonymous = server
        .client()
        .commit(
            "test://other/anonymous.md",
            &x,
            Precondition::IfAbsent,
            None,
        )
        .await
        .expect_err("无凭证");
    assert_eq!(anonymous.kind(), ErrorKind::Forbidden);
}

#[tokio::test]
async fn scenario_agrees_with_the_python_parity_run() {
    // Python `_scenario` 的同一序列：capabilities → commit → head → batch_head → skeleton →
    // 更新 → list → 冲突 → move → head ×2 → delete
    let Some(server) = ReferenceServer::spawn(SERVER_ARGS) else {
        return;
    };
    let client = server.client_as(FULL);
    let base = "test://parity/rust";
    let (a, b, gone) = (
        format!("{base}/a.md"),
        format!("{base}/b.md"),
        format!("{base}/gone.md"),
    );
    let (first, second) = (doc(&["alpha", "beta"]), doc(&["alpha"]));

    let capabilities = client.capabilities().await.unwrap();
    assert_eq!(
        (
            capabilities.limits.max_payload_bytes,
            capabilities.limits.blob_chunk_bytes
        ),
        (65536, 16384)
    );
    let created = client
        .commit(&a, &first, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(
        (
            created.status,
            created.doc_hash.clone(),
            created.delta.added,
            created.delta.removed,
            created.delta.retained
        ),
        (CommitStatus::Created, doc_hash(&first), 2, 0, 0)
    );
    assert_eq!(
        client.head(&a).await.unwrap(),
        Some(created.doc_hash.clone())
    );
    assert_eq!(
        client.batch_head(&[a.clone(), gone]).await.unwrap(),
        [Some(created.doc_hash.clone()), None]
    );
    // 骨架逐字段：文档对象（file_type 与页 hash 列表）、每页的元素 hash 列表
    let skeleton = client.get_skeleton(&a).await.unwrap().unwrap();
    let hashes = first.hashes(dpe_sdk::CONTRACT).unwrap();
    assert_eq!(skeleton.doc_hash, created.doc_hash);
    assert_eq!(skeleton.file_uri, a);
    assert_eq!(skeleton.document.file_type, "md");
    let page_hashes: Vec<String> = hashes
        .pages
        .iter()
        .map(|page| page.page_hash.clone())
        .collect();
    assert_eq!(skeleton.document.pages, page_hashes);
    let elements: Vec<Vec<String>> = skeleton
        .pages
        .iter()
        .map(|page| page.elements.clone())
        .collect();
    let expected: Vec<Vec<String>> = hashes
        .pages
        .iter()
        .map(|page| page.elements.clone())
        .collect();
    assert_eq!(elements, expected);
    let updated = client
        .commit(
            &a,
            &second,
            Precondition::BaseHash(created.doc_hash.clone()),
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        (
            updated.status,
            updated.doc_hash.clone(),
            updated.delta.added,
            updated.delta.removed,
            updated.delta.retained
        ),
        (CommitStatus::Updated, doc_hash(&second), 0, 1, 1)
    );
    let page = client
        .list_page(&format!("{base}/"), None, None)
        .await
        .unwrap();
    let listed: Vec<(String, String)> = page
        .documents
        .iter()
        .map(|entry| {
            (
                entry.file_uri.trim_start_matches(base).to_owned(),
                entry.doc_hash.clone(),
            )
        })
        .collect();
    assert_eq!(listed, [("/a.md".to_owned(), updated.doc_hash.clone())]);
    let conflict = client
        .commit(
            &a,
            &doc(&["conflict"]),
            Precondition::BaseHash(created.doc_hash.clone()),
            None,
        )
        .await
        .expect_err("冲突");
    assert_eq!(
        (conflict.kind(), conflict.code()),
        (
            ErrorKind::PreconditionFailed,
            Some("DPE_PRECONDITION_FAILED")
        )
    );
    let moved = client
        .move_document(&a, &b, &updated.doc_hash)
        .await
        .unwrap();
    assert_eq!(moved.doc_hash, updated.doc_hash);
    assert_eq!(client.head(&a).await.unwrap(), None);
    assert_eq!(
        client.head(&b).await.unwrap(),
        Some(updated.doc_hash.clone())
    );
    client.delete(&b, &updated.doc_hash).await.unwrap();
    assert_eq!(client.head(&b).await.unwrap(), None);
}
