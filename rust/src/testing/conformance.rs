//! 文档一致性检查：验证上层产出的 DPE 文档满足增量投递所依赖的约束。
//!
//! 本 crate 不定义 connector 的形态。上层用自己的方式对**同一份未修改的源数据产出两次文档**，
//! 交给 [`check_documents`] 即可：
//!
//! ```ignore
//! let (first, second) = (build_doc(&item).await?, build_doc(&item).await?);
//! check_documents(&first, &second, CheckOptions { expected_file_uri: Some(uri), ..Default::default() })
//!     .await
//!     .assert_ok();
//! ```
//!
//! 检查项与 Python SDK 的 `dpe_protocol.testing.check_documents` 一致：
//! `static` / `file_uri` / `deterministic` / `push_roundtrip`。

use std::collections::HashSet;
use std::fmt;
use std::sync::Arc;

use super::FakeRobotServer;
use crate::hashing::{compute_hashes, HashStrategyRef};
use crate::push::{CommitStatus, DpePushClient};
use crate::schema::Document;
use crate::validation::check_document;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConformanceIssue {
    pub rule: &'static str,
    pub message: String,
    pub file_uri: Option<String>,
}

impl fmt::Display for ConformanceIssue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}: {}", self.rule, self.file_uri.as_deref().unwrap_or("-"), self.message)
    }
}

#[derive(Debug, Clone, Default)]
pub struct ConformanceReport {
    pub issues: Vec<ConformanceIssue>,
}

impl ConformanceReport {
    pub fn ok(&self) -> bool {
        self.issues.is_empty()
    }

    pub fn rules(&self) -> HashSet<&'static str> {
        self.issues.iter().map(|i| i.rule).collect()
    }

    /// 合并另一份报告（例如逐个文档检查后汇总）。
    pub fn extend(&mut self, other: ConformanceReport) {
        self.issues.extend(other.issues);
    }

    /// 有任何问题即 panic，便于在测试中使用。
    pub fn assert_ok(&self) {
        assert!(
            self.ok(),
            "document conformance failed:\n{}",
            self.issues.iter().map(ToString::to_string).collect::<Vec<_>>().join("\n")
        );
    }
}

#[derive(Debug, Clone, Copy)]
pub struct CheckOptions<'a> {
    /// 调用方预期的 `file_uri`
    pub expected_file_uri: Option<&'a str>,
    /// 是否经 FakeRobotServer 做投递往返检查
    pub push: bool,
}

impl Default for CheckOptions<'_> {
    fn default() -> Self {
        Self { expected_file_uri: None, push: true }
    }
}

/// 对同一份未修改源数据的两次产出运行全部检查。
pub async fn check_documents(first: &Document, second: &Document, options: CheckOptions<'_>) -> ConformanceReport {
    let mut report = ConformanceReport::default();
    let uri = first.file_uri.to_string();
    let mut issue = |rule, message: String| {
        report.issues.push(ConformanceIssue { rule, message, file_uri: Some(uri.clone()) });
    };

    let violations = check_document(first, None);
    for violation in &violations {
        issue("static", violation.clone());
    }
    if let Some(expected) = options.expected_file_uri {
        if uri != expected {
            issue("file_uri", format!("document file_uri {uri} differs from expected {expected}"));
        }
    }
    if second.file_uri.as_str() != uri {
        issue("file_uri", format!("two builds produced different file_uri ({uri} != {})", second.file_uri));
    }

    let strategy = HashStrategyRef::default_v1();
    match (compute_hashes(first, &strategy), compute_hashes(second, &strategy)) {
        (Ok(a), Ok(b)) if a.doc_hash != b.doc_hash => {
            issue("deterministic", format!("two builds produced different doc_hash ({} != {})", a.doc_hash, b.doc_hash))
        }
        (Err(e), _) | (_, Err(e)) => issue("static", format!("hashing failed: {e}")),
        _ => {}
    }

    if options.push && violations.is_empty() {
        let client = DpePushClient::new("http://conformance.test", Arc::new(FakeRobotServer::default()));
        let pushed = match client.push(first, None).await {
            Ok(result) => client.push(second, Some(result.doc_hash)).await,
            Err(e) => Err(e),
        };
        match pushed {
            Ok(again) if again.status == CommitStatus::Unchanged => {}
            Ok(again) => issue(
                "push_roundtrip",
                format!("re-pushing the second build returned {:?}, expected Unchanged", again.status),
            ),
            Err(e) => issue("push_roundtrip", format!("push failed: {e}")),
        }
    }
    report
}
