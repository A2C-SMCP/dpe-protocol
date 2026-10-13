//! 媒体来源对参考服务端（#25）：与 Python `tests/test_media_http.py`（#15）逐条对等。
//!
//! 来源由 Rust 的 `dpe_sdk::media` 适配产出，交给核心 `deliver` 的 `blobs`；服务端为小限额
//! （blob 按 100 字节分块）。
//!
//! **与 Python 的一处差异**：Python 的路径来源用例 monkeypatch `open` 计数每次读取（哈希一趟
//! ≤ chunk_bytes、上传阶段 ≤ 100）；Rust 无法拦截路径来源内部的文件读取，这里对路径来源只断言
//! 分块区间（每块 ≤ 100、拼回整文件——读取器按核心给的区间读，区间有界即读取有界），读取次数的
//! 计数型断言由 `read_blob` 来源的用例承担（`deliver_uses_a_read_blob_source_without_reading_it_whole`）。

#![cfg(feature = "http")]

mod reference_common;

use std::io;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use dpe_sdk::media::{source_from_path, source_from_read_blob};
use dpe_sdk::protocol::{BlobSource, Precondition};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{ExpandedDocument, Request};
use serde_json::json;

use reference_common::{
    chunk_spans, doc_hash, drive, from_value, is_blob_chunk_from, Injection, ReferenceServer,
    SMALL_LIMITS,
};

const URI: &str = "test://media/doc";

fn media_bytes(size: usize) -> Vec<u8> {
    (0..size).map(|i| (i % 251) as u8).collect()
}

/// 引用一个 blob 的文档：blob 永远不能内联（core §6），必然经暂存会话上传。
fn media_doc(reference: &str) -> ExpandedDocument {
    from_value(json!({
        "file_type": "png",
        "pages": [{"elements": [
            {"category": "Image", "blob": reference, "mime_type": "image/png"}
        ]}],
    }))
}

fn args() -> Vec<&'static str> {
    let mut all = vec!["--prefix", "/r/1"];
    all.extend_from_slice(SMALL_LIMITS);
    all
}

struct TempFile(std::path::PathBuf);

impl TempFile {
    fn with(data: &[u8]) -> TempFile {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "dpe-reference-media-{}-{}.bin",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, data).expect("写临时文件");
        TempFile(path)
    }
}

impl Drop for TempFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn blob_requests(injection: &Injection) -> Vec<Request> {
    injection
        .sent
        .iter()
        .filter(|request| request.target.contains("/blobs/"))
        .cloned()
        .collect()
}

#[tokio::test]
async fn deliver_uploads_a_path_source_in_bounded_chunks() {
    // 大于 blob_chunk_bytes 的文件：分块上传后 commit 成功；每块 ≤ 100、拼起来恰好是整个文件
    let Some(server) = ReferenceServer::spawn(&args()) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let data = media_bytes(1024);
    let file = TempFile::with(&data);
    let source = source_from_path(&file.0, 256).unwrap();
    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(&data));
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());
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

    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Created, 1)
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
    let spans = chunk_spans(&blob_requests(&recorder));
    assert!(!spans.is_empty() && spans.iter().all(|span| span.2 == 1024));
    assert_eq!((spans[0].0, spans.last().unwrap().1), (0, 1023));
    assert!(
        spans.iter().all(|span| span.1 - span.0 < 100),
        "单块 ≤ blob_chunk_bytes"
    );
    let total: u64 = spans.iter().map(|span| span.1 - span.0 + 1).sum();
    assert_eq!(total, 1024, "上传总字节 == 文件大小");
}

fn is_second_chunk(request: &Request) -> bool {
    is_blob_chunk_from(request, 100)
}

#[tokio::test]
async fn lost_chunk_resumes_with_only_the_remaining_bytes() {
    // 中断续传：丢掉第二块（100-199）的响应后，核心先断点查询、再从已收偏移续传——已收字节
    // 不重发、不开新会话，续传的字节数恰好是剩余部分
    let Some(server) = ReferenceServer::spawn(&args()) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let data = media_bytes(1024);
    let file = TempFile::with(&data);
    let source = source_from_path(&file.0, 128).unwrap();
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());
    let mut lossy = Injection::new().lose(is_second_chunk);

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
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
    let blobs = blob_requests(&lossy);
    let methods: Vec<&str> = blobs
        .iter()
        .take(3)
        .map(|request| request.method.as_str())
        .collect();
    assert_eq!(methods, ["PUT", "PUT", "HEAD"]);
    let spans = chunk_spans(&blobs);
    assert_eq!(
        spans[..2],
        [(0, 99, 1024), (100, 199, 1024)],
        "第二块已到达服务端、响应被丢弃"
    );
    let expected: Vec<(u64, u64, u64)> = (200..1024)
        .step_by(100)
        .map(|start| (start, (start + 99).min(1023), 1024))
        .collect();
    assert_eq!(
        spans[2..],
        expected[..],
        "续传从 200 开始：不重发 0-199、也不从头重来"
    );
    let resumed: u64 = spans[2..].iter().map(|span| span.1 - span.0 + 1).sum();
    assert_eq!(resumed, 1024 - 200);
    assert_eq!(lossy.dropped, 1);
}

#[tokio::test]
async fn deliver_uses_a_read_blob_source_without_reading_it_whole() {
    // 计数型来源（read_blob 回调）：哈希一趟与上传阶段的每次请求都 ≤ chunk_bytes，从未整读；
    // 提交成功后读回与文档一致
    let Some(server) = ReferenceServer::spawn(&args()) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let mut data = b"\x89PNG".to_vec();
    data.extend(media_bytes(597)); // 601 字节
    let calls: Arc<Mutex<Vec<(u64, u64)>>> = Arc::default();
    let log = Arc::clone(&calls);
    let served = data.clone();
    let read = move |offset: u64, max_bytes: u64| -> io::Result<(Vec<u8>, bool)> {
        log.lock().unwrap().push((offset, max_bytes));
        let start = (offset as usize).min(served.len());
        let end = (start + max_bytes as usize).min(served.len());
        Ok((served[start..end].to_vec(), end >= served.len()))
    };
    let source = source_from_read_blob(read, Some(601), 64).unwrap();
    let hash_pass = calls.lock().unwrap().len();
    assert_eq!(hash_pass, 601usize.div_ceil(64), "一趟覆盖全部字节");
    assert!(calls
        .lock()
        .unwrap()
        .iter()
        .all(|&(_, max_bytes)| max_bytes == 64));
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());

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
        &mut Injection::new(),
    )
    .await
    .unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&document));
    let calls = calls.lock().unwrap();
    assert!(
        calls
            .iter()
            .all(|&(_, max_bytes)| (1..=64).contains(&max_bytes)),
        "全程从未整读"
    );
    assert!(calls.len() > hash_pass, "上传阶段继续按偏移请求同一份快照");
}

#[tokio::test]
async fn unchanged_documents_never_touch_the_source() {
    // 未变文档（unchanged）不触碰来源：blobs 回调不发生、也就不重新哈希
    let Some(server) = ReferenceServer::spawn(&args()) else {
        return;
    };
    let client = server.client();
    let data = media_bytes(512);
    let file = TempFile::with(&data);
    let reference = source_from_path(&file.0, 128)
        .unwrap()
        .blob_ref()
        .to_owned();
    let document = media_doc(&reference);
    let mut built = Vec::new();
    let mut provider = |requested: &str| -> Option<BlobSource> {
        built.push(requested.to_owned());
        source_from_path(&file.0, 128).ok() // 真的被要求时才哈希
    };

    let first = client
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .await
        .unwrap();
    assert_eq!(first.status, CommitStatus::Created);
    let second = client
        .deliver(
            URI,
            &document,
            Precondition::BaseHash(first.doc_hash.clone()),
            Some(&mut provider),
            None,
        )
        .await
        .unwrap();

    assert_eq!(second.status, CommitStatus::Unchanged);
    assert_eq!(built, [reference], "未变：来源没有被再次要求");
}
