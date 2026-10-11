//! sans-IO 协议核心的写操作（#48）：快路径 commit、CAS、delete / move 与重试规则（core §5.2）。
//!
//! 与 Python `tests/test_protocol_write.py`（#48 / PR #82，含 #81 的裸 412 新口径）的场景逐条
//! 对等；「对参考服务端引擎」一组除外——Rust SDK 尚无内存版参考服务端，随 `testing` 模块落地。

mod protocol_common;

use dpe_sdk::protocol::{
    Method, Operation, Precondition, Step, CONTRACT_HEADER, DOC_HASH_HEADER, ERROR_CODE_HEADER,
};
use dpe_sdk::wire::{CommitResult, CommitStatus, Delta, MoveResult};
use dpe_sdk::{Error, ErrorKind, ExpandedDocument};
use serde_json::{json, Value};

use protocol_common::{
    bare_412, capabilities_body, caps_from, commit_body, committed, core, core_from,
    core_with_resends, doc, doc_hash, empty, gateway, h1, h2, h_drill, head_found, head_missing,
    json_ok, json_response_with, moved, moved_with, problem, run, unchanged_result, Lost, Script,
};

const URI: &str = "s3://bucket/a.md";
const TARGET: &str = "documents?uri=s3%3A%2F%2Fbucket%2Fa.md";

/// 与 Python `test_protocol_write.DOC` 对等：一页两个元素的文档（元素总数 2）。
fn test_doc() -> ExpandedDocument {
    doc(&[&["x", "y"]])
}

/// 由 dpe-hash 原语独立算出的 `["x","y"]` 文档快路径请求体与 doc_hash（不经 `wire_parts`，
/// 防同源错误互相掩盖）。
fn expected_xy_body() -> (Value, String) {
    let element_x = dpe_hash::content_hash(
        &json!({"category": "NarrativeText", "text": "x"}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let element_y = dpe_hash::content_hash(
        &json!({"category": "NarrativeText", "text": "y"}),
        dpe_sdk::CONTRACT,
    )
    .unwrap();
    let page = json!({"elements": [element_x, element_y]});
    let page_hash =
        dpe_hash::object_hash(&page, dpe_hash::ObjectKind::Page, dpe_sdk::CONTRACT).unwrap();
    let document = json!({"file_type": "md", "pages": [page_hash]});
    let doc = dpe_hash::object_hash(&document, dpe_hash::ObjectKind::Document, dpe_sdk::CONTRACT)
        .unwrap();
    (
        json!({
            "document": document,
            "pages": [page],
            "objects": [
                {"category": "NarrativeText", "text": "x"},
                {"category": "NarrativeText", "text": "y"},
            ],
        }),
        doc,
    )
}

/// 驱动 `&mut` 操作到下一个请求（状态机误用用例用）。
fn next_send<O: Operation>(op: &mut O) -> dpe_sdk::Request
where
    O::Output: std::fmt::Debug,
{
    match op.poll() {
        Ok(Step::Send(request)) => request,
        other => panic!("期望 Send，实际 {other:?}"),
    }
}

/// 期望操作失败并返回错误。
fn expect_error<T: std::fmt::Debug>(result: Result<T, Error>) -> Error {
    match result {
        Ok(value) => panic!("期望失败，实际 {value:?}"),
        Err(error) => error,
    }
}

/// 请求体解析为 JSON 值。
fn body_json(request: &dpe_sdk::Request) -> Value {
    match &request.body {
        dpe_sdk::protocol::Body::Bytes(bytes) => {
            serde_json::from_slice(bytes).expect("请求体是 JSON")
        }
        other => panic!("期望 JSON 请求体，实际 {other:?}"),
    }
}

fn methods(requests: &[dpe_sdk::Request]) -> Vec<&'static str> {
    requests
        .iter()
        .map(|request| request.method.as_str())
        .collect()
}

fn first_request<O: dpe_sdk::Operation>(mut op: O) -> dpe_sdk::Request
where
    O::Output: std::fmt::Debug,
{
    match op.poll() {
        Ok(dpe_sdk::Step::Send(request)) => request,
        other => panic!("期望先产出请求，实际 {other:?}"),
    }
}

/// 每个写请求都是 `retry_safe = false`：请求可能已发出之后传输层 MUST NOT 自行重发
/// （negotiate / upload_* 随 Part 3 落地后在同名测试补断言）。
#[test]
fn write_requests_are_not_retry_safe() {
    let c = core("dpe1");
    let document = test_doc();
    let requests = [
        first_request(c.commit(URI, &document, Precondition::IfAbsent).unwrap()),
        first_request(
            c.commit(URI, &document, Precondition::BaseHash(h1()))
                .unwrap(),
        ),
        first_request(c.commit(URI, &document, Precondition::Force).unwrap()),
        first_request(c.delete(URI, &h1()).unwrap()),
        first_request(c.move_document(URI, "s3://bucket/b.md", &h1()).unwrap()),
    ];
    for request in requests {
        assert!(!request.retry_safe, "{request:?}");
    }
}

// ---------------------------------------------------------------------- commit：请求形状

#[test]
fn commit_if_absent_request() {
    let document = test_doc();
    let (result, server) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        [committed(&document, "created", 2, 0, 0)],
    );
    assert_eq!(result.unwrap().status, CommitStatus::Created);
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("PUT", TARGET)
    );
    assert_eq!(request.headers.get(CONTRACT_HEADER), Some("dpe1"));
    assert_eq!(request.headers.get("If-None-Match"), Some("*"));
    assert_eq!(request.headers.get("If-Match"), None);
    assert_eq!(
        request.headers.get("Content-Type"),
        Some("application/json")
    );
    // 请求体与由 dpe-hash 原语独立构造的期望体全等（document.pages 是 page_hash 序列、
    // 页对象的 elements 已换成 content_hash 列表、objects 是元素对象本身）
    let (expected, doc) = expected_xy_body();
    assert_eq!(body_json(request), expected);
    assert_eq!(doc, doc_hash(&document), "独立算出的 doc_hash 应一致");
}

/// 条件头由客户端用 doc_hash 自行构造（HTTP 绑定 §3.1），不回显 ETag。
#[test]
fn commit_base_hash_builds_if_match_itself() {
    let document = test_doc();
    let one = h1();
    let (_, server) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(one.clone()))
            .unwrap(),
        [committed(&document, "updated", 1, 1, 1)],
    );
    let request = &server.requests[0];
    assert_eq!(
        request.headers.get("If-Match"),
        Some(format!("\"{one}\"").as_str())
    );
    assert_eq!(request.headers.get("If-None-Match"), None);
    assert!(body_json(request).get("force").is_none());
}

#[test]
fn commit_force_has_no_condition_header() {
    let document = test_doc();
    let (_, server) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        [committed(&document, "updated", 1, 1, 1)],
    );
    let request = &server.requests[0];
    assert_eq!(request.headers.get("If-Match"), None);
    assert_eq!(request.headers.get("If-None-Match"), None);
    assert_eq!(body_json(request)["force"], Value::Bool(true));
}

/// 重复的页对象与元素对象各内联一份；文档对象仍按完整序列计。
#[test]
fn commit_dedups_pages_and_objects() {
    let document = doc(&[&["x", "y"], &["x", "y"], &["y", "z"]]);
    let response = json_response_with(201, commit_body(&document, "created", 6, 0, 0), &[]);
    let (result, server) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        [response],
    );
    assert_eq!(result.unwrap().status, CommitStatus::Created);
    let body = body_json(&server.requests[0]);
    // document.pages 是完整 hash 序列（前两页同对象），页对象的 elements 已换成 hash 列表
    let content = |text: &str| {
        dpe_hash::content_hash(
            &json!({"category": "NarrativeText", "text": text}),
            dpe_sdk::CONTRACT,
        )
        .unwrap()
    };
    let (x, y, z) = (content("x"), content("y"), content("z"));
    let page = |elements: Vec<String>| json!({"elements": elements});
    let page_hash = |elements: Vec<String>| {
        dpe_hash::object_hash(
            &page(elements),
            dpe_hash::ObjectKind::Page,
            dpe_sdk::CONTRACT,
        )
        .unwrap()
    };
    assert_eq!(
        body["document"]["pages"],
        json!([
            page_hash(vec![x.clone(), y.clone()]),
            page_hash(vec![x.clone(), y.clone()]),
            page_hash(vec![y.clone(), z.clone()]),
        ])
    );
    assert_eq!(
        body["pages"],
        json!([page(vec![x.clone(), y.clone()]), page(vec![y, z.clone()])])
    );
    assert_eq!(
        body["objects"],
        json!([
            {"category": "NarrativeText", "text": "x"},
            {"category": "NarrativeText", "text": "y"},
            {"category": "NarrativeText", "text": "z"},
        ])
    );
}

#[test]
fn base_hash_must_belong_to_the_declared_contract() {
    let c = core("dpe1");
    let document = test_doc();
    let cases: [(&str, ErrorKind); 3] = [
        (&"1".repeat(64), ErrorKind::ContractUnsupported), // 裸 hash
        (&h_drill(), ErrorKind::ContractUnsupported),      // 不是声明的契约
        ("dpe1:XYZ", ErrorKind::Validation),
    ];
    for (value, kind) in cases {
        let error =
            expect_error(c.commit(URI, &document, Precondition::BaseHash(value.to_owned())));
        assert_eq!(error.kind(), kind, "{value}");
        let error = expect_error(c.delete(URI, value));
        assert_eq!(error.kind(), kind, "{value}");
        let error = expect_error(c.move_document(URI, "s3://b/c", value));
        assert_eq!(error.kind(), kind, "{value}");
    }
}

#[test]
fn write_operations_validate_uris_locally() {
    let c = core("dpe1");
    let document = test_doc();
    let one = h1();
    assert_eq!(
        expect_error(c.commit("no scheme", &document, Precondition::IfAbsent)).kind(),
        ErrorKind::Validation
    );
    assert_eq!(
        expect_error(c.delete("no scheme", &one)).kind(),
        ErrorKind::Validation
    );
    assert_eq!(
        expect_error(c.move_document("no scheme", URI, &one)).kind(),
        ErrorKind::Validation
    );
    assert_eq!(
        expect_error(c.move_document(URI, "no scheme", &one)).kind(),
        ErrorKind::Validation
    );
}

#[test]
fn commit_under_drill_contract() {
    let mut caps_body = capabilities_body();
    caps_body["hash_contracts"] = json!(["dpe1", "dpe2"]);
    let c = core_from(caps_from(&caps_body).unwrap(), "dpe2").unwrap();
    let document = test_doc();
    let expected = document.doc_hash("dpe2").unwrap();
    let response = json_response_with(
        201,
        json!({
            "status": "created",
            "doc_hash": expected,
            "delta": {"added": 2, "removed": 0, "retained": 0},
        }),
        &[],
    );
    let (result, server) = run(
        c.commit(URI, &document, Precondition::IfAbsent).unwrap(),
        [response],
    );
    assert_eq!(result.unwrap().doc_hash, expected);
    assert_eq!(
        server.requests[0].headers.get(CONTRACT_HEADER),
        Some("dpe2")
    );
}

// ---------------------------------------------------------------------- commit：结果校验

#[test]
fn commit_results() {
    let document = test_doc();
    for response in [
        committed(&document, "created", 2, 0, 0),
        committed(&document, "updated", 1, 3, 1),
        committed(&document, "unchanged", 0, 0, 2),
    ] {
        let (result, _) = run(
            core("dpe1")
                .commit(URI, &document, Precondition::BaseHash(h1()))
                .unwrap(),
            [response],
        );
        assert_eq!(result.unwrap().doc_hash, doc_hash(&document));
    }
}

#[test]
fn commit_rejects_inconsistent_results() {
    let document = test_doc();
    let one = h1();
    let responses = vec![
        json_response_with(200, commit_body(&document, "created", 2, 0, 0), &[]),
        json_response_with(201, commit_body(&document, "updated", 1, 0, 1), &[]),
        json_response_with(
            200,
            json!({
                "status": "updated",
                "doc_hash": one,
                "delta": {"added": 1, "removed": 0, "retained": 1},
            }),
            &[],
        ),
        committed(&document, "updated", 1, 0, 0), // added + retained ≠ 元素个数
        committed(&document, "created", 1, 0, 1), // created 时 retained 应为 0
        committed(&document, "created", 2, 1, 0), // created 时 removed 应为 0
        committed(&document, "unchanged", 1, 0, 1), // unchanged 时 added 应为 0
        json_response_with(
            200,
            commit_body(&document, "updated", 1, 0, 1),
            &[(DOC_HASH_HEADER, &one)],
        ),
        json_ok(json!({"status": "updated"})),
        empty(204, &[]),
    ];
    for (i, response) in responses.into_iter().enumerate() {
        let (result, _) = run(
            core("dpe1")
                .commit(URI, &document, Precondition::BaseHash(h1()))
                .unwrap(),
            [response],
        );
        let error = expect_error(result);
        assert_eq!(error.kind(), ErrorKind::UnexpectedResponse, "第 {i} 个响应");
    }

    // 大计数不得加法溢出：u64::MAX + 2 在 debug 下不 panic、按不自洽上报（与 Python 的
    // 任意精度行为一致，均报 UnexpectedResponse）
    let response = json_response_with(
        200,
        json!({
            "status": "updated",
            "doc_hash": doc_hash(&document),
            "delta": {"added": u64::MAX, "removed": 0, "retained": 2},
        }),
        &[],
    );
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        [response],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);

    // 负数计数（Python `committed("updated", -1, 0, 3)`）：wire 层直接不合规
    let response = json_response_with(
        200,
        json!({
            "status": "updated",
            "doc_hash": doc_hash(&document),
            "delta": {"added": -1, "removed": 0, "retained": 3},
        }),
        &[],
    );
    let (result, _) = run(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        [response],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn commit_errors_are_raised_as_is() {
    // 按 code 分派、如实上报：不重发、不 head，绝不改用 force（core §5.2）
    let document = test_doc();
    let cases: [(&str, u16, ErrorKind); 6] = [
        (
            "DPE_PRECONDITION_FAILED",
            412,
            ErrorKind::PreconditionFailed,
        ),
        ("DPE_ALREADY_EXISTS", 412, ErrorKind::AlreadyExists),
        ("DPE_NOT_FOUND", 412, ErrorKind::NotFound),
        ("DPE_MISSING_CONTENT", 400, ErrorKind::MissingContent),
        ("DPE_FORBIDDEN", 403, ErrorKind::Forbidden),
        ("DPE_PAYLOAD_TOO_LARGE", 413, ErrorKind::PayloadTooLarge),
    ];
    for (code, status, kind) in cases {
        let mut script = Script::new().response(problem(code, status, Value::Null));
        let result = protocol_common::drive_script(
            core("dpe1")
                .commit(URI, &document, Precondition::BaseHash(h1()))
                .unwrap(),
            &mut script,
        );
        assert_eq!(expect_error(result).kind(), kind, "{code}");
        assert_eq!(methods(&script.requests), vec!["PUT"], "{code}");
    }
}

// ---------------------------------------------------------------------- commit：响应丢失

#[test]
fn commit_lost_response_is_resent_and_recovers_as_unchanged() {
    let document = test_doc();
    let mut script =
        Script::new()
            .lost("响应丢失")
            .response(committed(&document, "unchanged", 0, 0, 2));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(script.requests[0], script.requests[1]);
}

#[test]
fn commit_resend_then_precondition_failed_is_reported() {
    let document = test_doc();
    let mut script = Script::new().lost("响应丢失").response(problem(
        "DPE_PRECONDITION_FAILED",
        412,
        Value::Null,
    ));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::PreconditionFailed);
    assert_eq!(methods(&script.requests), vec!["PUT", "PUT"]);
}

#[test]
fn commit_resends_are_bounded() {
    let document = test_doc();
    for max_resends in [0u32, 1, 3] {
        let mut script = Script::new();
        for _ in 0..=max_resends {
            script = script.lost("响应丢失");
        }
        let c = core_with_resends(
            caps_from(&capabilities_body()).unwrap(),
            "dpe1",
            max_resends,
        )
        .unwrap();
        let result = protocol_common::drive_script(
            c.commit(URI, &document, Precondition::IfAbsent).unwrap(),
            &mut script,
        );
        let error = expect_error(result);
        assert_eq!(
            error.kind(),
            ErrorKind::Transport,
            "max_resends={max_resends}"
        );
        assert_eq!(
            methods(&script.requests),
            vec!["PUT"; (max_resends + 1) as usize]
        );
    }
}

// ---------------------------------------------------------------------- commit：force

#[test]
fn force_lost_response_is_not_replayed_head_confirms() {
    let document = test_doc();
    let mut script = Script::new()
        .lost("响应丢失")
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
    assert_eq!(script.requests[1].target, TARGET);
}

#[test]
fn force_lost_response_not_confirmed_is_reported() {
    let document = test_doc();
    for current in [Some(h1()), None] {
        let mut script = Script::new().lost("put").response(match &current {
            Some(value) => head_found(value),
            None => head_missing(),
        });
        let result = protocol_common::drive_script(
            core("dpe1")
                .commit(URI, &document, Precondition::Force)
                .unwrap(),
            &mut script,
        );
        let error = expect_error(result);
        assert_eq!(error.kind(), ErrorKind::ForceNotConfirmed);
        let payload = error.force_not_confirmed().expect("ForceNotConfirmed 载荷");
        assert_eq!(payload.doc_hash, doc_hash(&document));
        assert_eq!(payload.current, current);
        assert!(payload.checked);
        assert!(!error.is_retryable());
        // cause 是那一次 PUT 的响应丢失
        let cause = std::error::Error::source(&error).expect("cause");
        assert_eq!(cause.to_string(), "响应丢失：put");
        assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
    }
}

/// head 本身的响应丢失按只读重发；仍失败则无法确认（`checked = false`），同样不重放 force。
#[test]
fn force_lost_response_and_head_failure() {
    let document = test_doc();
    let mut script = Script::new()
        .lost("put")
        .lost("head 1")
        .lost("head 2")
        .lost("head 3");
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::ForceNotConfirmed);
    let payload = error.force_not_confirmed().expect("ForceNotConfirmed 载荷");
    assert!(!payload.checked);
    assert!(payload.current.is_none());
    let cause = std::error::Error::source(&error).expect("cause");
    assert_eq!(cause.to_string(), "响应丢失：head 3");
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "HEAD", "HEAD", "HEAD"]
    );
}

#[test]
fn force_head_resend_then_confirms() {
    let document = test_doc();
    let mut script = Script::new()
        .lost("put")
        .lost("head 1")
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD", "HEAD"]);
}

#[test]
fn force_head_error_is_not_confirmed() {
    let document = test_doc();
    let mut script =
        Script::new()
            .lost("put")
            .response(problem("DPE_UNAVAILABLE", 503, Value::Null));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::ForceNotConfirmed);
    assert!(!error.force_not_confirmed().expect("载荷").checked);
    let cause = std::error::Error::source(&error).expect("cause");
    assert_eq!(
        cause.downcast_ref::<Error>().map(Error::kind),
        Some(ErrorKind::Unavailable)
    );
}

// ---------------------------------------------------------------------- commit：无 DPE 错误码的 412

#[test]
fn commit_bare_412_head_equal_is_success() {
    let document = test_doc();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
}

#[test]
fn commit_bare_412_is_judged_by_head() {
    let document = test_doc();
    let cases: [(Precondition, bool, ErrorKind); 3] = [
        (Precondition::BaseHash(h1()), false, ErrorKind::NotFound),
        (
            Precondition::BaseHash(h1()),
            true,
            ErrorKind::PreconditionFailed,
        ),
        (Precondition::IfAbsent, true, ErrorKind::AlreadyExists),
    ];
    for (precondition, found, kind) in cases {
        let mut script = Script::new().response(bare_412()).response(if found {
            head_found(&h2())
        } else {
            head_missing()
        });
        let result = protocol_common::drive_script(
            core("dpe1").commit(URI, &document, precondition).unwrap(),
            &mut script,
        );
        assert_eq!(expect_error(result).kind(), kind);
        assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
    }
}

/// `If-None-Match: *` 且 doc_hash 等于提交内容：unchanged 先于条件求值（§3.2），直接成功。
#[test]
fn commit_if_absent_bare_412_head_equal_is_success() {
    let document = test_doc();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
}

/// `If-None-Match: *` 且文档不存在：前置条件实际成立，412 是中间层误判，原样重发一次。
#[test]
fn commit_if_absent_bare_412_head_missing_resends_once() {
    let document = test_doc();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_missing())
        .response(committed(&document, "created", 2, 0, 0));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap().status, CommitStatus::Created);
    let (put, head, again) = (
        &script.requests[0],
        &script.requests[1],
        &script.requests[2],
    );
    assert_eq!(head.method.as_str(), "HEAD");
    assert_eq!(again, put);
}

/// 重发后仍是无错误码的 412 且文档仍不存在：按非协议错误上报，MUST NOT 报 `DPE_NOT_FOUND`。
#[test]
fn commit_if_absent_bare_412_twice_head_still_missing_is_reported() {
    let document = test_doc();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_missing())
        .response(bare_412())
        .response(head_missing());
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(412));
    assert!(error.message().contains("if_absent"), "{error}");
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "HEAD", "PUT", "HEAD"]
    );
}

/// 重发前他人创建了文档：第二次 412 再经 head 判定，不误报为不合规响应。
#[test]
fn commit_if_absent_bare_412_twice_is_judged_by_head_again() {
    let document = test_doc();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_missing())
        .response(bare_412())
        .response(head_found(&h2()));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::AlreadyExists);

    let mut script = Script::new()
        .response(bare_412())
        .response(head_missing())
        .response(bare_412())
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(
        methods(&script.requests),
        vec!["PUT", "HEAD", "PUT", "HEAD"]
    );
}

#[test]
fn commit_lost_then_bare_412_is_judged_by_head() {
    let document = test_doc();
    let mut script = Script::new()
        .lost("响应丢失")
        .response(bare_412())
        .response(head_found(&doc_hash(&document)));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));
    assert_eq!(methods(&script.requests), vec!["PUT", "PUT", "HEAD"]);
}

#[test]
fn commit_412_with_error_code_header_is_not_bare() {
    let document = test_doc();
    let mut script = Script::new().response(empty(
        412,
        &[(ERROR_CODE_HEADER, "DPE_PRECONDITION_FAILED")],
    ));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::PreconditionFailed);
    assert_eq!(methods(&script.requests), vec!["PUT"]);
}

/// force 不带条件头，412 无从谈起：不回退 head，按非协议错误上报。
#[test]
fn force_bare_412_is_unexpected() {
    let document = test_doc();
    let mut script = Script::new().response(bare_412());
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(methods(&script.requests), vec!["PUT"]);
}

// ---------------------------------------------------------------------- delete

#[test]
fn delete_request() {
    let one = h1();
    let (result, server) = run(core("dpe1").delete(URI, &one).unwrap(), [empty(204, &[])]);
    result.unwrap();
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("DELETE", TARGET)
    );
    assert_eq!(request.body, dpe_sdk::protocol::Body::None);
    assert_eq!(
        request.headers.get("If-Match"),
        Some(format!("\"{one}\"").as_str())
    );
    assert_eq!(request.headers.get(CONTRACT_HEADER), Some("dpe1"));
}

#[test]
fn delete_not_found_on_first_attempt_is_reported() {
    let (result, _) = run(
        core("dpe1").delete(URI, &h1()).unwrap(),
        [problem("DPE_NOT_FOUND", 412, Value::Null)],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::NotFound);
}

/// 响应丢失后重发得到 `DPE_NOT_FOUND`：目标状态「不存在」已达成（core §5.2）。
#[test]
fn delete_resend_not_found_is_success() {
    let mut script =
        Script::new()
            .lost("响应丢失")
            .response(problem("DPE_NOT_FOUND", 412, Value::Null));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    result.unwrap();
    assert_eq!(methods(&script.requests), vec!["DELETE", "DELETE"]);
}

#[test]
fn delete_resend_succeeds() {
    let mut script = Script::new().lost("响应丢失").response(empty(204, &[]));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    result.unwrap();
    assert_eq!(methods(&script.requests), vec!["DELETE", "DELETE"]);
}

#[test]
fn delete_resend_precondition_failed_is_reported() {
    let mut script = Script::new().lost("响应丢失").response(problem(
        "DPE_PRECONDITION_FAILED",
        412,
        Value::Null,
    ));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    assert_eq!(expect_error(result).kind(), ErrorKind::PreconditionFailed);
}

#[test]
fn delete_resends_are_bounded() {
    let mut script = Script::new().lost("1").lost("2").lost("3");
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    assert_eq!(expect_error(result).kind(), ErrorKind::Transport);
    assert_eq!(methods(&script.requests), vec!["DELETE"; 3]);
}

#[test]
fn delete_bare_412_head_missing_is_success() {
    let mut script = Script::new().response(bare_412()).response(head_missing());
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    result.unwrap();
    assert_eq!(methods(&script.requests), vec!["DELETE", "HEAD"]);
}

#[test]
fn delete_bare_412_head_differs_is_precondition_failed() {
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&h2()));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    assert_eq!(expect_error(result).kind(), ErrorKind::PreconditionFailed);
    assert_eq!(methods(&script.requests), vec!["DELETE", "HEAD"]);
}

/// doc_hash 仍等于 `If-Match`：中间层误判，原样重发一次（HTTP 绑定 §3.2）。
#[test]
fn delete_bare_412_head_equal_resends_once() {
    let one = h1();
    for response in [empty(204, &[]), problem("DPE_NOT_FOUND", 412, Value::Null)] {
        let mut script = Script::new()
            .response(bare_412())
            .response(head_found(&one))
            .response(response);
        let result =
            protocol_common::drive_script(core("dpe1").delete(URI, &one).unwrap(), &mut script);
        result.unwrap();
        let (first, head, again) = (
            &script.requests[0],
            &script.requests[1],
            &script.requests[2],
        );
        assert_eq!(head.method.as_str(), "HEAD");
        assert_eq!(again, first);
    }
}

#[test]
fn delete_bare_412_twice_with_head_still_equal_is_reported() {
    let one = h1();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&one))
        .response(bare_412())
        .response(head_found(&one));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &one).unwrap(), &mut script);
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(412));
    assert_eq!(
        methods(&script.requests),
        vec!["DELETE", "HEAD", "DELETE", "HEAD"]
    );
}

/// 重发前他人改写或删除了文档：第二次 412 再经 head 判定，不误报为不合规响应。
#[test]
fn delete_bare_412_twice_is_judged_by_head_again() {
    let one = h1();
    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&one))
        .response(bare_412())
        .response(head_found(&h2()));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &one).unwrap(), &mut script);
    assert_eq!(expect_error(result).kind(), ErrorKind::PreconditionFailed);

    let mut script = Script::new()
        .response(bare_412())
        .response(head_found(&one))
        .response(bare_412())
        .response(head_missing());
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &one).unwrap(), &mut script);
    result.unwrap();
    assert_eq!(
        methods(&script.requests),
        vec!["DELETE", "HEAD", "DELETE", "HEAD"]
    );
}

#[test]
fn delete_lost_then_bare_412_head_missing_is_success() {
    let mut script = Script::new()
        .lost("响应丢失")
        .response(bare_412())
        .response(head_missing());
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    result.unwrap();
    assert_eq!(methods(&script.requests), vec!["DELETE", "DELETE", "HEAD"]);
}

// ---------------------------------------------------------------------- 无 DPE 错误码的 502 / 504

/// 网关已转发、源站结果未知：force 不重放，经 head 确认（core §5.2）。
#[test]
fn force_gateway_timeout_is_confirmed_by_head() {
    let document = test_doc();
    for status in [502u16, 504] {
        let mut script = Script::new()
            .response(gateway(status))
            .response(head_found(&doc_hash(&document)));
        let result = protocol_common::drive_script(
            core("dpe1")
                .commit(URI, &document, Precondition::Force)
                .unwrap(),
            &mut script,
        );
        assert_eq!(result.unwrap(), unchanged_result(&document, 2));
        assert_eq!(methods(&script.requests), vec!["PUT", "HEAD"]);
    }
}

#[test]
fn force_gateway_timeout_not_confirmed() {
    let document = test_doc();
    let mut script = Script::new()
        .response(gateway(504))
        .response(head_found(&h1()));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::ForceNotConfirmed);
    assert!(error.force_not_confirmed().expect("载荷").checked);
    let cause = std::error::Error::source(&error).expect("cause");
    let cause = cause.downcast_ref::<Error>().expect("cause 是 SDK 错误");
    assert_eq!(cause.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(cause.status(), Some(504));
}

#[test]
fn gateway_timeout_is_resent_for_cas_writes() {
    let document = test_doc();
    let mut script =
        Script::new()
            .response(gateway(504))
            .response(committed(&document, "unchanged", 0, 0, 2));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::BaseHash(h1()))
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), unchanged_result(&document, 2));

    let mut script =
        Script::new()
            .response(gateway(502))
            .response(problem("DPE_NOT_FOUND", 412, Value::Null));
    let result =
        protocol_common::drive_script(core("dpe1").delete(URI, &h1()).unwrap(), &mut script);
    result.unwrap();

    let one = h1();
    let mut script = Script::new().response(gateway(504)).response(moved(&one));
    let result = protocol_common::drive_script(
        core("dpe1")
            .move_document(URI, "s3://bucket/b.md", &one)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), MoveResult { doc_hash: one });
}

#[test]
fn gateway_timeout_resends_are_bounded() {
    let document = test_doc();
    let mut script = Script::new()
        .response(gateway(504))
        .response(gateway(504))
        .response(gateway(504));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::IfAbsent)
            .unwrap(),
        &mut script,
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(504));
    assert_eq!(methods(&script.requests), vec!["PUT"; 3]);
}

/// 带 DPE 错误码的 502 / 504 是服务端给出的结果，按 code 分派，不当作响应丢失。
#[test]
fn gateway_status_with_error_code_is_a_dpe_error() {
    let document = test_doc();
    let mut script = Script::new().response(problem("DPE_UNAVAILABLE", 504, Value::Null));
    let result = protocol_common::drive_script(
        core("dpe1")
            .commit(URI, &document, Precondition::Force)
            .unwrap(),
        &mut script,
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::Unavailable);
    assert_eq!(methods(&script.requests), vec!["PUT"]);
}

#[test]
fn delete_unexpected_status() {
    let (result, _) = run(
        core("dpe1").delete(URI, &h1()).unwrap(),
        [json_ok(json!({}))],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

// ---------------------------------------------------------------------- move

#[test]
fn move_request() {
    let one = h1();
    let (result, server) = run(
        core("dpe1")
            .move_document(URI, "s3://bucket/b.md", &one)
            .unwrap(),
        [moved(&one)],
    );
    assert_eq!(
        result.unwrap(),
        MoveResult {
            doc_hash: one.clone()
        }
    );
    let request = &server.requests[0];
    assert_eq!(
        (request.method.as_str(), request.target.as_str()),
        ("POST", "move")
    );
    assert_eq!(request.headers.get("If-Match"), None);
    assert_eq!(
        body_json(request),
        json!({"from_uri": URI, "to_uri": "s3://bucket/b.md", "base_hash": one})
    );
}

#[test]
fn move_rejects_inconsistent_results() {
    let one = h1();
    let responses = vec![
        moved(&h2()),
        moved_with(&one, &[(DOC_HASH_HEADER, &h2())]),
        json_ok(json!({})),
        empty(204, &[]),
    ];
    for response in responses {
        let (result, _) = run(
            core("dpe1")
                .move_document(URI, "s3://bucket/b.md", &one)
                .unwrap(),
            [response],
        );
        assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
    }
}

/// 服务端对重发按「目标状态已达成」返回成功（core §5.2 第 4 步）。
#[test]
fn move_lost_response_is_resent() {
    let one = h1();
    let mut script = Script::new().lost("1").lost("2").response(moved(&one));
    let result = protocol_common::drive_script(
        core("dpe1")
            .move_document(URI, "s3://bucket/b.md", &one)
            .unwrap(),
        &mut script,
    );
    assert_eq!(result.unwrap(), MoveResult { doc_hash: one });
    assert_eq!(script.requests.len(), 3);
    let bodies: Vec<Value> = script.requests.iter().map(body_json).collect();
    assert_eq!(bodies[0], bodies[1]);
    assert_eq!(bodies[1], bodies[2]);
}

/// 首次 move 之后目标又被改写或删除，重发得到的错误如实上报（core §5.2）。
#[test]
fn move_resend_errors_are_reported() {
    let one = h1();
    let cases: [(&str, u16, ErrorKind); 3] = [
        ("DPE_NOT_FOUND", 404, ErrorKind::NotFound),
        (
            "DPE_PRECONDITION_FAILED",
            409,
            ErrorKind::PreconditionFailed,
        ),
        ("DPE_ALREADY_EXISTS", 409, ErrorKind::AlreadyExists),
    ];
    for (code, status, kind) in cases {
        let mut script =
            Script::new()
                .lost("响应丢失")
                .response(problem(code, status, Value::Null));
        let result = protocol_common::drive_script(
            core("dpe1")
                .move_document(URI, "s3://bucket/b.md", &one)
                .unwrap(),
            &mut script,
        );
        assert_eq!(expect_error(result).kind(), kind, "{code}");
    }
}

// ---------------------------------------------------------------------- 状态机：误用与终态

/// 与 Part 1 的 `state_machine_misuse_returns_invalid_argument` 对等：写操作的四个阶段
/// （Submit / HeadAfter412 / Resend412 / ConfirmForce）同样拒绝误用并在 `Err` 后进入终态。
#[test]
fn write_state_machines_reject_misuse_and_are_terminal() {
    let c = core("dpe1");
    let document = test_doc();
    let one = h1();

    // 等待响应时 poll：报错但不改变状态（在飞响应随后仍可回送）
    let mut op = c.commit(URI, &document, Precondition::IfAbsent).unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    op.on_response(committed(&document, "created", 2, 0, 0))
        .expect("用法错误后仍可回送");
    assert!(matches!(op.poll(), Ok(Step::Done(_))));
    // 完成后继续 poll / 再回送都是用法错误
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    assert_eq!(
        expect_error(op.on_response(empty(204, &[]))).kind(),
        ErrorKind::InvalidArgument
    );

    // 未发出请求就回送
    let mut op = c.delete(URI, &one).unwrap();
    assert_eq!(
        expect_error(op.on_response(empty(204, &[]))).kind(),
        ErrorKind::InvalidArgument
    );
    let mut op = c.move_document(URI, "s3://bucket/b.md", &one).unwrap();
    assert_eq!(
        expect_error(op.on_transport_error(Box::new(Lost("x")))).kind(),
        ErrorKind::InvalidArgument
    );

    // 裸 412 链：HEAD 在飞时 poll 报错、状态保持，随后照常续走
    let mut op = c.commit(URI, &document, Precondition::IfAbsent).unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_response(bare_412()).expect("回送裸 412");
    assert_eq!(next_send(&mut op).method, Method::Head);
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    op.on_response(head_missing()).expect("回送 HEAD");
    assert_eq!(next_send(&mut op).method, Method::Put);
    op.on_response(committed(&document, "created", 2, 0, 0))
        .expect("回送重发结果");
    assert!(matches!(op.poll(), Ok(Step::Done(_))));

    // 业务错误后终态：poll / on_response / on_transport_error 都是用法错误
    let mut op = c
        .commit(URI, &document, Precondition::BaseHash(one.clone()))
        .unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    expect_error(op.on_response(problem("DPE_FORBIDDEN", 403, Value::Null)));
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    assert_eq!(
        expect_error(op.on_response(empty(200, &[]))).kind(),
        ErrorKind::InvalidArgument
    );
    assert_eq!(
        expect_error(op.on_transport_error(Box::new(Lost("x")))).kind(),
        ErrorKind::InvalidArgument
    );

    // 传输错误预算用尽后终态（delete：1 次首发 + max_resends 次重发）
    let mut op = c.delete(URI, &one).unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_transport_error(Box::new(Lost("1")))
        .expect("预算内原样重发");
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_transport_error(Box::new(Lost("2")))
        .expect("预算内原样重发");
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    expect_error(op.on_transport_error(Box::new(Lost("3"))));
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);

    // force 的 head 确认阶段：在飞时 poll 报错，随后照常确认成功
    let mut op = c.commit(URI, &document, Precondition::Force).unwrap();
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_transport_error(Box::new(Lost("put")))
        .expect("进入 head 确认");
    assert_eq!(next_send(&mut op).method, Method::Head);
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    op.on_response(head_found(&doc_hash(&document)))
        .expect("回送 HEAD");
    assert!(matches!(op.poll(), Ok(Step::Done(_))));
}

// ---------------------------------------------------------------------- wire：CommitResult / MoveResult

#[test]
fn commit_result_wire_shape() {
    let body = json!({
        "status": "created",
        "doc_hash": h1(),
        "delta": {"added": 1, "removed": 0, "retained": 0},
    });
    let result = CommitResult::from_value(&body).unwrap();
    assert_eq!(result.status, CommitStatus::Created);
    assert_eq!(
        result.delta,
        Delta {
            added: 1,
            removed: 0,
            retained: 0
        }
    );
    // status 是封闭枚举；计数必须是非负整数
    for bad in [
        json!({"status": "merged", "doc_hash": h1(), "delta": {"added": 0, "removed": 0, "retained": 0}}),
        json!({"status": "created", "doc_hash": h1(), "delta": {"added": -1, "removed": 0, "retained": 0}}),
        json!({"status": "created", "doc_hash": h1(), "delta": {"added": 1.0, "removed": 0, "retained": 0}}),
        json!({"status": "created", "doc_hash": h1()}),
    ] {
        assert!(CommitResult::from_value(&bad).is_err(), "{bad}");
    }
}

#[test]
fn move_result_wire_shape() {
    assert_eq!(
        MoveResult::from_value(&json!({"doc_hash": h1()}))
            .unwrap()
            .doc_hash,
        h1()
    );
    assert!(MoveResult::from_value(&json!({})).is_err());
    assert!(MoveResult::from_value(&json!({"doc_hash": 5})).is_err());
}
