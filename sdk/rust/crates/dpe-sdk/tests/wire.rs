//! 响应报文（HTTP 绑定 §4、§5）：解析、未知成员宽容、内嵌对象仍封闭、字段类型严格。
//!
//! 与 Python `tests/test_wire.py` 对等（commit 结果与缺失清单的完整用例随写操作落地）。
//!
//! **类型严格**：字符串必须是 JSON 字符串、限额必须是 JSON 整数（`8.0`、`"8"`、`true` 一律
//! 不合规），与 Python 侧 pydantic 的校验对齐——`wire.py` 的限额用
//! `Annotated[int, Field(strict=True, ge=1)]`（TTL 为 `ge=3600`），其余字段 pydantic v2 的
//! 默认模式同样拒绝 `int` 冒充 `str` 与 `float` 冒充 `int`。依据与对照见 PR 说明的
//! 「Python 用例 → Rust 用例」映射表。

mod protocol_common;

use dpe_sdk::wire::{BatchHeads, Capabilities, ListPage, Missing, PageUploadMissing, Skeleton};
use serde_json::{json, Value};

use protocol_common::{capabilities_body, h1, skeleton_body};

fn parse_capabilities(body: &Value) -> Result<Capabilities, dpe_sdk::Error> {
    Capabilities::from_value(body)
}

fn capabilities_error(body: &Value) -> dpe_sdk::Error {
    parse_capabilities(body).expect_err("必须不合规")
}

#[test]
fn capabilities_round_trip() {
    let body = capabilities_body();
    let capabilities = parse_capabilities(&body).unwrap();
    assert_eq!(capabilities.protocol, "dpe/1");
    assert_eq!(capabilities.hash_contracts, vec!["dpe1".to_owned()]);
    assert_eq!(capabilities.content_encodings, vec!["gzip".to_owned()]);
    assert_eq!(capabilities.features, vec!["move".to_owned()]);
    let limits = &capabilities.limits;
    assert_eq!(limits.max_payload_bytes, 8_388_608);
    assert_eq!(limits.page_max_bytes, 268_435_456);
    assert_eq!(limits.staging_ttl_seconds, 86_400);
    assert_eq!(limits.blob_max_bytes, 104_857_600);
    assert_eq!(limits.blob_chunk_bytes, 8_388_608);
    assert_eq!(limits.batch_head_max, 2);
    assert_eq!(limits.list_page_max, 1000);
}

#[test]
fn envelopes_ignore_unknown_members() {
    let mut body = capabilities_body();
    body["future"] = json!(1);
    body["limits"]["new_limit"] = json!(2);
    assert_eq!(
        parse_capabilities(&body).unwrap(),
        parse_capabilities(&capabilities_body()).unwrap()
    );
}

/// 限额必须是正整数（前 6 条是 Python 侧 `test_limits_must_be_positive_integers` 的同一组
/// 用例，其余为类别补充）。
#[test]
fn limits_must_be_positive_integers() {
    let cases: [(&str, Value); 9] = [
        ("batch_head_max", json!(0)),
        ("batch_head_max", json!(-1)),
        ("batch_head_max", json!(true)),
        ("list_page_max", json!("1000")),
        ("blob_chunk_bytes", json!(1.5)),
        ("staging_ttl_seconds", json!(3599)),
        ("max_payload_bytes", json!(8.0)),
        ("max_payload_bytes", json!(null)),
        ("staging_ttl_seconds", json!(0)),
    ];
    for (field, value) in cases {
        let mut body = capabilities_body();
        body["limits"][field] = value.clone();
        let error = capabilities_error(&body);
        assert_eq!(
            error.kind(),
            dpe_sdk::ErrorKind::UnexpectedResponse,
            "{field}={value}"
        );
        assert!(
            error.message().contains("Capabilities"),
            "{field}={value}: {error}"
        );
    }

    // 非限额字段同样严格：str 不接受 int，数组元素必须是字符串，必有成员必须出现
    let mut body = capabilities_body();
    body["protocol"] = json!(1);
    assert!(parse_capabilities(&body).is_err());

    let mut body = capabilities_body();
    body["hash_contracts"] = json!([1]);
    assert!(parse_capabilities(&body).is_err());

    let mut body = capabilities_body();
    body.as_object_mut().unwrap().remove("features");
    assert!(parse_capabilities(&body).is_err());

    assert!(parse_capabilities(&json!([])).is_err());
}

/// 超过 `u64` 上限的整数字面量饱和为 `u64::MAX`（对分批、分块与超限判定与 Python 的任意
/// 精度整数行为等价；见 `Limits` 的说明）。
#[test]
fn limits_saturate_beyond_u64() {
    let text = r#"{"protocol": "dpe/1", "hash_contracts": ["dpe1"],
             "limits": {"max_payload_bytes": 1000000000000000000000000000000,
                        "page_max_bytes": 1, "staging_ttl_seconds": 3600,
                        "blob_max_bytes": 1, "blob_chunk_bytes": 1,
                        "batch_head_max": 1, "list_page_max": 1},
             "content_encodings": [], "features": []}"#;
    let value = dpe_hash::parse_ijson(text).expect("合法 JSON");
    let capabilities = parse_capabilities(&value).unwrap();
    assert_eq!(capabilities.limits.max_payload_bytes, u64::MAX);
}

#[test]
fn batch_heads_and_list_page() {
    let one = h1();
    let heads = BatchHeads::from_value(&json!({"heads": [{"doc_hash": one}, null]})).unwrap();
    assert_eq!(heads.heads.len(), 2);
    assert_eq!(heads.heads[0].as_ref().unwrap().doc_hash, one);
    assert!(heads.heads[1].is_none());

    // heads 项形状不对
    assert!(BatchHeads::from_value(&json!({"heads": [5]})).is_err());
    assert!(BatchHeads::from_value(&json!({"heads": [{}]})).is_err());
    assert!(BatchHeads::from_value(&json!({"heads": null})).is_err());
    assert!(BatchHeads::from_value(&json!({})).is_err());

    // 解析失败的消息按模型名包装（同 Python 的「响应体不是合法的 BatchHeads：…」口径）
    let error = BatchHeads::from_value(&json!({"heads": null})).unwrap_err();
    assert!(error.message().contains("BatchHeads"), "{error}");
    let error =
        ListPage::from_value(&json!({"documents": {"a": 1}, "next_cursor": null})).unwrap_err();
    assert!(error.message().contains("ListPage"), "{error}");

    let page = ListPage::from_value(&json!({
        "documents": [{"file_uri": "x:a", "doc_hash": one}],
        "next_cursor": null,
    }))
    .unwrap();
    assert_eq!(page.documents[0].file_uri, "x:a");
    assert_eq!(page.next_cursor, None);

    // next_cursor 必须出现（可为 null）；documents 必须是数组；doc_hash 必须是字符串
    assert!(ListPage::from_value(&json!({"documents": []})).is_err());
    assert!(ListPage::from_value(&json!({"documents": {"a": 1}, "next_cursor": null})).is_err());
    assert!(ListPage::from_value(&json!({
        "documents": [{"file_uri": "x:a", "doc_hash": 5}],
        "next_cursor": null,
    }))
    .is_err());
}

#[test]
fn skeleton_round_trip() {
    let body = skeleton_body("dpe1", "x:a");
    let skeleton = Skeleton::from_value(&body, "dpe1").unwrap();
    assert_eq!(skeleton.file_uri, "x:a");
    assert_eq!(skeleton.doc_hash, body["doc_hash"].as_str().unwrap());
    assert_eq!(skeleton.document.title, Some(Some("T".to_owned())));
    assert_eq!(skeleton.pages.len(), 3);
    assert_eq!(skeleton.document.pages.len(), 3);
}

#[test]
fn skeleton_objects_stay_closed() {
    let mut body = skeleton_body("dpe1", "x:a");
    body["pages"][0]["page_number"] = json!(1);
    let error = Skeleton::from_value(&body, "dpe1").expect_err("内嵌页对象仍封闭");
    assert_eq!(error.kind(), dpe_sdk::ErrorKind::UnexpectedResponse);
    assert!(error.message().contains("page_number"), "{error}");

    // 子 hash 按显式传入的契约校验
    let body = skeleton_body("dpe2", "x:a");
    assert!(Skeleton::from_value(&body, "dpe1").is_err());
}

#[test]
fn missing_defaults_to_empty_layers() {
    let missing = Missing::default();
    assert!(missing.pages.is_empty());
    assert!(missing.content_hashes.is_empty());
    assert!(missing.blobs.is_empty());
}

#[test]
fn page_upload_missing_truncation_flag() {
    // 页对象上传响应的 missing_truncated（HTTP §4.6）：缺省 false，出现时必须是 JSON 布尔；
    // 与 Python test_page_upload_missing_truncation_flag 对等
    let plain = PageUploadMissing::from_value(&json!({"missing_content_hashes": []})).unwrap();
    assert!(!plain.missing_truncated);
    let flagged = PageUploadMissing::from_value(
        &json!({"missing_content_hashes": [h1()], "missing_truncated": true}),
    )
    .unwrap();
    assert!(flagged.missing_truncated);
    assert_eq!(flagged.missing_content_hashes, vec![h1()]);
    for bad in [json!("true"), json!(1), Value::Null] {
        let body = json!({"missing_content_hashes": [], "missing_truncated": bad});
        assert!(
            PageUploadMissing::from_value(&body).is_err(),
            "missing_truncated={bad} 必须不合规"
        );
    }
}
