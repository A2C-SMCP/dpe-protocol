//! 媒体来源经核心投递（#24）：分块上传有界、续传只补剩余字节、未变文档不碰来源。
//!
//! 与 Python 侧 `tests/test_media_http.py` 的场景逐条对等。来源由 `dpe_sdk::media` 产出、交给
//! `ProtocolCore::deliver` 的 `blobs` 参数；分块、断点续传都走核心的既有路径，应答按脚本给出
//! （与 `protocol_staging.rs` 的分块 / 续传用例同一测法）。驱动时先解析请求体再记录请求——与
//! 传输适配层的顺序一致，来源的读取因此真实发生、可计数。对参考服务端的跨语言验收归 #25。

mod protocol_common;

use std::io;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use dpe_sdk::media::{source_from_path, source_from_read_blob};
use dpe_sdk::protocol::{BlobSource, Precondition, ProtocolCore};
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{Error, ExpandedDocument, Request};
use serde_json::{json, Value};

use protocol_common::{
    caps_with_limits, committed, core_with_caps, drive_ref, negotiated, problem, resume, uploaded,
    Script,
};

const URI: &str = "test://media/doc";

fn media_bytes(size: usize) -> Vec<u8> {
    (0..size).map(|i| (i % 251) as u8).collect()
}

/// 引用一个 blob 的文档：blob 永远不能内联（core §6），必然经暂存会话上传。
fn media_doc(reference: &str) -> ExpandedDocument {
    ExpandedDocument::from_value(
        &json!({
            "file_type": "png",
            "pages": [{"elements": [
                {"category": "Image", "blob": reference, "mime_type": "image/png"}
            ]}],
        }),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 单块 100 字节的核心：blob 超过 `max_payload_bytes`（500）才分块、块长取两者较小者
/// （HTTP 绑定 §4.7）；单个 Image 元素的文档内联体放得下，快路径先报缺 blob，再开会话上传。
fn chunked() -> ProtocolCore {
    core_with_caps(
        caps_with_limits(&[("max_payload_bytes", 500), ("blob_chunk_bytes", 100)]),
        2,
    )
}

fn missing_blob(reference: &str) -> dpe_sdk::Response {
    problem(
        "DPE_MISSING_CONTENT",
        400,
        json!({"missing": {"pages": [], "content_hashes": [], "blobs": [reference]}}),
    )
}

/// 分块应答：中间块 202 + 已收偏移，最后一块 201（`chunk_ranges` 之后的 commit 另给）。
fn chunk_replies(script: Script, from: u64, total: u64) -> Script {
    let mut script = script;
    let mut offset = from;
    while offset + 100 < total {
        offset += 100;
        script = script.response(uploaded(202, Some(offset), Value::Null));
    }
    script.response(uploaded(201, None, json!({})))
}

/// 驱动并在记录前解析请求体（与传输适配层的顺序一致：来源的读取真实发生）。
fn drive<O: dpe_sdk::protocol::Operation>(
    op: &mut O,
    script: &mut Script,
) -> Result<O::Output, Error> {
    drive_ref(op, |request| {
        request.body_bytes().expect("来源可读");
        script.send(request)
    })
}

/// 请求的 `Content-Range` 解析为 (起点, 终点, 总量)。
fn spans(requests: &[Request]) -> Vec<(u64, u64, u64)> {
    requests
        .iter()
        .filter_map(|request| request.headers.get("Content-Range"))
        .map(|value| {
            let (span, total) = value
                .strip_prefix("bytes ")
                .and_then(|rest| rest.split_once('/'))
                .expect("Content-Range 形如 bytes a-b/n");
            let (first, last) = span.split_once('-').expect("区间形如 a-b");
            (
                first.parse().unwrap(),
                last.parse().unwrap(),
                total.parse().unwrap(),
            )
        })
        .collect()
}

fn blob_requests(requests: &[Request]) -> Vec<Request> {
    requests
        .iter()
        .filter(|request| request.target.contains("/blobs/"))
        .cloned()
        .collect()
}

/// 计数型 `read_blob`：按 §6.7 形状服务一段字节，记录每次 `(offset, max_bytes, 返回字节数)`。
type Calls = Arc<Mutex<Vec<(u64, u64, usize)>>>;

fn counting_read_blob(
    data: Vec<u8>,
    calls: &Calls,
) -> impl Fn(u64, u64) -> io::Result<(Vec<u8>, bool)> + Send + Sync + 'static {
    let log = Arc::clone(calls);
    move |offset: u64, max_bytes: u64| {
        let start = (offset as usize).min(data.len());
        let end = (start + max_bytes as usize).min(data.len());
        log.lock().unwrap().push((offset, max_bytes, end - start));
        Ok((data[start..end].to_vec(), end >= data.len()))
    }
}

struct TempFile(std::path::PathBuf);

impl TempFile {
    fn with(data: &[u8]) -> TempFile {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "dpe-sdk-media-staging-{}-{}.bin",
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

#[test]
fn deliver_uploads_a_path_source_in_bounded_chunks() {
    // 大于 blob_chunk_bytes 的文件：分块上传后 commit 成功；每块 ≤ 100、拼起来恰好是整个文件
    let data = media_bytes(1024);
    let file = TempFile::with(&data);
    let source = source_from_path(&file.0, 256).unwrap();
    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(&data));
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());
    let script = Script::new()
        .response(missing_blob(source.blob_ref()))
        .response(negotiated("st-1", &[], &[]));
    let mut script =
        chunk_replies(script, 0, 1024).response(committed(&document, "created", 1, 0, 0));
    let core = chunked();
    let mut op = core
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();

    let result = drive(&mut op, &mut script).unwrap();

    assert_eq!(
        (result.status, result.delta.added),
        (CommitStatus::Created, 1)
    );
    let blobs = blob_requests(&script.requests);
    let spans = spans(&blobs);
    assert!(spans.iter().all(|&(_, _, total)| total == 1024));
    assert_eq!((spans[0].0, spans.last().unwrap().1), (0, 1023));
    assert!(
        spans.iter().all(|&(first, last, _)| last - first < 100),
        "单块 ≤ blob_chunk_bytes"
    );
    let uploaded: Vec<u8> = blobs
        .iter()
        .flat_map(|request| request.body_bytes().unwrap().unwrap())
        .collect();
    assert_eq!(uploaded, data, "上传总字节 == 文件内容");
}

#[test]
fn lost_chunk_resumes_with_only_the_remaining_bytes() {
    // 第二块（100-199）的响应丢失、服务端其实已收：断点查询得到 200，从 200 续传——已收字节
    // 不重发、不开新会话；来源被读取的总字节数 == 实际上传的字节数（不从头重读）
    let data = media_bytes(1024);
    let calls = Calls::default();
    let read = counting_read_blob(data.clone(), &calls);
    let source = source_from_read_blob(read, Some(1024), 64).unwrap();
    let hash_pass = calls.lock().unwrap().len();
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());
    let script = Script::new()
        .response(missing_blob(source.blob_ref()))
        .response(negotiated("st-1", &[], &[]))
        .response(uploaded(202, Some(100), Value::Null))
        .lost("块 2 的响应丢失")
        .response(resume(200));
    let mut script =
        chunk_replies(script, 200, 1024).response(committed(&document, "created", 1, 0, 0));
    let core = chunked();
    let mut op = core
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();

    let result = drive(&mut op, &mut script).unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    let blobs = blob_requests(&script.requests);
    let methods: Vec<&str> = blobs
        .iter()
        .map(|request| request.method.as_str())
        .collect();
    assert_eq!(methods[..3], ["PUT", "PUT", "HEAD"]);
    let spans = spans(&blobs);
    assert_eq!(spans[..2], [(0, 99, 1024), (100, 199, 1024)]);
    let expected: Vec<(u64, u64, u64)> = (200..1024)
        .step_by(100)
        .map(|start| (start, (start + 99).min(1023), 1024))
        .collect();
    assert_eq!(spans[2..], expected[..], "续传从已收偏移 200 开始");
    let resumed: u64 = spans[2..]
        .iter()
        .map(|&(first, last, _)| last - first + 1)
        .sum();
    assert_eq!(resumed, 1024 - 200, "续传字节数 == 总量 − 已收");
    // 来源侧：前两块（0-199）各读一次；断点查询之后的读取全部从已收偏移 200 起，不回头重读
    let upload = calls.lock().unwrap()[hash_pass..].to_vec();
    let mut covered = 0usize;
    let split = upload
        .iter()
        .position(|&(_, _, returned)| {
            covered += returned;
            covered >= 200
        })
        .expect("前两块的读取")
        + 1;
    assert!(upload[..split].iter().all(|&(offset, _, _)| offset < 200));
    assert!(
        upload[split..].iter().all(|&(offset, _, _)| offset >= 200),
        "续传后不回头读已收字节：{:?}",
        &upload[split..]
    );
}

#[test]
fn deliver_uses_a_read_blob_source_without_reading_it_whole() {
    // 计数型来源：哈希一趟与上传阶段的每次请求都 ≤ chunk_bytes，从未整读
    let mut data = b"\x89PNG".to_vec();
    data.extend(media_bytes(597)); // 601 字节
    let calls = Calls::default();
    let read = counting_read_blob(data.clone(), &calls);
    let source = source_from_read_blob(read, Some(601), 64).unwrap();
    let hash_pass = calls.lock().unwrap().len();
    assert_eq!(hash_pass, 601usize.div_ceil(64), "一趟覆盖全部字节");
    let document = media_doc(source.blob_ref());
    let mut provider = |_: &str| Some(source.clone());
    let script = Script::new()
        .response(missing_blob(source.blob_ref()))
        .response(negotiated("st-1", &[], &[]));
    let mut script =
        chunk_replies(script, 0, 601).response(committed(&document, "created", 1, 0, 0));
    let core = chunked();
    let mut op = core
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();

    let result = drive(&mut op, &mut script).unwrap();

    assert_eq!(result.status, CommitStatus::Created);
    let calls = calls.lock().unwrap();
    assert!(
        calls
            .iter()
            .all(|&(_, max_bytes, _)| (1..=64).contains(&max_bytes)),
        "全程从未整读"
    );
    assert!(calls.len() > hash_pass, "上传阶段继续按偏移请求同一份快照");
}

#[test]
fn unchanged_documents_never_touch_the_source() {
    // 未变文档（unchanged）不触碰来源：blobs 回调不发生、也就不重新哈希
    let data = media_bytes(512);
    let file = TempFile::with(&data);
    let reference = source_from_path(&file.0, 128)
        .unwrap()
        .blob_ref()
        .to_owned();
    let document = media_doc(&reference);
    let built = AtomicUsize::new(0);
    let mut provider = |_: &str| -> Option<BlobSource> {
        built.fetch_add(1, Ordering::SeqCst);
        source_from_path(&file.0, 128).ok() // 真的被要求时才哈希
    };
    let base = document.doc_hash(dpe_sdk::CONTRACT).unwrap();
    let mut script = Script::new().response(committed(&document, "unchanged", 0, 0, 1));
    let core = chunked();
    let mut op = core
        .deliver(
            URI,
            &document,
            Precondition::BaseHash(base),
            Some(&mut provider),
            None,
        )
        .unwrap();

    let result = drive(&mut op, &mut script).unwrap();

    assert_eq!(result.status, CommitStatus::Unchanged);
    assert_eq!(built.load(Ordering::SeqCst), 0, "未变：来源没有被要求");
    assert!(blob_requests(&script.requests).is_empty());
}
