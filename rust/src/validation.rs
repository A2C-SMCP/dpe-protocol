//! 文档级校验：一份 DPE 文档是否满足投递约束。
//!
//! 只检查「看一份文档就能判断」的规则；需要对比两次产出才能判断的约束（确定性）
//! 见 `testing::check_documents`（需启用 `testing` feature）。

use crate::push::check_pushable;
use crate::schema::Document;
use crate::uri::DPE_SCHEME;

/// 返回违规描述列表（空表示通过）。`expected_file_uri` 为调用方预期的 `file_uri`，不一致即违规。
pub fn check_document(doc: &Document, expected_file_uri: Option<&str>) -> Vec<String> {
    let mut violations = check_pushable(doc);
    if let Err(crate::DpeError::Validation(v)) = doc.validate() {
        violations.extend(v);
    }
    if doc.file_uri.scheme() != DPE_SCHEME {
        violations.push(format!("file_uri must use the reserved dpe:// namespace, got {}", doc.file_uri));
    }
    if let Some(expected) = expected_file_uri {
        if doc.file_uri.as_str() != expected {
            violations.push(format!("document file_uri {} differs from expected {expected}", doc.file_uri));
        }
    }
    violations
}
