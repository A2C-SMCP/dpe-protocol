//! connector 契约 §4.1.1 结构上界（长度 ≤1024、分组嵌套深度 ≤64、展开规模 ≤4096）的资源余量测量。
//!
//! 每个用例给出**转译后**的 pattern（子集内 pattern 经「解析 → 转译为显式字符类/码点区间」
//! 后再交给引擎，见 §4.1.1 与 `scripts/pattern_subset.py`），用 regex crate 的**默认限额**
//! 编译：全部 MUST 成功——这是「子集内的任何 pattern MUST 被接受」的可复测证据。
//!
//! 用例与 `vectors/config_schema_patterns.json` 的 `valid_patterns` 对应；若将来 regex 升级或
//! Rust 运行器落地后发现默认限额不够，说明条款需要收紧（而不是让实现调大限额兜底）。

use regex::Regex;
use std::time::Instant;

struct Case {
    name: &'static str,
    units: u32,
    source: &'static str,
    pattern: String,
}

fn sparse_class(n: u32) -> String {
    // 互不相邻、跨字节宽度的多字节码点——UTF-8 自动机代价最高的一类字符类
    let mut body = String::new();
    let mut cp = 0x100u32;
    let mut count = 0;
    while count < n {
        if !(0xD800..=0xDFFF).contains(&cp) {
            body.push_str(&format!("\\x{{{cp:x}}}"));
            count += 1;
        }
        cp += 0x110;
    }
    body
}

fn cases() -> Vec<Case> {
    vec![
        Case {
            name: "[^0-9A-Za-z_]{255}",
            units: 255,
            source: "\\W{255} 的等价简化形态（真转译输出为显式区间）",
            pattern: "[^0-9A-Za-z_]{255}".into(),
        },
        Case {
            name: "[^\\n\\r] × 1024",
            units: 1024,
            source: "1024 个 .（取反类形态；真转译输出见下一例）",
            pattern: "[^\\n\\r]".repeat(1024),
        },
        Case {
            name: "显式区间 . × 1024",
            units: 1024,
            source: "1024 个 .（转译输出形态）",
            pattern: "[\\x{0}-\\x{9}\\x{b}\\x{c}\\x{e}-\\x{d7ff}\\x{e000}-\\x{10ffff}]"
                .repeat(1024),
        },
        Case {
            name: "16 码点手写类 × {255}",
            units: 4080,
            source: "最坏合法形状（向量同款）",
            pattern: format!("[{}]{{255}}", sparse_class(16)),
        },
        Case {
            name: "[aaaa]{1024}",
            units: 4096,
            source: "计数不去重的上界",
            pattern: "[aaaa]{1024}".into(),
        },
        Case {
            name: "(?:a|bc){1365}",
            units: 4095,
            source: "选择与嵌套的上界",
            pattern: "(?:a|bc){1365}".into(),
        },
        Case {
            name: "a{4096}",
            units: 4096,
            source: "{m} 的上界",
            pattern: "a{4096}".into(),
        },
        Case {
            name: "64 层嵌套 + a{4096}",
            units: 4096,
            source: "嵌套深度与展开规模同时顶格（转译输出形态）",
            pattern: format!("{}{}{}", "(?:".repeat(64), "a{4096}", ")".repeat(64)),
        },
        Case {
            name: "64 层嵌套每层带量词",
            units: 1,
            source: "最坏嵌套形状（引擎解析嵌套还计入量词与字符类）",
            pattern: format!("{}[a]*{}", "(?:".repeat(64), ")*".repeat(64)),
        },
        Case {
            name: "(?:){4096}（零尺寸原子计数）",
            units: 4096,
            source: "分组至少计 1；编译与匹配都不应退化",
            pattern: "(?:){4096}".into(),
        },
        Case {
            name: "995 码点手写类（无界）",
            units: 995,
            source: "长度顶格下最贵的不量化类",
            pattern: format!("[{}]", sparse_class(995)),
        },
    ]
}

/// 越界形状（§4.1.1 判定为不合法）：只报告，不影响退出码——引擎变强或变弱都不改变条款，
/// 它们的失败也不是本探针要证明的结论，列在这里是为了「复跑时能直接看到边界的另一侧」。
fn oversize_cases() -> Vec<(&'static str, String)> {
    vec![
        (
            "(?:.×48){255}（越界：展开规模 12240）",
            format!("(?:{}){{255}}", ".".repeat(48)),
        ),
        (
            "[995 码点类]{255}（越界：254000）",
            format!("[{}]{{255}}", sparse_class(995)),
        ),
        (
            "65 层嵌套（越界：深度 65）",
            format!("{}{}{}", "(?:".repeat(65), "a", ")".repeat(65)),
        ),
        (
            "125 层每层带量词（越界，§4.1.1 深度 ≤64）",
            format!("{}[a]*{}", "(?:".repeat(125), ")*".repeat(125)),
        ),
        ("原生 \\W{255}（未转译形态，仅供对照）", "\\W{255}".into()),
    ]
}

fn main() {
    println!("regex 版本见 Cargo.lock（2026-10-09 基线：1.13.1）；默认限额 10MB");
    println!(
        "{:<28} {:>6}  {:>7}  {:<6} 来源",
        "用例", "规模", "编译", "结果"
    );
    let mut failed = 0;
    for case in cases() {
        let t0 = Instant::now();
        let result = Regex::new(&case.pattern);
        let ms = t0.elapsed().as_millis();
        let ok = result.is_ok();
        if !ok {
            failed += 1;
        }
        let detail = match &result {
            Ok(_) => String::new(),
            Err(e) => format!("（{}）", e.to_string().lines().next().unwrap_or("")),
        };
        println!(
            "{:<28} {:>6}  {:>5}ms  {:<6} {}{}",
            case.name,
            case.units,
            ms,
            if ok { "OK" } else { "ERR" },
            case.source,
            detail
        );
    }
    println!();
    println!("越界形状（§4.1.1 判不合法；仅对照，均应以 ERR 或与合法侧无关的方式呈现）");
    for (name, pattern) in oversize_cases() {
        let t0 = Instant::now();
        let result = Regex::new(&pattern);
        println!(
            "{:<44} {:>5}ms  {}",
            name,
            t0.elapsed().as_millis(),
            if result.is_ok() {
                "OK（引擎变得更强，条款不受影响）"
            } else {
                "ERR（预期一侧）"
            }
        );
    }
    if failed > 0 {
        eprintln!("{failed} 个用例超出默认限额：§4.1.1 的结构上界需要收紧（见 README）");
        std::process::exit(1);
    }
    println!("全部在默认限额内：条款维持（余量与版本见 README.md）");
}
