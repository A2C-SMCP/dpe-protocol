//! 示例：本地 Markdown 目录 → DPE → Robot（与 Python 示例 `examples/push_markdown_dir.py` 规则一致）。
//!
//! 这是**上层调用方**的示例，演示如何使用本 crate：自行遍历源数据、构造 Document、交给
//! [`StatefulPusher`] 投递，并用 [`check_documents`] 自检。遍历、解析、调度都属于上层逻辑，
//! 本 crate 不提供也不规定其形态。
//!
//! 每个 `.md` 文件对应一个 Document、一页（`number=0`），按块切分为 element：
//! 标题 → Title、列表项 → ListItem、围栏代码 → CodeSnippet、表格 → Table、其余段落 → NarrativeText。
//!
//! ```text
//! cargo run --all-features --example push_markdown_dir -- <markdown-dir> <tenant> [prefix]
//! ```
//!
//! 默认只做一致性自检并打印 hash；设置环境变量 `DPE_ROBOT_URL`（及可选的 `DPE_TOKEN`）后增量投递到 Robot。

use std::path::Path;
use std::sync::LazyLock;

use dpe_protocol::hashing::{compute_hashes, primitives::digest, HashStrategyRef};
use dpe_protocol::push::DpePushClient;
use dpe_protocol::schema::{DocElement, DocMetadata, DocPage, Document, ElementCategory, ElementMetadata, FileType};
use dpe_protocol::state::JsonFileStateStore;
use dpe_protocol::testing::{check_documents, CheckOptions, ConformanceReport};
use dpe_protocol::{make_dpe_uri, Result, StatefulPusher};
use regex::Regex;

static HEADING: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(#{1,6})\s+(.*?)\s*#*\s*$").unwrap());
static LIST_ITEM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$").unwrap());
static FENCE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*(```|~~~)").unwrap());
static TABLE_SEPARATOR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$").unwrap());

pub fn markdown_to_elements(text: &str) -> Vec<DocElement> {
    let lines: Vec<&str> = text.lines().collect();
    let mut elements = Vec::new();
    let mut paragraph: Vec<&str> = Vec::new();
    let mut table: Vec<&str> = Vec::new();

    fn flush_paragraph(paragraph: &mut Vec<&str>, elements: &mut Vec<DocElement>) {
        if !paragraph.is_empty() {
            elements.push(DocElement::new(ElementCategory::NarrativeText, paragraph.join("\n").trim()));
            paragraph.clear();
        }
    }
    fn flush_table(table: &mut Vec<&str>, elements: &mut Vec<DocElement>) {
        if !table.is_empty() {
            elements.push(table_element(table));
            table.clear();
        }
    }

    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        if let Some(fence) = FENCE.captures(line) {
            flush_paragraph(&mut paragraph, &mut elements);
            flush_table(&mut table, &mut elements);
            let marker = fence.get(1).unwrap().as_str();
            let mut body = Vec::new();
            i += 1;
            while i < lines.len() && !lines[i].trim().starts_with(marker) {
                body.push(lines[i]);
                i += 1;
            }
            elements.push(DocElement::new(ElementCategory::CodeSnippet, body.join("\n")));
        } else if line.trim_start().starts_with('|') {
            flush_paragraph(&mut paragraph, &mut elements);
            table.push(line);
        } else if let Some(heading) = HEADING.captures(line) {
            flush_paragraph(&mut paragraph, &mut elements);
            flush_table(&mut table, &mut elements);
            let depth = heading.get(1).unwrap().as_str().len() as i64 - 1;
            elements.push(
                DocElement::new(ElementCategory::Title, heading.get(2).unwrap().as_str())
                    .with_metadata(ElementMetadata { category_depth: Some(depth), ..Default::default() }),
            );
        } else if let Some(item) = LIST_ITEM.captures(line) {
            flush_paragraph(&mut paragraph, &mut elements);
            flush_table(&mut table, &mut elements);
            elements.push(DocElement::new(ElementCategory::ListItem, item.get(1).unwrap().as_str().trim()));
        } else if line.trim().is_empty() {
            flush_paragraph(&mut paragraph, &mut elements);
            flush_table(&mut table, &mut elements);
        } else {
            flush_table(&mut table, &mut elements);
            paragraph.push(line);
        }
        i += 1;
    }
    flush_paragraph(&mut paragraph, &mut elements);
    flush_table(&mut table, &mut elements);
    elements
}

/// Python `html.escape(s, quote=True)`。
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

fn table_element(lines: &[&str]) -> DocElement {
    let rows: Vec<Vec<&str>> = lines
        .iter()
        .filter(|l| !TABLE_SEPARATOR.is_match(l))
        .map(|l| l.trim().trim_matches('|').split('|').map(str::trim).collect())
        .collect();
    let body: String = rows
        .iter()
        .map(|row| {
            format!("<tr>{}</tr>", row.iter().map(|c| format!("<td>{}</td>", html_escape(c))).collect::<String>())
        })
        .collect();
    let text = rows.iter().map(|row| row.join(" ")).collect::<Vec<_>>().join("\n");
    DocElement::new(ElementCategory::Table, text)
        .with_metadata(ElementMetadata { text_as_html: Some(format!("<table>{body}</table>")), ..Default::default() })
}

/// 递归列出 `root` 下文件名以 `suffix` 结尾的文件（相对路径，`/` 分隔，按字典序）。
async fn list_files(root: &Path, suffix: &str) -> Result<Vec<String>> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let mut entries = tokio::fs::read_dir(&dir).await?;
        while let Some(entry) = entries.next_entry().await? {
            let path = entry.path();
            if entry.file_type().await?.is_dir() {
                stack.push(path);
            } else if path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(suffix)) {
                let rel = path.strip_prefix(root).expect("walked under root");
                out.push(rel.components().map(|c| c.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/"));
            }
        }
    }
    out.sort();
    Ok(out)
}

fn file_uri_for(rel: &str, tenant: &str, prefix: &str) -> Result<url::Url> {
    let prefix = prefix.trim_matches('/');
    make_dpe_uri(tenant, &if prefix.is_empty() { rel.to_string() } else { format!("{prefix}/{rel}") })
}

async fn build_document(root: &Path, rel: &str, tenant: &str, prefix: &str) -> Result<Document> {
    let text = tokio::fs::read_to_string(root.join(rel)).await?;
    let elements = markdown_to_elements(&text);
    let title = elements.iter().find(|e| e.category == ElementCategory::Title).map(|e| e.text.clone());
    let mut doc =
        Document::new(file_uri_for(rel, tenant, prefix)?, FileType::Md, vec![DocPage::new(0, title, elements)]);
    // 只放由内容决定的字段；mtime 之类会让未改动的文件也产生新 doc_hash
    doc.doc_metadata = DocMetadata { filename: rel.rsplit('/').next().map(str::to_string), ..Default::default() };
    Ok(doc)
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [root, tenant, rest @ ..] = args.as_slice() else {
        eprintln!("usage: push_markdown_dir <markdown-dir> <tenant> [prefix]");
        std::process::exit(2);
    };
    let (root, prefix) = (Path::new(root), rest.first().map(String::as_str).unwrap_or(""));
    let files = list_files(root, ".md").await?;

    // 1. 一致性自检：每个文件构造两次文档
    let mut report = ConformanceReport::default();
    for rel in &files {
        let (first, second) =
            (build_document(root, rel, tenant, prefix).await?, build_document(root, rel, tenant, prefix).await?);
        let expected = file_uri_for(rel, tenant, prefix)?.to_string();
        let options = CheckOptions { expected_file_uri: Some(&expected), ..Default::default() };
        report.extend(check_documents(&first, &second, options).await);
    }
    println!("conformance: {} issues", report.issues.len());
    report.assert_ok();

    // 2. 未配置 Robot 时只打印 hash
    let Ok(robot_url) = std::env::var("DPE_ROBOT_URL") else {
        for rel in &files {
            let doc = build_document(root, rel, tenant, prefix).await?;
            let hashes = compute_hashes(&doc, &HashStrategyRef::default_v1())?;
            let elements: usize = hashes.pages.iter().map(|p| p.content_hashes.len()).sum();
            println!("{}  doc_hash={}  elements={elements}", doc.file_uri, hashes.doc_hash);
        }
        return Ok(());
    };

    // 3. 增量投递：文件内容未变（fingerprint 相同）时跳过
    let mut client = DpePushClient::with_reqwest(robot_url)?;
    if let Ok(token) = std::env::var("DPE_TOKEN") {
        client = client.with_token(token);
    }
    let state = JsonFileStateStore::new(".dpe-state/markdown.json");
    let pusher = StatefulPusher::new(&client, &state);
    for rel in &files {
        let uri = file_uri_for(rel, tenant, prefix)?;
        let fingerprint = digest(&tokio::fs::read(root.join(rel)).await?);
        if !pusher.needs_push(uri.as_str(), Some(&fingerprint)).await? {
            println!("{uri}  skipped");
            continue;
        }
        let result = pusher.push(&build_document(root, rel, tenant, prefix).await?, Some(fingerprint)).await?;
        println!("{uri}  {:?}  {:?}", result.status, result.counts);
    }
    Ok(())
}
