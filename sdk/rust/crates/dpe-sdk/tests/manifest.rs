//! 清单与实例配置校验的行为测试（connector 契约 §4.1–§4.2；§4.1.1 向量的消费见
//! pattern_vectors.rs）。与 Python SDK 的 `test_run_manifest.py` 同口径。

use serde_json::{json, Map, Value};

use dpe_sdk::run::manifest::{
    parse_manifest, validate_config, Manifest, MANIFEST_FILENAME, MAX_JSON_DEPTH,
};

/// §4.1 的示例清单。
fn example() -> Value {
    json!({
        "manifest_version": 1,
        "name": "git-connector",
        "version": "0.1.0",
        "description": "以 Git 仓库为数据源",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"repo": {"type": "string"}, "ref": {"type": "string", "default": "main"}},
            "required": ["repo"],
            "additionalProperties": false,
        },
        "secrets": [{"name": "GIT_TOKEN", "description": "读取私有仓库的访问令牌", "required": false}],
    })
}

fn mutated(mutate: impl FnOnce(&mut Map<String, Value>)) -> Value {
    let mut value = example();
    mutate(value.as_object_mut().unwrap());
    value
}

fn with_config_schema(schema: Value) -> Value {
    mutated(|map| {
        map.insert("config_schema".to_string(), schema);
    })
}

fn manifest_with(schema: Value) -> Manifest {
    parse_manifest(&with_config_schema(schema)).expect("清单应有效")
}

fn code_of(data: &Value) -> &'static str {
    parse_manifest(data).expect_err("清单应不合法").code()
}

#[test]
fn example_manifest_is_valid() {
    assert_eq!(MANIFEST_FILENAME, "dpe-connector.json");
    assert_eq!(MAX_JSON_DEPTH, 64);
    let manifest = parse_manifest(&example()).unwrap();
    assert_eq!(manifest.manifest_version(), 1);
    assert_eq!(manifest.name(), "git-connector");
    assert_eq!(manifest.version(), "0.1.0");
    assert_eq!(manifest.description(), Some("以 Git 仓库为数据源"));
    assert_eq!(manifest.protocol_versions(), ["dpe-connector/1"]);
    assert_eq!(manifest.secret_names(), ["GIT_TOKEN"].into());
    assert!(!manifest.secrets()[0].required());
}

#[test]
fn secret_required_defaults_to_true() {
    let data = mutated(|map| {
        map.insert("secrets".to_string(), json!([{"name": "TOKEN"}]));
    });
    assert!(parse_manifest(&data).unwrap().secrets()[0].required());
    let data = mutated(|map| {
        map.remove("secrets");
    });
    assert!(parse_manifest(&data).unwrap().secrets().is_empty());
}

#[test]
fn invalid_manifests_are_rejected() {
    let cases: Vec<Value> = vec![
        mutated(|map| {
            map.insert("extra".to_string(), json!(1)); // 封闭 schema
        }),
        mutated(|map| {
            map["secrets"][0]["extra"] = json!(1); // secrets 项同样封闭
        }),
        mutated(|map| {
            map.insert("manifest_version".to_string(), json!(2));
        }),
        mutated(|map| {
            map.insert("manifest_version".to_string(), json!(true));
        }),
        mutated(|map| {
            map.insert("manifest_version".to_string(), json!("1"));
        }),
        mutated(|map| {
            map.insert("name".to_string(), json!(""));
        }),
        mutated(|map| {
            map.insert("version".to_string(), json!(1));
        }),
        mutated(|map| {
            map.insert("protocol_versions".to_string(), json!([]));
        }),
        mutated(|map| {
            map.insert("protocol_versions".to_string(), json!([1]));
        }),
        mutated(|map| {
            map.insert("description".to_string(), Value::Null);
        }),
        mutated(|map| {
            map.remove("config_schema");
        }),
        mutated(|map| {
            map.insert("config_schema".to_string(), json!({"type": "string"}));
        }),
        mutated(|map| {
            map.insert("config_schema".to_string(), json!({"properties": {}}));
        }),
        mutated(|map| {
            map.insert(
                "config_schema".to_string(),
                json!({"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"}),
            );
        }),
        mutated(|map| {
            map.insert(
                "config_schema".to_string(),
                json!({"type": "object", "properties": {"repo": {"type": 5}}}), // 元层：type 取值不合法
            );
        }),
        mutated(|map| {
            map["secrets"][0]["name"] = json!("git_token");
        }),
        mutated(|map| {
            map["secrets"][0]["name"] = json!("DPE_TOKEN");
        }),
        mutated(|map| {
            map["secrets"][0]["name"] = json!("PATH");
        }),
        mutated(|map| {
            map["secrets"][0]["name"] = json!("LC_TOKEN");
        }),
        mutated(|map| {
            map["secrets"][0]["name"] = json!("SYSTEMROOT"); // Windows 基础环境，任何平台都拒绝
        }),
        mutated(|map| {
            map["secrets"][0]["required"] = json!("yes");
        }),
        mutated(|map| {
            map.insert("secrets".to_string(), json!([{"name": "A"}, {"name": "A"}]));
        }),
    ];
    for data in cases {
        assert_eq!(code_of(&data), "manifest_invalid", "{data}");
    }
}

#[test]
fn config_validation() {
    let manifest = parse_manifest(&example()).unwrap();
    let config = json!({"repo": "https://example.com/r.git"});
    validate_config(&manifest, &config).unwrap();
    for bad in [
        json!({}),
        json!({"repo": 1}),
        json!({"repo": "r", "token": "x"}),
    ] {
        let failure = validate_config(&manifest, &bad).expect_err("配置应被拒绝");
        assert_eq!(failure.code(), "config_schema_violation", "{bad}");
    }
}

#[test]
fn format_is_annotation_only() {
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {"mail": {"type": "string", "format": "email"}},
    }));
    validate_config(&manifest, &json!({"mail": "not-an-email"})).unwrap();
}

#[test]
fn internal_ref_resolves() {
    let manifest = manifest_with(json!({
        "type": "object",
        "$defs": {"name": {"type": "string"}},
        "properties": {"repo": {"$ref": "#/$defs/name"}},
    }));
    validate_config(&manifest, &json!({"repo": "r"})).unwrap();
    assert!(validate_config(&manifest, &json!({"repo": 1})).is_err());
}

#[test]
fn schema_must_be_self_contained() {
    let draft = "https://json-schema.org/draft/2020-12/schema";
    let bad_schemas = [
        // 配置不触及该属性时也必须拒绝：清单合法性只取决于清单本身
        json!({"type": "object", "properties": {"repo": {"$ref": "https://example.com/s.json"}}}),
        json!({"type": "object", "properties": {"repo": {"$ref": "other.json#/x"}}}),
        // 元模式 URI 同样是文档外引用
        json!({"type": "object", "properties": {"repo": {"$ref": draft}}}),
        json!({"type": "object", "properties": {"repo": {"$ref": "#/$defs/absent"}}}),
        json!({"type": "object", "properties": {"repo": {"type": "string", "pattern": "\\p{L}"}}}),
        json!({"type": "object", "patternProperties": {"(": {}}}),
        serde_json::from_str::<Value>(
            r#"{"type": "object", "properties": {"n": {"maximum": 1e400}}}"#,
        )
        .unwrap(),
    ];
    for schema in bad_schemas {
        assert_eq!(
            code_of(&with_config_schema(schema.clone())),
            "manifest_invalid",
            "{schema}"
        );
    }
}

#[test]
fn defs_refs_resolve_and_decode() {
    // 名字按 RFC 6901 §6 解码：先百分号解码、再按 / 拆分、最后处理 ~1/~0
    let manifest = manifest_with(json!({
        "type": "object",
        "$defs": {"name": {"type": "string", "minLength": 2}},
        "properties": {"a": {"$ref": "#/$defs/name"}, "b": {"$ref": "#/$defs/name"}},
    }));
    validate_config(&manifest, &json!({"a": "ok", "b": "ok"})).unwrap();
    assert!(validate_config(&manifest, &json!({"a": "x"})).is_err());

    let spaced = manifest_with(json!({
        "type": "object",
        "$defs": {"a b": {"type": "string", "pattern": "^a$"}},
        "properties": {"x": {"$ref": "#/$defs/a%20b"}},
    }));
    validate_config(&spaced, &json!({"x": "a"})).unwrap();

    let slashed = manifest_with(json!({
        "type": "object",
        "$defs": {"a/b": true},
        "properties": {"x": {"$ref": "#/$defs/a%7E1b"}},
    }));
    validate_config(&slashed, &json!({"x": 1})).unwrap();

    let non_ascii = manifest_with(json!({
        "type": "object",
        "$defs": {"é": {"type": "string", "pattern": "^a$"}},
        "properties": {"x": {"$ref": "#/$defs/%C3%A9"}},
    }));
    validate_config(&non_ascii, &json!({"x": "a"})).unwrap();

    // 前缀必须字面 #/；非法百分号序列与 ~ 后非 0/1 一律拒绝
    let defs = json!({"name": true, "a~2b": true, "a%ZZb": true});
    for reference in ["#%2F$defs%2Fname", "#/$defs/a~2b", "#/$defs/a%ZZb"] {
        let schema = json!({
            "type": "object",
            "$defs": defs,
            "properties": {"x": {"$ref": reference}},
        });
        assert_eq!(
            code_of(&with_config_schema(schema)),
            "manifest_invalid",
            "{reference}"
        );
    }
    for reference in [
        "#",
        "#name",
        "#/$defs/a/b",
        "#/properties/x",
        "other.json#/$defs/x",
    ] {
        let schema = json!({
            "type": "object",
            "properties": {"x": {"$ref": reference}},
        });
        assert_eq!(
            code_of(&with_config_schema(schema)),
            "manifest_invalid",
            "{reference}"
        );
    }
}

#[test]
fn closed_keyword_subset() {
    let banned = [
        json!({"properties": {"x": {"$id": "https://self/x"}}}),
        json!({"properties": {"x": {"$anchor": "n"}}}),
        json!({"properties": {"x": {"$dynamicRef": "#m"}}}),
        json!({"definitions": {"d": {}}}),
        json!({"dependencies": {"a": ["b"]}}),
        json!({"properties": {"x": {"contentSchema": {"type": "string"}}}}),
        json!({"properties": {"x": {"multipleOf": 2}}}),
        json!({"properties": {"x": {"customKeyword": 1}}}),
        json!({"unevaluatedProperties": false}),
        json!({"prefixItems": [{"type": "string"}]}),
        json!({"properties": {"x": {"$defs": {"d": {}}}}}), // $defs 非根
        json!({"properties": {"x": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}}),
        json!({"$schema": "http://json-schema.org/draft-07/schema#"}),
        json!({"type": "array", "items": [{"type": "string"}]}), // items 数组形式
    ];
    for extra in banned {
        let mut schema = json!({"type": "object"});
        schema
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        assert_eq!(
            code_of(&with_config_schema(schema.clone())),
            "manifest_invalid",
            "{schema}"
        );
    }

    let allowed = json!({
        "type": "object",
        "$schema": "https://json-schema.org/draft/2020-12/schema#",
        "$comment": "说明",
        "properties": {"x": {"type": "string", "minLength": 1, "maxLength": 5, "pattern": "^a$"}},
        "if": {"required": ["x"]},
        "then": {"required": ["x"]},
        "propertyNames": {"pattern": "^[a-z]+$"},
        "additionalProperties": false,
    });
    parse_manifest(&with_config_schema(allowed)).unwrap();
}

fn chained_defs(n: usize, layers: usize) -> Value {
    let mut defs = Map::new();
    for i in 0..n {
        let mut node = if i + 1 < n {
            json!({"$ref": format!("#/$defs/d{}", i + 1)})
        } else {
            json!({"type": "string", "pattern": "^a$"})
        };
        for _ in 0..layers {
            node = json!({"allOf": [node]});
        }
        defs.insert(format!("d{i}"), node);
    }
    Value::Object(defs)
}

fn schema_with(n: usize, layers: usize) -> Value {
    json!({
        "type": "object",
        "$defs": chained_defs(n, layers),
        "properties": {"x": {"$ref": "#/$defs/d0"}},
    })
}

#[test]
fn expanded_depth_bound() {
    // 顶格：纯引用链 62 跳（展开深度 64）——必须能求值
    let manifest = manifest_with(schema_with(62, 0));
    validate_config(&manifest, &json!({"x": "a"})).unwrap();
    assert!(validate_config(&manifest, &json!({"x": "b"})).is_err());

    // 链 × 原地嵌套（31 跳 × 1 层 allOf）同样在界内
    let mixed = manifest_with(schema_with(31, 1));
    validate_config(&mixed, &json!({"x": "a"})).unwrap();

    // 越界：63 跳 / 32 跳 × 1 层
    for schema in [schema_with(63, 0), schema_with(32, 1)] {
        assert_eq!(code_of(&with_config_schema(schema)), "manifest_invalid");
    }
}

#[test]
fn manifest_json_depth_bound() {
    // §4.1：清单文件的 JSON 嵌套深度 ≤ 64，超出（含数据位置上的深度）判 manifest_invalid
    fn nested_properties(levels: usize) -> Value {
        let mut node = json!({"type": "object"});
        for _ in 0..levels {
            node = json!({"type": "object", "properties": {"a": node}});
        }
        node
    }
    fn nested_array(levels: usize) -> Value {
        let mut node = json!("x");
        for _ in 0..levels {
            node = json!([node]);
        }
        node
    }

    let manifest = manifest_with(nested_properties(30)); // 清单深度 63
    validate_config(&manifest, &json!({})).unwrap();
    assert_eq!(
        code_of(&with_config_schema(nested_properties(31))),
        "manifest_invalid"
    ); // 65

    manifest_with(json!({"type": "object", "default": nested_array(61)})); // 深度 64
    assert_eq!(
        code_of(&with_config_schema(
            json!({"type": "object", "default": nested_array(62)})
        )),
        "manifest_invalid" // 65
    );
}

#[test]
fn deeply_nested_schema_and_config_are_classified() {
    // 深度取 1000（远深于上界 64）：serde_json 对内存值（`Value`）的构造与析构是递归的，
    // 深度需留在测试线程 2 MiB 栈的余量内；清单校验自身的 JSON 深度判定、越界数值扫描与
    // 配置求值都是迭代/有界实现，不随输入深度递归。
    let mut deep = json!({"type": "object"});
    for _ in 0..1000 {
        deep = json!({"type": "object", "properties": {"a": deep}});
    }
    assert_eq!(code_of(&with_config_schema(deep)), "manifest_invalid");

    // 封闭模型下不存在递归引用（$defs 引用必须无环），求值深度由 schema 的静态深度决定：
    // 宽松 schema 配极深配置直接通过，不会递归崩溃
    let manifest = manifest_with(json!({"type": "object"}));
    let mut config = json!({});
    for _ in 0..1000 {
        config = json!({"a": config});
    }
    validate_config(&manifest, &config).unwrap();
}

#[test]
fn json_schema_equality_semantics() {
    // enum：1 与 1.0 相等
    let manifest = manifest_with(json!({"type": "object", "properties": {"n": {"enum": [1]}}}));
    validate_config(&manifest, &json!({"n": 1})).unwrap();
    validate_config(&manifest, &json!({"n": 1.0})).unwrap();
    assert!(validate_config(&manifest, &json!({"n": 2})).is_err());

    // const：0 与 false 不等（布尔不是数值）
    let manifest = manifest_with(json!({"type": "object", "properties": {"c": {"const": 0}}}));
    validate_config(&manifest, &json!({"c": 0.0})).unwrap();
    for bad in [json!({"c": false}), json!({"c": "0"})] {
        assert!(validate_config(&manifest, &bad).is_err(), "{bad}");
    }

    // uniqueItems：1 与 1.0 视为相同，0 与 false 视为不同
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {"x": {"type": "array", "uniqueItems": true}},
    }));
    assert!(validate_config(&manifest, &json!({"x": [1, 1.0]})).is_err());
    validate_config(&manifest, &json!({"x": [0, false]})).unwrap();

    // enum 为空：合法但任何值都不通过
    let manifest = manifest_with(json!({"type": "object", "properties": {"x": {"enum": []}}}));
    assert!(validate_config(&manifest, &json!({"x": 1})).is_err());
}

#[test]
fn config_semantics() {
    // oneOf 恰一分支
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {"x": {"oneOf": [{"type": "integer"}, {"maximum": 2}]}},
    }));
    assert!(validate_config(&manifest, &json!({"x": 1})).is_err()); // 两个分支同时命中
    validate_config(&manifest, &json!({"x": 5})).unwrap();

    // if/then/else
    let manifest = manifest_with(json!({
        "type": "object",
        "if": {"required": ["a"]},
        "then": {"required": ["b"]},
    }));
    validate_config(&manifest, &json!({"b": 1})).unwrap(); // if 不成立、无 else
    validate_config(&manifest, &json!({"a": 1, "b": 1})).unwrap();
    assert!(validate_config(&manifest, &json!({"a": 1})).is_err());

    // dependentRequired
    let manifest = manifest_with(json!({
        "type": "object",
        "dependentRequired": {"a": ["b"]},
    }));
    validate_config(&manifest, &json!({"b": 1})).unwrap();
    validate_config(&manifest, &json!({"a": 1, "b": 1})).unwrap();
    assert!(validate_config(&manifest, &json!({"a": 1})).is_err());

    // propertyNames 按子集语义匹配属性名
    let manifest = manifest_with(json!({
        "type": "object",
        "propertyNames": {"pattern": "^[a-z]+$"},
    }));
    validate_config(&manifest, &json!({"abc": 1})).unwrap();
    assert!(validate_config(&manifest, &json!({"é": 1})).is_err());

    // items / minItems / exclusiveMinimum / not / type 数组
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {
            "xs": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "n": {"exclusiveMinimum": 0},
            "s": {"not": {"type": "string"}},
            "t": {"type": ["string", "null"]},
        },
    }));
    validate_config(&manifest, &json!({"xs": [1, 2], "n": 1, "s": 1, "t": null})).unwrap();
    for bad in [
        json!({"xs": []}),
        json!({"xs": [1, "a"]}),
        json!({"n": 0}),
        json!({"s": "a"}),
        json!({"t": 1}),
    ] {
        assert!(validate_config(&manifest, &bad).is_err(), "{bad}");
    }
}
