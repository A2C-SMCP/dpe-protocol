//! `config_schema` 的编译与求值（connector 契约 §4.1.1–§4.2）。
//!
//! 编译发生在清单校验通过之后。不变量（由 [`crate::run::manifest::parse_manifest`] 保证）：
//! 关键字封闭、schema 位置是对象或布尔值、`$ref` 形式合法且目标存在、`$defs` 无环、JSON 深度
//! 与展开深度均在界内、`pattern` 属于 §4.1.1 子集。因此本模块不重复判定清单合法性，只把 schema
//! 塑成求值树：`$ref` 解析为 `$defs` 下标，`pattern` / `patternProperties` 在此编译为
//! [`Pattern`]（显式码点区间），求值路径不会看到本地引擎的默认正则语义。
//!
//! 求值按 draft 2020-12 语义实现允许关键字子集，与 Python SDK（jsonschema）行为对等：
//! 类型判别区分布尔与数值；`enum` / `const` / `uniqueItems` 的等值按 JSON Schema 语义
//! （`1 == 1.0`、`1 != true`）；`format` 等注解不参与判定。

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Number, Value};

use crate::run::pattern::Pattern;

/// 单次失败：实例中的位置（RFC 6901 JSON Pointer，根为 `""`）与说明。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ConfigFailure {
    pub(crate) path: String,
    pub(crate) message: String,
}

impl ConfigFailure {
    fn new(path: &str, message: String) -> Self {
        ConfigFailure {
            path: path.to_string(),
            message,
        }
    }
}

/// 编译后的 `config_schema`：求值树 + `$defs`（下标 → 节点，`$ref` 以此为跳转目标）。
#[derive(Debug)]
pub(crate) struct CompiledSchema {
    root: Arc<Compiled>,
    defs: Vec<Arc<Compiled>>,
}

impl CompiledSchema {
    /// 编译已通过清单校验的 `config_schema`。不重复校验合法性；不变量被破坏即 panic。
    pub(crate) fn build(schema: &Value) -> CompiledSchema {
        let (names, values): (Vec<&String>, Vec<&Value>) =
            match schema.get("$defs").and_then(Value::as_object) {
                Some(defs) => (defs.keys().collect(), defs.values().collect()),
                None => (Vec::new(), Vec::new()),
            };
        let index: HashMap<&str, usize> = names
            .iter()
            .enumerate()
            .map(|(i, name)| (name.as_str(), i))
            .collect();
        let defs: Vec<Arc<Compiled>> = values
            .iter()
            .map(|value| Arc::new(compile(value, &index)))
            .collect();
        let root = Arc::new(compile(schema, &index));
        CompiledSchema { root, defs }
    }

    /// 按 `config_schema`（§4.2）校验实例配置；返回首个失败。
    pub(crate) fn validate(&self, config: &Value) -> Result<(), ConfigFailure> {
        eval(&self.root, config, "", &self.defs)
    }
}

#[derive(Debug)]
enum Compiled {
    Bool(bool),
    Node(Box<Node>),
}

#[derive(Debug, Default)]
struct Node {
    ty: Option<Vec<JsonType>>,
    properties: HashMap<String, Arc<Compiled>>,
    /// （编译后的 pattern, 原文, 子 schema）；匹配多个模式的属性名各子 schema 都适用。
    pattern_properties: Vec<(Pattern, String, Arc<Compiled>)>,
    pattern: Option<(Pattern, String)>,
    additional_properties: Option<Arc<Compiled>>,
    property_names: Option<Arc<Compiled>>,
    required: Vec<String>,
    items: Option<Arc<Compiled>>,
    all_of: Vec<Arc<Compiled>>,
    any_of: Vec<Arc<Compiled>>,
    one_of: Vec<Arc<Compiled>>,
    not: Option<Arc<Compiled>>,
    if_: Option<Arc<Compiled>>,
    then_: Option<Arc<Compiled>>,
    else_: Option<Arc<Compiled>>,
    ref_index: Option<usize>,
    enum_: Option<Vec<Value>>,
    const_: Option<Value>,
    min_length: Option<u64>,
    max_length: Option<u64>,
    minimum: Option<f64>,
    maximum: Option<f64>,
    exclusive_minimum: Option<f64>,
    exclusive_maximum: Option<f64>,
    min_items: Option<u64>,
    max_items: Option<u64>,
    unique_items: bool,
    min_properties: Option<u64>,
    max_properties: Option<u64>,
    dependent_required: Vec<(String, Vec<String>)>,
}

/// §4.1.1 的 `$ref` 唯一形式：恰为 `#/$defs/<名字>` 一层。解码顺序（先百分号解码、再按 `/`
/// 拆分、最后处理 `~1`/`~0`，RFC 6901 §6）不可换；任何不合形式（含 `%2F` 编码前缀、非法百分号
/// 序列、`~` 后非 0/1、空名字、深于一层、外部 URI）返回 `None`。
pub(crate) fn parse_defs_ref(reference: &str) -> Option<String> {
    let fragment = reference.strip_prefix("#/")?;
    let bytes = fragment.as_bytes();
    // 非法百分号序列（`%` 后不是两位十六进制）一律拒绝
    let mut cursor = 0usize;
    while cursor < bytes.len() {
        if bytes[cursor] == b'%' {
            let hex = bytes.get(cursor + 1..cursor + 3)?;
            if !hex.iter().all(u8::is_ascii_hexdigit) {
                return None;
            }
            cursor += 3;
        } else {
            cursor += 1;
        }
    }
    let mut decoded: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut cursor = 0usize;
    while cursor < bytes.len() {
        if bytes[cursor] == b'%' {
            let hex = std::str::from_utf8(&bytes[cursor + 1..cursor + 3]).ok()?;
            decoded.push(u8::from_str_radix(hex, 16).ok()?);
            cursor += 3;
        } else {
            decoded.push(bytes[cursor]);
            cursor += 1;
        }
    }
    let decoded = String::from_utf8(decoded).ok()?;
    let mut segments = decoded.split('/');
    let first = segments.next()?;
    let name = segments.next()?;
    if segments.next().is_some() || first != "$defs" || name.is_empty() {
        return None;
    }
    let chars: Vec<char> = name.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if c == '~' && !matches!(chars.get(i + 1), Some('0') | Some('1')) {
            return None;
        }
    }
    Some(name.replace("~1", "/").replace("~0", "~"))
}

// ---------------------------------------------------------------------------
// 编译
// ---------------------------------------------------------------------------

fn compile(value: &Value, index: &HashMap<&str, usize>) -> Compiled {
    match value {
        Value::Bool(flag) => Compiled::Bool(*flag),
        Value::Object(map) => {
            let mut node = Node::default();
            for (keyword, value) in map {
                match keyword.as_str() {
                    "type" => node.ty = Some(parse_types(value)),
                    "properties" => {
                        let mut properties = HashMap::new();
                        for (name, sub) in
                            value.as_object().expect("清单校验保证 properties 是对象")
                        {
                            properties.insert(name.clone(), Arc::new(compile(sub, index)));
                        }
                        node.properties = properties;
                    }
                    "patternProperties" => {
                        for (key, sub) in value
                            .as_object()
                            .expect("清单校验保证 patternProperties 是对象")
                        {
                            let pattern = Pattern::new(key)
                                .expect("清单校验保证 patternProperties 键属于 §4.1.1 子集");
                            node.pattern_properties.push((
                                pattern,
                                key.clone(),
                                Arc::new(compile(sub, index)),
                            ));
                        }
                    }
                    "pattern" => {
                        let text = value.as_str().expect("清单校验保证 pattern 是字符串");
                        node.pattern = Some((
                            Pattern::new(text).expect("清单校验保证 pattern 属于 §4.1.1 子集"),
                            text.to_string(),
                        ));
                    }
                    "additionalProperties" => {
                        node.additional_properties = Some(Arc::new(compile(value, index)))
                    }
                    "propertyNames" => node.property_names = Some(Arc::new(compile(value, index))),
                    "required" => {
                        node.required = value
                            .as_array()
                            .expect("清单校验保证 required 是字符串数组")
                            .iter()
                            .map(|item| {
                                item.as_str()
                                    .expect("清单校验保证 required 成员是字符串")
                                    .to_string()
                            })
                            .collect();
                    }
                    "items" => node.items = Some(Arc::new(compile(value, index))),
                    "allOf" | "anyOf" | "oneOf" => {
                        let subs: Vec<Arc<Compiled>> = value
                            .as_array()
                            .expect("清单校验保证组合关键字是 schema 数组")
                            .iter()
                            .map(|sub| Arc::new(compile(sub, index)))
                            .collect();
                        match keyword.as_str() {
                            "allOf" => node.all_of = subs,
                            "anyOf" => node.any_of = subs,
                            _ => node.one_of = subs,
                        }
                    }
                    "not" => node.not = Some(Arc::new(compile(value, index))),
                    "if" => node.if_ = Some(Arc::new(compile(value, index))),
                    "then" => node.then_ = Some(Arc::new(compile(value, index))),
                    "else" => node.else_ = Some(Arc::new(compile(value, index))),
                    "$ref" => {
                        let reference = value.as_str().expect("清单校验保证 $ref 是字符串");
                        let name =
                            parse_defs_ref(reference).expect("清单校验保证 $ref 是唯一合法形式");
                        node.ref_index = Some(
                            *index
                                .get(name.as_str())
                                .expect("清单校验保证 $ref 目标存在于 $defs"),
                        );
                    }
                    "enum" => {
                        node.enum_ =
                            Some(value.as_array().expect("清单校验保证 enum 是数组").clone());
                    }
                    "const" => node.const_ = Some(value.clone()),
                    "minLength" => node.min_length = Some(int_bound(value)),
                    "maxLength" => node.max_length = Some(int_bound(value)),
                    "minItems" => node.min_items = Some(int_bound(value)),
                    "maxItems" => node.max_items = Some(int_bound(value)),
                    "minProperties" => node.min_properties = Some(int_bound(value)),
                    "maxProperties" => node.max_properties = Some(int_bound(value)),
                    "minimum" => node.minimum = Some(numeric_bound(value)),
                    "maximum" => node.maximum = Some(numeric_bound(value)),
                    "exclusiveMinimum" => node.exclusive_minimum = Some(numeric_bound(value)),
                    "exclusiveMaximum" => node.exclusive_maximum = Some(numeric_bound(value)),
                    "uniqueItems" => {
                        node.unique_items =
                            value.as_bool().expect("清单校验保证 uniqueItems 是布尔值");
                    }
                    "dependentRequired" => {
                        for (trigger, names) in value
                            .as_object()
                            .expect("清单校验保证 dependentRequired 是对象")
                        {
                            node.dependent_required.push((
                                trigger.clone(),
                                names
                                    .as_array()
                                    .expect("清单校验保证 dependentRequired 的值是字符串数组")
                                    .iter()
                                    .map(|item| {
                                        item.as_str().expect("清单校验保证成员是字符串").to_string()
                                    })
                                    .collect(),
                            ));
                        }
                    }
                    // 注解关键字（title、default、format 等）与 $defs / $schema 不参与求值
                    _ => {}
                }
            }
            Compiled::Node(Box::new(node))
        }
        _ => unreachable!("清单校验保证 schema 位置是对象或布尔值"),
    }
}

fn parse_types(value: &Value) -> Vec<JsonType> {
    match value {
        Value::String(text) => {
            vec![JsonType::parse(text).expect("清单校验保证 type 取值已知")]
        }
        Value::Array(items) => items
            .iter()
            .map(|item| {
                JsonType::parse(item.as_str().expect("清单校验保证 type 成员是字符串"))
                    .expect("清单校验保证 type 取值已知")
            })
            .collect(),
        _ => unreachable!("清单校验保证 type 是字符串或字符串数组"),
    }
}

/// 非负整数上界（长度类关键字）：超出 u64 的值饱和到 `u64::MAX`——语义不变，
/// 因为可观测长度不可能达到该量级。
fn int_bound(value: &Value) -> u64 {
    let number = value.as_number().expect("清单校验保证非负整数");
    if let Some(exact) = number.as_u64() {
        return exact;
    }
    number.as_f64().map_or(u64::MAX, |f| f as u64)
}

/// 数值边界：清单校验已拒绝越界数值（core §2.8），因此此处必可表示为 f64。
fn numeric_bound(value: &Value) -> f64 {
    value
        .as_number()
        .expect("清单校验保证是数值")
        .as_f64()
        .expect("清单校验已拒绝超出 double 范围的数值")
}

// ---------------------------------------------------------------------------
// 求值
// ---------------------------------------------------------------------------

fn eval(
    schema: &Compiled,
    config: &Value,
    path: &str,
    defs: &[Arc<Compiled>],
) -> Result<(), ConfigFailure> {
    match schema {
        Compiled::Bool(true) => Ok(()),
        Compiled::Bool(false) => Err(ConfigFailure::new(
            path,
            "布尔 schema false 不通过".to_string(),
        )),
        Compiled::Node(node) => eval_node(node, config, path, defs),
    }
}

fn eval_node(
    node: &Node,
    config: &Value,
    path: &str,
    defs: &[Arc<Compiled>],
) -> Result<(), ConfigFailure> {
    if let Some(index) = node.ref_index {
        eval(&defs[index], config, path, defs)?;
    }
    if let Some(types) = &node.ty {
        if !types.iter().any(|json_type| json_type.matches(config)) {
            let names: Vec<&str> = types.iter().map(|json_type| json_type.name()).collect();
            return Err(ConfigFailure::new(
                path,
                format!("值不是 {} 类型", names.join(" / ")),
            ));
        }
    }
    if let Some(values) = &node.enum_ {
        if !values.iter().any(|value| json_equal(value, config)) {
            return Err(ConfigFailure::new(path, "值不在 enum 中".to_string()));
        }
    }
    if let Some(value) = &node.const_ {
        if !json_equal(value, config) {
            return Err(ConfigFailure::new(path, "值与 const 不相等".to_string()));
        }
    }
    for sub in &node.all_of {
        eval(sub, config, path, defs)?;
    }
    if !node.any_of.is_empty()
        && !node
            .any_of
            .iter()
            .any(|sub| eval(sub, config, path, defs).is_ok())
    {
        return Err(ConfigFailure::new(
            path,
            "anyOf 至少需一个分支通过".to_string(),
        ));
    }
    if !node.one_of.is_empty() {
        let passed = node
            .one_of
            .iter()
            .filter(|sub| eval(sub, config, path, defs).is_ok())
            .count();
        if passed != 1 {
            return Err(ConfigFailure::new(
                path,
                format!("oneOf 恰需一个分支通过（通过 {passed} 个）"),
            ));
        }
    }
    if let Some(sub) = &node.not {
        if eval(sub, config, path, defs).is_ok() {
            return Err(ConfigFailure::new(path, "not 分支通过".to_string()));
        }
    }
    if let Some(if_schema) = &node.if_ {
        if eval(if_schema, config, path, defs).is_ok() {
            if let Some(then_schema) = &node.then_ {
                eval(then_schema, config, path, defs)?;
            }
        } else if let Some(else_schema) = &node.else_ {
            eval(else_schema, config, path, defs)?;
        }
    }
    if let Value::Object(map) = config {
        for name in &node.required {
            if !map.contains_key(name) {
                return Err(ConfigFailure::new(path, format!("缺少必填属性 {name:?}")));
            }
        }
        for (name, value) in map {
            let child = child_path(path, name);
            let mut matched = false;
            if let Some(sub) = node.properties.get(name) {
                matched = true;
                eval(sub, value, &child, defs)?;
            }
            for (pattern, original, sub) in &node.pattern_properties {
                if pattern.is_match(name) {
                    matched = true;
                    let result = eval(sub, value, &child, defs);
                    if let Err(failure) = result {
                        return Err(ConfigFailure::new(
                            &failure.path,
                            format!(
                                "（属性 {name:?} 匹配 pattern {original:?}）{}",
                                failure.message
                            ),
                        ));
                    }
                }
            }
            if !matched {
                if let Some(additional) = &node.additional_properties {
                    match additional.as_ref() {
                        Compiled::Bool(false) => {
                            return Err(ConfigFailure::new(
                                &child,
                                format!("出现未允许的属性 {name:?}"),
                            ));
                        }
                        other => eval(other, value, &child, defs)?,
                    }
                }
            }
        }
        if let Some(sub) = &node.property_names {
            for name in map.keys() {
                if let Err(failure) = eval(sub, &Value::String(name.clone()), path, defs) {
                    return Err(ConfigFailure::new(
                        &failure.path,
                        format!("属性名 {name:?}：{}", failure.message),
                    ));
                }
            }
        }
        if let Some(min) = node.min_properties {
            if (map.len() as u64) < min {
                return Err(ConfigFailure::new(
                    path,
                    format!("属性个数少于 minProperties（{} < {min}）", map.len()),
                ));
            }
        }
        if let Some(max) = node.max_properties {
            if (map.len() as u64) > max {
                return Err(ConfigFailure::new(
                    path,
                    format!("属性个数多于 maxProperties（{} > {max}）", map.len()),
                ));
            }
        }
        for (trigger, names) in &node.dependent_required {
            if map.contains_key(trigger) {
                for name in names {
                    if !map.contains_key(name) {
                        return Err(ConfigFailure::new(
                            path,
                            format!(
                                "属性 {trigger:?} 存在时要求属性 {name:?}（dependentRequired）"
                            ),
                        ));
                    }
                }
            }
        }
    }
    if let Value::Array(items) = config {
        if let Some(sub) = &node.items {
            for (index, item) in items.iter().enumerate() {
                eval(sub, item, &index_path(path, index), defs)?;
            }
        }
        if let Some(min) = node.min_items {
            if (items.len() as u64) < min {
                return Err(ConfigFailure::new(
                    path,
                    format!("数组长度少于 minItems（{} < {min}）", items.len()),
                ));
            }
        }
        if let Some(max) = node.max_items {
            if (items.len() as u64) > max {
                return Err(ConfigFailure::new(
                    path,
                    format!("数组长度多于 maxItems（{} > {max}）", items.len()),
                ));
            }
        }
        if node.unique_items {
            // O(n²) 两两比较：配置规模由使用者掌握，以正确性优先；Python 侧（jsonschema 可排序时
            // 先排序做相邻比较）的复杂度更低——大数组场景再按需优化，此处记录差异。
            for i in 0..items.len() {
                for j in (i + 1)..items.len() {
                    if json_equal(&items[i], &items[j]) {
                        return Err(ConfigFailure::new(
                            path,
                            format!("数组元素 {i} 与 {j} 重复（uniqueItems）"),
                        ));
                    }
                }
            }
        }
    }
    if let Value::String(text) = config {
        if let Some((pattern, original)) = &node.pattern {
            if !pattern.is_match(text) {
                return Err(ConfigFailure::new(
                    path,
                    format!("值 {text:?} 与 pattern {original:?} 不匹配"),
                ));
            }
        }
        let length = text.chars().count() as u64;
        if let Some(min) = node.min_length {
            if length < min {
                return Err(ConfigFailure::new(
                    path,
                    format!("字符串长度少于 minLength（{length} < {min}）"),
                ));
            }
        }
        if let Some(max) = node.max_length {
            if length > max {
                return Err(ConfigFailure::new(
                    path,
                    format!("字符串长度多于 maxLength（{length} > {max}）"),
                ));
            }
        }
    }
    if let Value::Number(number) = config {
        let value = number_as_f64(number);
        // 数值可达 ±∞（越界的字面量，如 1e400），但不可为 NaN——直接比较即完整语义
        if let Some(min) = node.minimum {
            if value < min {
                return Err(ConfigFailure::new(
                    path,
                    format!("数值小于 minimum（{value} < {min}）"),
                ));
            }
        }
        if let Some(max) = node.maximum {
            if value > max {
                return Err(ConfigFailure::new(
                    path,
                    format!("数值大于 maximum（{value} > {max}）"),
                ));
            }
        }
        if let Some(min) = node.exclusive_minimum {
            if value <= min {
                return Err(ConfigFailure::new(
                    path,
                    format!("数值不大于 exclusiveMinimum（{value} <= {min}）"),
                ));
            }
        }
        if let Some(max) = node.exclusive_maximum {
            if value >= max {
                return Err(ConfigFailure::new(
                    path,
                    format!("数值不小于 exclusiveMaximum（{value} >= {max}）"),
                ));
            }
        }
    }
    Ok(())
}

fn child_path(path: &str, segment: &str) -> String {
    format!("{path}/{}", segment.replace('~', "~0").replace('/', "~1"))
}

fn index_path(path: &str, index: usize) -> String {
    format!("{path}/{index}")
}

// ---------------------------------------------------------------------------
// 类型与等值（JSON Schema 语义）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum JsonType {
    Object,
    Array,
    String,
    Number,
    Integer,
    Boolean,
    Null,
}

/// `type` 取值是否是 draft 2020-12 的已知类型（§4.1.1 封闭关键字子集的元层检查）。
pub(crate) fn is_known_type(text: &str) -> bool {
    JsonType::parse(text).is_some()
}

impl JsonType {
    fn parse(text: &str) -> Option<JsonType> {
        Some(match text {
            "object" => JsonType::Object,
            "array" => JsonType::Array,
            "string" => JsonType::String,
            "number" => JsonType::Number,
            "integer" => JsonType::Integer,
            "boolean" => JsonType::Boolean,
            "null" => JsonType::Null,
            _ => return None,
        })
    }

    fn name(self) -> &'static str {
        match self {
            JsonType::Object => "object",
            JsonType::Array => "array",
            JsonType::String => "string",
            JsonType::Number => "number",
            JsonType::Integer => "integer",
            JsonType::Boolean => "boolean",
            JsonType::Null => "null",
        }
    }

    fn matches(self, value: &Value) -> bool {
        match self {
            JsonType::Object => value.is_object(),
            JsonType::Array => value.is_array(),
            JsonType::String => value.is_string(),
            // 布尔不是数值（core §2.8 的 JSON Schema 语义）
            JsonType::Number => value.is_number(),
            JsonType::Integer => match value {
                Value::Number(number) => is_integral(number),
                _ => false,
            },
            JsonType::Boolean => value.is_boolean(),
            JsonType::Null => value.is_null(),
        }
    }
}

/// 整数形式字面量（无小数点/指数）——任意位数的整数都是 integer。
pub(crate) fn int_form(number: &Number) -> bool {
    !number.to_string().contains(['.', 'e', 'E'])
}

/// JSON Schema 的 `integer`：字面量为整数形式，或小数部分为零（`1.0`、`1e2` 是整数；`inf` 不是）。
fn is_integral(number: &Number) -> bool {
    int_form(number)
        || number
            .as_f64()
            .is_some_and(|f| f.is_finite() && f.fract() == 0.0)
}

/// 数值比较用 f64；超出 double 范围的量（`1e400`）按 ±∞ 参与比较——
/// 与 Python（`json.loads` 得到 `inf`）一致。
fn number_as_f64(number: &Number) -> f64 {
    number.as_f64().unwrap_or_else(|| {
        if number.to_string().starts_with('-') {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        }
    })
}

/// JSON Schema 的等值：`1 == 1.0`，`0 != false`；数组/对象逐元素递归。
///
/// 整数形式之间按字面量精确比较（任意位数）；混合形式（`1` 与 `1.0`）经 f64 比较——
/// 超出 2^53 的混合形式按 f64 舍入后的值比较（如 const 为 `1.152921504606847e18`、config 为
/// `2^60+1` 时 Python 的精确比较判不通过、这里可能判通过）。该取舍同样作用于 `const` / `enum` /
/// `uniqueItems`；合法 I-JSON 数据不含此类输入（core §2.6，两个 SDK 的装载器都先行拒绝）。
fn json_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => number_equal(x, y),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(i, j)| json_equal(i, j))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(key, value)| y.get(key).is_some_and(|other| json_equal(value, other)))
        }
        _ => a == b,
    }
}

fn number_equal(x: &Number, y: &Number) -> bool {
    if int_form(x) && int_form(y) {
        return normalize_int(&x.to_string()) == normalize_int(&y.to_string());
    }
    match (x.as_f64(), y.as_f64()) {
        (Some(left), Some(right)) => left == right,
        _ => x.to_string() == y.to_string(),
    }
}

/// 整数字面量的规范形（去前导零与 `-0`）。
fn normalize_int(text: &str) -> String {
    let negative = text.starts_with('-');
    let digits = text.trim_start_matches('-').trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    if digits == "0" {
        "0".to_string()
    } else if negative {
        format!("-{digits}")
    } else {
        digits.to_string()
    }
}
