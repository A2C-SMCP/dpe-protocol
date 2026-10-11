//! sans-IO 协议核心（#21）：能力协商、只读操作、错误分派与驱动约定。
//!
//! 与 Python `tests/test_protocol.py`（#47）的场景逐条对等；响应按 HTTP 绑定 §4.1–§4.4、§5 的
//! 报文形状手工构造，用假服务端驱动操作。

mod protocol_common;

use dpe_sdk::protocol::{
    fetch_capabilities, retry_after_seconds_at, Body, Method, Operation, Step, CONTRACT_HEADER,
    DOC_HASH_HEADER, ERROR_CODE_HEADER,
};
use dpe_sdk::wire::Capabilities;
use dpe_sdk::{Error, ErrorKind, PageObject};
use serde_json::{json, Value};

use protocol_common::{
    capabilities, capabilities_body, caps_from, core, core_from, empty, h1, h2, h_drill, json_ok,
    percent_decode, problem, run, skeleton_body, Lost, Script, Server,
};

// ---------------------------------------------------------------------- 辅助

/// 取出操作的第一个请求（驱动到 `Send` 为止）。
fn first_request<O: Operation>(mut op: O) -> dpe_sdk::Request
where
    O::Output: std::fmt::Debug,
{
    match op.poll() {
        Ok(Step::Send(request)) => request,
        other => panic!("期望先产出请求，实际 {other:?}"),
    }
}

/// 请求体解析为 JSON 值（非 JSON 请求体即失败）。
fn body_json(request: &dpe_sdk::Request) -> Value {
    match &request.body {
        Body::Bytes(bytes) => serde_json::from_slice(bytes).expect("请求体是 JSON"),
        other => panic!("期望 JSON 请求体，实际 {other:?}"),
    }
}

/// 期望操作失败并返回错误。
fn expect_error<T: std::fmt::Debug>(result: Result<T, Error>) -> Error {
    match result {
        Ok(value) => panic!("期望失败，实际 {value:?}"),
        Err(error) => error,
    }
}

/// 200 + JSON 体 + 附加头的响应。
fn ok_with(body: Value, headers: &[(&str, &str)]) -> dpe_sdk::Response {
    let mut all: Vec<(String, String)> =
        vec![("Content-Type".to_owned(), "application/json".to_owned())];
    all.extend(
        headers
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
    );
    dpe_sdk::Response::new(
        200,
        all,
        serde_json::to_vec(&body).expect("JSON 值总能序列化"),
    )
}

// ---------------------------------------------------------------------- 能力协商

#[test]
fn fetch_capabilities_sends_no_contract_header() {
    let (result, server) = run(fetch_capabilities(), [json_ok(capabilities_body())]);
    assert_eq!(result.unwrap(), capabilities());
    let request = &server.requests[0];
    assert_eq!(request.method, Method::Get);
    assert_eq!(request.target, "capabilities");
    assert_eq!(request.body, Body::None);
    // 唯一不带任何头（含契约头）的请求
    assert!(request.headers.is_empty());
    // 只读请求：传输层可自行原样重发
    assert!(request.retry_safe);
}

#[test]
fn fetch_capabilities_rejects_malformed_body() {
    let (result, _) = run(
        fetch_capabilities(),
        [json_ok(json!({"protocol": "dpe/1"}))],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("Capabilities"), "{error}");
    assert_eq!(error.status(), Some(200));
}

/// 限值必须是正整数：0、负数、bool、字符串、浮点与 TTL < 3600 都不合规。
///
/// 与 Python 侧 pydantic 的 strict 校验（`wire.py` 的 `_Positive` 与
/// `staging_ttl_seconds`）对齐，见 PR 说明的「Python 用例 → Rust 用例」映射表。
#[test]
fn fetch_capabilities_rejects_non_positive_limits() {
    let cases: [(&str, Value); 6] = [
        ("batch_head_max", json!(0)),
        ("batch_head_max", json!(-1)),
        ("batch_head_max", json!(true)),
        ("list_page_max", json!("1000")),
        ("blob_chunk_bytes", json!(1.5)),
        ("staging_ttl_seconds", json!(3599)),
    ];
    for (name, value) in cases {
        let mut body = capabilities_body();
        body["limits"][name] = value.clone();
        let (result, _) = run(fetch_capabilities(), [json_ok(body)]);
        let error = expect_error(result);
        assert_eq!(
            error.kind(),
            ErrorKind::UnexpectedResponse,
            "{name}={value}"
        );
        assert!(
            error.message().contains("Capabilities"),
            "{name}={value}: {error}"
        );
    }
}

/// 信封对未知成员宽容（同 Python `test_envelopes_ignore_unknown_members`）。
#[test]
fn capabilities_tolerate_unknown_members() {
    let mut body = capabilities_body();
    body["future"] = json!(1);
    body["limits"]["new_limit"] = json!(2);
    let (result, _) = run(fetch_capabilities(), [json_ok(body)]);
    assert_eq!(result.unwrap(), capabilities());
}

#[test]
fn contract_must_be_accepted_by_server_no_downgrade() {
    let mut body = capabilities_body();
    body["hash_contracts"] = json!(["dpe2"]);
    let caps = caps_from(&body).unwrap();
    assert_eq!(
        expect_error(core_from(caps, "dpe1")).kind(),
        ErrorKind::ContractUnsupported
    );

    let mut body = capabilities_body();
    body["hash_contracts"] = json!([]);
    let caps = caps_from(&body).unwrap();
    assert_eq!(
        expect_error(core_from(caps, "dpe1")).kind(),
        ErrorKind::ContractUnsupported
    );

    // 演练契约只在显式指定且服务端接受时使用
    let mut body = capabilities_body();
    body["hash_contracts"] = json!(["dpe1", "dpe2"]);
    let caps = caps_from(&body).unwrap();
    assert_eq!(core_from(caps.clone(), "dpe2").unwrap().contract(), "dpe2");
    assert_eq!(core_from(caps, "dpe1").unwrap().contract(), "dpe1");
}

#[test]
fn unknown_contract_and_protocol() {
    let caps = caps_from(&capabilities_body()).unwrap();
    let error = expect_error(core_from(caps, "dpe9"));
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    assert!(error.message().contains("dpe9"), "{error}");

    let mut body = capabilities_body();
    body["protocol"] = json!("dpe/2");
    let caps = caps_from(&body).unwrap();
    assert_eq!(
        expect_error(core_from(caps, "dpe1")).kind(),
        ErrorKind::IncompatibleServer
    );

    // 校验顺序同 Python：协议版本先于契约判定
    let mut body = capabilities_body();
    body["protocol"] = json!("dpe/2");
    body["hash_contracts"] = json!(["dpe9"]);
    let caps = caps_from(&body).unwrap();
    assert_eq!(
        expect_error(core_from(caps, "dpe9")).kind(),
        ErrorKind::IncompatibleServer
    );
}

/// 每个请求都带契约头，且只读操作的 `retry_safe` 一律为 `true`。
///
/// 写路径（commit / delete / move / negotiate / upload_*）的 `retry_safe` 为 `false`，
/// 断点查询为 `true`——随各操作的实现落地后在同名测试里逐项断言。
#[test]
fn every_read_request_declares_the_contract_and_is_retry_safe() {
    let mut body = capabilities_body();
    body["hash_contracts"] = json!(["dpe1", "dpe2"]);
    let c = core_from(caps_from(&body).unwrap(), "dpe2").unwrap();
    let uris = vec!["a:1".to_owned()];
    let requests = [
        first_request(c.head("a:1")),
        first_request(c.batch_head(&uris)),
        first_request(c.get_skeleton("a:1").unwrap()),
        first_request(c.list_page("", None, None).unwrap()),
    ];
    for request in requests {
        assert_eq!(
            request.headers.get(CONTRACT_HEADER),
            Some("dpe2"),
            "{request:?}"
        );
        assert!(request.retry_safe, "{request:?}");
    }
}

// ---------------------------------------------------------------------- 编码

#[test]
fn query_values_are_percent_encoded() {
    for uri in [
        "s3://b/k?x=1&y=2#f",
        "file:///a&b=%41+c",
        "feishu://doc/a+b",
    ] {
        let c = core("dpe1");
        let targets = [
            first_request(c.head(uri)).target,
            first_request(c.get_skeleton(uri).unwrap()).target,
        ];
        for target in targets {
            let (path, query) = target.split_once('?').expect("带查询串");
            assert_eq!(path, "documents");
            let (key, value) = query.split_once('=').expect("名值对");
            assert_eq!(key, "uri");
            assert!(
                value
                    .chars()
                    .all(|ch| ch.is_ascii() && !"?&#/ +".contains(ch)),
                "{value}"
            );
            assert_eq!(percent_decode(value), uri);
        }
    }
}

/// head 不做本地 URI 校验（由服务端判 `DPE_VALIDATION`）：非常规字符仍按 RFC 3986 编码。
#[test]
fn query_values_encode_invalid_uris_for_head() {
    let uri = "file:///a b/%41/中文";
    let target = first_request(core("dpe1").head(uri)).target;
    let (path, query) = target.split_once('?').expect("带查询串");
    let (key, value) = query.split_once('=').expect("名值对");
    assert_eq!((path, key), ("documents", "uri"));
    assert!(value
        .chars()
        .all(|ch| ch.is_ascii() && !"?&#/ +".contains(ch)));
    assert_eq!(percent_decode(value), uri);
}

#[test]
fn list_page_query() {
    let c = core("dpe1");
    assert_eq!(
        first_request(c.list_page("", None, None).unwrap()).target,
        "documents?prefix="
    );
    assert_eq!(
        first_request(c.list_page("s3://b/a&b/", Some("c/1="), Some(10)).unwrap()).target,
        "documents?prefix=s3%3A%2F%2Fb%2Fa%26b%2F&cursor=c%2F1%3D&limit=10"
    );
    // prefix 不是完整的 URI，不做校验也不规范化：非 ASCII 按 RFC 3986 百分号编码（core §3）
    assert_eq!(
        first_request(c.list_page("s3://b/中文/", None, None).unwrap()).target,
        "documents?prefix=s3%3A%2F%2Fb%2F%E4%B8%AD%E6%96%87%2F"
    );
}

// ---------------------------------------------------------------------- head

#[test]
fn head_found() {
    let expected = h1();
    let (result, server) = run(
        core("dpe1").head("a:1"),
        [empty(200, &[(DOC_HASH_HEADER, &expected)])],
    );
    assert_eq!(result.unwrap(), Some(expected));
    assert_eq!(server.requests[0].method, Method::Head);
    assert_eq!(server.requests[0].body, Body::None);
}

#[test]
fn head_not_found_by_error_code_header() {
    // 头字段名不区分大小写
    let (result, _) = run(
        core("dpe1").head("a:1"),
        [empty(404, &[("dpe-error-code", "DPE_NOT_FOUND")])],
    );
    assert_eq!(result.unwrap(), None);
}

#[test]
fn head_404_without_error_code_is_not_a_dpe_error() {
    let error = expect_error(run(core("dpe1").head("a:1"), [empty(404, &[])]).0);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert_eq!(error.status(), Some(404));
    assert!(!error.is_retryable());
    assert!(error.code().is_none());
}

#[test]
fn head_errors_dispatch_by_header() {
    let error = expect_error(
        run(
            core("dpe1").head("a:1"),
            [empty(403, &[(ERROR_CODE_HEADER, "DPE_FORBIDDEN")])],
        )
        .0,
    );
    assert_eq!(error.kind(), ErrorKind::Forbidden);

    let error = expect_error(
        run(
            core("dpe1").head("a:1"),
            [empty(
                429,
                &[
                    (ERROR_CODE_HEADER, "DPE_RATE_LIMITED"),
                    ("Retry-After", "7"),
                ],
            )],
        )
        .0,
    );
    assert_eq!(error.kind(), ErrorKind::RateLimited);
    assert!(error.is_retryable());
    assert_eq!(error.retry_after(), Some(7.0));
}

#[test]
fn head_rejects_missing_or_foreign_doc_hash() {
    let (result, _) = run(core("dpe1").head("a:1"), [empty(200, &[])]);
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);

    let drill = h_drill();
    let (result, _) = run(
        core("dpe1").head("a:1"),
        [empty(200, &[(DOC_HASH_HEADER, &drill)])],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);

    let (result, _) = run(
        core("dpe1").head("a:1"),
        [empty(200, &[(DOC_HASH_HEADER, "dpe1:xyz")])],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);

    let valid = h1();
    let (result, _) = run(
        core("dpe1").head("a:1"),
        [empty(204, &[(DOC_HASH_HEADER, &valid)])],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

// ---------------------------------------------------------------------- batch_head

#[test]
fn batch_head_empty_makes_no_request() {
    let (result, server) = run(core("dpe1").batch_head(&[]), Vec::new());
    assert_eq!(result.unwrap(), Vec::<Option<String>>::new());
    assert!(server.requests.is_empty());
}

#[test]
fn batch_head_single_batch_at_limit() {
    let one = h1();
    let uris = vec!["a:1".to_owned(), "a:2".to_owned()];
    let (result, server) = run(
        core("dpe1").batch_head(&uris),
        [json_ok(json!({"heads": [{"doc_hash": one}, null]}))],
    );
    assert_eq!(result.unwrap(), vec![Some(one), None]);
    let request = &server.requests[0];
    assert_eq!(
        (request.method, request.target.as_str()),
        (Method::Post, "heads")
    );
    assert_eq!(
        request.headers.get("Content-Type"),
        Some("application/json")
    );
    assert_eq!(body_json(request), json!({"uris": ["a:1", "a:2"]}));
}

#[test]
fn batch_head_splits_by_batch_head_max() {
    let (one, two) = (h1(), h2());
    let uris = vec!["a:1".to_owned(), "a:2".to_owned(), "a:3".to_owned()];
    let (result, server) = run(
        core("dpe1").batch_head(&uris),
        [
            json_ok(json!({"heads": [null, {"doc_hash": one}]})),
            json_ok(json!({"heads": [{"doc_hash": two}]})),
        ],
    );
    assert_eq!(result.unwrap(), vec![None, Some(one), Some(two)]);
    let chunks: Vec<Value> = server
        .requests
        .iter()
        .map(|request| body_json(request)["uris"].clone())
        .collect();
    assert_eq!(chunks, vec![json!(["a:1", "a:2"]), json!(["a:3"])]);
}

#[test]
fn batch_head_checks_every_batch() {
    let uris = vec!["a:1".to_owned(), "a:2".to_owned(), "a:3".to_owned()];
    let (result, _) = run(
        core("dpe1").batch_head(&uris),
        [
            json_ok(json!({"heads": [null, null]})),
            json_ok(json!({"heads": [null, null]})),
        ],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("2 项"), "{error}");

    let drill = h_drill();
    let (result, _) = run(
        core("dpe1").batch_head(&uris),
        [
            json_ok(json!({"heads": [null, null]})),
            json_ok(json!({"heads": [{"doc_hash": drill}]})),
        ],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn batch_head_rejects_count_mismatch_and_foreign_hash() {
    let uris = vec!["a:1".to_owned(), "a:2".to_owned()];
    let (result, _) = run(
        core("dpe1").batch_head(&uris),
        [json_ok(json!({"heads": [null]}))],
    );
    let error = expect_error(result);
    assert!(error.message().contains("1 项"), "{error}");

    let drill = h_drill();
    let (result, _) = run(
        core("dpe1").batch_head(&uris),
        [json_ok(json!({"heads": [null, {"doc_hash": drill}]}))],
    );
    let error = expect_error(result);
    assert!(error.message().contains("heads[1]"), "{error}");
}

#[test]
fn batch_head_error_stops_and_raises() {
    let uris = vec!["a:1".to_owned(), "a:2".to_owned(), "a:3".to_owned()];
    let (result, server) = run(
        core("dpe1").batch_head(&uris),
        [
            json_ok(json!({"heads": [null, null]})),
            problem("DPE_VALIDATION", 400, Value::Null),
        ],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::Validation);
    assert_eq!(server.requests.len(), 2);
}

// ---------------------------------------------------------------------- get_skeleton

#[test]
fn get_skeleton() {
    // 第 1、3 页内容相同：document.pages 中同一页 hash 出现两次，页对象也重复给出（§4.3）
    let body = skeleton_body("dpe1", "a:1");
    assert_eq!(body["document"]["pages"][0], body["document"]["pages"][2]);
    let doc_hash = body["doc_hash"].as_str().unwrap().to_owned();
    let (result, server) = run(
        core("dpe1").get_skeleton("a:1").unwrap(),
        [ok_with(body.clone(), &[(DOC_HASH_HEADER, &doc_hash)])],
    );
    let skeleton = result.unwrap().expect("文档存在");
    assert_eq!(skeleton.doc_hash, doc_hash);
    for (i, page) in body["pages"].as_array().unwrap().iter().enumerate() {
        assert_eq!(
            skeleton.pages[i],
            PageObject::from_value(page, "dpe1").unwrap()
        );
    }
    assert_eq!(server.requests[0].method, Method::Get);
}

#[test]
fn get_skeleton_under_drill_contract() {
    let body = skeleton_body("dpe2", "a:1");
    let mut caps_body = capabilities_body();
    caps_body["hash_contracts"] = json!(["dpe1", "dpe2"]);
    let c = core_from(caps_from(&caps_body).unwrap(), "dpe2").unwrap();
    let (result, _) = run(c.get_skeleton("a:1").unwrap(), [json_ok(body.clone())]);
    assert!(result
        .unwrap()
        .expect("文档存在")
        .doc_hash
        .starts_with("dpe2:"));

    // 同一份 dpe2 骨架交给 dpe1 核心：hash 不属于声明的契约
    let (result, _) = run(core("dpe1").get_skeleton("a:1").unwrap(), [json_ok(body)]);
    assert_eq!(expect_error(result).kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn get_skeleton_not_found() {
    let (result, _) = run(
        core("dpe1").get_skeleton("a:1").unwrap(),
        [problem("DPE_NOT_FOUND", 404, Value::Null)],
    );
    assert!(result.unwrap().is_none());
}

/// 骨架的完整性：页内容、页序、页数、doc_hash、文档对象、头与 file_uri 任一不符都拒绝
/// （core §3、§1.1；#39 追加项）。
#[test]
fn get_skeleton_integrity() {
    for how in [
        "page_content",
        "page_order",
        "page_count",
        "doc_hash",
        "document",
        "header",
        "file_uri",
    ] {
        let mut body = skeleton_body("dpe1", "a:1");
        let mut headers: Vec<(String, String)> = Vec::new();
        match how {
            "page_content" => body["pages"][1]["title"] = json!("changed"),
            "page_order" => body["pages"].as_array_mut().unwrap().swap(0, 1),
            "page_count" => {
                body["pages"].as_array_mut().unwrap().pop();
            }
            "doc_hash" => body["doc_hash"] = json!(h1()),
            "document" => body["document"]["title"] = json!("changed"),
            "header" => headers.push((DOC_HASH_HEADER.to_owned(), h1())),
            "file_uri" => body["file_uri"] = json!("a:2"),
            other => panic!("未知的篡改方式：{other}"),
        }
        let header_refs: Vec<(&str, &str)> = headers
            .iter()
            .map(|(name, value)| (name.as_str(), value.as_str()))
            .collect();
        let (result, _) = run(
            core("dpe1").get_skeleton("a:1").unwrap(),
            [ok_with(body, &header_refs)],
        );
        let error = expect_error(result);
        assert_eq!(error.kind(), ErrorKind::UnexpectedResponse, "{how}");
        assert!(error.message().contains("骨架不自洽"), "{how}: {error}");
    }
}

#[test]
fn get_skeleton_rejects_invalid_objects() {
    let mut body = skeleton_body("dpe1", "a:1");
    body["pages"][0]["extra"] = json!(1);
    let (result, _) = run(core("dpe1").get_skeleton("a:1").unwrap(), [json_ok(body)]);
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("Skeleton"), "{error}");
}

/// 请求可以带未规范化的 uri（服务端按规范化形式定位）；响应返回规范化后的 file_uri 应当通过。
#[test]
fn get_skeleton_accepts_unnormalized_request_uri() {
    let body = skeleton_body("dpe1", "feishu://doc/a");
    let (result, server) = run(
        core("dpe1").get_skeleton("FEISHU://doc/%61").unwrap(),
        [json_ok(body)],
    );
    assert_eq!(
        result.unwrap().expect("文档存在").file_uri,
        "feishu://doc/a"
    );
    // 请求侧仍按原样发送，不做规范化
    assert!(server.requests[0]
        .target
        .ends_with("uri=FEISHU%3A%2F%2Fdoc%2F%2561"));
}

/// 响应返回另一篇文档的 file_uri：拒绝，不能把别的文档的骨架当成目标文档接受（#39）。
#[test]
fn get_skeleton_rejects_other_documents_file_uri() {
    let body = skeleton_body("dpe1", "feishu://doc/b");
    let (result, _) = run(
        core("dpe1").get_skeleton("feishu://doc/a").unwrap(),
        [json_ok(body)],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("file_uri"), "{error}");
}

/// 请求 uri 不符合 core §1.1 文法（非 ASCII 等）：本地即失败，不发请求。
#[test]
fn get_skeleton_rejects_invalid_request_uri_without_request() {
    let server = Server::new([json_ok(skeleton_body("dpe1", "a:1"))]);
    let error = expect_error(core("dpe1").get_skeleton("feishu://doc/季度报告"));
    assert_eq!(error.kind(), ErrorKind::Validation);
    assert_eq!(error.code(), Some("DPE_VALIDATION"));
    assert!(server.requests.is_empty());
}

// ---------------------------------------------------------------------- list

#[test]
fn list_page() {
    let one = h1();
    let (result, _) = run(
        core("dpe1").list_page("a:", None, None).unwrap(),
        [json_ok(json!({
            "documents": [{"file_uri": "a:1", "doc_hash": one}],
            "next_cursor": "c1",
        }))],
    );
    let page = result.unwrap();
    assert_eq!(page.documents.len(), 1);
    assert_eq!(page.documents[0].file_uri, "a:1");
    assert_eq!(page.documents[0].doc_hash, one);
    assert_eq!(page.next_cursor.as_deref(), Some("c1"));

    let (result, _) = run(
        core("dpe1").list_page("a:", Some("c1"), None).unwrap(),
        [json_ok(json!({"documents": [], "next_cursor": null}))],
    );
    assert_eq!(result.unwrap().next_cursor, None);
}

#[test]
fn list_page_rejects_foreign_hash() {
    let drill = h_drill();
    let (result, _) = run(
        core("dpe1").list_page("", None, None).unwrap(),
        [json_ok(json!({
            "documents": [{"file_uri": "a:1", "doc_hash": drill}],
            "next_cursor": null,
        }))],
    );
    let error = expect_error(result);
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("documents[0]"), "{error}");
}

#[test]
fn list_page_limit_range() {
    for limit in [0, 1001] {
        let error = expect_error(core("dpe1").list_page("", None, Some(limit)));
        assert_eq!(error.kind(), ErrorKind::InvalidArgument);
        assert!(error.message().contains("1 到 1000"), "{error}");
    }
    assert!(
        first_request(core("dpe1").list_page("", None, Some(1000)).unwrap())
            .target
            .ends_with("&limit=1000")
    );
}

#[test]
fn list_page_invalid_cursor() {
    let (result, _) = run(
        core("dpe1").list_page("", Some("bogus"), None).unwrap(),
        [problem("DPE_VALIDATION", 400, Value::Null)],
    );
    assert_eq!(expect_error(result).kind(), ErrorKind::Validation);
}

// ---------------------------------------------------------------------- 错误分派

/// 用 list 一页触发错误响应的分派。
fn list_error(response: dpe_sdk::Response) -> Error {
    expect_error(run(core("dpe1").list_page("", None, None).unwrap(), [response]).0)
}

#[test]
fn problem_body_code_wins_over_header() {
    let body = serde_json::to_vec(&json!({"code": "DPE_FORBIDDEN", "detail": "no"})).unwrap();
    let response = dpe_sdk::Response::new(
        403,
        [
            ("Content-Type", "application/problem+json; charset=utf-8"),
            (ERROR_CODE_HEADER, "DPE_X"),
        ],
        body,
    );
    let error = list_error(response);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert_eq!(error.message(), "no");
}

#[test]
fn problem_body_without_header() {
    let body =
        serde_json::to_vec(&json!({"code": "DPE_SESSION_EXPIRED", "title": "gone"})).unwrap();
    let error = list_error(dpe_sdk::Response::new(
        410,
        [("Content-Type", "application/problem+json")],
        body,
    ));
    assert_eq!(error.kind(), ErrorKind::SessionExpired);
    assert_eq!(error.message(), "gone");
}

#[test]
fn header_used_when_body_is_not_a_problem() {
    let cases: [(&str, &[u8]); 5] = [
        ("application/problem+json", b""),
        ("application/problem+json", b"{not json"),
        ("application/problem+json", b"{\"code\": 1}"),
        ("application/json", b"{\"code\": \"DPE_VALIDATION\"}"),
        ("text/html", b"<html>"),
    ];
    for (content_type, body) in cases {
        let error = list_error(dpe_sdk::Response::new(
            403,
            [
                ("Content-Type", content_type),
                (ERROR_CODE_HEADER, "DPE_FORBIDDEN"),
            ],
            body.to_vec(),
        ));
        assert_eq!(error.kind(), ErrorKind::Forbidden, "{content_type}");
    }
}

#[test]
fn no_problem_body_and_no_header_is_not_a_dpe_error() {
    let cases = [
        (
            dpe_sdk::Response::new(500, [("Content-Type", "text/plain")], Vec::new()),
            None,
        ),
        (
            dpe_sdk::Response::new(
                502,
                [("Content-Type", "text/html")],
                b"<html>bad gateway</html>".to_vec(),
            ),
            None,
        ),
        (
            dpe_sdk::Response::new(
                400,
                [("Content-Type", "application/json")],
                b"{\"code\": \"DPE_VALIDATION\"}".to_vec(),
            ),
            None,
        ),
        (
            dpe_sdk::Response::new(
                503,
                [(ERROR_CODE_HEADER, " "), ("Retry-After", "3")],
                Vec::new(),
            ),
            Some(3.0),
        ),
    ];
    for (response, expected_retry_after) in cases {
        let status = response.status();
        let error = list_error(response);
        assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
        assert!(error.code().is_none());
        assert_eq!(error.status(), Some(status));
        assert_eq!(error.retry_after(), expected_retry_after);
    }
}

#[test]
fn unknown_code_is_preserved() {
    let error = list_error(empty(418, &[(ERROR_CODE_HEADER, "DPE_FUTURE")]));
    assert_eq!(error.kind(), ErrorKind::Unknown);
    assert_eq!(error.code(), Some("DPE_FUTURE"));
    assert!(!error.is_retryable());
}

#[test]
fn missing_content_header_only_has_untrusted_list() {
    let error = list_error(empty(400, &[(ERROR_CODE_HEADER, "DPE_MISSING_CONTENT")]));
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert!(error.missing_truncated());
    assert_eq!(error.missing().unwrap().pages.len(), 0);
}

#[test]
fn redirect_and_other_statuses_are_unexpected() {
    let error = list_error(empty(
        302,
        &[
            ("Location", "elsewhere"),
            (ERROR_CODE_HEADER, "DPE_NOT_FOUND"),
        ],
    ));
    assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
}

#[test]
fn success_body_not_json() {
    let bodies: [&[u8]; 3] = [
        b"",
        b"{",
        b"{\"documents\": [], \"next_cursor\": null, \"x\": 1, \"x\": 2}",
    ];
    for body in bodies {
        let response =
            dpe_sdk::Response::new(200, [("Content-Type", "application/json")], body.to_vec());
        let error = list_error(response);
        assert_eq!(error.kind(), ErrorKind::UnexpectedResponse);
        assert!(error.code().is_none()); // 不冒充请求侧的 DPE_VALIDATION
    }
}

// ---------------------------------------------------------------------- Retry-After

/// `Retry-After` 换算（RFC 9110 §10.2.3）：delta-seconds、HTTP-date（含过时的 RFC 850 与
/// asctime）与无法表示的大数；三种访问器结果可区分：「没有给」`None`、有限值、`INFINITY`。
#[test]
fn retry_after_seconds_table() {
    // 2026-10-08T12:00:00Z（与 Python 侧测试同一时刻）
    let now = 1_791_460_800_i64;

    // 没有给 / 无法解析
    assert_eq!(retry_after_seconds_at("", now), None);
    assert_eq!(retry_after_seconds_at("-1", now), None);
    assert_eq!(retry_after_seconds_at("1.5", now), None);
    assert_eq!(retry_after_seconds_at("soon", now), None);
    assert_eq!(retry_after_seconds_at("٣", now), None); // 非 ASCII 数字
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 25:00:30 GMT", now),
        None
    );
    assert_eq!(
        retry_after_seconds_at("Thu, 32 Oct 2026 12:00:30 GMT", now),
        None
    );
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Foo 2026 12:00:30 GMT", now),
        None
    );

    // delta-seconds
    assert_eq!(retry_after_seconds_at(" 120 ", now), Some(120.0));
    assert_eq!(retry_after_seconds_at("0", now), Some(0.0));

    // 合法但无法表示的秒数：仍是「等待很久」，不是「没有提示」
    let huge = retry_after_seconds_at(&"9".repeat(400), now).unwrap();
    assert!(huge.is_infinite());
    let huger = retry_after_seconds_at(&"9".repeat(5000), now).unwrap();
    assert!(huger.is_infinite());

    // HTTP-date（IMF-fixdate 与过时但接收方 MUST 接受的格式；宽松边界与 Python 的
    // email.utils 实测逐条对齐）
    let table: [(&str, Option<f64>); 33] = [
        ("Thu, 08 Oct 2026 12:00:30 GMT", Some(30.0)),
        ("Thu, 08 Oct 2026 12:00:00 GMT", Some(0.0)),
        ("Thu, 08 Oct 2026 11:59:59 GMT", Some(0.0)), // 已过去 → 0
        ("Thu, 08 Oct 2026 12:00:30", Some(30.0)),    // 无时区 → 按 UTC
        ("Thu, 08 Oct 2026 12:00:30 +0000", Some(30.0)),
        ("Thu, 08 Oct 2026 12:00:30 GMT+00:00", Some(30.0)), // 无法识别的时区 → UTC
        ("Thu, 08 Oct 2026 12:00:30 UTC", Some(30.0)),
        ("Thu, 08 Oct 2026 12:00 GMT", Some(0.0)), // 可省秒
        ("Thu, 08 Oct 2026 12:00:30 -0000", Some(30.0)), // 未声明时区 → UTC
        ("Thursday, 08-Oct-26 12:00:30 GMT", Some(30.0)), // RFC 850（可带完整星期名）
        ("Thu, 08-Oct-2026 12:00:30 GMT", Some(30.0)),
        ("Thu, 08 Oct 2026 12:00:30 EST", Some(5.0 * 3600.0 + 30.0)), // 时区名表
        ("Thu, 08 Oct 2026 12:00:30 AST", Some(4.0 * 3600.0 + 30.0)),
        ("Thu, 08 Oct 2026 12:00:30 -500", Some(5.0 * 3600.0 + 30.0)), // 数字时区
        ("Thu, 08 Oct 2026 12:00:30 500", Some(0.0)),                  // +5 小时 → 已过去
        ("Thu Nov  6 08:49:37 1994", Some(0.0)),                       // asctime（久远的过去）
        ("08 Oct 2026 12:00:30 GMT", Some(30.0)),                      // 无星期名
        ("Thu, 08 Oct 26 12:00:30 GMT", Some(30.0)),                   // 2 位年份
        ("Thu, 08 January 2026 12:00:30 GMT", Some(0.0)),              // 完整月份名（一月已过去）
        ("Mon, 06 Nov 1994 08:49:37 GMT", Some(0.0)),                  // RFC 9110 的示例（已过去）
        ("Thursday Aug 27 14:37:31 2068", None), // 完整星期名的 asctime 不合法
        ("Thu, , 08 Oct 2026 12:00:30 GMT", None), // 连续分隔符不合法
        ("08 Oct 2026 12:00:30 2400", None),     // 时区偏移不少于 24 小时（datetime.timezone 约束）
        ("08 Oct 2026 12:00:30 -9999", None),
        ("08 Oct 2026 12:00:30 2359", Some(0.0)), // 边界：偏移 23:59（86340 秒）仍接受，时刻已过去
        ("08 Oct 2026 12:00:30 -2359", Some(86_370.0)), // 负向边界：+23:59 之后
        // 超大令牌：不溢出、不 panic（debug 也不），一律返回 None；Python 侧同组抛
        // OverflowError（#109 记录，Rust 不复制崩溃）
        ("08 Oct 2026 12:00:30 999999999999999999", None),
        ("08 Oct 2026 12:00:30 9223372036854775807", None), // i64::MAX
        ("08 Oct 2026 12:00:30 -9223372036854775808", None), // i64::MIN
        ("08 Oct 2026 12:00:30 999999999999999999999999", None), // 放不进 i64 的字面量
        ("08 Oct 2026 12:00:30-3+4", None), // 粘连 token 先按 '+' 切分（同 email.utils）
        // 以下两条是「Rust 只接受 ASCII 十进制」的钉住用例：Python 的 int() 额外接受 Unicode
        // 十进制数字（`int("٠٨")`）与下划线分隔（`int("1_0")`），记录在 #109，不复制。
        ("٠٨ Oct 2026 12:00:30 GMT", None),
        ("08 Oct 1_0 12:00:30 GMT", None),
    ];
    for (value, expected) in table {
        assert_eq!(retry_after_seconds_at(value, now), expected, "{value}");
    }

    // 美系时区缩写（同 Python email.utils 的表）与数值时区
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 12:00:30 EST", now),
        Some(5.0 * 3600.0 + 30.0)
    );
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 12:00:30 PST", now),
        Some(8.0 * 3600.0 + 30.0)
    );
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 12:00:30 -0500", now),
        Some(5.0 * 3600.0 + 30.0)
    );
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 12:00:30 +05:30", now),
        Some(30.0)
    );
    // 尾随内容与大小写
    assert_eq!(
        retry_after_seconds_at("Thu, 08 Oct 2026 12:00:30 GMT extra", now),
        Some(30.0)
    );
}

// ---------------------------------------------------------------------- 驱动约定

#[test]
fn transport_errors_propagate_as_transport_kind() {
    let mut script = Script::new().lost("连接失败");
    let error = expect_error(protocol_common::drive_script(
        core("dpe1").head("a:1"),
        &mut script,
    ));
    assert_eq!(error.kind(), ErrorKind::Transport);
    assert!(!error.is_retryable());
    assert_eq!(error.code(), None);
    // 原始传输错误可经 source() 取回
    let source = std::error::Error::source(&error).expect("透传原始传输错误");
    assert_eq!(source.to_string(), "响应丢失：连接失败");
    assert_eq!(script.requests.len(), 1);
}

#[test]
fn state_machine_misuse_returns_invalid_argument() {
    let c = core("dpe1");

    // 在等待响应时 poll：报错但不改变状态（在飞响应随后仍可正常回送，同 Python 生成器）
    let value = h1();
    let mut op = c.head("a:1");
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
    op.on_response(empty(200, &[(DOC_HASH_HEADER, &value)]))
        .expect("用法错误之后仍可回送");
    assert!(matches!(op.poll(), Ok(Step::Done(Some(_)))));

    // 向没有在飞请求的操作回送响应
    let uris = vec!["a:1".to_owned()];
    let mut op = c.batch_head(&uris);
    let error = expect_error(op.on_response(empty(200, &[])));
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);

    // 完成后继续 poll
    let value = h1();
    let mut op = c.head("a:1");
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    op.on_response(empty(200, &[(DOC_HASH_HEADER, &value)]))
        .expect("回送成功");
    assert!(matches!(op.poll(), Ok(Step::Done(Some(_)))));
    let error = expect_error(op.poll());
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);

    // on_response 出错后进入终态
    let mut op = c.head("a:1");
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    assert!(op.on_response(empty(404, &[])).is_err());
    let error = expect_error(op.poll());
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
}

/// 「`Err` 返回后操作进入终态」是结构性的：每个操作（含多轮往返的 batch_head）在
/// `on_response` / `on_transport_error` 返回 `Err` 之后，`poll` 与再次回送都必须是
/// `InvalidArgument`——不会静默跳过失败、也不会二次成功（同 Python 生成器的终止语义）。
#[test]
fn every_operation_is_terminal_after_error() {
    macro_rules! assert_terminal {
        ($op:expr) => {{
            // 业务错误（404 无错误码 → UnexpectedResponse）
            let mut op = $op;
            assert!(matches!(op.poll(), Ok(Step::Send(_))), "先发请求");
            expect_error(op.on_response(empty(404, &[])));
            assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
            assert_eq!(
                expect_error(op.on_response(empty(404, &[]))).kind(),
                ErrorKind::InvalidArgument
            );
            assert_eq!(
                expect_error(op.on_transport_error(Box::new(Lost("连接失败")))).kind(),
                ErrorKind::InvalidArgument
            );
            // 传输错误
            let mut op = $op;
            assert!(matches!(op.poll(), Ok(Step::Send(_))), "先发请求");
            expect_error(op.on_transport_error(Box::new(Lost("连接失败"))));
            assert_eq!(expect_error(op.poll()).kind(), ErrorKind::InvalidArgument);
            assert_eq!(
                expect_error(op.on_response(empty(200, &[]))).kind(),
                ErrorKind::InvalidArgument
            );
        }};
    }

    let c = core("dpe1");
    assert_terminal!(fetch_capabilities());
    assert_terminal!(c.head("a:1"));
    let uris = vec!["a:1".to_owned(), "a:2".to_owned()];
    assert_terminal!(c.batch_head(&uris));
    assert_terminal!(c.get_skeleton("a:1").unwrap());
    assert_terminal!(c.list_page("", None, None).unwrap());
}

/// batch_head 的定向回归：某批出错后即使被继续驱动，也 MUST NOT 跳过失败批次继续发送
/// （此前会发出 `{"uris":["a:3"]}` 并给出错位结果）。
#[test]
fn batch_head_stops_after_failed_batch() {
    let uris = vec!["a:1".to_owned(), "a:2".to_owned(), "a:3".to_owned()];
    let c = core("dpe1");
    let mut op = c.batch_head(&uris);
    assert!(matches!(op.poll(), Ok(Step::Send(_))));
    expect_error(op.on_response(problem("DPE_VALIDATION", 400, Value::Null)));
    // 终态：不会再发下一批
    let error = expect_error(op.poll());
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
}

#[test]
fn response_headers_are_case_insensitive() {
    let value = h1();
    let response = empty(200, &[(DOC_HASH_HEADER, &value)]);
    assert_eq!(response.header("dpe-doc-hash"), Some(value.as_str()));
    assert_eq!(response.header("DPE-DOC-HASH"), Some(value.as_str()));
    assert_eq!(response.header("Dpe-Doc-Hash"), Some(value.as_str()));
    assert_eq!(response.header("missing"), None);
    // 构造时统一小写
    assert!(response
        .headers()
        .all(|(name, _)| name == name.to_ascii_lowercase()));
}

// ---------------------------------------------------------------------- 核心访问器

#[test]
fn core_accessors() {
    let c = core("dpe1");
    assert_eq!(c.contract(), "dpe1");
    assert_eq!(c.limits().batch_head_max, 2);
    assert_eq!(c.capabilities().protocol, "dpe/1");
    assert_eq!(c.max_resends(), dpe_sdk::protocol::DEFAULT_MAX_RESENDS);
    let caps: &Capabilities = c.capabilities();
    assert_eq!(caps.hash_contracts, vec!["dpe1".to_owned()]);
}
