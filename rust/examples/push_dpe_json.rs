//! 示例：上游已产出的 DPE JSON（内核 `Document` dump 格式）→ Robot。
//!
//! 适用于上游已经完成解析的场景（例如解析服务的输出）：读取 `*.dpe.json`，
//! 经 [`Document::from_kernel_dump`] 剔除内核字段后自检并打印 hash。
//!
//! ```text
//! cargo run --all-features --example push_dpe_json -- <dir>
//! ```

use std::path::Path;

use dpe_protocol::hashing::{compute_hashes, HashStrategyRef};
use dpe_protocol::schema::Document;
use dpe_protocol::testing::{check_documents, CheckOptions, ConformanceReport};
use dpe_protocol::Result;

async fn load(path: &Path) -> Result<Document> {
    Document::from_kernel_dump(serde_json::from_slice(&tokio::fs::read(path).await?)?)
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let Some(root) = std::env::args().nth(1) else {
        eprintln!("usage: push_dpe_json <dir>");
        std::process::exit(2);
    };
    let mut paths = Vec::new();
    let mut dir = tokio::fs::read_dir(&root).await?;
    while let Some(item) = dir.next_entry().await? {
        if item.file_name().to_string_lossy().ends_with(".dpe.json") {
            paths.push(item.path());
        }
    }
    paths.sort();

    let mut report = ConformanceReport::default();
    for path in &paths {
        report.extend(check_documents(&load(path).await?, &load(path).await?, CheckOptions::default()).await);
    }
    println!("conformance: {} issues", report.issues.len());
    report.assert_ok();

    for path in &paths {
        let doc = load(path).await?;
        println!("{}  doc_hash={}", doc.file_uri, compute_hashes(&doc, &HashStrategyRef::default_v1())?.doc_hash);
    }
    Ok(())
}
