//! 凭证（connector 契约 §4.3）：来源引用、基础环境名单与「同源」判定。
//!
//! 两类凭证严格分属：remote 凭证只属于运行器，用于向 remote 鉴权；数据源凭证只属于插件，
//! 用于读取内容源与 blob 字节。实例定义中的凭证 MUST 以来源引用表示，MUST NOT 含凭证值本身
//! （§4.3）——本模块承载其中与定义校验相关的部分：[`SourceRef`]（封闭的来源引用）、
//! [`is_base_env_name`]（基础环境名单）与 [`same_source`]（「remote 凭证不可达」的判定）。
//!
//! 凭证解析（`resolve`）、插件环境构造与脱敏（Python SDK `dpe_sdk.run.secrets` 的其余部分）
//! 随 Rust 运行器落地，不在本模块范围内；与 Python SDK 的 `dpe_sdk.run.secrets` 在同名 API
//! 上行为对等。

use std::collections::{HashSet, VecDeque};
use std::ffi::OsString;
use std::path::{Component, Path, PathBuf};

use serde_json::Value;

/// §4.3 基础环境（第 1 部分）的全平台名单，另加全部 `LC_*`。清单与实例定义的同名检查按全平台
/// 名单判定，使同一份声明在任何平台上得出相同结论。
const BASE_ENV_NAMES: [&str; 11] = [
    // POSIX
    "PATH",
    "HOME",
    "LANG",
    "TZ",
    "TMPDIR",
    // Windows
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "PATHEXT",
    "COMSPEC",
];

/// `name` 是否属于基础环境（任何平台）。
pub fn is_base_env_name(name: &str) -> bool {
    BASE_ENV_NAMES.contains(&name) || name.starts_with("LC_")
}

/// 凭证来源引用：恰含 `env` 或 `file` 之一的封闭对象（§4.3）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceRef {
    env: Option<String>,
    file: Option<String>,
}

impl SourceRef {
    /// 取 `env` 形态的变量名；`file` 形态时为 `None`。
    pub fn env(&self) -> Option<&str> {
        self.env.as_deref()
    }

    /// 取 `file` 形态的路径（相对路径的基准是实例定义所在目录，§4.4）；`env` 形态时为 `None`。
    pub fn file(&self) -> Option<&str> {
        self.file.as_deref()
    }

    /// 校验已解析的来源引用：封闭成员、恰含 `env` 或 `file` 之一、显式的 null 是类型不符。
    ///
    /// 失败返回消息（调用方按所在载体归类错误，如实例定义的 `definition_invalid`），消息只写
    /// 成员名、不回显取值：写错位置的字面量凭证会完整落入日志与 §7.4 轮报告（§4.3「不外泄」），
    /// Python SDK 的消息同样不含输入值。
    pub(crate) fn parse(value: &Value) -> Result<SourceRef, String> {
        let Some(map) = value.as_object() else {
            return Err(String::from("来源引用必须是对象"));
        };
        for (member, member_value) in map {
            match member.as_str() {
                "env" | "file" => {}
                other => return Err(format!("来源引用出现了未定义的成员 {other:?}")),
            }
            if member_value.is_null() {
                return Err(format!("来源引用的 {member} 不得为 null"));
            }
        }
        let env = match map.get("env") {
            None => None,
            Some(Value::String(name)) => Some(name.clone()),
            Some(_) => return Err(String::from("来源引用的 env 必须是字符串")),
        };
        let file = match map.get("file") {
            None => None,
            Some(Value::String(path)) => Some(path.clone()),
            Some(_) => return Err(String::from("来源引用的 file 必须是字符串")),
        };
        if env.is_none() == file.is_none() {
            return Err("来源引用必须恰含 env 或 file 之一".to_string());
        }
        Ok(SourceRef { env, file })
    }

    /// `file` 形态的路径段（构造保证恰有其一）。
    fn path_of(&self) -> &str {
        self.file.as_deref().expect("file 形态必有 file")
    }
}

/// 两个引用是否指向同一来源：同名 `env`，或解析为同一路径的 `file`（§4.3）。
///
/// `file` 路径无法解析（如符号链接成环）时返回 `Err`，由定义校验归为不合法（与 Python SDK
/// `Path.resolve()` 抛 `RuntimeError` 的口径一致）。
pub fn same_source(a: &SourceRef, b: &SourceRef, base_dir: &Path) -> Result<bool, String> {
    if a.env.is_some() || b.env.is_some() {
        return Ok(a.env.is_some() && a.env == b.env);
    }
    let a_path = resolve_file_path(&base_dir.join(a.path_of()))?;
    let b_path = resolve_file_path(&base_dir.join(b.path_of()))?;
    Ok(a_path == b_path)
}

/// 解析 `file` 引用指向的实际路径（§4.3「解析为同一路径的 file」）。
///
/// 与 Python SDK `Path.resolve()`（POSIX realpath）同语义：逐段展开符号链接；无法继续解析的
/// 段（不存在、权限不足、中间段不是目录）按字面保留，其后的 `..` 仍按词法弹出；相对路径以
/// 进程当前目录为基准。符号链接成环（同一「当前路径 + 剩余段」状态重现）时返回 `Err`
/// （Python SDK 在同样情形抛 `RuntimeError`，由定义校验归为不合法）。
fn resolve_file_path(path: &Path) -> Result<PathBuf, String> {
    let mut resolved = PathBuf::new();
    let mut pending: VecDeque<OsString> = VecDeque::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => resolved.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => pending.push_back(OsString::from("..")),
            Component::Normal(name) => pending.push_back(name.to_os_string()),
        }
    }
    if resolved.as_os_str().is_empty() {
        resolved = std::env::current_dir()
            .map_err(|exc| format!("凭证文件路径无法解析（当前目录不可用：{exc}）"))?;
    }
    // 符号链接成环检测：同一「当前路径 + 剩余段」状态重现即环（与 Python 的 seen 集合同口径）
    let mut seen: HashSet<(PathBuf, Vec<OsString>)> = HashSet::new();
    while let Some(name) = pending.pop_front() {
        if name.is_empty() {
            continue;
        }
        if name == ".." {
            resolved.pop();
            continue;
        }
        let candidate = resolved.join(&name);
        let Ok(metadata) = std::fs::symlink_metadata(&candidate) else {
            // 不存在 / 权限不足 / 中间段不是目录：按字面保留，解析结束于此段
            resolved = candidate;
            continue;
        };
        if !metadata.file_type().is_symlink() {
            resolved = candidate;
            continue;
        }
        if !seen.insert((candidate.clone(), pending.iter().cloned().collect())) {
            return Err(format!(
                "凭证文件路径无法解析（符号链接成环）：{}",
                candidate.display()
            ));
        }
        let Ok(target) = std::fs::read_link(&candidate) else {
            resolved = candidate;
            continue;
        };
        if target.is_absolute() {
            // 绝对目标从目标自身的根开始（Windows 上可能换盘符）
            resolved = target
                .components()
                .take_while(|component| {
                    matches!(component, Component::Prefix(_) | Component::RootDir)
                })
                .collect();
        }
        // 相对目标以「链接所在目录」（即当前 resolved）为基准：目标的段插回队首；
        // 绝对目标已重置根，同样插回队首
        for component in target.components().rev() {
            match component {
                Component::Prefix(_) | Component::RootDir | Component::CurDir => {}
                Component::ParentDir => pending.push_front(OsString::from("..")),
                Component::Normal(name) => pending.push_front(name.to_os_string()),
            }
        }
    }
    Ok(resolved)
}
