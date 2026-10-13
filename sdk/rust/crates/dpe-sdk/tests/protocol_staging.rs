//! sans-IO 协议核心的暂存路径（#49）：协商、上传、分块断点续传与会话（core §3.2–§3.4）。
//!
//! 与 Python `tests/test_protocol_staging.py`（#49 / PR #87，含 #50 的 400 偏移重同步）逐条
//! 对等；「对参考服务端」一组（`test_protocol_staging_http.py`）除外——Rust SDK 尚无内存版
//! 参考服务端，随 `testing` 模块落地。

mod protocol_common;

use dpe_sdk::protocol::{
    BlobReader, BlobSource, Method, Operation, Precondition, Step, TransportError, CONTRACT_HEADER,
    UPLOAD_OFFSET_HEADER,
};
use dpe_sdk::{Error, ErrorKind, PageObject};
use serde_json::{json, Value};

use protocol_common::{
    bare_412, chunk_ranges, chunked_core, core, doc, doc_hash, empty, gateway, h1, h2, h_drill,
    head_found, head_missing, image_bytes, image_doc, image_ref, json_response_with, negotiated,
    negotiated_at, offset_problem, problem, resume, run, session, session_with_id, staged_core,
    uploaded, Script, SESSION_EXPIRES,
};

const URI: &str = "s3://bucket/a.md";

/// 期望操作失败并返回错误。
fn expect_error<T: std::fmt::Debug>(result: Result<T, Error>) -> Error {
    match result {
        Ok(value) => panic!("期望失败，实际 {value:?}"),
        Err(error) => error,
    }
}

/// 请求体解析为 JSON 值。
fn body_json(request: &dpe_sdk::Request) -> Value {
    match request.body_bytes().expect("请求体可取出") {
        Some(bytes) => serde_json::from_slice(&bytes).expect("请求体是 JSON"),
        None => Value::Null,
    }
}

fn methods(requests: &[dpe_sdk::Request]) -> Vec<&'static str> {
    requests
        .iter()
        .map(|request| request.method.as_str())
        .collect()
}

/// 按任意偏移给出固定字节的读取器（短读用例）。
struct FixedReader(Vec<u8>);

impl BlobReader for FixedReader {
    fn read(&self, _offset: u64, _length: u64) -> std::io::Result<Vec<u8>> {
        Ok(self.0.clone())
    }
}

/// 页对象的紧凑 JSON 载荷（与核心序列化一致）。
fn page_payload(page: &PageObject) -> Vec<u8> {
    serde_json::to_vec(page).expect("页对象总能序列化")
}

// ---------------------------------------------------------------------- 构造与本地校验

#[test]
fn blob_source_from_bytes_and_seekable() {
    let source = BlobSource::from_bytes(b"hello".to_vec());
    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(b"hello"));
    assert_eq!(source.size(), 5);
    assert_eq!(source.read(1, 3).unwrap(), b"ell");
    assert_eq!(source.read(4, 10).unwrap(), b"o"); // 越过后缀的读取由调用方按 size 约束

    let blob_ref = format!("sha256:{}", "0".repeat(64));
    let stream = BlobSource::from_seekable(
        std::io::Cursor::new(b"abcdef".to_vec()),
        blob_ref.clone(),
        6,
    );
    assert_eq!((stream.blob_ref(), stream.size()), (blob_ref.as_str(), 6));
    assert_eq!(stream.read(2, 3).unwrap(), b"cde");
    assert_eq!(stream.read(5, 1).unwrap(), b"f");
}

/// 第一次 `read` 返回 `Interrupted`、之后正常给出数据的桩流（标准 `Read` 约定下应被重试）。
struct InterruptedOnce {
    inner: std::io::Cursor<Vec<u8>>,
    interrupted: bool,
}

impl std::io::Read for InterruptedOnce {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if !self.interrupted {
            self.interrupted = true;
            return Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "被信号打断",
            ));
        }
        self.inner.read(buf)
    }
}

impl std::io::Seek for InterruptedOnce {
    fn seek(&mut self, position: std::io::SeekFrom) -> std::io::Result<u64> {
        self.inner.seek(position)
    }
}

/// `Interrupted` 按标准 `Read` 约定重试，不把整次读取判失败。
#[test]
fn seekable_source_retries_on_interrupted_reads() {
    let blob_ref = format!("sha256:{}", "0".repeat(64));
    let source = BlobSource::from_seekable(
        InterruptedOnce {
            inner: std::io::Cursor::new(b"abcdef".to_vec()),
            interrupted: false,
        },
        blob_ref.clone(),
        6,
    );
    assert_eq!(source.read(0, 6).unwrap(), b"abcdef");
    assert_eq!(source.read(2, 3).unwrap(), b"cde");
}

#[test]
fn request_body_bytes_resolves_blob_ranges() {
    let request = dpe_sdk::Request {
        method: Method::Put,
        target: "x".to_owned(),
        headers: dpe_sdk::protocol::Headers::new(),
        body: dpe_sdk::protocol::Body::BlobRange(dpe_sdk::protocol::BlobRange {
            source: BlobSource::from_bytes(b"abcdef".to_vec()),
            offset: 2,
            length: 3,
        }),
        retry_safe: false,
    };
    assert_eq!(request.body_bytes().unwrap(), Some(b"cde".to_vec()));

    let raw = dpe_sdk::Request {
        method: Method::Put,
        target: "x".to_owned(),
        headers: dpe_sdk::protocol::Headers::new(),
        body: dpe_sdk::protocol::Body::Bytes(b"raw".to_vec()),
        retry_safe: false,
    };
    assert_eq!(raw.body_bytes().unwrap(), Some(b"raw".to_vec()));

    let empty = dpe_sdk::Request {
        method: Method::Get,
        target: "x".to_owned(),
        headers: dpe_sdk::protocol::Headers::new(),
        body: dpe_sdk::protocol::Body::None,
        retry_safe: true,
    };
    assert_eq!(empty.body_bytes().unwrap(), None);

    // 源给的字节数与声明的区间不符：报错（调用方的源有问题）
    let short = dpe_sdk::Request {
        method: Method::Put,
        target: "x".to_owned(),
        headers: dpe_sdk::protocol::Headers::new(),
        body: dpe_sdk::protocol::Body::BlobRange(dpe_sdk::protocol::BlobRange {
            source: BlobSource::new(
                format!("sha256:{}", "0".repeat(64)),
                5,
                std::sync::Arc::new(FixedReader(b"ab".to_vec())),
            ),
            offset: 0,
            length: 5,
        }),
        retry_safe: false,
    };
    let error = short.body_bytes().expect_err("长度不符必须报错");
    assert!(error.to_string().contains("字节"), "{error}");
}

#[test]
fn upload_blob_validates_the_source_locally() {
    let mut sess = session();
    // 超过 blob_max_bytes
    let c = core_with_blob_max(4);
    let error = expect_error(c.upload_blob(&mut sess, BlobSource::from_bytes(vec![0u8; 5]), None));
    assert_eq!(error.kind(), ErrorKind::PayloadTooLarge);
    assert!(error.message().contains("blob_max_bytes"), "{error}");

    // ref 不是 sha256 引用
    let mut sess = session();
    let bad = BlobSource::new(
        format!("dpe1:{}", "0".repeat(64)),
        1,
        std::sync::Arc::new(FixedReader(b"x".to_vec())),
    );
    let error = expect_error(core("dpe1").upload_blob(&mut sess, bad, None));
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    assert!(error.message().contains("sha256"), "{error}");
}

/// 只改 `blob_max_bytes` 的核心。
fn core_with_blob_max(max: u64) -> dpe_sdk::protocol::ProtocolCore {
    protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("blob_max_bytes", max)]),
        2,
    )
}

#[test]
fn upload_page_and_element_reject_oversized_objects_locally() {
    let mut sess = session();
    let page = PageObject::from_value(
        &json!({"title": "x".repeat(300), "elements": []}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("page_max_bytes", 128)]),
        2,
    );
    let error = expect_error(c.upload_page(&mut sess, &page));
    assert_eq!(error.kind(), ErrorKind::PayloadTooLarge);
    assert!(error.message().contains("page_max_bytes"), "{error}");

    let mut sess = session();
    let element = dpe_sdk::ElementObject::from_value(
        &json!({"category": "NarrativeText", "text": "x".repeat(300)}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 128)]),
        2,
    );
    let error = expect_error(c.upload_element(&mut sess, &element));
    assert_eq!(error.kind(), ErrorKind::PayloadTooLarge);
    assert!(error.message().contains("max_payload_bytes"), "{error}");
}

// ---------------------------------------------------------------------- negotiate

#[test]
fn negotiate_request_shape_and_result() {
    let document = doc(&[&["x"]]);
    let (page_hashes, _) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let (result, server) = run(
        core("dpe1").negotiate(URI, &document).unwrap(),
        [negotiated("st-1", &[page_hash.as_str()], &[])],
    );
    let result = result.unwrap();
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("POST", "negotiate")
    );
    assert_eq!(request.headers.get(CONTRACT_HEADER), Some("dpe1"));
    assert_eq!(
        request.headers.get("Content-Type"),
        Some("application/json")
    );
    let body = body_json(request);
    assert_eq!(body["file_uri"], URI);
    assert!(body["document"].is_object());
    assert!(!request.retry_safe, "negotiate 是写路径的第一步");
    assert_eq!(result.missing_pages, vec![page_hash]);
    assert!(result.missing_content_hashes.is_empty());
    assert_eq!(
        (result.session.id.as_str(), result.session.uri.as_str()),
        ("st-1", URI)
    );
    assert_eq!(result.session.expires_at, SESSION_EXPIRES);
}

#[test]
fn negotiate_normalizes_the_session_uri() {
    let (result, _) = run(
        core("dpe1")
            .negotiate("S3://bucket/a.md", &doc(&[&["x"]]))
            .unwrap(),
        [negotiated("st-1", &[], &[])],
    );
    assert_eq!(result.unwrap().session.uri, URI);
}

#[test]
fn negotiate_rejects_response_hashes_outside_the_contract() {
    let document = doc(&[&["x"]]);
    let (result, _) = run(
        core("dpe1").negotiate(URI, &document).unwrap(),
        [negotiated("st-1", &[h_drill().as_str()], &[])],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);

    let (result, _) = run(
        core("dpe1").negotiate(URI, &document).unwrap(),
        [negotiated("st-1", &[], &[h_drill().as_str()])],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn negotiate_rejects_a_non_rfc3339_expiry() {
    let document = doc(&[&["x"]]);
    let (result, _) = run(
        core("dpe1").negotiate(URI, &document).unwrap(),
        [negotiated_at("st-1", &[], &[], "whenever")],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("RFC 3339"), "{error}");
}

#[test]
fn negotiate_validates_the_uri_locally() {
    let document = doc(&[&["x"]]);
    assert_eq!(
        expect_error(core("dpe1").negotiate("no scheme", &document)).kind(),
        ErrorKind::Validation
    );
    assert_eq!(
        expect_error(core("dpe1").negotiate("feishu://doc/ 空格", &document)).kind(),
        ErrorKind::Validation
    );
}

// ---------------------------------------------------------------------- 上传原语

#[test]
fn upload_page_whole_request_and_missing_elements() {
    let mut sess = session();
    let (one, two) = (h1(), h2());
    let page = PageObject::from_value(
        &json!({"title": "t", "elements": [&one, &two]}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let page_hash = page.page_hash(dpe_sdk::CONTRACT).unwrap();
    let (missing, server) = run(
        core("dpe1").upload_page(&mut sess, &page).unwrap(),
        [uploaded(
            201,
            None,
            json!({"missing_content_hashes": [one]}),
        )],
    );
    let request = &server.requests[0];
    assert_eq!(request.method.as_str(), "PUT");
    assert_eq!(request.target, format!("staging/st-1/pages/{page_hash}"));
    assert_eq!(
        request.headers.get("Content-Type"),
        Some("application/json")
    );
    assert_eq!(request.headers.get("Content-Range"), None);
    assert!(!request.retry_safe);
    assert_eq!(
        body_json(request),
        json!({"title": "t", "elements": [&one, &two]})
    );
    assert_eq!(missing.unwrap(), vec![one]);
}

#[test]
fn upload_page_over_max_payload_is_chunked() {
    let page = PageObject::from_value(
        &json!({"title": "y".repeat(500), "elements": []}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let payload = page_payload(&page);
    let c = chunked_core(2);
    let ranges = chunk_ranges(payload.len() as u64, 100);
    let mut script = Script::new();
    for (start, length) in &ranges[..ranges.len() - 1] {
        script = script.response(uploaded(202, Some(start + length), Value::Null));
    }
    script = script.response(uploaded(201, None, json!({"missing_content_hashes": []})));
    let mut sess = session();
    let mut op = c.upload_page(&mut sess, &page).unwrap();
    let missing = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert!(missing.is_empty());
    let sent: Vec<(Option<String>, Vec<u8>)> = script
        .requests
        .iter()
        .map(|request| {
            (
                request.headers.get("Content-Range").map(str::to_owned),
                request.body_bytes().unwrap().unwrap(),
            )
        })
        .collect();
    let expected: Vec<(Option<String>, Vec<u8>)> = ranges
        .iter()
        .map(|(start, length)| {
            (
                Some(format!(
                    "bytes {}-{}/{}",
                    start,
                    start + length - 1,
                    payload.len()
                )),
                payload[*start as usize..(start + length) as usize].to_vec(),
            )
        })
        .collect();
    assert_eq!(sent, expected);
}

#[test]
fn upload_page_duplicate_and_created_are_both_success() {
    let (one, two) = (h1(), h2());
    let page = PageObject::from_value(&json!({"elements": [one]}), dpe_sdk::CONTRACT).unwrap();
    let mut sess = session();
    let (missing, _) = run(
        core("dpe1").upload_page(&mut sess, &page).unwrap(),
        [uploaded(
            201,
            None,
            json!({"missing_content_hashes": [two]}),
        )],
    );
    assert_eq!(missing.unwrap(), vec![two]);

    let mut sess = session();
    let (missing, _) = run(
        core("dpe1").upload_page(&mut sess, &page).unwrap(),
        [uploaded(200, None, json!({"missing_content_hashes": []}))],
    );
    assert!(missing.unwrap().is_empty());
}

#[test]
fn upload_element_request_and_missing_blobs() {
    let mut sess = session();
    let reference = image_ref();
    let element = dpe_sdk::ElementObject::from_value(
        &json!({"category": "Image", "blob": &reference, "mime_type": "image/png"}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let content_hash = element.content_hash(dpe_sdk::CONTRACT).unwrap();
    let (missing, server) = run(
        core("dpe1").upload_element(&mut sess, &element).unwrap(),
        [uploaded(201, None, json!({"missing_blobs": [&reference]}))],
    );
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        (
            "PUT",
            format!("staging/st-1/objects/{content_hash}").as_str()
        )
    );
    assert_eq!(missing.unwrap(), vec![reference]);
}

#[test]
fn upload_element_rejects_a_malformed_blob_ref_in_the_response() {
    let mut sess = session();
    let element = dpe_sdk::ElementObject::from_value(
        &json!({"category": "NarrativeText", "text": "x"}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let (result, _) = run(
        core("dpe1").upload_element(&mut sess, &element).unwrap(),
        [uploaded(201, None, json!({"missing_blobs": ["dpe1:x"]}))],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn upload_session_expired_is_reported() {
    let mut sess = session();
    let page = PageObject::from_value(&json!({"elements": []}), dpe_sdk::CONTRACT).unwrap();
    let (result, _) = run(
        core("dpe1").upload_page(&mut sess, &page).unwrap(),
        [problem("DPE_SESSION_EXPIRED", 410, Value::Null)],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::SessionExpired);
}

#[test]
fn upload_blob_whole_request_uses_the_media_type() {
    let mut sess = session();
    let reference = image_ref();
    let (_, server) = run(
        core("dpe1")
            .upload_blob(
                &mut sess,
                BlobSource::from_bytes(image_bytes()),
                Some("image/png"),
            )
            .unwrap(),
        [uploaded(201, None, json!({}))],
    );
    let request = &server.requests[0];
    assert_eq!(request.target, format!("staging/st-1/blobs/{reference}"));
    assert_eq!(request.headers.get("Content-Type"), Some("image/png"));
    assert_eq!(request.headers.get("Content-Range"), None);
    assert_eq!(request.body_bytes().unwrap(), Some(image_bytes()));

    let mut sess = session();
    let (_, server) = run(
        core("dpe1")
            .upload_blob(&mut sess, BlobSource::from_bytes(image_bytes()), None)
            .unwrap(),
        [uploaded(201, None, json!({}))],
    );
    assert_eq!(
        server.requests[0].headers.get("Content-Type"),
        Some("application/octet-stream")
    );
}

// ---------------------------------------------------------------------- blob 分块与断点续传

/// 250 字节、单块 100：块为 [0-99]、[100-199]、[200-249]。
fn blob() -> Vec<u8> {
    vec![b'x'; 250]
}

/// 响应丢失且服务端未收到该块：先断点查询，再从已收偏移续传（core §3.4）。
#[test]
fn blob_chunk_lost_response_resumes_from_the_server_offset() {
    let target = format!("staging/st-1/blobs/{}", dpe_hash::blob_ref(&blob()));
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .lost("块 2 的响应丢失")
        .response(resume(100))
        .response(uploaded(202, Some(200), Value::Null))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), Some("image/png"))
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    let requests = &script.requests;
    assert_eq!(methods(requests), vec!["PUT", "PUT", "HEAD", "PUT", "PUT"]);
    assert_eq!(
        requests[0].headers.get("Content-Range"),
        Some("bytes 0-99/250")
    );
    assert_eq!(
        requests[1].headers.get("Content-Range"),
        Some("bytes 100-199/250")
    );
    assert_eq!(requests[2].target, target);
    assert_eq!(requests[2].headers.get("Content-Range"), None);
    assert_eq!(
        requests[3].headers.get("Content-Range"),
        Some("bytes 100-199/250")
    );
    assert_eq!(
        requests[3].body_bytes().unwrap(),
        Some(blob()[100..200].to_vec())
    );
    assert_eq!(
        requests[4].headers.get("Content-Range"),
        Some("bytes 200-249/250")
    );
    assert_eq!(requests[4].headers.get("Content-Type"), Some("image/png"));
}

/// 写请求与 `negotiate` / `upload_*` 都不是重发安全的；分块恢复的断点查询（HEAD）是只读的
/// （`retry_safe = true`）——与 Python `retry_safe` 标注逐个对照（协议模块说明）。
#[test]
fn retry_safe_flags_on_uploads_and_the_resume_query() {
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .lost("块 2 的响应丢失")
        .response(resume(100))
        .response(uploaded(202, Some(200), Value::Null))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    let flags: Vec<(&str, bool)> = script
        .requests
        .iter()
        .map(|request| (request.method.as_str(), request.retry_safe))
        .collect();
    assert_eq!(
        flags,
        vec![
            ("PUT", false),
            ("PUT", false),
            ("HEAD", true),
            ("PUT", false),
            ("PUT", false),
        ]
    );
}

/// 响应丢失但服务端已收完：偏移等于总量即已完成，重发末块（非空区间）取缺失清单。
#[test]
fn blob_chunk_completed_before_resume_resends_the_last_chunk() {
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .response(uploaded(202, Some(200), Value::Null))
        .lost("末块的响应丢失")
        .response(resume(250))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    let third = &script.requests[2];
    let head = &script.requests[3];
    let resent = &script.requests[4];
    assert_eq!(
        (head.method.as_str(), head.target.as_str()),
        ("HEAD", third.target.as_str())
    );
    assert_eq!(
        resent.headers.get("Content-Range"),
        Some("bytes 200-249/250")
    );
    assert_eq!(
        resent.body_bytes().unwrap(),
        Some(blob()[200..250].to_vec())
    );
}

#[test]
fn blob_chunk_lost_first_response_restarts_from_zero() {
    let mut script = Script::new()
        .lost("首块响应丢失")
        .response(resume(0))
        .response(uploaded(202, Some(100), Value::Null))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(vec![b'x'; 120]), None)
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    let frames: Vec<(&str, Option<&str>)> = script
        .requests
        .iter()
        .map(|request| {
            (
                request.method.as_str(),
                request.headers.get("Content-Range"),
            )
        })
        .collect();
    assert_eq!(
        frames,
        vec![
            ("PUT", Some("bytes 0-99/120")),
            ("HEAD", None),
            ("PUT", Some("bytes 0-99/120")),
            ("PUT", Some("bytes 100-119/120")),
        ]
    );
}

/// 偏移不连续的 400 带 `DPE-Upload-Offset`：直接据它重同步，免一次断点查询（#50）。
#[test]
fn blob_chunk_offset_mismatch_resyncs_from_the_header_without_head() {
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .response(offset_problem(200))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    assert_eq!(methods(&script.requests), vec!["PUT", "PUT", "PUT"]); // 无 HEAD
    assert_eq!(
        script.requests[2].headers.get("Content-Range"),
        Some("bytes 200-249/250")
    );
}

#[test]
fn blob_chunk_recovery_budget_is_bounded() {
    let mut script = Script::new()
        .lost("丢了")
        .response(resume(0))
        .lost("又丢了");
    let mut sess = session();
    let c = chunked_core(1);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert_eq!(error.to_string(), "响应丢失：又丢了");
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD", "PUT"]);
}

/// 无错误码的 504（网关已转发、结果未知）：与响应丢失同处理——先断点查询再续传。
#[test]
fn blob_chunk_gateway_lost_resumes_via_head() {
    let mut script = Script::new()
        .response(gateway(504))
        .response(resume(0))
        .response(uploaded(202, Some(100), Value::Null))
        .response(uploaded(201, None, json!({})));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(vec![b'x'; 120]), None)
        .unwrap();
    op_poll_to_end(&mut op, &mut script);
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD", "PUT", "PUT"]);
}

#[test]
fn blob_chunk_gateway_budget_exhausted_is_reported() {
    let mut script = Script::new()
        .response(gateway(504))
        .response(resume(0))
        .response(gateway(504));
    let mut sess = session();
    let c = chunked_core(1);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(504));
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD", "PUT"]);
}

/// 断点查询得 410：会话不可用如实抛出（HEAD 只有错误码头），由 deliver 重开。
#[test]
fn blob_chunk_resume_session_expired_is_reported() {
    let mut script =
        Script::new()
            .lost("块响应丢失")
            .response(problem("DPE_SESSION_EXPIRED", 410, Value::Null));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::SessionExpired);
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
}

/// 偏移重同步值超过总量：判不合规，不把它当作「已收」。
#[test]
fn blob_chunk_resync_hint_beyond_the_total_is_rejected() {
    let mut script = Script::new().response(offset_problem(999));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("超过总量"), "{error}");
    assert_eq!(methods(&script.requests), vec!["PUT"]);
}

/// 超长数字串不逃出 SDK 的错误分类：按「头不可用」判不合规，不抛裸数值转换错误。
#[test]
fn blob_chunk_absurd_offset_header_is_rejected() {
    let mut script = Script::new().response(dpe_sdk::Response::new(
        202,
        [
            ("DPE-Session-Expires".to_owned(), SESSION_EXPIRES.to_owned()),
            (UPLOAD_OFFSET_HEADER.to_owned(), "1".repeat(5000)),
        ],
        Vec::new(),
    ));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(202));
}

#[test]
fn blob_chunk_resend_after_completion_counts_against_the_budget() {
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .response(uploaded(202, Some(200), Value::Null))
        .lost("末块丢失")
        .response(resume(250))
        .lost("末块重发又丢失");
    let mut sess = session();
    let c = chunked_core(1);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert_eq!(error.to_string(), "响应丢失：末块重发又丢失");
}

/// 不前进的 202（服务端重复旧状态）：判不合规，不无上界地重发同一块。
#[test]
fn blob_chunk_stalled_202_is_rejected() {
    let mut script = Script::new()
        .response(uploaded(202, Some(0), Value::Null))
        .response(uploaded(202, Some(0), Value::Null));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("202"), "{error}");
    assert_eq!(methods(&script.requests), vec!["PUT"]); // 第一块即判，不再重发
}

#[test]
fn blob_chunk_202_offset_beyond_the_total_is_rejected() {
    let mut script = Script::new().response(uploaded(202, Some(251), Value::Null));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("未到齐"), "{error}");
    assert_eq!(methods(&script.requests), vec!["PUT"]);
}

/// 已完成后重发末块又得 202：到齐应是 201/200，202 语义不自洽，判不合规。
#[test]
fn blob_chunk_202_after_completion_is_rejected() {
    let mut script = Script::new()
        .response(uploaded(202, Some(100), Value::Null))
        .response(uploaded(202, Some(200), Value::Null))
        .lost("末块响应丢失")
        .response(resume(250))
        .response(uploaded(202, Some(250), Value::Null));
    let mut sess = session();
    let c = chunked_core(2);
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(blob()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("未到齐"), "{error}");
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "PUT", "PUT", "HEAD", "PUT"]
    );
}

/// 驱动操作直到结束（结果丢弃）。
fn op_poll_to_end<O: Operation>(op: &mut O, script: &mut Script)
where
    O::Output: std::fmt::Debug,
{
    let outcome = protocol_common::drive_script_ref(op, script);
    assert!(outcome.is_ok(), "期望成功，实际 {outcome:?}");
}

/// 驱动操作直到出错。
fn op_drive_to_error<O: Operation>(op: &mut O, script: &mut Script) -> Error
where
    O::Output: std::fmt::Debug,
{
    protocol_common::drive_script_ref(op, script).expect_err("期望失败")
}

// ---------------------------------------------------------------------- deliver：快路径

const TARGET: &str = "documents?uri=s3%3A%2F%2Fbucket%2Fa.md";

/// 缺 blob 的 `DPE_MISSING_CONTENT`（Python `missing=` 形态）。
fn missing_blobs(references: &[&str]) -> dpe_sdk::Response {
    problem(
        "DPE_MISSING_CONTENT",
        400,
        json!({"missing": {"pages": [], "content_hashes": [], "blobs": references}}),
    )
}

#[test]
fn deliver_fast_path_is_a_single_commit() {
    let document = doc(&[&["x", "y"]]);
    let (result, server) = run(
        core("dpe1")
            .deliver(URI, &document, Precondition::IfAbsent, None, None)
            .unwrap(),
        [protocol_common::committed(&document, "created", 2, 0, 0)],
    );
    assert_eq!(result.unwrap().status, dpe_sdk::wire::CommitStatus::Created);
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("PUT", TARGET)
    );
    assert!(body_json(request).get("staging_session").is_none());
}

#[test]
fn deliver_fast_path_force_has_no_condition_header() {
    let document = doc(&[&["x", "y"]]);
    let (_, server) = run(
        core("dpe1")
            .deliver(URI, &document, Precondition::Force, None, None)
            .unwrap(),
        [protocol_common::committed(&document, "updated", 1, 1, 1)],
    );
    let request = &server.requests[0];
    assert_eq!(request.headers.get("If-Match"), None);
    assert_eq!(request.headers.get("If-None-Match"), None);
    assert_eq!(body_json(request)["force"], Value::Bool(true));
}

/// blob 永远不能内联：开会话上传后，以内联 + `staging_session` 重新 commit（core §6）。
/// 快路径恢复同样只计没有进展的轮次（#127）：首次内联 commit 缺 blob、在新会话里补传后
/// 完成——预算为 0 也成立（同 Python `test_deliver_fast_path_recovery_with_progress_needs_no_budget`）。
#[test]
fn deliver_fast_path_recovery_with_progress_needs_no_budget() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(uploaded(201, None, json!({})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = core_with_resends(0);
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
}

#[test]
fn deliver_recovers_a_missing_blob_into_a_session() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(uploaded(201, None, json!({})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = core("dpe1");
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);

    let requests = &script.requests;
    assert!(body_json(&requests[0]).get("staging_session").is_none());
    assert_eq!(
        (requests[1].method.as_str(), requests[1].target.as_str()),
        ("POST", "negotiate")
    );
    assert_eq!(
        requests[2].target,
        format!("staging/st-1/blobs/{reference}")
    );
    assert_eq!(requests[2].body_bytes().unwrap(), Some(image_bytes()));
    assert_eq!(requests[2].headers.get("Content-Type"), Some("image/png"));
    let body = body_json(&requests[3]);
    assert_eq!(body["staging_session"], "st-1");
    assert!(body["pages"].is_array() && body["objects"].is_array()); // 仍是「内联 + 会话」
    assert!(body.get("force").is_none());
}

#[test]
fn deliver_missing_blob_without_a_provider_is_reported() {
    let document = image_doc();
    let reference = image_ref();
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]));
    let c = core("dpe1");
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(error.missing().unwrap().blobs, vec![reference]);
    let session = error.session().expect("错误带当前会话");
    assert_eq!(session.id, "st-1");
    assert_eq!(methods(&script.requests), vec!["PUT", "POST"]); // 未上传任何字节
    assert!(!error.outcome_unknown()); // commit 收到了明确响应
}

#[test]
fn deliver_rejects_a_provider_source_with_the_wrong_ref() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(b"other".to_vec()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]));
    let c = core("dpe1");
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    assert!(error.message().contains("不符"), "{error}");
}

#[test]
fn deliver_rejects_a_manifest_entry_not_in_the_document() {
    let document = doc(&[&["x"]]);
    let one = h1();
    let mut script = Script::new()
        .response(problem(
            "DPE_MISSING_CONTENT",
            400,
            json!({"missing": {"content_hashes": [one]}}),
        ))
        .response(negotiated("st-1", &[], &[]));
    let c = core("dpe1");
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("不是本次提交引用"), "{error}");
}

#[test]
fn deliver_rejects_a_manifest_blob_not_in_the_document() {
    let other = dpe_hash::blob_ref(b"other");
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(b"other".to_vec()));
    let mut script = Script::new()
        .response(missing_blobs(&[&other]))
        .response(negotiated("st-1", &[], &[]));
    let c = core("dpe1");
    let document = image_doc();
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("不是本次提交引用"), "{error}");
}

#[test]
fn deliver_rejects_a_malformed_manifest_blob() {
    let bad = format!("dpe1:{}", "0".repeat(64));
    let mut script = Script::new()
        .response(missing_blobs(&[&bad]))
        .response(negotiated("st-1", &[], &[]));
    let c = core("dpe1");
    let document = image_doc();
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("不是合法的 blob 引用"), "{error}");
}

/// 缺 blob 来源是本地判定：直接逃出恢复循环——已上传的页与元素不因空转而重传。
#[test]
fn deliver_without_a_blob_source_fails_fast() {
    let document = image_doc();
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let reference = image_ref();
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 200)]),
        2,
    );
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hashes[0].as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": [&reference]})));
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(methods(&script.requests), vec!["POST", "PUT", "PUT"]); // 页与元素各传一次
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(error.missing().unwrap().blobs, vec![reference]);
    assert_eq!(error.session().expect("带会话").id, "st-1");
}

/// 元素上传响应给出的域外 blob 同样先过校验：不把未校验的引用交给取字节回调。
#[test]
fn deliver_rejects_an_out_of_document_blob_from_an_element_response() {
    let document = doc(&[&["x"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let other = dpe_hash::blob_ref(b"other");
    // 回调须为 Send（#131）：计数用原子量，不用 Cell
    let asked = std::sync::atomic::AtomicU32::new(0);
    let mut provider = |_reference: &str| {
        asked.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Some(BlobSource::from_bytes(Vec::new()))
    };
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hashes[0].as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": [&other]})));
    let c = staged_core();
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    // 持有 Send 回调的 sans-IO 操作本身是 Send，自带传输的驱动方可以跨线程持有它
    fn assert_send<T: Send>(_: &T) {}
    assert_send(&op);
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("不是本次提交引用"), "{error}");
    assert_eq!(
        asked.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "不得向回调索取未校验的引用"
    );
}

/// 内联体贴着上限（加上会话 id 即超限）：恢复改走暂存路径，请求体只含文档对象。
#[test]
fn deliver_fast_path_recovery_falls_back_when_the_body_overflows() {
    let document = image_doc();
    let parts = protocol_common::parts_for(&document);
    let inline_body = json!({
        "document": parts.document,
        "pages": parts.pages,
        "objects": parts.objects,
    });
    let limit = protocol_common::wire_json_len(&inline_body);
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", limit)]),
        2,
    );
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(missing_blobs(&[&reference]))
        .response(uploaded(201, None, json!({})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "POST", "PUT", "PUT", "PUT"]
    );
    assert_eq!(
        script.requests[0].body_bytes().unwrap().unwrap().len() as u64,
        limit
    );
    assert_eq!(
        body_json(&script.requests[2]),
        json!({"document": parts.document, "staging_session": "st-1"})
    );
    assert_eq!(
        script.requests[3].target,
        format!("staging/st-1/blobs/{reference}")
    );
}

// ---------------------------------------------------------------------- deliver：暂存路径

#[test]
fn deliver_staged_path_follows_the_layers() {
    let document = doc(&[&["x", "y"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hash.as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(protocol_common::committed(&document, "created", 2, 0, 0));
    let c = staged_core();
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);

    let requests = &script.requests;
    assert_eq!(requests[0].target, "negotiate");
    assert_eq!(
        requests[1].target,
        format!("staging/st-1/pages/{page_hash}")
    );
    assert_eq!(
        (requests[2].target.as_str(), requests[3].target.as_str()),
        (
            format!("staging/st-1/objects/{}", content_hashes[0][0]).as_str(),
            format!("staging/st-1/objects/{}", content_hashes[0][1]).as_str()
        )
    );
    let parts = protocol_common::parts_for(&document);
    assert_eq!(
        body_json(&requests[4]),
        json!({"document": parts.document, "staging_session": "st-1"})
    );
}

/// commit 报 MISSING_CONTENT：按清单补进同一会话后重提（core §3.4）。
#[test]
fn deliver_recovery_uploads_into_the_same_session() {
    let document = doc(&[&["x"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let mut script = Script::new()
        .response(negotiated("st-1", &[], &[]))
        .response(problem(
            "DPE_MISSING_CONTENT",
            400,
            json!({"missing": {"pages": [page_hash], "content_hashes": [], "blobs": []}}),
        ))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = staged_core();
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    let targets: Vec<&str> = script.requests.iter().map(|r| r.target.as_str()).collect();
    assert_eq!(targets[2], format!("staging/st-1/pages/{page_hash}"));
    assert_eq!(targets[4], TARGET);
    assert_eq!(body_json(&script.requests[4])["staging_session"], "st-1");
}

#[test]
fn deliver_renegotiates_when_the_session_expires_mid_flow() {
    let document = doc(&[&["x"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hash.as_str()], &[]))
        .response(problem("DPE_SESSION_EXPIRED", 410, Value::Null))
        .response(negotiated("st-2", &[page_hash.as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = staged_core();
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    let targets: Vec<&str> = script.requests.iter().map(|r| r.target.as_str()).collect();
    assert_eq!(targets[1], format!("staging/st-1/pages/{page_hash}"));
    assert_eq!(targets[3], format!("staging/st-2/pages/{page_hash}"));
    assert_eq!(body_json(&script.requests[5])["staging_session"], "st-2");
}

/// 服务端始终报同一份缺失清单（无进展）：恢复轮耗尽后如实抛出，不无限重试。
#[test]
fn deliver_recovery_is_bounded() {
    let document = doc(&[&["x"]]);
    let (page_hashes, _) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let missing = problem(
        "DPE_MISSING_CONTENT",
        400,
        json!({"missing": {"pages": [page_hash]}}),
    );
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hash.as_str()], &[]))
        .response(uploaded(201, None, json!({"missing_content_hashes": []})))
        .response(missing.clone())
        .response(uploaded(201, None, json!({"missing_content_hashes": []})))
        .response(missing);
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 120)]),
        1,
    );
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(
        methods(&script.requests),
        vec!["POST", "PUT", "PUT", "PUT", "PUT"]
    );
}

/// 截断的清单逐轮补齐（#127）：每轮列出的都是本会话没上传过的对象，即有进展，不计
/// `max_resends`——预算为 0 也能完成（同 Python
/// `test_deliver_progress_rounds_do_not_consume_the_budget`）。
#[test]
fn deliver_progress_rounds_do_not_consume_the_budget() {
    let document = doc(&[&["a", "b", "c"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let (e1, e2, e3) = (
        content_hashes[0][0].clone(),
        content_hashes[0][1].clone(),
        content_hashes[0][2].clone(),
    );
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hash.as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": [e1], "missing_truncated": true}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(problem(
            "DPE_MISSING_CONTENT",
            400,
            json!({"missing": {"content_hashes": [e2]}, "missing_truncated": true}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(problem(
            "DPE_MISSING_CONTENT",
            400,
            json!({"missing": {"content_hashes": [e3]}}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(protocol_common::committed(&document, "created", 3, 0, 0));
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 120)]),
        0,
    );
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
}

/// 服务端反复列出本会话已上传过的对象：每轮计一次预算，用尽后失败，不会无限循环（同 Python
/// `test_deliver_stalls_when_the_server_relists_an_uploaded_object`）。
#[test]
fn deliver_stalls_when_the_server_relists_an_uploaded_object() {
    let document = doc(&[&["a"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let e1 = content_hashes[0][0].clone();
    let stuck = problem(
        "DPE_MISSING_CONTENT",
        400,
        json!({"missing": {"content_hashes": [e1]}}),
    );
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hash.as_str()], &[]))
        .response(uploaded(201, None, json!({"missing_content_hashes": [e1]})))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(stuck.clone()) // 已上传过 e1：无进展，预算 2 → 1
        .response(uploaded(200, None, json!({"missing_blobs": []})))
        .response(stuck.clone()) // 1 → 0
        .response(uploaded(200, None, json!({"missing_blobs": []})))
        .response(stuck); // 预算用尽：失败
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 120)]),
        2,
    );
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(script.requests.len(), 8);
}

// ---------------------------------------------------------------------- deliver：会话复用与 CAS

#[test]
fn deliver_with_a_provided_session_commits_directly() {
    let document = doc(&[&["x"]]);
    let parts = protocol_common::parts_for(&document);
    let (_, server) = run(
        core("dpe1")
            .deliver(
                URI,
                &document,
                Precondition::IfAbsent,
                None,
                Some(session_with_id("st-9")),
            )
            .unwrap(),
        [protocol_common::committed(&document, "created", 1, 0, 0)],
    );
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("PUT", TARGET)
    );
    assert_eq!(
        body_json(request),
        json!({"document": parts.document, "staging_session": "st-9"})
    );
}

/// 会话只作缓存：传入失效会话时重开 negotiate，结果与不复用时一致（core §3.4）。
#[test]
fn deliver_with_an_expired_session_renegotiates() {
    let document = doc(&[&["x"]]);
    let (page_hashes, content_hashes) = protocol_common::page_and_element_hashes(&document);
    let page_hash = page_hashes[0].clone();
    let mut script = Script::new()
        .response(problem("DPE_SESSION_EXPIRED", 410, Value::Null))
        .response(negotiated("st-2", &[page_hash.as_str()], &[]))
        .response(uploaded(
            201,
            None,
            json!({"missing_content_hashes": content_hashes[0]}),
        ))
        .response(uploaded(201, None, json!({"missing_blobs": []})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = staged_core();
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            None,
            Some(session_with_id("st-9")),
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    assert_eq!(body_json(&script.requests[4])["staging_session"], "st-2");
}

/// 快路径开会话后上传遇 410：重开 negotiate，补传保留清单，以「内联 + 新会话」重提。
#[test]
fn deliver_fast_path_renegotiates_when_the_session_expires() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(problem("DPE_SESSION_EXPIRED", 410, Value::Null))
        .response(negotiated("st-2", &[], &[]))
        .response(uploaded(201, None, json!({})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let c = core("dpe1");
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    let targets: Vec<&str> = script.requests.iter().map(|r| r.target.as_str()).collect();
    assert_eq!(targets[2], format!("staging/st-1/blobs/{reference}"));
    assert_eq!(targets[4], format!("staging/st-2/blobs/{reference}")); // 新会话里重新补传
    let body = body_json(&script.requests[5]);
    assert_eq!(body["staging_session"], "st-2");
    assert!(body["pages"].is_array() && body["objects"].is_array()); // 仍是「内联 + 会话」
}

/// CAS 三类原样抛出（绝不自动 force），错误带当前会话供上层复用（core §3.4、§5.2）。
#[test]
fn deliver_reports_cas_failures_as_is_with_the_session() {
    let document = doc(&[&["x"]]);
    let cases: [(&str, u16, ErrorKind); 3] = [
        (
            "DPE_PRECONDITION_FAILED",
            412,
            ErrorKind::PreconditionFailed,
        ),
        ("DPE_ALREADY_EXISTS", 412, ErrorKind::AlreadyExists),
        ("DPE_NOT_FOUND", 412, ErrorKind::NotFound),
    ];
    for (code, status, kind) in cases {
        let mut script = Script::new().response(problem(code, status, Value::Null));
        let c = staged_core();
        let mut op = c
            .deliver(
                URI,
                &document,
                Precondition::BaseHash(h1()),
                None,
                Some(session_with_id("st-9")),
            )
            .unwrap();
        let error = op_drive_to_error(&mut op, &mut script);
        assert_eq!(error.kind(), kind, "{code}");
        assert_eq!(
            error.session().map(|s| s.id.as_str()),
            Some("st-9"),
            "{code}"
        );
        assert_eq!(methods(&script.requests), vec!["PUT"], "{code}"); // 不重发、不 head、不 force
        assert!(!error.outcome_unknown(), "{code}");
    }
}

#[test]
fn deliver_cas_failure_without_a_session_has_none() {
    let document = doc(&[&["x", "y"]]);
    let mut script = Script::new().response(problem("DPE_PRECONDITION_FAILED", 412, Value::Null));
    let c = core("dpe1");
    let mut op = c
        .deliver(URI, &document, Precondition::BaseHash(h1()), None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    assert!(error.session().is_none());
}

#[test]
fn deliver_validates_locally_without_requests() {
    let document = doc(&[&["x"]]);
    assert_eq!(
        expect_error(core("dpe1").deliver(
            "no scheme",
            &document,
            Precondition::IfAbsent,
            None,
            None
        ))
        .kind(),
        ErrorKind::Validation
    );
    let drill_hash = h2().replace("dpe1", "dpe2");
    assert_eq!(
        expect_error(core("dpe1").deliver(
            URI,
            &document,
            Precondition::BaseHash(drill_hash),
            None,
            None
        ))
        .kind(),
        ErrorKind::ContractUnsupported
    );
}

// ---------------------------------------------------------------------- 写操作的「结果是否未知」

/// 传输适配层可标注「请求确定没发出」（建连失败等；`protocol::not_sent`）。
fn not_sent(note: &'static str) -> TransportError {
    dpe_sdk::protocol::not_sent(protocol_common::Lost(note))
}

/// `NotSent` 标记的形状：包装原始错误、`source()` 可取回；普通传输错误不带标记。
#[test]
fn not_sent_marker_wraps_the_transport_error() {
    let marked = not_sent("建连失败");
    let marker = marked
        .downcast_ref::<dpe_sdk::protocol::NotSent>()
        .expect("带「确定没发出」标记");
    assert!(marker.error().to_string().contains("建连失败"));
    let inner: &(dyn std::error::Error + 'static) = marked.as_ref();
    assert_eq!(
        inner.source().expect("source").to_string(),
        "响应丢失：建连失败"
    );

    let plain: TransportError = Box::new(protocol_common::Lost("超时")) as TransportError;
    assert!(plain.downcast_ref::<dpe_sdk::protocol::NotSent>().is_none());
}

#[test]
fn outcome_unknown_false_when_the_write_was_never_sent() {
    // 首发就建连失败（确定没发出）：全部尝试都未发出 → false
    let document = doc(&[&["x"]]);
    let mut script = Script::new()
        .error(not_sent("1"))
        .error(not_sent("2"))
        .error(not_sent("3"));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(!error.outcome_unknown());
}

#[test]
fn outcome_unknown_false_on_a_coded_rejection() {
    let document = doc(&[&["x"]]);
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        [problem("DPE_FORBIDDEN", 403, Value::Null)],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert!(!error.outcome_unknown());
}

#[test]
fn outcome_unknown_true_when_the_first_response_was_lost() {
    // 首发响应丢失（可能已到达），重发收到 429（带码）并耗尽 → true
    let document = doc(&[&["x"]]);
    let mut script =
        Script::new()
            .lost("首发丢失")
            .response(problem("DPE_RATE_LIMITED", 429, Value::Null));
    let c = core_with_resends(1);
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::RateLimited);
    assert!(error.outcome_unknown());

    // 首发响应丢失，重发全部建连失败 → 仍为 true（首次尝试不确定）
    let mut script = Script::new()
        .lost("首发丢失")
        .error(not_sent("1"))
        .error(not_sent("2"));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(error.outcome_unknown());
}

#[test]
fn outcome_unknown_true_on_a_bare_5xx() {
    let document = doc(&[&["x"]]);
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        [empty(500, &[])],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.outcome_unknown());

    // 裸 503 同此；带 DPE 错误码的 5xx 是明确结果
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        [problem("DPE_UNAVAILABLE", 503, Value::Null)],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::Unavailable);
    assert!(!error.outcome_unknown());
}

#[test]
fn outcome_unknown_false_for_delete_and_move_definitive_results() {
    let mut script = Script::new().response(problem("DPE_PRECONDITION_FAILED", 412, Value::Null));
    let c = core("dpe1");
    let mut op = c.delete(URI, &h1()).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert!(!error.outcome_unknown());

    let mut script = Script::new().response(problem("DPE_ALREADY_EXISTS", 409, Value::Null));
    let c = core("dpe1");
    let mut op = c.move_document(URI, "s3://bucket/b.md", &h1()).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert!(!error.outcome_unknown());

    // 裸 502（网关丢失）：move 原样重发至预算用尽 → 结果未知
    let mut script = Script::new()
        .response(empty(502, &[]))
        .response(empty(502, &[]))
        .response(empty(502, &[]));
    let c = core("dpe1");
    let mut op = c.move_document(URI, "s3://bucket/b.md", &h1()).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(502));
    assert!(error.outcome_unknown());
    assert_eq!(methods(&script.requests), vec!["POST", "POST", "POST"]);
}

#[test]
fn outcome_unknown_false_when_deliver_fails_before_the_commit() {
    // negotiate 阶段失败：commit 还没发出 → false
    let document = image_doc();
    let c = staged_core();
    let mut script = Script::new().response(problem("DPE_FORBIDDEN", 403, Value::Null));
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert!(!error.outcome_unknown());

    // 上传阶段失败：同样 false
    let (page_hashes, _) = protocol_common::page_and_element_hashes(&document);
    let mut script = Script::new()
        .response(negotiated("st-1", &[page_hashes[0].as_str()], &[]))
        .response(problem("DPE_SESSION_EXPIRED", 410, Value::Null))
        .response(problem("DPE_FORBIDDEN", 403, Value::Null));
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert!(!error.outcome_unknown());
}

#[test]
fn outcome_unknown_true_when_deliver_lost_its_commit_response() {
    // 快路径 commit 的响应丢失、重发也丢失（预算耗尽）→ 结果未知
    let document = doc(&[&["x"]]);
    let c = core_with_resends(1);
    let mut script = Script::new().lost("1").lost("2");
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(error.outcome_unknown());
}

/// 只改 `max_resends` 的核心（结果未知矩阵用）。
fn core_with_resends(max_resends: u32) -> dpe_sdk::protocol::ProtocolCore {
    protocol_common::core_with_caps(protocol_common::capabilities(), max_resends)
}

/// 带 DPE 错误码的响应之外的无码响应一律不确定（含成功但自洽校验失败、无码 4xx、无码 5xx）；
/// 只有「带码响应 / 校验通过的成功 / `NotSent` 传输错误」三种结局算确定。
#[test]
fn outcome_unknown_true_on_unresolved_or_uncoded_responses() {
    let document = doc(&[&["x"]]);

    // 2xx 但自洽校验失败（delta 与元素个数不符）→ 不确定
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        [json_response_with(
            200,
            json!({
                "status": "updated",
                "doc_hash": doc_hash(&document),
                // added + retained = 0 ≠ 元素个数 1：自洽校验失败
                "delta": {"added": 0, "removed": 0, "retained": 0},
            }),
            &[],
        )],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.outcome_unknown());

    // 无码 401（代理产物）→ 不确定
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        [empty(401, &[("WWW-Authenticate", "Bearer")])],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(401));
    assert!(error.outcome_unknown());
}

/// 裸 412 的两种「没能化解」：head 判定本身失败、重发后仍是裸 412。
#[test]
fn outcome_unknown_true_when_a_bare_412_stays_unresolved() {
    let document = doc(&[&["x"]]);
    let one = h1();

    // head 判定本身失败（head 收到带码的 503）→ 不确定
    let mut script =
        Script::new()
            .response(bare_412())
            .response(problem("DPE_UNAVAILABLE", 503, Value::Null));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::BaseHash(one.clone()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Unavailable);
    assert!(error.outcome_unknown());

    // 重发后仍是裸 412（IfAbsent 的误判链走完）→ 不确定
    let mut script = Script::new()
        .response(bare_412())
        .response(head_missing())
        .response(bare_412())
        .response(head_missing());
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.outcome_unknown());

    // 对照：裸 412 经 head 化解（判定为前置条件不成立）→ 确定
    let mut script = Script::new().response(bare_412()).response(head_missing());
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::BaseHash(one), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::NotFound);
    assert!(!error.outcome_unknown());

    // 对照：force 不带条件头，裸 412 无从化解 → 不确定
    let mut script = Script::new().response(bare_412());
    let c = core("dpe1");
    let mut op = c.commit(URI, &document, Precondition::Force, None).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.outcome_unknown());
}

// ---------------------------------------------------------------------- 审查回归（B1 / B2 / W2）

/// B1 回归：暂存路径的首个 negotiate 返回带码 `DPE_MISSING_CONTENT` 时如实报错，不 panic
/// （没有会话可补传，不进入恢复循环；同 Python：初始 `_negotiate` 不在恢复循环内）。
#[test]
fn staged_deliver_reports_a_coded_missing_content_from_the_first_negotiate() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", 200)]),
        2,
    );
    let mut script = Script::new().response(problem(
        "DPE_MISSING_CONTENT",
        400,
        json!({"missing": {"pages": [], "content_hashes": [], "blobs": [&reference]}}),
    ));
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(methods(&script.requests), vec!["POST"]); // 没有可补传的会话，不再发请求
    assert!(!error.outcome_unknown()); // 写请求从未发出
}

/// B2 回归：快路径回落暂存路径时恢复预算重置（同 Python：`_inline` 与 `_staged` 各自新预算）。
#[test]
fn fast_path_fallback_resets_the_recovery_budget() {
    let document = image_doc();
    let parts = protocol_common::parts_for(&document);
    let inline_body = json!({
        "document": parts.document,
        "pages": parts.pages,
        "objects": parts.objects,
    });
    let limit = protocol_common::wire_json_len(&inline_body);
    let c = protocol_common::core_with_caps(
        protocol_common::caps_with_limits(&[("max_payload_bytes", limit)]),
        1, // max_resends=1：回落后的暂存阶段必须拿到新的预算才能完成
    );
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(missing_blobs(&[&reference]))
        .response(uploaded(201, None, json!({})))
        .response(protocol_common::committed(&document, "created", 1, 0, 0));
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let result = protocol_common::drive_script_ref(&mut op, &mut script).unwrap();
    assert_eq!(result.status, dpe_sdk::wire::CommitStatus::Created);
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "POST", "PUT", "PUT", "PUT"]
    );
}

/// W2-1：deliver 跨阶段累计「结果未知」——首发 commit 的响应丢失（不确定），此后各阶段收到
/// 的都是带码明确响应，最终失败仍应为真。
#[test]
fn outcome_unknown_accumulates_across_deliver_stages() {
    let document = image_doc();
    let reference = image_ref();
    let mut provider = |_reference: &str| Some(BlobSource::from_bytes(image_bytes()));
    let mut script = Script::new()
        .lost("首发 commit 响应丢失")
        .response(missing_blobs(&[&reference]))
        .response(negotiated("st-1", &[], &[]))
        .response(uploaded(201, None, json!({})))
        .response(problem("DPE_FORBIDDEN", 403, Value::Null));
    let c = core("dpe1");
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            Some(&mut provider),
            None,
        )
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert!(error.outcome_unknown());
}

/// W2-2：暂存路径的 commit 响应丢失（带会话直提），重发也丢失 → 结果未知。
#[test]
fn outcome_unknown_true_when_a_staged_commit_response_is_lost() {
    let document = doc(&[&["x"]]);
    let c = protocol_common::core_with_caps(protocol_common::capabilities(), 1);
    let mut script = Script::new().lost("1").lost("2");
    let mut op = c
        .deliver(
            URI,
            &document,
            Precondition::IfAbsent,
            None,
            Some(session_with_id("st-9")),
        )
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(error.outcome_unknown());
    assert_eq!(methods(&script.requests), vec!["PUT", "PUT"]);
}

/// W2-3：delete 的裸 502 重发耗尽 → 结果未知。
#[test]
fn outcome_unknown_true_when_delete_exhausts_bare_502_resends() {
    let one = h1();
    let mut script = Script::new()
        .response(empty(502, &[]))
        .response(empty(502, &[]))
        .response(empty(502, &[]));
    let c = core("dpe1");
    let mut op = c.delete(URI, &one).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(502));
    assert!(error.outcome_unknown());
    assert_eq!(methods(&script.requests), vec!["DELETE"; 3]);
}

/// W2-4：裸 412 经 head 化解为明确的 `DPE_ALREADY_EXISTS` / `DPE_PRECONDITION_FAILED` → 确定。
#[test]
fn outcome_unknown_false_when_a_bare_412_resolves_to_a_coded_verdict() {
    let document = doc(&[&["x"]]);
    // IfAbsent：文档存在（且内容不同）→ ALREADY_EXISTS
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&h2()));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::AlreadyExists);
    assert!(!error.outcome_unknown());

    // If-Match：文档存在但内容不同 → PRECONDITION_FAILED
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&h2()));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::BaseHash(h1()), None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    assert!(!error.outcome_unknown());
}

// ------------------------------------------------- python 结果标记矩阵（#22 交接核对）的补缺

/// 裸 503（无 DPE 错误码）：网关或上游可能已处理 → 不确定（带码的 `DPE_UNAVAILABLE` 是
/// 明确裁决，见 `outcome_unknown_true_on_a_bare_5xx`）。
#[test]
fn outcome_unknown_true_on_a_bare_503() {
    let document = doc(&[&["x"]]);
    let mut script = Script::new().response(empty(503, &[]));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(503));
    assert!(error.outcome_unknown());
}

/// 裸 504（网关已转发、源站结果未知）：原样重发至预算用尽 → 不确定。
#[test]
fn outcome_unknown_true_when_a_bare_504_exhausts_the_resend_budget() {
    let document = doc(&[&["x"]]);
    let mut script = Script::new()
        .response(empty(504, &[]))
        .response(empty(504, &[]))
        .response(empty(504, &[]));
    let c = core("dpe1");
    let mut op = c
        .commit(URI, &document, Precondition::IfAbsent, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(504));
    assert!(error.outcome_unknown());
    assert_eq!(methods(&script.requests), vec!["PUT", "PUT", "PUT"]); // 首发 + 2 次重发
}

/// deliver 的 negotiate 阶段遇传输失败（未打「确定未发出」标记）：commit 尚未发出，
/// 整体按「确定未生效」打标——单次尝试的不确定性不参与（同 Python
/// `test_deliver_marks_determined_when_the_commit_was_never_sent`）。
#[test]
fn outcome_unknown_false_when_negotiate_fails_with_a_lost_transport_error() {
    let document = image_doc();
    let c = staged_core();
    let mut script = Script::new().lost("negotiate 响应丢失");
    let mut op = c
        .deliver(URI, &document, Precondition::IfAbsent, None, None)
        .unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(!error.outcome_unknown());
}

/// 裸 412 经 head 化解的 delete 形态（head 显示内容不同 → 条件不成立）→ 确定。
#[test]
fn outcome_unknown_false_when_a_bare_412_resolves_for_delete() {
    let one = h1();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&h2()));
    let c = core("dpe1");
    let mut op = c.delete(URI, &one).unwrap();
    let error = op_drive_to_error(&mut op, &mut script);
    assert_eq!(error.kind(), ErrorKind::PreconditionFailed);
    assert!(!error.outcome_unknown());
    assert_eq!(methods(&script.requests), vec!["DELETE", "HEAD"]);
}

/// W3 回归：`upload_blob` 完成后再 poll 是用法错误（与另两个 upload 操作一致）。
#[test]
fn upload_blob_poll_after_completion_is_a_usage_error() {
    let c = core("dpe1");
    let mut sess = session();
    let mut op = c
        .upload_blob(&mut sess, BlobSource::from_bytes(image_bytes()), None)
        .unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_response(uploaded(201, None, json!({})))
        .expect("回送成功");
    assert!(matches!(op.poll(), Ok(Step::Done(()))));
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
}
