//! dpe-hash 原位重算基准：只报告耗时，不设门槛（对等 Python 的 `tools/bench_hash.py`，
//! 供内核评估 TFROB-958 的迁移窗口）。
//!
//! 合成一篇 `--pages` × `--per-page` 个元素的文档（混合 category、带版面坐标 metadata），
//! 分别在 dpe1 与升级演练契约 dpe2 下计时 `document_hashes`，按 Markdown 表格输出，
//! CI 把输出写进 job summary。
//!
//! 用法：
//!
//! ```text
//! cargo run --release -p dpe-hash --example bench_hash -- [--pages 1000] [--per-page 100] [--repeat 3]
//! ```

use std::time::Instant;

use dpe_hash::{blob_ref, document_hashes, CONTRACT, DRILL_CONTRACT};
use serde_json::{json, Value};

fn element(page: usize, i: usize) -> Value {
    let y = i as f64 / 100.0;
    let bx = json!({"points": [[0.1, y], [0.9, y + 0.01]], "system": "PixelSpace"});
    if i % 20 == 0 {
        return json!({
            "category": "Image",
            "blob": blob_ref(format!("{page}-{i}").as_bytes()),
            "mime_type": "image/png",
            "metadata": {"coordinates": bx, "image_url": format!("https://cdn.example/{page}/{i}.png")},
        });
    }
    if i % 10 == 0 {
        return json!({
            "category": "Table",
            "text": format!("表 {page}.{i}"),
            "text_as_html": format!("<table><tr><td>{page}</td><td>{i}</td></tr></table>"),
            "metadata": {"coordinates": bx},
        });
    }
    json!({
        "category": if i == 1 { "Title" } else { "NarrativeText" },
        "text": format!("第 {page} 页第 {i} 段：{}", "正文内容 ".repeat(20)),
        "metadata": {"coordinates": bx, "lang": "zh"},
    })
}

fn synth(pages: usize, per_page: usize) -> Value {
    let pages: Vec<Value> = (0..pages)
        .map(|p| {
            json!({
                "title": format!("p{p}"),
                "page_metadata": {"page_label": (p + 1).to_string()},
                "elements": (0..per_page).map(|i| element(p, i)).collect::<Vec<_>>(),
            })
        })
        .collect();
    json!({
        "file_type": "pdf",
        "title": "基准文档",
        "doc_metadata": {"author": "bench", "created_at": "2026-10-02T00:00:00Z"},
        "pages": pages,
    })
}

fn arg(args: &[String], name: &str, default: usize) -> usize {
    args.iter().position(|a| a == name).map_or(default, |i| {
        args.get(i + 1)
            .and_then(|v| v.parse().ok())
            .unwrap_or_else(|| panic!("{name} 需要一个正整数参数"))
    })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let pages = arg(&args, "--pages", 1000);
    let per_page = arg(&args, "--per-page", 100);
    let repeat = arg(&args, "--repeat", 3).max(1);

    let doc = synth(pages, per_page);
    let total = pages * per_page;
    println!("### dpe-hash 原位重算基准（{pages} 页 × {per_page} = {total} 元素）\n");
    let profile = if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    };
    println!(
        "Rust dpe-hash {}（{}，{profile}），取 {repeat} 次最小值\n",
        dpe_hash::VERSION,
        std::env::consts::ARCH
    );
    println!("| 契约 | 耗时 (s) | 元素/秒 |");
    println!("| --- | ---: | ---: |");
    for contract in [CONTRACT, DRILL_CONTRACT] {
        let mut best = f64::INFINITY;
        for _ in 0..repeat {
            let start = Instant::now();
            document_hashes(&doc, contract).expect("合成文档合法");
            best = best.min(start.elapsed().as_secs_f64());
        }
        println!("| {contract} | {best:.2} | {:.0} |", total as f64 / best);
    }
}
