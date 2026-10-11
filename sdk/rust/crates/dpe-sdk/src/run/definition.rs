//! 实例定义（connector 契约 §4.4，独立运行器规范档）：解析与校验。
//!
//! 实例定义是封闭 schema（`runner` 内部除外），不含凭证值——凭证只以来源引用出现（§4.3）。
//! 相对路径（`plugin.manifest`、`file` 引用、含路径分隔符的 `argv[0]`）以定义文件所在目录为
//! 基准。违例一律返回 `definition_invalid` 的 [`InstanceFailure`]，不启动插件。
//!
//! [`parse_definition`] / [`load_definition`] 只做定义自身的校验，编排层可据此先取得 `id`、
//! 取得实例锁（§5.2），再做依赖清单的 [`check_against_manifest`]。状态目录、排他锁与前缀登记
//! 不在本模块。与 Python SDK 的 `dpe_sdk.run.definition` 行为对等。

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use dpe_hash::{nesting_depth, normalize_file_uri, MAX_NESTING_DEPTH};
use serde_json::{Map, Value};

use crate::run::manifest::{has_numeric_violation, Manifest};
use crate::run::secrets::{is_base_env_name, same_source, SourceRef};
use crate::run::InstanceFailure;

/// §4.4：实例定义的封闭成员集。
const DEFINITION_MEMBERS: [&str; 9] = [
    "definition_version",
    "id",
    "remote",
    "uri_prefix",
    "plugin",
    "config",
    "secrets",
    "conflict_policy",
    "runner",
];

/// §4.4：`remote` 的封闭成员集。
const REMOTE_MEMBERS: [&str; 2] = ["url", "credential"];

/// §4.4：`plugin` 的封闭成员集。
const PLUGIN_MEMBERS: [&str; 3] = ["manifest", "command", "env"];

/// §4.4：`conflict_policy` 的封闭成员集。
const CONFLICT_MEMBERS: [&str; 2] = ["commit", "delete"];

/// `remote`：基 URL 与可选的 remote 凭证引用（§4.4）。
#[derive(Debug, Clone)]
pub struct RemoteSpec {
    url: String,
    credential: Option<SourceRef>,
}

impl RemoteSpec {
    /// remote 的基 URL（http.md §1），已校验为 http(s) 且不含 userinfo。
    pub fn url(&self) -> &str {
        &self.url
    }

    /// remote 凭证的来源引用；缺省表示不发送 `Authorization`（§4.4）。
    pub fn credential(&self) -> Option<&SourceRef> {
        self.credential.as_ref()
    }
}

/// `plugin`：清单路径、启动命令与非秘密环境变量（§4.4）。
#[derive(Debug, Clone)]
pub struct PluginSpec {
    manifest: String,
    command: Vec<String>,
    env: BTreeMap<String, String>,
}

impl PluginSpec {
    /// 清单路径（§4.1）；相对路径的基准见 [`InstanceDefinition::base_dir`]。
    pub fn manifest(&self) -> &str {
        &self.manifest
    }

    /// 启动插件的 argv（原样，未解析；解析见 [`resolve_command`]）。
    pub fn command(&self) -> &[String] {
        &self.command
    }

    /// 注入插件的非秘密环境变量（§4.3）。
    pub fn env(&self) -> &BTreeMap<String, String> {
        &self.env
    }
}

/// 写入冲突的策略取值（§7.2）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConflictAction {
    /// 以源为准，重新提交（`"source_wins"`）。
    SourceWins,
    /// 只上报冲突（`"report"`）。
    Report,
}

impl ConflictAction {
    /// 轮报告与日志中的取值（§7.2）。
    pub fn as_str(self) -> &'static str {
        match self {
            ConflictAction::SourceWins => "source_wins",
            ConflictAction::Report => "report",
        }
    }
}

/// `conflict_policy`（§7.2）：commit 缺省以源为准，delete 缺省只上报。
#[derive(Debug, Clone, Copy)]
pub struct ConflictPolicy {
    commit: ConflictAction,
    delete: ConflictAction,
}

impl Default for ConflictPolicy {
    fn default() -> Self {
        ConflictPolicy {
            commit: ConflictAction::SourceWins,
            delete: ConflictAction::Report,
        }
    }
}

impl ConflictPolicy {
    /// 写入冲突（commit）的策略，缺省 `source_wins`。
    pub fn commit(&self) -> ConflictAction {
        self.commit
    }

    /// 删除冲突（delete）的策略，缺省 `report`。
    pub fn delete(&self) -> ConflictAction {
        self.delete
    }
}

/// 一个实例的定义（§4.4）；`base_dir` 是相对路径的基准（定义文件所在目录）。
///
/// 相对路径与凭证同源检查都依赖 `base_dir`，所以它是校验的必需输入：只能经
/// [`parse_definition`] / [`load_definition`] 构造，不存在绕过它的入口。
#[derive(Debug)]
pub struct InstanceDefinition {
    definition_version: u32,
    id: String,
    remote: RemoteSpec,
    uri_prefix: String,
    plugin: PluginSpec,
    config: Map<String, Value>,
    secrets: BTreeMap<String, SourceRef>,
    conflict_policy: ConflictPolicy,
    runner: Map<String, Value>,
    base_dir: PathBuf,
}

impl InstanceDefinition {
    /// 本规范定义的版本（恒为 1）。
    pub fn definition_version(&self) -> u32 {
        self.definition_version
    }

    /// 实例的本地标识（§4.4），匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`。
    pub fn id(&self) -> &str {
        &self.id
    }

    /// remote 与其凭证引用（§4.4）。
    pub fn remote(&self) -> &RemoteSpec {
        &self.remote
    }

    /// 实例 URI 前缀（§3），已校验为规范化不动点。
    pub fn uri_prefix(&self) -> &str {
        &self.uri_prefix
    }

    /// 插件规格（§4.4）。
    pub fn plugin(&self) -> &PluginSpec {
        &self.plugin
    }

    /// 实例配置（§4.2），原样保留（键序由 JSON 值表示决定）。
    pub fn config(&self) -> &Map<String, Value> {
        &self.config
    }

    /// 数据源凭证的来源引用（§4.3）；键为清单声明的凭证名。
    pub fn secrets(&self) -> &BTreeMap<String, SourceRef> {
        &self.secrets
    }

    /// 写入冲突的策略（§7.2）。
    pub fn conflict_policy(&self) -> &ConflictPolicy {
        &self.conflict_policy
    }

    /// 运行器调优参数（§4.4）：内部不封闭，运行器忽略不认识的键。
    pub fn runner(&self) -> &Map<String, Value> {
        &self.runner
    }

    /// 相对路径的基准（定义文件所在目录，§4.4）。
    pub fn base_dir(&self) -> &Path {
        &self.base_dir
    }

    /// 清单文件的路径（`base_dir / plugin.manifest`）。
    pub fn manifest_path(&self) -> PathBuf {
        self.base_dir.join(self.plugin.manifest())
    }
}

/// 校验已解析的实例定义；`base_dir` 为相对路径的基准（定义文件所在目录）。
pub fn parse_definition(
    data: &Value,
    base_dir: &Path,
) -> Result<InstanceDefinition, InstanceFailure> {
    // §4.4：定义文件（含 config）的 JSON 嵌套深度 ≤ MAX_NESTING_DEPTH，计数口径见 core §2.8
    // 第 0 步（读取阶段判定；计数复用 dpe-hash，不另写一份）
    if nesting_depth(data, None) > MAX_NESTING_DEPTH {
        return Err(invalid(format!(
            "实例定义的 JSON 嵌套深度超过 {MAX_NESTING_DEPTH}（§4.4）"
        )));
    }
    let map = closed_object(data, &DEFINITION_MEMBERS, "实例定义")?;

    let definition_version = match map.get("definition_version") {
        Some(Value::Number(number)) if number.as_u64() == Some(1) => 1,
        Some(other) => {
            return Err(invalid(format!(
                "不认识的 definition_version {other}（本规范定义 1）"
            )));
        }
        None => return Err(invalid("实例定义缺少必填成员 \"definition_version\"")),
    };

    let id = required_string(map, "id", "实例定义")?;
    if !is_valid_id(&id) {
        return Err(invalid("id 须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
    }

    let remote_map = closed_object(
        map.get("remote")
            .ok_or_else(|| invalid("实例定义缺少必填成员 \"remote\""))?,
        &REMOTE_MEMBERS,
        "remote",
    )?;
    let url = required_string(remote_map, "url", "remote")?;
    check_remote_url(&url)?;
    let credential = match remote_map.get("credential") {
        None => None,
        Some(value) => Some(SourceRef::parse(value).map_err(invalid)?),
    };
    let remote = RemoteSpec { url, credential };

    let uri_prefix = required_string(map, "uri_prefix", "实例定义")?;
    match normalize_file_uri(&uri_prefix) {
        Err(exc) => return Err(invalid(format!("uri_prefix 不是合法的 URI：{exc}"))),
        Ok(normalized) if normalized != uri_prefix => {
            return Err(invalid(format!(
                "uri_prefix 不是规范化不动点（规范化形式为 {normalized:?}）"
            )));
        }
        Ok(_) => {}
    }

    let plugin_map = closed_object(
        map.get("plugin")
            .ok_or_else(|| invalid("实例定义缺少必填成员 \"plugin\""))?,
        &PLUGIN_MEMBERS,
        "plugin",
    )?;
    let manifest = match plugin_map.get("manifest") {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::String(_)) => return Err(invalid("plugin.manifest 必须是非空字符串")),
        Some(_) => return Err(invalid("plugin.manifest 必须是字符串")),
        None => return Err(invalid("plugin 缺少必填成员 \"manifest\"")),
    };
    let command = match plugin_map.get("command") {
        Some(Value::Array(items)) if !items.is_empty() => {
            let mut command = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    Value::String(text) => command.push(text.clone()),
                    _ => return Err(invalid("plugin.command 的成员必须是字符串")),
                }
            }
            command
        }
        Some(_) => return Err(invalid("plugin.command 必须是非空字符串数组")),
        None => return Err(invalid("plugin 缺少必填成员 \"command\"")),
    };
    if command[0].is_empty() {
        return Err(invalid("plugin.command[0] 不得为空"));
    }
    if command.iter().any(|arg| arg.contains('\0')) {
        return Err(invalid("plugin.command 不得含 NUL"));
    }
    let env = match plugin_map.get("env") {
        None => BTreeMap::new(),
        Some(Value::Object(entries)) => {
            let mut env = BTreeMap::new();
            for (name, value) in entries {
                let Some(text) = value.as_str() else {
                    return Err(invalid(format!("plugin.env 的变量 {name:?} 必须是字符串")));
                };
                if name.is_empty()
                    || name.contains('=')
                    || name.contains('\0')
                    || text.contains('\0')
                {
                    return Err(invalid(format!(
                        "plugin.env 的变量 {name:?} 无法作为环境变量"
                    )));
                }
                if name.starts_with("DPE_") {
                    return Err(invalid(format!(
                        "plugin.env 的变量 {name:?} 不得以 DPE_ 开头"
                    )));
                }
                if is_base_env_name(name) {
                    return Err(invalid(format!(
                        "plugin.env 的变量 {name:?} 不得与基础环境同名"
                    )));
                }
                env.insert(name.clone(), text.to_string());
            }
            env
        }
        Some(_) => return Err(invalid("plugin.env 必须是对象")),
    };
    let plugin = PluginSpec {
        manifest,
        command,
        env,
    };

    let config_value = map
        .get("config")
        .ok_or_else(|| invalid("实例定义缺少必填成员 \"config\""))?;
    let Value::Object(config) = config_value else {
        return Err(invalid("config 必须是对象"));
    };
    // 配置原样交给插件并参与内容身份比较（§1、§4.2）：越界数值无法原样表达
    if has_numeric_violation(config_value) {
        return Err(invalid(
            "config 含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）",
        ));
    }
    let config = config.clone();

    let secrets = match map.get("secrets") {
        None => BTreeMap::new(),
        Some(Value::Object(entries)) => {
            let mut secrets = BTreeMap::new();
            for (name, value) in entries {
                secrets.insert(name.clone(), SourceRef::parse(value).map_err(invalid)?);
            }
            secrets
        }
        Some(_) => return Err(invalid("secrets 必须是对象")),
    };

    let conflict_policy = match map.get("conflict_policy") {
        None => ConflictPolicy::default(),
        Some(value) => {
            let policy = closed_object(value, &CONFLICT_MEMBERS, "conflict_policy")?;
            let commit =
                conflict_action(policy.get("commit"), ConflictAction::SourceWins, "commit")?;
            let delete = conflict_action(policy.get("delete"), ConflictAction::Report, "delete")?;
            ConflictPolicy { commit, delete }
        }
    };

    let runner = match map.get("runner") {
        None => Map::new(),
        Some(Value::Object(entries)) => entries.clone(),
        Some(_) => return Err(invalid("runner 必须是对象")),
    };

    // `base_dir` 是校验的必需输入（相对路径与凭证同源检查都以它为基准）：相对路径按进程当前
    // 目录补全，与 Python SDK `Path.absolute()` 同口径（不做规范化）
    let base_dir = if base_dir.is_absolute() {
        base_dir.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|exc| invalid(format!("无法取当前目录作为相对路径基准：{exc}")))?
            .join(base_dir)
    };

    // §4.3「remote 凭证不可达」：数据源凭证不得与 remote 凭证同源
    if let Some(remote_ref) = &remote.credential {
        for (name, reference) in &secrets {
            if same_source(reference, remote_ref, &base_dir).map_err(invalid)? {
                return Err(invalid(format!(
                    "凭证 {name} 的来源与 remote.credential 相同"
                )));
            }
        }
    }

    Ok(InstanceDefinition {
        definition_version,
        id,
        remote,
        uri_prefix,
        plugin,
        config,
        secrets,
        conflict_policy,
        runner,
        base_dir,
    })
}

/// 读取并校验实例定义文件（§4.4）；`base_dir` 取文件所在目录。
pub fn load_definition(path: &Path) -> Result<InstanceDefinition, InstanceFailure> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|exc| invalid(format!("无法取当前目录：{exc}")))?
            .join(path)
    };
    // 读入即按 I-JSON 严格解析（拒绝重复键、孤立代理项与 NaN 等非 JSON 数值）并检查越界数值，
    // 与 Python SDK `load_json_file` 同口径（消息不含文件内容）
    let bytes = std::fs::read(&absolute)
        .map_err(|exc| invalid(format!("无法读取 {}：{exc}", absolute.display())))?;
    let Ok(text) = String::from_utf8(bytes) else {
        return Err(invalid(format!(
            "{} 不是 UTF-8 编码的 I-JSON",
            absolute.display()
        )));
    };
    let data = dpe_hash::parse_ijson(&text)
        .map_err(|exc| invalid(format!("{} 不是合法的 I-JSON：{exc}", absolute.display())))?;
    if has_numeric_violation(&data) {
        return Err(invalid(format!(
            "{} 含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）",
            absolute.display()
        )));
    }
    let parent = absolute
        .parent()
        .ok_or_else(|| invalid(format!("{} 没有所在目录", absolute.display())))?;
    parse_definition(&data, parent)
}

/// 依赖清单的定义校验（§4.4）：`secrets` 只能含清单声明的名字，`plugin.env` 不得与已声明的
/// 凭证同名。
pub fn check_against_manifest(
    definition: &InstanceDefinition,
    manifest: &Manifest,
) -> Result<(), InstanceFailure> {
    let declared = manifest.secret_names();
    let undeclared: Vec<&str> = definition
        .secrets
        .keys()
        .map(String::as_str)
        .filter(|name| !declared.contains(name))
        .collect();
    if !undeclared.is_empty() {
        return Err(invalid(format!(
            "secrets 含清单未声明的凭证：{}",
            undeclared.join(", ")
        )));
    }
    let clash: Vec<&str> = definition
        .plugin
        .env
        .keys()
        .map(String::as_str)
        .filter(|name| declared.contains(name))
        .collect();
    if !clash.is_empty() {
        return Err(invalid(format!(
            "plugin.env 与已声明的凭证同名：{}",
            clash.join(", ")
        )));
    }
    Ok(())
}

/// 把 `plugin.command` 解析为可直接执行的 argv（不经 shell，§4.4）。
///
/// `argv[0]` 含路径分隔符时以 `base_dir` 为基准解析（要求是可执行文件），否则在插件环境的
/// `PATH` 中查找（与 `shutil.which` 同口径：整个 `PATH` 为空视为找不到，空条目按当前目录）；
/// 找不到可执行文件时返回 `definition_invalid` 的 [`InstanceFailure`]。
///
/// Windows 上只做精确文件名的查找，不模拟 `shutil.which` 的 `PATHEXT` 展开与「当前目录优先」；
/// 两侧的一致性测试都以 POSIX 可执行位语义为准（Python SDK 的 `resolve_command` 测试同样是
/// POSIX-only）。
///
/// 返回 [`OsString`]：`argv[0]` 可能是非 UTF-8 路径，`String` 会静默损坏它（参数本身来自
/// JSON 字符串，必为 UTF-8）。
pub fn resolve_command(
    definition: &InstanceDefinition,
    env: &BTreeMap<String, String>,
) -> Result<Vec<OsString>, InstanceFailure> {
    let (program, args) = definition
        .plugin
        .command
        .split_first()
        .expect("command 非空（构造保证）");
    let mut argv: Vec<OsString> = Vec::with_capacity(1 + args.len());
    if program.contains('/') || (cfg!(windows) && program.contains('\\')) {
        let path = definition.base_dir.join(program);
        if !is_executable_file(&path) {
            return Err(invalid(format!(
                "plugin.command[0] 不是可执行文件：{program}"
            )));
        }
        argv.push(path.into_os_string());
    } else {
        let path_var = env.get("PATH").map(String::as_str).unwrap_or("");
        let found = search_path(program, path_var)
            .ok_or_else(|| invalid(format!("plugin.command[0] 在 PATH 中找不到：{program}")))?;
        argv.push(found.into_os_string());
    }
    argv.extend(args.iter().map(OsString::from));
    Ok(argv)
}

fn invalid(message: impl Into<String>) -> InstanceFailure {
    InstanceFailure::definition_invalid(message)
}

/// 封闭对象（§4.4）：未定义成员即拒绝；成员以 null 显式给出是类型不符（可选成员只能缺省）。
///
/// 消息只写成员名与位置，不回显成员的取值：取值可能包含凭证或源内容（§4.3「不外泄」——
/// 失败消息会进入日志与 §7.4 轮报告；Python 侧的消息同样不含输入值）。
fn closed_object<'a>(
    value: &'a Value,
    allowed: &[&str],
    what: &str,
) -> Result<&'a Map<String, Value>, InstanceFailure> {
    let Some(map) = value.as_object() else {
        return Err(invalid(format!("{what} 必须是 JSON 对象")));
    };
    for (member, member_value) in map {
        if !allowed.contains(&member.as_str()) {
            return Err(invalid(format!("{what} 出现了未定义的成员 {member:?}")));
        }
        if member_value.is_null() {
            return Err(invalid(format!("{what} 的 {member} 不得为 null")));
        }
    }
    Ok(map)
}

/// 必填字符串成员（严格：只有 JSON 字符串才算）；消息不回显取值（见 [`closed_object`]）。
fn required_string(
    map: &Map<String, Value>,
    member: &str,
    what: &str,
) -> Result<String, InstanceFailure> {
    match map.get(member) {
        Some(Value::String(text)) => Ok(text.clone()),
        Some(_) => Err(invalid(format!("{what} 的 {member} 必须是字符串"))),
        None => Err(invalid(format!("{what} 缺少必填成员 {member:?}"))),
    }
}

/// §4.4：`id` 匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`。
fn is_valid_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    if bytes.is_empty() || bytes.len() > 128 {
        return false;
    }
    bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

/// `conflict_policy` 的取值（§7.2）：只接受 `"source_wins"` 与 `"report"`。
fn conflict_action(
    value: Option<&Value>,
    default: ConflictAction,
    member: &str,
) -> Result<ConflictAction, InstanceFailure> {
    match value {
        None => Ok(default),
        Some(Value::String(text)) => match text.as_str() {
            "source_wins" => Ok(ConflictAction::SourceWins),
            "report" => Ok(ConflictAction::Report),
            _ => Err(invalid(format!(
                "conflict_policy.{member} 的取值不在 §7.2 的策略内"
            ))),
        },
        Some(_) => Err(invalid(format!("conflict_policy.{member} 必须是字符串"))),
    }
}

/// `remote.url` 的检查（§4.4、http.md §1）：http(s) 绝对 URL、含 host、不含 userinfo。
///
/// 与 Python SDK 对 `urlsplit` 的用法同口径（CPython 3.11 的行为为基准）：先去掉 URL 内全部
/// `\t` `\r` `\n`、再去掉前导的 C0 控制字符与空格；scheme 大小写不敏感；netloc 只在 `//` 后
/// 存在、到第一个 `/` `?` `#` 为止；`[` `]` 必须成对，括号前不得有数据、括号后只允许
/// `:port`，括号内是 IPv6（可带 `%zone` 作用域）或 IPvFuture；host 是 `[` `]` 之间的内容或
/// 第一个 `:` 之前的全部；非 ASCII netloc 另做 NFKC 引入分隔符的检查（[`netloc_introduces_delimiter`]）。
///
/// 消息不回显 URL（可能含 userinfo 密码，形式恰是「凭证误写在 URL 里」，§4.3「不外泄」）。
fn check_remote_url(url: &str) -> Result<(), InstanceFailure> {
    let cleaned: String = url
        .chars()
        .filter(|character| !matches!(character, '\t' | '\r' | '\n'))
        .collect();
    let cleaned = cleaned.trim_start_matches(|character: char| character <= '\u{20}');
    let Some((scheme, rest)) = split_scheme(cleaned) else {
        return Err(invalid("remote.url 须为 http(s) 绝对 URL"));
    };
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return Err(invalid("remote.url 的方案必须是 http 或 https"));
    }
    let netloc = match rest.strip_prefix("//") {
        Some(after) => {
            let end = after.find(['/', '?', '#']).unwrap_or(after.len());
            &after[..end]
        }
        None => "",
    };
    if netloc.contains('@') {
        return Err(invalid(
            "remote.url 不得含 userinfo（凭证经 remote.credential 引用）",
        ));
    }
    let bracketed = netloc.contains('[') || netloc.contains(']');
    let host = if bracketed {
        if netloc.contains('[') != netloc.contains(']') {
            return Err(invalid("remote.url 的 IPv6 字面量括号不成对"));
        }
        // 与 `urlsplit._check_bracketed_netloc` 同口径：`[` 前不得有数据，`]` 后只允许 `:port`
        let (before, after_open) = netloc.split_once('[').expect("已判含 '['");
        if !before.is_empty() {
            return Err(invalid("remote.url 的 `[` 前不得有数据"));
        }
        let (host, after_close) = after_open.split_once(']').expect("已判含 ']'");
        if !after_close.is_empty() && !after_close.starts_with(':') {
            return Err(invalid("remote.url 的 `]` 后只允许 `:port`"));
        }
        check_bracketed_host(host)?;
        host
    } else {
        match netloc.find(':') {
            Some(colon) => &netloc[..colon],
            None => netloc,
        }
    };
    if netloc_introduces_delimiter(netloc) {
        return Err(invalid(
            "remote.url 的 netloc 在 NFKC 归一化后会引入分隔符字符",
        ));
    }
    if host.is_empty() {
        return Err(invalid("remote.url 缺少 host"));
    }
    Ok(())
}

/// 括号内的 host 校验（与 `urlsplit._check_bracketed_host` 同口径）：`v` 开头按 IPvFuture
/// （RFC 3986 §3.2.2：`v` + 1 个以上十六进制数字 + `.` + 1 个以上非换行字符），否则必须是
/// IPv6 地址（可带一个非空、不含第二个 `%` 的 `%zone` 作用域）；IPv4 文本不在允许之列
/// （IPv4 串不是合法 IPv6，统一按解析失败处理）。
fn check_bracketed_host(host: &str) -> Result<(), InstanceFailure> {
    if let Some(rest) = host.strip_prefix('v') {
        let Some((hex, tail)) = rest.split_once('.') else {
            return Err(invalid("remote.url 的 IPvFuture 地址非法"));
        };
        let well_formed = !hex.is_empty()
            && hex.chars().all(|c| c.is_ascii_hexdigit())
            && !tail.is_empty()
            && !tail.contains('\n');
        if !well_formed {
            return Err(invalid("remote.url 的 IPvFuture 地址非法"));
        }
        return Ok(());
    }
    let address = match host.split_once('%') {
        None => host,
        Some((address, scope)) if !scope.is_empty() && !scope.contains('%') => address,
        Some(_) => return Err(invalid("remote.url 的 IPv6 作用域标识非法")),
    };
    if address.parse::<std::net::Ipv6Addr>().is_err() {
        return Err(invalid("remote.url 的括号内必须是 IPv6 或 IPvFuture 地址"));
    }
    Ok(())
}

/// netloc 是否在 NFKC 归一化后引入 `/` `?` `#` `@` `:`（与 `urlsplit._checknetloc` 同口径）。
///
/// IDNA 按 NFKC 等价处理主机名，某些字符（如 `℀` → `a/c`）归一化后会凭空产生分隔符，是
/// 同形混淆的通道。CPython 的做法是去掉已允许出现的 `@ : # ?` 后对整串做 NFKC，再查
/// `/ ? # @ :`——分隔符只可能由某个字符的展开引入（ASCII 标点不参与规范组合），因此这里
/// 等价地逐字符查表：表中是全部「NFKC 展开含分隔符」的码点（19 个）。表按 CPython 3.11 的
/// `unicodedata`（Unicode 14）扫描生成：
///
/// `python3 -c "import unicodedata as u; print([hex(c) for c in range(0x110000) if
/// (lambda n: n != chr(c) and any(d in n for d in '/?#@:'))(u.normalize('NFKC', chr(c)))])"`
fn netloc_introduces_delimiter(netloc: &str) -> bool {
    netloc.chars().any(|character| {
        !matches!(character, '@' | ':' | '#' | '?')
            && matches!(
                character,
                '\u{2047}' // ⁇ → '??'
                | '\u{2048}' // ⁈ → '?!'
                | '\u{2049}' // ⁉ → '!?'
                | '\u{2100}' // ℀ → 'a/c'
                | '\u{2101}' // ℁ → 'a/s'
                | '\u{2105}' // ℅ → 'c/o'
                | '\u{2106}' // ℆ → 'c/u'
                | '\u{2a74}' // ⩴ → '::='
                | '\u{fe13}' // ︓ → ':'
                | '\u{fe16}' // ︖ → '?'
                | '\u{fe55}' // ﹕ → ':'
                | '\u{fe56}' // ﹖ → '?'
                | '\u{fe5f}' // ﹟ → '#'
                | '\u{fe6b}' // ﹫ → '@'
                | '\u{ff03}' // ＃ → '#'
                | '\u{ff0f}' // ／ → '/'
                | '\u{ff1a}' // ： → ':'
                | '\u{ff1f}' // ？ → '?'
                | '\u{ff20}' // ＠ → '@'
            )
    })
}

/// RFC 3986 §3.1 的 scheme 切分（与 `urlsplit` 同口径）：首个 `:` 之前是
/// `ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )` 才算 scheme，返回（scheme, `:` 后的部分）。
fn split_scheme(url: &str) -> Option<(&str, &str)> {
    let colon = url.find(':')?;
    if colon == 0 {
        return None;
    }
    let candidate = &url[..colon];
    let mut characters = candidate.chars();
    let first = characters.next()?;
    if !first.is_ascii_alphabetic() {
        return None;
    }
    if !characters.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')) {
        return None;
    }
    Some((candidate, &url[colon + 1..]))
}

/// 在 `PATH` 中查找可执行文件（与 `shutil.which` 同口径）：整个 `PATH` 为空视为找不到
/// （`shutil.which` 在 `PATH=''` 时返回 `None`），而空条目按**当前目录**解析
/// （`os.path.join('', cmd)` = `cmd`，即 `PATH=':'` 会在当前目录查找）；返回按 `PATH` 条目
/// 拼接的路径，不做规范化。
fn search_path(program: &str, path: &str) -> Option<PathBuf> {
    if path.is_empty() {
        return None;
    }
    let separator = if cfg!(windows) { ';' } else { ':' };
    for entry in path.split(separator) {
        let candidate = Path::new(entry).join(program);
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

/// 路径是否是可执行文件（POSIX 上要求任一可执行位；Windows 上只要求是文件）。
///
/// POSIX 上的判定是 `mode & 0o111`（任一执行位），与 Python `os.access(X_OK)` 的「按实际
/// uid/gid 判定」有取舍：属主为他人、权限 0o710 且进程不属于该属主的文件，`os.access` 判否
/// 而这里判可执行（等价 root 语义）；ACL 授予执行权而 mode 位为 0 时方向相反。要复刻
/// `access(2)` 需要 libc 依赖，v1 取 mode 位这一可移植近似：误判只会让 `exec` 在启动时失败
/// 并以实例级失败上报，不会把不该执行的路径交给本进程执行。
fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = std::fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}
