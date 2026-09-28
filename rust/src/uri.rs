//! 外部源 `file_uri` 的保留命名空间：`dpe://{tenant}/{path}`。
//!
//! `file_uri` 在 Robot Memory 中是 UNIQUE 键，外部源必须与 Robot 内部源（feishu / cos 等）隔离。
//! push-protocol-v1 将 `dpe://{tenant}/{path}` 列为候选方案（待决策项 #2），本 SDK 先按此实现。

use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use url::Url;

use crate::error::{DpeError, Result};

pub const DPE_SCHEME: &str = "dpe";

/// 与 Python `urllib.parse.quote(s, safe="")` 一致：仅保留字母数字与 `_.-~`。
const SEGMENT: &AsciiSet = &NON_ALPHANUMERIC.remove(b'_').remove(b'.').remove(b'-').remove(b'~');

fn valid_tenant(tenant: &str) -> bool {
    !tenant.is_empty()
        && tenant.split('.').all(|label| {
            !label.is_empty()
                && label.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
                && !label.starts_with('-')
                && !label.ends_with('-')
        })
}

/// 构造规范化的 `dpe://` URI（与 Python SDK 的 `make_dpe_uri` 结果一致）。
///
/// - `tenant`：小写字母数字、`-`、`.`（如 `acme-crm`、`com.acme.wiki`）
/// - `path`：源内路径，以 `/` 分隔，各段自动做百分号编码
pub fn make_dpe_uri(tenant: &str, path: &str) -> Result<Url> {
    if !valid_tenant(tenant) {
        return Err(DpeError::InvalidUri(format!(
            "invalid dpe tenant {tenant:?}: use lowercase letters, digits, '-' and '.'"
        )));
    }
    let segments: Vec<String> =
        path.split('/').filter(|s| !s.is_empty()).map(|s| utf8_percent_encode(s, SEGMENT).to_string()).collect();
    if segments.is_empty() {
        return Err(DpeError::InvalidUri("dpe uri path must not be empty".into()));
    }
    Url::parse(&format!("{DPE_SCHEME}://{tenant}/{}", segments.join("/")))
        .map_err(|e| DpeError::InvalidUri(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_normalized_uri() {
        assert_eq!(make_dpe_uri("acme-crm", "/docs/a b.md").unwrap().as_str(), "dpe://acme-crm/docs/a%20b.md");
        assert_eq!(
            make_dpe_uri("acme", "wiki/手册 v1.md").unwrap().as_str(),
            "dpe://acme/wiki/%E6%89%8B%E5%86%8C%20v1.md"
        );
        assert!(make_dpe_uri("Acme", "x").is_err());
        assert!(make_dpe_uri("acme", "/").is_err());
    }
}
