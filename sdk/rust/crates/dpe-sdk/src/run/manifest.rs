//! 插件清单与实例配置校验（connector 契约 §4.1–§4.2）。
//!
//! 清单是静态声明，不启动插件即可读取。[`parse_manifest`] 完成 §4.1 的全部静态检查：
//! 封闭成员与字段类型、清单文件的 JSON 嵌套深度 ≤ 64、`config_schema`（含 `$defs` 中的子 schema）
//! 只使用 §4.1.1 的封闭关键字子集、`$ref` 只有 `#/$defs/<名字>` 一种形式（`$defs` 仅根、引用
//! 无环、目标存在）、schema 的展开深度 ≤ 64、`pattern` 与 `patternProperties` 的键属于 §4.1.1
//! 子集。从不访问网络或文件：引用无法在文档内解析的清单在读取时即被拒绝，因此清单是否合法
//! 只取决于清单本身，与配置内容无关。
//!
//! [`validate_config`] 按清单的 `config_schema`（draft 2020-12 语义）校验实例配置；`format` 等
//! 注解不参与判定，`default` 不填充、配置原样传递（§4.2）。与 Python SDK 的
//! `dpe_sdk.run.manifest` 行为对等。

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde_json::{Map, Number, Value};

use crate::run::pattern::check_pattern;
use crate::run::schema::{int_form, is_known_type, parse_defs_ref, CompiledSchema};
use crate::run::InstanceFailure;

/// 清单的文件名（§4.1）；`plugin.manifest` 指向的文件不强制此名。
pub const MANIFEST_FILENAME: &str = "dpe-connector.json";

/// §4.1 / §4.4：清单与实例定义文件的 JSON 嵌套深度上限。
pub const MAX_JSON_DEPTH: usize = 64;

/// §4.1.1：schema 的展开深度上限（根计 1，schema 位置与 `$ref` 跳转各 +1）。
const MAX_EXPANDED_DEPTH: u64 = 64;

/// core §2.6：整数绝对值上限（与 JCS 一致）。
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

const DRAFT_2020_12: &str = "https://json-schema.org/draft/2020-12/schema";

/// §4.1：清单的封闭成员集。
const MANIFEST_MEMBERS: [&str; 7] = [
    "manifest_version",
    "name",
    "version",
    "description",
    "protocol_versions",
    "config_schema",
    "secrets",
];

/// §4.1：`secrets` 每项的封闭成员集。
const SECRET_MEMBERS: [&str; 3] = ["name", "description", "required"];

/// §4.3 基础环境（§4.3 第 1 部分）的全平台名单，另加全部 `LC_*`。
const BASE_ENV_NAMES: [&str; 11] = [
    "PATH",
    "HOME",
    "LANG",
    "TZ",
    "TMPDIR", // POSIX
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "PATHEXT",
    "COMSPEC", // Windows
];

/// §4.1.1 封闭关键字子集：`config_schema`（含 `$defs` 中与所有嵌套位置）只允许这些关键字。
const ALLOWED_KEYWORDS: [&str; 43] = [
    // 结构与组合
    "type",
    "properties",
    "patternProperties",
    "additionalProperties",
    "required",
    "propertyNames",
    "items",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "$defs",
    // 引用与方言
    "$ref",
    "$schema",
    // 通用
    "enum",
    "const",
    // 字符串 / 数值 / 数组 / 对象
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "dependentRequired",
    // 注解
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "format",
    "contentEncoding",
    "contentMediaType",
    "$comment",
];

/// 值为 schema 的关键字（数组见 [`SUBSCHEMA_LIST`]，映射见 [`SUBSCHEMA_MAP`]）。
const SUBSCHEMA_VALUE: [&str; 7] = [
    "additionalProperties",
    "propertyNames",
    "items",
    "not",
    "if",
    "then",
    "else",
];
const SUBSCHEMA_LIST: [&str; 3] = ["allOf", "anyOf", "oneOf"];
const SUBSCHEMA_MAP: [&str; 2] = ["properties", "patternProperties"];

/// 清单 `secrets` 的一项（§4.1）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretDeclaration {
    name: String,
    description: Option<String>,
    required: bool,
}

impl SecretDeclaration {
    /// 注入插件进程的环境变量名。
    pub fn name(&self) -> &str {
        &self.name
    }

    /// 供宿主渲染表单的说明。
    pub fn description(&self) -> Option<&str> {
        self.description.as_deref()
    }

    /// 缺省 true；`false` 表示无法解析时不注入（§4.3）。
    pub fn required(&self) -> bool {
        self.required
    }
}

/// 插件清单 `dpe-connector.json`（§4.1），封闭 schema；构造即通过全部静态检查。
#[derive(Debug)]
pub struct Manifest {
    manifest_version: u32,
    name: String,
    version: String,
    description: Option<String>,
    protocol_versions: Vec<String>,
    config_schema: Value,
    secrets: Vec<SecretDeclaration>,
    compiled: CompiledSchema,
}

impl Manifest {
    /// 本规范定义的清单版本（§4.1，恒为 1）。
    pub fn manifest_version(&self) -> u32 {
        self.manifest_version
    }

    /// MUST 与 initialize 响应的 `plugin.name` 相同（§4.1）。
    pub fn name(&self) -> &str {
        &self.name
    }

    /// MUST 与 initialize 响应的 `plugin.version` 相同（§4.1）。
    pub fn version(&self) -> &str {
        &self.version
    }

    /// 供展示的说明。
    pub fn description(&self) -> Option<&str> {
        self.description.as_deref()
    }

    /// 插件支持的线协议版本（§6.2）。
    pub fn protocol_versions(&self) -> &[String] {
        &self.protocol_versions
    }

    /// 实例配置的 JSON Schema（draft 2020-12；§4.1.1 封闭子集）。
    pub fn config_schema(&self) -> &Value {
        &self.config_schema
    }

    /// 数据源凭证声明（§4.3）。
    pub fn secrets(&self) -> &[SecretDeclaration] {
        &self.secrets
    }

    /// 已声明的凭证名集合。
    pub fn secret_names(&self) -> BTreeSet<&str> {
        self.secrets
            .iter()
            .map(|secret| secret.name.as_str())
            .collect()
    }
}

/// 校验已解析的清单；不合法时返回 `manifest_invalid` 的 [`InstanceFailure`]。
pub fn parse_manifest(data: &Value) -> Result<Manifest, InstanceFailure> {
    let map = data
        .as_object()
        .ok_or_else(|| invalid("清单必须是 JSON 对象"))?;
    // §4.1：JSON 嵌套深度 ≤ 64（读取阶段判定，先于任何语义校验）
    if json_depth(data) > MAX_JSON_DEPTH {
        return Err(invalid(format!(
            "清单的 JSON 嵌套深度超过 {MAX_JSON_DEPTH}（§4.1）"
        )));
    }
    // §4.1：封闭成员；可选成员只能缺省，显式的 null 是类型不符
    for (member, value) in map {
        if !MANIFEST_MEMBERS.contains(&member.as_str()) {
            return Err(invalid(format!("清单出现了未定义的成员 {member:?}")));
        }
        if value.is_null() {
            return Err(invalid(format!("{member} 不得为 null")));
        }
    }

    let manifest_version = match map.get("manifest_version") {
        Some(Value::Number(number)) if number.as_u64() == Some(1) => 1,
        Some(other) => {
            return Err(invalid(format!(
                "不认识的 manifest_version {other}（本规范定义 1）"
            )));
        }
        None => return Err(invalid("清单缺少必填成员 \"manifest_version\"")),
    };
    let name = required_nonempty_string(map, "name")?;
    let version = required_nonempty_string(map, "version")?;
    let description = match map.get("description") {
        Some(Value::String(text)) => Some(text.clone()),
        Some(other) => return Err(invalid(format!("description 必须是字符串（实得 {other}）"))),
        None => None,
    };
    let protocol_versions = match map.get("protocol_versions") {
        Some(Value::Array(items)) if !items.is_empty() => {
            let mut versions = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    Value::String(text) => versions.push(text.clone()),
                    other => {
                        return Err(invalid(format!(
                            "protocol_versions 的成员必须是字符串（实得 {other}）"
                        )));
                    }
                }
            }
            versions
        }
        Some(other) => {
            return Err(invalid(format!(
                "protocol_versions 必须是非空字符串数组（实得 {other}）"
            )));
        }
        None => return Err(invalid("清单缺少必填成员 \"protocol_versions\"")),
    };
    let secrets = match map.get("secrets") {
        None => Vec::new(),
        Some(Value::Array(items)) => {
            let mut secrets = Vec::with_capacity(items.len());
            for item in items {
                secrets.push(parse_secret(item)?);
            }
            let names: BTreeSet<&str> = secrets.iter().map(|s| s.name.as_str()).collect();
            if names.len() != secrets.len() {
                return Err(invalid("凭证名在清单内必须唯一"));
            }
            secrets
        }
        Some(other) => return Err(invalid(format!("secrets 必须是数组（实得 {other}）"))),
    };

    let config_schema = match map.get("config_schema") {
        Some(schema @ Value::Object(_)) => schema,
        Some(other) => {
            return Err(invalid(format!("config_schema 必须是对象（实得 {other}）")));
        }
        None => return Err(invalid("清单缺少必填成员 \"config_schema\"")),
    };
    if has_numeric_violation(config_schema) {
        return Err(invalid(
            "config_schema 含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）",
        ));
    }
    // §4.1：根 MUST 为 `"type": "object"`
    if config_schema.get("type") != Some(&Value::String("object".to_string())) {
        return Err(invalid("config_schema 根必须为 \"type\": \"object\""));
    }
    check_closed_schema(config_schema)?;

    Ok(Manifest {
        manifest_version,
        name,
        version,
        description,
        protocol_versions,
        config_schema: config_schema.clone(),
        secrets,
        compiled: CompiledSchema::build(config_schema),
    })
}

/// 按清单的 `config_schema` 校验实例配置（§4.2）；不改写 `config`。
///
/// 不通过时返回 `config_schema_violation` 的 [`InstanceFailure`]，消息里的位置是类 JSON Pointer
/// 的展示路径：子路径按 RFC 6901 转义（`~0` / `~1`），根位置显示为 `/`（RFC 6901 的空根指针对
/// 人不可读）。消息格式不是契约的一部分：Python SDK 的消息使用 jsonschema 的 `$.x` 语法。
pub fn validate_config(manifest: &Manifest, config: &Value) -> Result<(), InstanceFailure> {
    manifest.compiled.validate(config).map_err(|failure| {
        let pointer = if failure.path.is_empty() {
            "/"
        } else {
            &failure.path
        };
        InstanceFailure::config_schema_violation(format!(
            "实例配置未通过 config_schema（{pointer}）：{}",
            failure.message
        ))
    })
}

fn invalid(message: impl Into<String>) -> InstanceFailure {
    InstanceFailure::manifest_invalid(message)
}

fn required_nonempty_string(
    map: &Map<String, Value>,
    member: &str,
) -> Result<String, InstanceFailure> {
    match map.get(member) {
        Some(Value::String(text)) if !text.is_empty() => Ok(text.clone()),
        Some(Value::String(_)) => Err(invalid(format!("{member} 必须是非空字符串"))),
        Some(other) => Err(invalid(format!("{member} 必须是字符串（实得 {other}）"))),
        None => Err(invalid(format!("清单缺少必填成员 {member:?}"))),
    }
}

fn parse_secret(item: &Value) -> Result<SecretDeclaration, InstanceFailure> {
    let map = item
        .as_object()
        .ok_or_else(|| invalid(format!("secrets 的每一项必须是对象（实得 {item}）")))?;
    for (member, value) in map {
        if !SECRET_MEMBERS.contains(&member.as_str()) {
            return Err(invalid(format!("secrets 项出现了未定义的成员 {member:?}")));
        }
        if value.is_null() {
            return Err(invalid(format!("secrets 项的 {member} 不得为 null")));
        }
    }
    let name = required_nonempty_string(map, "name")?;
    // §4.1：^[A-Z][A-Z0-9_]*$；不得以 DPE_ 开头（保留给运行器）；不得与基础环境变量同名
    if !is_secret_name(&name) {
        return Err(invalid(format!("凭证名 {name:?} 须匹配 ^[A-Z][A-Z0-9_]*$")));
    }
    if name.starts_with("DPE_") {
        return Err(invalid(format!(
            "凭证名 {name:?} 不得以 DPE_ 开头（保留给运行器）"
        )));
    }
    if is_base_env_name(&name) {
        return Err(invalid(format!("凭证名 {name:?} 不得与基础环境变量同名")));
    }
    let description = match map.get("description") {
        Some(Value::String(text)) => Some(text.clone()),
        Some(other) => {
            return Err(invalid(format!(
                "secrets 项的 description 必须是字符串（实得 {other}）"
            )));
        }
        None => None,
    };
    let required = match map.get("required") {
        Some(Value::Bool(flag)) => *flag,
        Some(other) => {
            return Err(invalid(format!(
                "secrets 项的 required 必须是布尔值（实得 {other}）"
            )));
        }
        None => true,
    };
    Ok(SecretDeclaration {
        name,
        description,
        required,
    })
}

fn is_secret_name(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_ascii_uppercase() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

fn is_base_env_name(name: &str) -> bool {
    BASE_ENV_NAMES.contains(&name) || name.starts_with("LC_")
}

// ---------------------------------------------------------------------------
// §4.1.1 封闭 schema 检查
// ---------------------------------------------------------------------------

struct SchemaChecker<'a> {
    names: BTreeSet<&'a str>,
    /// `$defs` 条目之间的引用边（owner → 目标名），供无环检查。
    edges: BTreeMap<&'a str, BTreeSet<String>>,
}

/// §4.1.1：封闭关键字子集、元层形状、`$defs` 位置、`$ref` 形式/目标/无环、展开深度。
fn check_closed_schema(schema: &Value) -> Result<(), InstanceFailure> {
    let defs = schema.get("$defs").and_then(Value::as_object);
    let names: BTreeSet<&str> = defs
        .map(|mapping| mapping.keys().map(String::as_str).collect())
        .unwrap_or_default();
    let mut checker = SchemaChecker {
        names,
        edges: BTreeMap::new(),
    };
    checker.check_node(schema, true, "", None)?;
    checker.check_defs_acyclic()?;
    check_expanded_depth(schema, defs)?;
    Ok(())
}

impl<'a> SchemaChecker<'a> {
    fn check_node(
        &mut self,
        node: &'a Value,
        is_root: bool,
        position: &str,
        owner: Option<&'a str>,
    ) -> Result<(), InstanceFailure> {
        let map = match node {
            Value::Bool(_) => return Ok(()),
            Value::Object(map) => map,
            _ => {
                return Err(invalid(format!(
                    "{position}: schema 必须是对象或布尔值（§4.1.1 封闭 schema）"
                )));
            }
        };
        for keyword in map.keys() {
            if !ALLOWED_KEYWORDS.contains(&keyword.as_str()) {
                return Err(invalid(format!(
                    "{position}: 不允许的关键字 {keyword:?}（§4.1.1 封闭关键字子集）"
                )));
            }
            if keyword == "$defs" && !is_root {
                return Err(invalid(format!(
                    "{position}: $defs 只允许出现在根上（§4.1.1）"
                )));
            }
            if keyword == "$schema" && !is_root {
                return Err(invalid(format!(
                    "{position}: $schema 只允许出现在根上（§4.1.1）"
                )));
            }
        }
        if let Some(dialect) = map.get("$schema") {
            if !matches!(dialect, Value::String(text) if text == DRAFT_2020_12 || text == &format!("{DRAFT_2020_12}#"))
            {
                return Err(invalid(format!(
                    "{position}: $schema {dialect} 必须缺省或为 {DRAFT_2020_12}"
                )));
            }
        }
        if let Some(reference) = map.get("$ref") {
            let Some(reference) = reference.as_str() else {
                return Err(invalid(format!("{position}: $ref 必须是字符串")));
            };
            let Some(name) = parse_defs_ref(reference) else {
                return Err(invalid(format!(
                    "{position}: $ref {reference:?} 形式不合法"
                )));
            };
            if !self.names.contains(name.as_str()) {
                return Err(invalid(format!(
                    "{position}: $ref {reference:?} 的目标不存在"
                )));
            }
            if let Some(owner) = owner {
                self.edges.entry(owner).or_default().insert(name);
            }
        }
        if matches!(map.get("items"), Some(Value::Array(_))) {
            return Err(invalid(format!(
                "{position}: items 只允许单个 schema（§4.1.1）"
            )));
        }
        if let Some(pattern) = map.get("pattern") {
            let Some(pattern) = pattern.as_str() else {
                return Err(invalid(format!("{position}: pattern 必须是字符串")));
            };
            if let Err(error) = check_pattern(pattern) {
                return Err(invalid(format!(
                    "{position}: pattern {pattern:?} 超出 §4.1.1 子集：{error}"
                )));
            }
        }
        if let Some(properties) = map.get("patternProperties") {
            let Some(properties) = properties.as_object() else {
                return Err(invalid(format!(
                    "{position}/patternProperties: 必须是「名字 → schema」映射（对象）"
                )));
            };
            for key in properties.keys() {
                if let Err(error) = check_pattern(key) {
                    return Err(invalid(format!(
                        "{position}/patternProperties: 键 {key:?} 超出 §4.1.1 子集：{error}"
                    )));
                }
            }
        }
        if let Some(types) = map.get("type") {
            check_types(types, position)?;
        }
        if let Some(enumeration) = map.get("enum") {
            if !enumeration.is_array() {
                return Err(invalid(format!("{position}: enum 必须是数组")));
            }
        }
        for keyword in [
            "title",
            "description",
            "$comment",
            "format",
            "contentEncoding",
            "contentMediaType",
        ] {
            if let Some(value) = map.get(keyword) {
                if !value.is_string() {
                    return Err(invalid(format!("{position}: {keyword} 必须是字符串")));
                }
            }
        }
        if let Some(examples) = map.get("examples") {
            if !examples.is_array() {
                return Err(invalid(format!("{position}: examples 必须是数组")));
            }
        }
        if let Some(unique_items) = map.get("uniqueItems") {
            if !unique_items.is_boolean() {
                return Err(invalid(format!("{position}: uniqueItems 必须是布尔值")));
            }
        }
        for keyword in ["deprecated", "readOnly", "writeOnly"] {
            if let Some(value) = map.get(keyword) {
                if !value.is_boolean() {
                    return Err(invalid(format!("{position}: {keyword} 必须是布尔值")));
                }
            }
        }
        for keyword in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"] {
            if let Some(value) = map.get(keyword) {
                if !value.is_number() {
                    return Err(invalid(format!(
                        "{position}: {keyword} 必须是数值（布尔不是数值）"
                    )));
                }
            }
        }
        for keyword in [
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
        ] {
            if let Some(value) = map.get(keyword) {
                if !is_nonnegative_integer(value) {
                    return Err(invalid(format!("{position}: {keyword} 必须是非负整数")));
                }
            }
        }
        if let Some(required) = map.get("required") {
            check_unique_strings(required, position, "required")?;
        }
        if let Some(dependent) = map.get("dependentRequired") {
            let Some(dependent) = dependent.as_object() else {
                return Err(invalid(format!(
                    "{position}: dependentRequired 必须是「名字 → 字符串数组」映射"
                )));
            };
            for (key, names) in dependent {
                check_unique_strings(names, position, &format!("dependentRequired[{key:?}]"))?;
            }
        }
        for keyword in SUBSCHEMA_VALUE {
            if let Some(value) = map.get(keyword) {
                self.check_node(value, false, &format!("{position}/{keyword}"), owner)?;
            }
        }
        for keyword in SUBSCHEMA_LIST {
            let Some(sequence) = map.get(keyword) else {
                continue;
            };
            let Some(sequence) = sequence.as_array() else {
                return Err(invalid(format!("{position}/{keyword}: 必须是 schema 数组")));
            };
            if sequence.is_empty() {
                return Err(invalid(format!(
                    "{position}/{keyword}: 必须是 schema 数组（至少一项）"
                )));
            }
            for (index, item) in sequence.iter().enumerate() {
                self.check_node(item, false, &format!("{position}/{keyword}/{index}"), owner)?;
            }
        }
        for keyword in SUBSCHEMA_MAP {
            let Some(mapping) = map.get(keyword) else {
                continue;
            };
            let Some(mapping) = mapping.as_object() else {
                return Err(invalid(format!(
                    "{position}/{keyword}: 必须是「名字 → schema」映射（对象）"
                )));
            };
            for (key, sub) in mapping {
                self.check_node(sub, false, &format!("{position}/{keyword}/{key}"), owner)?;
            }
        }
        if is_root {
            if let Some(defs) = map.get("$defs") {
                let Some(defs) = defs.as_object() else {
                    return Err(invalid("/$defs: 必须是「名字 → schema」映射（对象）"));
                };
                for (name, sub) in defs {
                    self.check_node(sub, false, &format!("/$defs/{name}"), Some(name))?;
                }
            }
        }
        Ok(())
    }

    /// §4.1.1：`$defs` 各条之间的引用 MUST 构成有向无环图——环会在求值时无限递归，
    /// 行为随实现而异；这同时排除了递归 schema（有意取舍）。
    fn check_defs_acyclic(&self) -> Result<(), InstanceFailure> {
        // 0 未访问 / 1 在栈上 / 2 已完成
        let mut state: BTreeMap<&str, u8> = BTreeMap::new();
        let empty: BTreeSet<String> = BTreeSet::new();
        for start in &self.names {
            if state.get(start).copied().unwrap_or(0) != 0 {
                continue;
            }
            state.insert(start, 1);
            let targets: Vec<String> = self
                .edges
                .get(start)
                .unwrap_or(&empty)
                .iter()
                .cloned()
                .collect();
            let mut stack: Vec<(&str, Vec<String>, usize)> = vec![(start, targets, 0)];
            while let Some((_, targets, index)) = stack.last_mut() {
                if *index < targets.len() {
                    let target = targets[*index].clone();
                    *index += 1;
                    // 目标不存在已在 check_node 阶段拒绝；这里防御性跳过
                    let Some(target) = self.names.get(target.as_str()).copied() else {
                        continue;
                    };
                    match state.get(target).copied().unwrap_or(0) {
                        1 => {
                            return Err(invalid(format!(
                                "$defs 引用成环（经 {target:?}，§4.1.1）：求值时会无限递归"
                            )));
                        }
                        0 => {
                            state.insert(target, 1);
                            let next: Vec<String> = self
                                .edges
                                .get(target)
                                .unwrap_or(&empty)
                                .iter()
                                .cloned()
                                .collect();
                            stack.push((target, next, 0));
                        }
                        _ => {}
                    }
                } else {
                    let (done, _, _) = stack.pop().expect("循环条件保证栈非空");
                    state.insert(done, 2);
                }
            }
        }
        Ok(())
    }
}

fn check_types(types: &Value, position: &str) -> Result<(), InstanceFailure> {
    match types {
        Value::String(text) => {
            if !is_known_type(text) {
                return Err(invalid(format!(
                    "{position}: type 取值 {text:?} 不是 2020-12 的已知类型"
                )));
            }
        }
        Value::Array(items) => {
            if items.is_empty() {
                return Err(invalid(format!("{position}: type 数组不得为空")));
            }
            let mut seen: BTreeSet<&str> = BTreeSet::new();
            for item in items {
                let Some(text) = item.as_str() else {
                    return Err(invalid(format!("{position}: type 数组成员必须是字符串")));
                };
                if !is_known_type(text) {
                    return Err(invalid(format!("{position}: type 数组含未知类型 {text:?}")));
                }
                if !seen.insert(text) {
                    return Err(invalid(format!("{position}: type 数组成员必须唯一")));
                }
            }
        }
        _ => {
            return Err(invalid(format!(
                "{position}: type 必须是字符串或字符串数组"
            )))
        }
    }
    Ok(())
}

fn check_unique_strings(
    value: &Value,
    position: &str,
    keyword: &str,
) -> Result<(), InstanceFailure> {
    let Some(items) = value.as_array() else {
        return Err(invalid(format!("{position}: {keyword} 必须是字符串数组")));
    };
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    for item in items {
        let Some(text) = item.as_str() else {
            return Err(invalid(format!("{position}: {keyword} 的成员必须是字符串")));
        };
        if !seen.insert(text) {
            return Err(invalid(format!("{position}: {keyword} 的成员必须唯一")));
        }
    }
    Ok(())
}

fn is_nonnegative_integer(value: &Value) -> bool {
    let Value::Number(number) = value else {
        return false;
    };
    if number.as_u64().is_some() {
        return true;
    }
    if int_form(number) {
        // 整数形式但超出 u64：非负即字面量不以 `-` 开头
        return !number.to_string().starts_with('-');
    }
    number
        .as_f64()
        .is_some_and(|f| f.is_finite() && f.fract() == 0.0 && f >= 0.0)
}

/// §4.1.1：展开深度 ≤ 64（根计 1，每进入一个 schema 位置 +1，每经过一次 `$ref` 跳转 +1 并沿
/// 引用进入目标继续计；未被引用的 `$defs` 条目不参与）。迭代后序 + 记忆化；`$defs` 已是 DAG。
fn check_expanded_depth(
    schema: &Value,
    defs: Option<&Map<String, Value>>,
) -> Result<(), InstanceFailure> {
    let empty = Map::new();
    let defs = defs.unwrap_or(&empty);
    let key = |node: &Value| node as *const Value as usize;
    let mut memo: HashMap<usize, u64> = HashMap::new();
    let mut stack: Vec<(&Value, bool)> = vec![(schema, false)];
    while let Some((node, visited)) = stack.pop() {
        if !node.is_object() {
            memo.insert(key(node), 1);
            continue;
        }
        if visited {
            let mut best = 0u64;
            for child in schema_children(node, defs) {
                best = best.max(memo.get(&key(child)).copied().unwrap_or(1));
            }
            if best + 1 > MAX_EXPANDED_DEPTH {
                return Err(invalid(format!(
                    "schema 的展开深度超过 {MAX_EXPANDED_DEPTH}（§4.1.1）"
                )));
            }
            memo.insert(key(node), best + 1);
            continue;
        }
        if memo.contains_key(&key(node)) {
            continue;
        }
        stack.push((node, true));
        for child in schema_children(node, defs) {
            if !memo.contains_key(&key(child)) {
                stack.push((child, false));
            }
        }
    }
    Ok(())
}

/// 展开深度的后继：schema 位置上的子 schema，以及 `$ref` 的目标（§4.1.1）。
fn schema_children<'a>(node: &'a Value, defs: &'a Map<String, Value>) -> Vec<&'a Value> {
    let mut children: Vec<&Value> = Vec::new();
    let Some(map) = node.as_object() else {
        return children;
    };
    for keyword in SUBSCHEMA_VALUE {
        if let Some(value) = map.get(keyword) {
            children.push(value);
        }
    }
    for keyword in SUBSCHEMA_LIST {
        if let Some(Value::Array(items)) = map.get(keyword) {
            children.extend(items.iter());
        }
    }
    for keyword in SUBSCHEMA_MAP {
        if let Some(Value::Object(mapping)) = map.get(keyword) {
            children.extend(mapping.values());
        }
    }
    if let Some(Value::String(reference)) = map.get("$ref") {
        if let Some(name) = parse_defs_ref(reference) {
            if let Some(target) = defs.get(&name) {
                children.push(target);
            }
        }
    }
    children
}

// ---------------------------------------------------------------------------
// JSON 深度与越界数值（§4.1、core §2.6）
// ---------------------------------------------------------------------------

/// JSON 值的嵌套深度（对象与数组各计一层，标量计 1；迭代实现，不受递归限额影响）。
pub(crate) fn json_depth(value: &Value) -> usize {
    let mut depth = 0usize;
    let mut stack: Vec<(&Value, usize)> = vec![(value, 1)];
    while let Some((item, level)) = stack.pop() {
        depth = depth.max(level);
        match item {
            Value::Object(map) => stack.extend(map.values().map(|child| (child, level + 1))),
            Value::Array(items) => stack.extend(items.iter().map(|child| (child, level + 1))),
            _ => {}
        }
    }
    depth
}

/// 值中是否有越界数值（core §2.6）：整数绝对值超过 2^53−1，或超出 double 范围的数（如 `1e400`）。
pub(crate) fn has_numeric_violation(value: &Value) -> bool {
    let mut stack: Vec<&Value> = vec![value];
    while let Some(item) = stack.pop() {
        match item {
            Value::Number(number) => {
                if number_violation(number) {
                    return true;
                }
            }
            Value::Object(map) => stack.extend(map.values()),
            Value::Array(items) => stack.extend(items.iter()),
            _ => {}
        }
    }
    false
}

/// 数值形式判定与 Python 的 int / float 二分同构：字面量有小数点/指数即浮点形式
/// （Python 侧为 float，非有限即越界），否则为整数（Python 侧为 int，绝对值超 2^53−1 即越界）。
fn number_violation(number: &Number) -> bool {
    if int_form(number) {
        if let Some(signed) = number.as_i64() {
            signed.unsigned_abs() > MAX_SAFE_INTEGER
        } else if let Some(unsigned) = number.as_u64() {
            unsigned > MAX_SAFE_INTEGER
        } else {
            true // 超出 i64/u64 的整数必大于 2^53−1
        }
    } else {
        number.as_f64().is_none() // 浮点形式超出 double 范围（Python 为 ±inf）
    }
}
