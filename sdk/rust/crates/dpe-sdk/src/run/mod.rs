//! dpe-run 的运行器侧协议核心（connector 契约）：插件清单与实例配置校验（§4.1–§4.2），
//! 含 §4.1.1 的 `config_schema` 可移植正则子集。
//!
//! - [`manifest`]：清单（`dpe-connector.json`）与实例配置校验（§4.1–§4.2），
//!   [`manifest::parse_manifest`] / [`manifest::validate_config`]；
//! - [`pattern`]：`config_schema` 的 `pattern` / `patternProperties` 子集（§4.1.1），
//!   含合法性判定（[`pattern::check_pattern`]）与匹配（[`pattern::Pattern`]）。
//!
//! 两部分都是 sans-IO：只处理已解析的 JSON 值，不访问网络或文件。与 Python SDK 的
//! `dpe_sdk.run.manifest` / `dpe_sdk.run._pattern` 行为对等。插件宿主与线协议（§6）不在
//! 本模块范围内，随后续任务落地。

use std::fmt;

pub mod manifest;
pub mod pattern;

pub(crate) mod schema;

/// §7.4：清单不合法（实例级失败）。
pub const MANIFEST_INVALID: &str = "manifest_invalid";
/// §7.1：实例配置未通过 `config_schema`（实例级失败）。
pub const CONFIG_SCHEMA_VIOLATION: &str = "config_schema_violation";

/// 实例级失败（connector 契约 §7.1）：不改动实例定义、清单或凭证就无法恢复，MUST NOT 自动重试。
/// `code` 取 §7.4 轮报告 `failures` 元素的码名。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstanceFailure {
    code: &'static str,
    message: String,
}

impl InstanceFailure {
    pub(crate) fn manifest_invalid(message: impl Into<String>) -> Self {
        InstanceFailure {
            code: MANIFEST_INVALID,
            message: message.into(),
        }
    }

    pub(crate) fn config_schema_violation(message: impl Into<String>) -> Self {
        InstanceFailure {
            code: CONFIG_SCHEMA_VIOLATION,
            message: message.into(),
        }
    }

    /// §7.4 报告字段的码名。
    pub fn code(&self) -> &'static str {
        self.code
    }

    /// 说明（MUST NOT 含凭证值，§4.3）；只写凭证名，不写解析值。
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for InstanceFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}（{}）", self.message, self.code)
    }
}

impl std::error::Error for InstanceFailure {}
