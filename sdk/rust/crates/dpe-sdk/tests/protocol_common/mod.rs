//! 协议核心测试的辅助：能力构造、按顺序回放的假服务端与响应构造（经模块声明共享）。
//!
//! 与 Python 侧 `tests/protocol_helpers.py` 对等；响应按 HTTP 绑定 §4、§5 的报文形状手工构造。

#![allow(dead_code)] // 各测试文件只用到其中一部分

use std::collections::VecDeque;
use std::fmt;

use dpe_sdk::protocol::{
    drive, Operation, ProtocolCore, Request, Response, TransportError, DEFAULT_MAX_RESENDS,
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
    steps: VecDeque<Result<Response, &'static str>>,
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

    pub fn lost(mut self, note: &'static str) -> Self {
        self.steps.push_back(Err(note));
        self
    }

    pub fn send(&mut self, request: Request) -> Result<Response, TransportError> {
        self.requests.push(request);
        match self.steps.pop_front().expect("脚本没有更多步骤") {
            Ok(response) => Ok(response),
            Err(note) => Err(Box::new(Lost(note))),
        }
    }
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
