//! 协议错误（core §6）：错误码全集、retryable 与 dpe-hash 错误的转换。
//!
//! 与 Python `tests/test_protocol_errors.py`（#47）的场景逐条对等。

use dpe_sdk::wire::Missing;
use dpe_sdk::{Error, ErrorKind};
use serde_json::{json, Value};

mod protocol_common;

use protocol_common::{h1, problem};

/// core §6 错误码表：code → retryable（与 Python 侧同一张表）。
const CORE_ERROR_TABLE: [(&str, ErrorKind, bool); 14] = [
    ("DPE_VALIDATION", ErrorKind::Validation, false),
    (
        "DPE_CONTRACT_UNSUPPORTED",
        ErrorKind::ContractUnsupported,
        false,
    ),
    ("DPE_CATEGORY_UNKNOWN", ErrorKind::CategoryUnknown, false),
    (
        "DPE_PRECONDITION_REQUIRED",
        ErrorKind::PreconditionRequired,
        false,
    ),
    (
        "DPE_PRECONDITION_FAILED",
        ErrorKind::PreconditionFailed,
        false,
    ),
    ("DPE_ALREADY_EXISTS", ErrorKind::AlreadyExists, false),
    ("DPE_NOT_FOUND", ErrorKind::NotFound, false),
    ("DPE_SESSION_EXPIRED", ErrorKind::SessionExpired, false),
    ("DPE_HASH_MISMATCH", ErrorKind::HashMismatch, false),
    ("DPE_MISSING_CONTENT", ErrorKind::MissingContent, false),
    ("DPE_PAYLOAD_TOO_LARGE", ErrorKind::PayloadTooLarge, false),
    ("DPE_FORBIDDEN", ErrorKind::Forbidden, false),
    ("DPE_RATE_LIMITED", ErrorKind::RateLimited, true),
    ("DPE_UNAVAILABLE", ErrorKind::Unavailable, true),
];

#[test]
fn every_core_code_has_a_kind_with_spec_retryable() {
    for (code, kind, retryable) in CORE_ERROR_TABLE {
        assert_eq!(kind.code(), Some(code), "{code}");
        assert_eq!(kind.retryable(), retryable, "{code}");
        let error = Error::from_problem(&json!({"code": code}), None).expect("core §6 的码");
        assert_eq!(error.code(), Some(code));
        assert_eq!(error.kind(), kind);
        assert_eq!(error.is_retryable(), retryable, "{code}");
    }
}

#[test]
fn local_kinds_have_no_code() {
    for kind in [
        ErrorKind::Unknown,
        ErrorKind::UnexpectedResponse,
        ErrorKind::IncompatibleServer,
        ErrorKind::ForceNotConfirmed,
        ErrorKind::InvalidArgument,
        ErrorKind::LocalRequest,
        ErrorKind::Transport,
        ErrorKind::StateStore,
    ] {
        assert_eq!(kind.code(), None, "{kind:?}");
        assert!(!kind.retryable(), "{kind:?}");
    }
}

#[test]
fn from_hash_error_keeps_code_and_path() {
    // dpe-hash 的各类校验错误 → 同码协议错误（Validation 类别合并多个 dpe-hash 子类）
    let cases: [(&str, Value, ErrorKind, &str); 3] = [
        (
            "category unknown",
            json!({"category": "Nope", "text": "x"}),
            ErrorKind::CategoryUnknown,
            "/category",
        ),
        (
            "file_type invalid",
            json!({"file_type": "MD", "pages": []}),
            ErrorKind::Validation,
            "/file_type",
        ),
        (
            "undefined field",
            json!({"file_type": "md", "pages": [], "extra": 1}),
            ErrorKind::Validation,
            "/extra",
        ),
    ];
    for (label, value, expected_kind, expected_path) in cases {
        let hash_error = match label {
            "category unknown" => dpe_hash::content_hash(&value, "dpe1"),
            _ => dpe_hash::object_hash(&value, dpe_hash::ObjectKind::Document, "dpe1"),
        }
        .expect_err("必须不合法");
        let converted = Error::from_hash_error(&hash_error, "/objects/3");
        assert_eq!(converted.kind(), expected_kind, "{label}");
        assert_eq!(converted.code(), Some(hash_error.code()), "{label}");
        assert_eq!(converted.message(), hash_error.message(), "{label}");
        assert_eq!(
            converted.path(),
            format!("/objects/3{expected_path}"),
            "{label}"
        );
    }

    // 契约前缀不受支持
    let hash_error =
        dpe_hash::parse_hash(&format!("dpe9:{}", "a".repeat(64))).expect_err("未知契约");
    let converted = Error::from_hash_error(&hash_error, "");
    assert_eq!(converted.kind(), ErrorKind::ContractUnsupported);
    assert_eq!(converted.code(), Some("DPE_CONTRACT_UNSUPPORTED"));
}

#[test]
fn missing_content_carries_list() {
    let page = h1();
    let error = Error::from_problem(
        &json!({
            "code": "DPE_MISSING_CONTENT",
            "missing": {"pages": [page]},
            "missing_truncated": true,
        }),
        None,
    )
    .expect("DPE_MISSING_CONTENT");
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(error.missing().unwrap().pages, vec![page]);
    assert!(error.missing().unwrap().blobs.is_empty());
    assert!(error.missing_truncated());
    assert_eq!(error.code(), Some("DPE_MISSING_CONTENT"));
}

/// 清单缺省、形状不对、三层全空都视为不可信（不等于「什么都不缺」，core §3.3）。
#[test]
fn from_problem_missing_list_trust_rules() {
    let malformed: [Value; 6] = [
        json!([format!("dpe1:{}", "1".repeat(64))]),
        json!("x"),
        json!(1),
        json!({"pages": null}),
        json!({"pages": [1]}),
        json!({"blobs": "sha256:x"}),
    ];
    for raw in malformed {
        let error = Error::from_problem(
            &json!({"code": "DPE_MISSING_CONTENT", "missing": raw}),
            None,
        )
        .expect("错误码不丢");
        assert_eq!(error.missing(), Some(&Missing::default()), "{raw}");
        assert!(error.missing_truncated(), "{raw}");
    }

    // 缺省清单不可信
    let bare = Error::from_problem(&json!({"code": "DPE_MISSING_CONTENT"}), None).unwrap();
    assert_eq!(bare.missing(), Some(&Missing::default()));
    assert!(bare.missing_truncated());

    // 空清单对象同样不可信（三层全空）
    let empty =
        Error::from_problem(&json!({"code": "DPE_MISSING_CONTENT", "missing": {}}), None).unwrap();
    assert!(empty.missing_truncated());

    // 有内容且未声明截断 → 可信
    let blob = format!("sha256:{}", "2".repeat(64));
    let trusted = Error::from_problem(
        &json!({"code": "DPE_MISSING_CONTENT", "missing": {"blobs": [blob]}}),
        None,
    )
    .unwrap();
    assert!(!trusted.missing_truncated());

    // missing_truncated 只认字面 true（"yes" 不算）
    let untrusted = Error::from_problem(
        &json!({
            "code": "DPE_MISSING_CONTENT",
            "missing": {"blobs": [format!("sha256:{}", "2".repeat(64))]},
            "missing_truncated": "yes",
        }),
        None,
    )
    .unwrap();
    assert!(!untrusted.missing_truncated());
}

#[test]
fn from_problem_dispatches_on_code_not_status() {
    let error = Error::from_problem(
        &json!({
            "type": "urn:dpe:error:not-found",
            "status": 412,
            "code": "DPE_NOT_FOUND",
            "detail": "gone",
            "retryable": false,
        }),
        None,
    )
    .expect("DPE_NOT_FOUND");
    assert_eq!(error.kind(), ErrorKind::NotFound);
    assert_eq!(error.message(), "gone");
}

#[test]
fn from_problem_message_falls_back_to_string_title() {
    let cases: [(Value, &str); 3] = [
        (
            json!({"code": "DPE_FORBIDDEN", "detail": 123, "title": "forbidden"}),
            "forbidden",
        ),
        (
            json!({"code": "DPE_FORBIDDEN", "detail": {}, "title": null}),
            "",
        ),
        (
            json!({"code": "DPE_FORBIDDEN", "detail": "", "title": "t"}),
            "t",
        ),
    ];
    for (problem, message) in cases {
        let error = Error::from_problem(&problem, None).unwrap();
        assert_eq!(error.message(), message, "{problem}");
    }
}

#[test]
fn from_problem_retry_after_and_unknown() {
    let error = Error::from_problem(
        &json!({"code": "DPE_UNAVAILABLE", "title": "busy"}),
        Some(3.0),
    )
    .unwrap();
    assert_eq!(error.kind(), ErrorKind::Unavailable);
    assert_eq!(error.retry_after(), Some(3.0));
    assert_eq!(error.message(), "busy");
    assert!(error.is_retryable());

    // 未知码：原码保留，只采信 literal true 的 retryable
    let unknown = Error::from_problem(
        &json!({"code": "DPE_NEW", "retryable": true, "detail": "d"}),
        None,
    )
    .unwrap();
    assert_eq!(unknown.kind(), ErrorKind::Unknown);
    assert_eq!(unknown.code(), Some("DPE_NEW"));
    assert!(unknown.is_retryable());
    assert_eq!(unknown.message(), "d");

    for flag in [json!("true"), json!(1)] {
        let error =
            Error::from_problem(&json!({"code": "DPE_NEW", "retryable": flag}), None).unwrap();
        assert!(!error.is_retryable(), "{flag}");
    }

    // 已知码不采信声明的 retryable
    let error =
        Error::from_problem(&json!({"code": "DPE_VALIDATION", "retryable": true}), None).unwrap();
    assert!(!error.is_retryable());

    // retry_after 传给任何码
    let error =
        Error::from_problem(&json!({"code": "DPE_NEW", "retryable": true}), Some(2.0)).unwrap();
    assert_eq!(error.retry_after(), Some(2.0));
}

#[test]
fn from_problem_rejects_non_dpe_bodies() {
    let bodies: [Value; 6] = [
        json!({"title": "not dpe"}),
        json!({"code": ""}),
        json!({"code": 1}),
        json!([]),
        Value::Null,
        json!("x"),
    ];
    for body in bodies {
        assert!(Error::from_problem(&body, None).is_none(), "{body}");
    }
}

#[test]
fn display_follows_message_and_path() {
    let error = Error::from_hash_error(
        &dpe_hash::content_hash(&json!({"category": "Nope", "text": "x"}), "dpe1")
            .expect_err("category 不合法"),
        "/objects/3",
    );
    assert_eq!(error.path(), "/objects/3/category");
    assert_eq!(
        error.to_string(),
        format!("{}（位置 /objects/3/category）", error.message())
    );

    // 空 message 的本地错误回退到 code 名（构造 `ErrorKind` 的路径经 problem 体验证）
    let forbidden = Error::from_problem(&json!({"code": "DPE_FORBIDDEN"}), None).unwrap();
    assert_eq!(forbidden.to_string(), "DPE_FORBIDDEN");

    // UnexpectedResponse 带 HTTP 状态码
    let (result, _) = protocol_common::run(
        protocol_common::core("dpe1")
            .list_page("", None, None)
            .unwrap(),
        [protocol_common::empty(404, &[])],
    );
    let error = result.expect_err("404 无错误码不是 DPE 错误");
    assert_eq!(
        error.to_string(),
        format!("{}（HTTP 404）", error.message())
    );
}

/// `problem` 辅助构造的响应与 `Error::from_problem` 的联调（冒烟，防辅助与实现脱钩）。
#[test]
fn problem_helper_round_trip() {
    let (result, _) = protocol_common::run(
        protocol_common::core("dpe1")
            .list_page("", None, None)
            .unwrap(),
        [problem("DPE_FORBIDDEN", 403, json!({"detail": "no"}))],
    );
    let error = result.expect_err("必须失败");
    assert_eq!(error.kind(), ErrorKind::Forbidden);
    assert_eq!(error.message(), "no");
}

// ---------------------------------------------------------------------- 同名响应字段（#205）

/// 同名字段出现多次：按 RFC 9110 §5.3 依出现顺序以 `, ` 合并，不只取最后一个（HTTP 绑定 §5）。
#[test]
fn repeated_response_fields_are_combined() {
    let response = dpe_sdk::protocol::Response::new(
        429,
        [("Retry-After", "1"), ("retry-after", "2"), ("X-Other", "a")],
        Vec::new(),
    );
    assert_eq!(response.header("retry-after"), Some("1, 2"));
    assert_eq!(response.header("x-other"), Some("a"));
}

/// 合并出的 `Retry-After` 列表不合文法：视同没有提示（与 Python 侧一致）。
#[test]
fn repeated_retry_after_is_no_hint() {
    let response = dpe_sdk::protocol::Response::new(
        429,
        [
            ("DPE-Error-Code", "DPE_RATE_LIMITED"),
            ("Retry-After", "1"),
            ("Retry-After", "2"),
        ],
        Vec::new(),
    );
    let error = dpe_sdk::protocol::error_from_response(&response);
    assert_eq!(error.kind(), ErrorKind::RateLimited);
    assert_eq!(error.retry_after(), None);
}

/// 重复的 `DPE-Error-Code`（不论是否相同）不是单个 code：没有 problem 体时是非协议错误，
/// 不从中挑一个；有 problem 体时照常按体内的 code。
#[test]
fn repeated_error_code_header_is_not_a_code() {
    for codes in [
        ["DPE_NOT_FOUND", "DPE_FORBIDDEN"],
        ["DPE_NOT_FOUND", "DPE_NOT_FOUND"],
    ] {
        let response = dpe_sdk::protocol::Response::new(
            404,
            [("DPE-Error-Code", codes[0]), ("DPE-Error-Code", codes[1])],
            Vec::new(),
        );
        let error = dpe_sdk::protocol::error_from_response(&response);
        assert_eq!(error.kind(), ErrorKind::UnexpectedResponse, "{codes:?}");
        assert_eq!(error.status(), Some(404));
    }
    let with_body = problem("DPE_FORBIDDEN", 403, json!({}));
    let mut headers: Vec<(String, String)> = with_body
        .headers()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
    headers.push(("DPE-Error-Code".into(), "DPE_NOT_FOUND".into()));
    let response = dpe_sdk::protocol::Response::new(403, headers, with_body.body().to_vec());
    let error = dpe_sdk::protocol::error_from_response(&response);
    assert_eq!(error.kind(), ErrorKind::Forbidden);
}
