//! 协议核心测试的辅助：能力构造、按顺序回放的假服务端与响应构造（经模块声明共享）。
//!
//! 与 Python 侧 `tests/protocol_helpers.py` 对等；响应按 HTTP 绑定 §4、§5 的报文形状手工构造。

#![allow(dead_code)] // 各测试文件只用到其中一部分

use std::collections::VecDeque;
use std::fmt;

use dpe_sdk::protocol::{
    drive, Operation, ProtocolCore, Request, Response, Step, TransportError, DEFAULT_MAX_RESENDS,
    DOC_HASH_HEADER, ERROR_CODE_HEADER,
};
use dpe_sdk::wire::Capabilities;
use dpe_sdk::Error;
use serde_json::{json, Value};

/// `dpe1:` + 64 位重复字符的 hash 字面量。
pub fn hash(digit: char) -> String {
    format!("dpe1:{}", digit.to_string().repeat(64))
}

pub fn h1() -> String {
    hash('1')
}

pub fn h2() -> String {
    hash('2')
}

/// 演练契约的 hash 字面量。
pub fn h_drill() -> String {
    format!("dpe2:{}", "3".repeat(64))
}

/// capabilities 响应体模板（`batch_head_max = 2`，便于测分批；与 Python 侧一致）。
pub fn capabilities_body() -> Value {
    json!({
        "protocol": "dpe/1",
        "hash_contracts": ["dpe1"],
        "limits": {
            "max_payload_bytes": 8388608,
            "page_max_bytes": 268435456,
            "staging_ttl_seconds": 86400,
            "blob_max_bytes": 104857600,
            "blob_chunk_bytes": 8388608,
            "batch_head_max": 2,
            "list_page_max": 1000,
        },
        "content_encodings": ["gzip"],
        "features": ["move"],
    })
}

pub fn capabilities() -> Capabilities {
    Capabilities::from_value(&capabilities_body()).expect("能力模板必须合法")
}

/// 用模板能力构造核心（`dpe1`）。
pub fn core(contract: &str) -> ProtocolCore {
    ProtocolCore::new(capabilities(), contract, DEFAULT_MAX_RESENDS)
        .expect("模板能力必须与核心构造兼容")
}

/// 带自定义能力的核心（构造失败返回 `Err`，供兼容性用例断言）。
pub fn core_from(capabilities: Capabilities, contract: &str) -> Result<ProtocolCore, Error> {
    ProtocolCore::new(capabilities, contract, DEFAULT_MAX_RESENDS)
}

/// 带自定义 max_resends 的核心（重发预算用例）。
pub fn core_with_resends(
    capabilities: Capabilities,
    contract: &str,
    max_resends: u32,
) -> Result<ProtocolCore, Error> {
    ProtocolCore::new(capabilities, contract, max_resends)
}

/// 由 JSON 值构造能力（用例常要改字段）。
pub fn caps_from(body: &Value) -> Result<Capabilities, Error> {
    Capabilities::from_value(body)
}

/// 200 + JSON 体的响应。
pub fn json_ok(body: Value) -> Response {
    json_response(200, body)
}

/// 指定状态 + JSON 体的响应。
pub fn json_response(status: u16, body: Value) -> Response {
    Response::new(
        status,
        [("Content-Type", "application/json")],
        serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    )
}

/// 无体的响应（HEAD、204 等）；`headers` 为附加头。
pub fn empty(status: u16, headers: &[(&str, &str)]) -> Response {
    Response::new(status, headers.iter().copied(), Vec::new())
}

/// problem 响应（HTTP 绑定 §5）：体 + `DPE-Error-Code` 头；`extra` 为附加的体成员（对象或 null）。
pub fn problem(code: &str, status: u16, extra: Value) -> Response {
    let mut body = json!({
        "type": "urn:dpe:error:x",
        "title": "t",
        "status": status,
        "code": code,
    });
    if let Some(extra) = extra.as_object() {
        let object = body.as_object_mut().expect("刚构造的对象");
        for (key, value) in extra {
            object.insert(key.clone(), value.clone());
        }
    }
    Response::new(
        status,
        [
            ("Content-Type", "application/problem+json"),
            ("DPE-Error-Code", code),
        ],
        serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    )
}

/// 按顺序回放响应的假服务端，并记录收到的请求。
pub struct Server {
    responses: VecDeque<Response>,
    pub requests: Vec<Request>,
}

impl Server {
    pub fn new(responses: impl IntoIterator<Item = Response>) -> Self {
        Server {
            responses: responses.into_iter().collect(),
            requests: Vec::new(),
        }
    }

    pub fn send(&mut self, request: Request) -> Result<Response, TransportError> {
        self.requests.push(request);
        Ok(self.responses.pop_front().expect("假服务端没有更多响应"))
    }
}

/// 驱动操作并按顺序回放 `responses`；返回结果与假服务端（含收到的请求）。
pub fn run<O: Operation>(
    op: O,
    responses: impl IntoIterator<Item = Response>,
) -> (Result<O::Output, Error>, Server) {
    let mut server = Server::new(responses);
    let result = drive(op, |request| server.send(request));
    (result, server)
}

/// 传输异常（模拟响应丢失）。
#[derive(Debug)]
pub struct Lost(pub &'static str);

impl fmt::Display for Lost {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "响应丢失：{}", self.0)
    }
}

impl std::error::Error for Lost {}

/// 按脚本逐次给出响应或传输异常（模拟响应丢失），并记录收到的请求。
pub struct Script {
    steps: VecDeque<Result<Response, TransportError>>,
    pub requests: Vec<Request>,
}

impl Script {
    pub fn new() -> Self {
        Script {
            steps: VecDeque::new(),
            requests: Vec::new(),
        }
    }

    pub fn response(mut self, response: Response) -> Self {
        self.steps.push_back(Ok(response));
        self
    }

    /// 传输异常（请求可能已到达服务端）。
    pub fn lost(mut self, note: &'static str) -> Self {
        self.steps.push_back(Err(Box::new(Lost(note))));
        self
    }

    /// 任意传输错误（如 `protocol::not_sent` 标注的建连失败）。
    pub fn error(mut self, error: TransportError) -> Self {
        self.steps.push_back(Err(error));
        self
    }

    pub fn send(&mut self, request: Request) -> Result<Response, TransportError> {
        self.requests.push(request);
        self.steps.pop_front().expect("脚本没有更多步骤")
    }
}

/// 同步驱动 `&mut` 操作直到完成（[`drive`] 的借用版本；调用方保留操作做后续断言）。
pub fn drive_ref<O: Operation>(
    op: &mut O,
    mut send: impl FnMut(Request) -> Result<Response, TransportError>,
) -> Result<O::Output, Error> {
    loop {
        match op.poll()? {
            Step::Send(request) => match send(request) {
                Ok(response) => op.on_response(response)?,
                Err(error) => op.on_transport_error(error)?,
            },
            Step::Done(output) => return Ok(output),
        }
    }
}

/// 用脚本驱动 `&mut` 操作；返回结果与脚本（含收到的请求）。
pub fn drive_script_ref<O: Operation>(op: &mut O, script: &mut Script) -> Result<O::Output, Error> {
    drive_ref(op, |request| script.send(request))
}

/// 用脚本驱动操作；返回结果与脚本（含收到的请求）。
pub fn drive_script<O: Operation>(op: O, script: &mut Script) -> Result<O::Output, Error> {
    drive(op, |request| script.send(request))
}

/// 百分号解码（RFC 3986，`+` 不是空格），用于核对查询串编码。
pub fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        if bytes[at] == b'%' && at + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[at + 1..at + 3]).expect("hex 是 ASCII");
            decoded.push(u8::from_str_radix(hex, 16).expect("百分号后是两位 hex"));
            at += 3;
        } else {
            decoded.push(bytes[at]);
            at += 1;
        }
    }
    String::from_utf8(decoded).expect("解码结果是 UTF-8")
}

/// 与 Python `test_protocol.skeleton_body()` 对等的骨架响应体：3 页（第 1、3 页内容相同）、
/// 文档 title 为 `T`。
pub fn skeleton_body(contract: &str, file_uri: &str) -> Value {
    let pages_text: [&[&str]; 3] = [&["x", "y"], &["z"], &["x", "y"]];
    let mut pages = Vec::new();
    let mut page_hashes = Vec::new();
    for texts in pages_text {
        let elements: Vec<String> = texts
            .iter()
            .map(|text| {
                dpe_hash::content_hash(
                    &json!({"category": "NarrativeText", "text": text}),
                    contract,
                )
                .expect("元素必须合法")
            })
            .collect();
        let page = json!({"elements": elements});
        let page_hash =
            dpe_hash::object_hash(&page, dpe_hash::ObjectKind::Page, contract).expect("页必须合法");
        pages.push(page);
        page_hashes.push(page_hash);
    }
    let document = json!({"file_type": "md", "title": "T", "pages": page_hashes});
    let doc_hash = dpe_hash::object_hash(&document, dpe_hash::ObjectKind::Document, contract)
        .expect("文档必须合法");
    json!({
        "file_uri": file_uri,
        "doc_hash": doc_hash,
        "document": document,
        "pages": pages,
    })
}

// ---------------------------------------------------------------------- 写操作（#48）

/// 由逐页文本构造展开文档（与 Python `engine_helpers.text_doc` 对等：每段文本一个
/// NarrativeText 元素）。
pub fn doc(pages: &[&[&str]]) -> dpe_sdk::ExpandedDocument {
    let pages: Vec<Value> = pages
        .iter()
        .map(|texts| {
            json!({
                "elements": texts
                    .iter()
                    .map(|text| json!({"category": "NarrativeText", "text": text}))
                    .collect::<Vec<_>>()
            })
        })
        .collect();
    dpe_sdk::ExpandedDocument::from_value(
        &json!({"file_type": "md", "pages": pages}),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 展开文档的 doc_hash（`dpe1`）。
pub fn doc_hash(document: &dpe_sdk::ExpandedDocument) -> String {
    document
        .doc_hash(dpe_sdk::CONTRACT)
        .expect("测试文档必须合法")
}

/// 200 + JSON 体 + 附加头的响应（与 Python `ok(body, status, headers)` 对等）。
pub fn json_response_with(status: u16, body: Value, headers: &[(&str, &str)]) -> Response {
    let mut all: Vec<(String, String)> =
        vec![("Content-Type".to_owned(), "application/json".to_owned())];
    all.extend(
        headers
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
    );
    Response::new(
        status,
        all,
        serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    )
}

/// commit 响应体（Python `committed` 的体部分）：`doc_hash` 取被测文档的 hash。
pub fn commit_body(
    document: &dpe_sdk::ExpandedDocument,
    status: &str,
    added: u64,
    removed: u64,
    retained: u64,
) -> Value {
    json!({
        "status": status,
        "doc_hash": doc_hash(document),
        "delta": {"added": added, "removed": removed, "retained": retained},
    })
}

/// commit 的成功响应（Python `committed`）：created 为 201、其余 200，默认带 `DPE-Doc-Hash`。
pub fn committed(
    document: &dpe_sdk::ExpandedDocument,
    status: &str,
    added: u64,
    removed: u64,
    retained: u64,
) -> Response {
    let hash = doc_hash(document);
    json_response_with(
        if status == "created" { 201 } else { 200 },
        commit_body(document, status, added, removed, retained),
        &[(DOC_HASH_HEADER, &hash)],
    )
}

/// 经 head 确认的成功结果（与原样重发得到的 `unchanged` 相同）。
pub fn unchanged_result(
    document: &dpe_sdk::ExpandedDocument,
    retained: u64,
) -> dpe_sdk::wire::CommitResult {
    dpe_sdk::wire::CommitResult {
        status: dpe_sdk::wire::CommitStatus::Unchanged,
        doc_hash: doc_hash(document),
        delta: dpe_sdk::wire::Delta {
            added: 0,
            removed: 0,
            retained,
        },
    }
}

/// head 命中（200 + `DPE-Doc-Hash` + ETag）。
pub fn head_found(value: &str) -> Response {
    empty(
        200,
        &[(DOC_HASH_HEADER, value), ("ETag", &format!("\"{value}\""))],
    )
}

/// head 未命中（404 + `DPE-Error-Code: DPE_NOT_FOUND`）。
pub fn head_missing() -> Response {
    empty(404, &[(ERROR_CODE_HEADER, "DPE_NOT_FOUND")])
}

/// 网关自行求值条件头得到的 412：既无 problem 体、也无 `DPE-Error-Code` 头。
pub fn bare_412() -> Response {
    Response::new(
        412,
        [("Content-Type", "text/html")],
        b"<h1>412</h1>".to_vec(),
    )
}

/// 无 DPE 错误码的网关响应（502 / 504）：写操作按「结果未知」处理。
pub fn gateway(status: u16) -> Response {
    Response::new(
        status,
        [("Content-Type", "text/html")],
        b"<h1>gateway</h1>".to_vec(),
    )
}

/// move 的成功响应（默认可带附加头：`moved_with`）。
pub fn moved(value: &str) -> Response {
    moved_with(value, &[])
}

/// move 的成功响应 + 附加头。
pub fn moved_with(value: &str, headers: &[(&str, &str)]) -> Response {
    let mut all: Vec<(&str, &str)> = vec![(DOC_HASH_HEADER, value)];
    all.extend(headers.iter().copied());
    json_response_with(200, json!({"doc_hash": value}), &all)
}

// ---------------------------------------------------------------------- 暂存路径（#49）

/// 会话过期时间的字面（RFC 3339 UTC；与 Python 侧 `SESSION_EXPIRES` 对等）。
pub const SESSION_EXPIRES: &str = "2026-10-01T00:00:00Z";
/// 暂存用例的文档 uri（与 Python 侧 `URI` 对等）。
pub const SESSION_URI: &str = "s3://bucket/a.md";

/// 测试用会话（与 Python `SESSION` 对等）。
pub fn session() -> dpe_sdk::protocol::Session {
    dpe_sdk::protocol::Session {
        id: "st-1".to_owned(),
        uri: SESSION_URI.to_owned(),
        expires_at: SESSION_EXPIRES.to_owned(),
    }
}

/// 带指定 id 的会话。
pub fn session_with_id(id: &str) -> dpe_sdk::protocol::Session {
    dpe_sdk::protocol::Session {
        id: id.to_owned(),
        uri: SESSION_URI.to_owned(),
        expires_at: SESSION_EXPIRES.to_owned(),
    }
}

/// negotiate 的成功响应（Python `negotiated`）。
pub fn negotiated(sid: &str, missing_pages: &[&str], missing_content_hashes: &[&str]) -> Response {
    negotiated_at(sid, missing_pages, missing_content_hashes, SESSION_EXPIRES)
}

/// negotiate 的成功响应（自定义过期时间）。
pub fn negotiated_at(
    sid: &str,
    missing_pages: &[&str],
    missing_content_hashes: &[&str],
    expires_at: &str,
) -> Response {
    json_ok(json!({
        "missing_pages": missing_pages,
        "missing_content_hashes": missing_content_hashes,
        "staging_session": {"id": sid, "expires_at": expires_at},
    }))
}

/// upload 的成功响应（Python `uploaded`）：`202` 无体（带偏移与过期时间），`200` / `201` 的
/// 体是下一层缺失清单（`{}` 表示没有缺失）。
pub fn uploaded(status: u16, offset: Option<u64>, missing: Value) -> Response {
    let mut headers: Vec<(String, String)> = vec![(
        dpe_sdk::protocol::SESSION_EXPIRES_HEADER.to_owned(),
        SESSION_EXPIRES.to_owned(),
    )];
    if let Some(offset) = offset {
        headers.push((
            dpe_sdk::protocol::UPLOAD_OFFSET_HEADER.to_owned(),
            offset.to_string(),
        ));
    }
    if status == 202 {
        return Response::new(status, headers, Vec::new());
    }
    let mut all: Vec<(String, String)> =
        vec![("Content-Type".to_owned(), "application/json".to_owned())];
    all.extend(headers);
    Response::new(
        status,
        all,
        serde_json::to_vec(&missing).expect("JSON 值总能序列化"),
    )
}

/// 断点查询（HEAD）的 200 响应（Python `resume`）。
pub fn resume(offset: u64) -> Response {
    Response::new(
        200,
        [
            (
                dpe_sdk::protocol::UPLOAD_OFFSET_HEADER.to_owned(),
                offset.to_string(),
            ),
            (
                dpe_sdk::protocol::SESSION_EXPIRES_HEADER.to_owned(),
                SESSION_EXPIRES.to_owned(),
            ),
        ],
        Vec::new(),
    )
}

/// 分块偏移不连续的 400：`DPE_VALIDATION` + `DPE-Upload-Offset`（Python `offset_problem`）。
pub fn offset_problem(offset: u64) -> Response {
    let body = json!({
        "type": "urn:dpe:error:validation",
        "title": "validation",
        "status": 400,
        "code": "DPE_VALIDATION",
    });
    Response::new(
        400,
        [
            (
                "Content-Type".to_owned(),
                "application/problem+json".to_owned(),
            ),
            ("DPE-Error-Code".to_owned(), "DPE_VALIDATION".to_owned()),
            (
                dpe_sdk::protocol::UPLOAD_OFFSET_HEADER.to_owned(),
                offset.to_string(),
            ),
        ],
        serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    )
}

/// 测试用的 blob 字节（与 Python `IMAGE` 对等）。
pub fn image_bytes() -> Vec<u8> {
    b"binary-image".to_vec()
}

/// 测试用 blob 的引用（与 Python `REF` 对等）。
pub fn image_ref() -> String {
    dpe_hash::blob_ref(&image_bytes())
}

/// Image 元素的测试文档（Python `image_doc`）。
pub fn image_doc() -> dpe_sdk::ExpandedDocument {
    dpe_sdk::ExpandedDocument::from_value(
        &json!({
            "file_type": "md",
            "pages": [{"elements": [
                {"category": "Image", "blob": image_ref(), "mime_type": "image/png"}
            ]}],
        }),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 覆盖 limits 的能力构造（Python `caps(limits={...})` 的对等）。
pub fn caps_with_limits(overrides: &[(&str, u64)]) -> Capabilities {
    let mut body = capabilities_body();
    for (key, value) in overrides {
        body["limits"][*key] = json!(value);
    }
    Capabilities::from_value(&body).expect("限额模板必须合法")
}

/// 指定能力与重发预算的核心。
pub fn core_with_caps(capabilities: Capabilities, max_resends: u32) -> ProtocolCore {
    ProtocolCore::new(capabilities, "dpe1", max_resends).expect("模板能力与核心兼容")
}

/// 小限额的核心（Python `staged_core`：内联体放不下，强制走暂存路径）。
pub fn staged_core() -> ProtocolCore {
    core_with_caps(caps_with_limits(&[("max_payload_bytes", 120)]), 2)
}

/// 250 字节、单块 100 的核心（Python `chunked_core`）。
pub fn chunked_core(max_resends: u32) -> ProtocolCore {
    core_with_caps(
        caps_with_limits(&[("max_payload_bytes", 100), ("blob_chunk_bytes", 100)]),
        max_resends,
    )
}

/// `(起点, 长度)` 的分块序列（Python `chunk_ranges`）。
pub fn chunk_ranges(total: u64, size: u64) -> Vec<(u64, u64)> {
    (0..total)
        .step_by(size as usize)
        .map(|start| (start, size.min(total - start)))
        .collect()
}

/// 与核心相同的紧凑 JSON 长度（断言内联体尺寸用）。
pub fn wire_json_len(value: &Value) -> u64 {
    serde_json::to_vec(value).expect("JSON 值总能序列化").len() as u64
}

/// 由逐页文本构造的文档的页 hash 与元素 hash（与 Python `inline()` 对等的最小形态）。
pub fn page_and_element_hashes(
    document: &dpe_sdk::ExpandedDocument,
) -> (Vec<String>, Vec<Vec<String>>) {
    let hashes = document
        .hashes(dpe_sdk::CONTRACT)
        .expect("测试文档必须合法");
    (
        hashes
            .pages
            .iter()
            .map(|page| page.page_hash.clone())
            .collect(),
        hashes
            .pages
            .iter()
            .map(|page| page.elements.clone())
            .collect(),
    )
}

/// 展开文档的线上部件（测试侧独立实现，与核心 `_wire_parts` 等价；用于构造期望请求体）。
pub struct Parts {
    pub document: Value,
    pub pages: Vec<Value>,
    pub objects: Vec<Value>,
}

/// 见 [`Parts`]。
pub fn parts_for(document: &dpe_sdk::ExpandedDocument) -> Parts {
    let hashes = document
        .hashes(dpe_sdk::CONTRACT)
        .expect("测试文档必须合法");
    let mut pages = Vec::new();
    let mut objects = Vec::new();
    let mut seen_pages = std::collections::HashSet::new();
    let mut seen_objects = std::collections::HashSet::new();
    for (page, page_hashes) in document.pages.iter().zip(&hashes.pages) {
        if !seen_pages.insert(page_hashes.page_hash.clone()) {
            continue;
        }
        let mut wire_page = serde_json::to_value(page).expect("页总能序列化");
        wire_page["elements"] = json!(page_hashes.elements);
        pages.push(wire_page);
        for (element, content_hash) in page.elements.iter().zip(&page_hashes.elements) {
            if !seen_objects.insert(content_hash.clone()) {
                continue;
            }
            objects.push(serde_json::to_value(element).expect("元素总能序列化"));
        }
    }
    let mut document_object = serde_json::to_value(document).expect("文档总能序列化");
    document_object["pages"] = json!(hashes
        .pages
        .iter()
        .map(|page| page.page_hash.clone())
        .collect::<Vec<_>>());
    Parts {
        document: document_object,
        pages,
        objects,
    }
}
