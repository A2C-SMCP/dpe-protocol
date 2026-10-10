//! connector 契约 §4.1.1 `config_schema` 的**可移植正则子集**（实现侧）。
//!
//! 合法性只由文法与结构上界决定（[`check_pattern`]），与本地正则引擎能否编译无关；匹配一律在
//! 「解析 → 转译为显式码点区间」之后进行——[`Pattern::new`] 交给 `regex` 的转译结果只含
//! 显式字符类/码点区间、非捕获分组与 `\A`/`\z` 锚点，不依赖引擎对 `\w`、`$`、`.` 等的默认语义
//! （Python SDK 的 `dpe_sdk.run._pattern` 与规范参考实现 `scripts/pattern_subset.py` 同构，
//! 三者行为对等；两个 SDK 都只消费向量，不 import 生成器）。
//!
//! 转译输出的资源余量：子集的结构上界（长度 ≤ 1024、嵌套深度 ≤ 64、展开规模 ≤ 4096）是照着
//! `regex` 的默认编译限额定的，最贵形状的复测见 `scripts/pattern_budget` 探针——子集内的任何
//! pattern 都必须被接受，实现不得以引擎资源上限为由拒绝（§4.1.1）。

use std::fmt;

use regex::Regex;

/// §4.1.1：pattern 长度上限（Unicode 标量值个数）。
pub const MAX_LENGTH: usize = 1024;
/// §4.1.1：展开规模上限。
pub const MAX_EXPANSION: u64 = 4096;
/// §4.1.1：分组嵌套深度上限（括号嵌套层数；最外层分组计 1）。
pub const MAX_NESTING: usize = 64;

/// §4.1.1 escape 产生式允许的转义（另加 `\$`、`\n`、`\r`、`\t` 与 `\xHH`）。
const ESCAPABLE: &str = "()*+-.?[\\]^{|}$";
/// 元字符：作字面 MUST 转义。
const METACHARS: &str = "\\.^$|?*+()[]{}";

fn control_escape(c: char) -> Option<u32> {
    match c {
        'n' => Some(0x0A),
        'r' => Some(0x0D),
        't' => Some(0x09),
        _ => None,
    }
}

/// pattern 不属于 §4.1.1 子集。[`PatternError::reason`] 是与参考实现同名的机器可读原因标记。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PatternError {
    reason: &'static str,
    message: String,
}

impl PatternError {
    fn new(reason: &'static str, message: impl Into<String>) -> Self {
        PatternError {
            reason,
            message: message.into(),
        }
    }

    /// 机器可读的原因标记（与规范参考实现/ Python SDK 同名；向量不跨实现断言它，仅供阅读与排查）。
    pub fn reason(&self) -> &'static str {
        self.reason
    }

    /// 人类可读的说明。
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for PatternError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for PatternError {}

// ---------------------------------------------------------------------------
// AST
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct Alt {
    branches: Vec<Vec<Piece>>,
}

#[derive(Debug)]
struct Piece {
    atom: Atom,
    quant: Option<Quant>,
}

#[derive(Debug)]
struct Quant {
    /// 转译时原样输出（`*`、`+`、`?`、`{m}`、`{m,}`、`{m,n}`）。
    text: String,
    /// 展开规模因子（`{m}`→m、`{m,n}`→n、`{m,}`→m、`*`/`+`/`?`→1；超出 u64 的计数饱和）。
    factor: u64,
}

#[derive(Debug)]
enum Atom {
    Literal(u32),
    AnyChar,
    /// `d` `D` `w` `W` `s` `S`。
    Shorthand(char),
    CharClass(CharClass),
    Group(Alt),
    Anchor {
        /// `true` → `^`（文本开头），`false` → `$`（文本结尾）。
        start: bool,
    },
}

#[derive(Debug)]
struct CharClass {
    items: Vec<ClassItem>,
    negated: bool,
}

/// 类内成员：单字符（`from == to`）或区间，或简写类。
#[derive(Debug)]
enum ClassItem {
    Range(u32, u32),
    Shorthand(char),
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

struct Parser {
    s: Vec<char>,
    i: usize,
    depth: usize,
}

impl Parser {
    fn fail(&self, reason: &'static str, message: impl Into<String>) -> PatternError {
        PatternError::new(reason, message)
    }

    fn peek(&self) -> Option<char> {
        self.s.get(self.i).copied()
    }

    fn take(&mut self) -> char {
        let c = self.s[self.i];
        self.i += 1;
        c
    }

    fn text(&self, from: usize, to: usize) -> String {
        self.s[from..to].iter().collect()
    }

    // pattern = branch *("|" branch)
    fn alt(&mut self, top: bool) -> Result<Alt, PatternError> {
        let mut alt = Alt {
            branches: vec![self.branch(top)?],
        };
        while self.peek() == Some('|') {
            self.take();
            alt.branches.push(self.branch(top)?);
        }
        Ok(alt)
    }

    // branch = ["^"] *piece ["$"]
    fn branch(&mut self, top: bool) -> Result<Vec<Piece>, PatternError> {
        let mut pieces: Vec<Piece> = Vec::new();
        if self.peek() == Some('^') {
            if !top {
                return Err(self.fail("anchor_position", "^ 只允许在顶层分支的开头，作字面须转义"));
            }
            self.take();
            pieces.push(Piece {
                atom: Atom::Anchor { start: true },
                quant: None,
            });
        }
        loop {
            match self.peek() {
                None | Some('|') | Some(')') => break,
                Some('$') => {
                    if !top {
                        return Err(
                            self.fail("anchor_position", "$ 只允许在顶层分支的结尾，作字面须转义")
                        );
                    }
                    self.take();
                    pieces.push(Piece {
                        atom: Atom::Anchor { start: false },
                        quant: None,
                    });
                    if !matches!(self.peek(), None | Some('|') | Some(')')) {
                        return Err(self.fail("anchor_position", "$ 只能出现在分支结尾"));
                    }
                    break;
                }
                Some('^') => {
                    return Err(self.fail("anchor_position", "^ 只能出现在分支开头，作字面须转义"));
                }
                Some(_) => {
                    let atom = self.atom()?;
                    let quant = self.quantifier()?;
                    pieces.push(Piece { atom, quant });
                }
            }
        }
        Ok(pieces)
    }

    fn quantifier(&mut self) -> Result<Option<Quant>, PatternError> {
        match self.peek() {
            Some('*') | Some('+') | Some('?') => {
                let c = self.take();
                self.reject_suffix()?;
                Ok(Some(Quant {
                    text: c.to_string(),
                    factor: 1,
                }))
            }
            Some('{') => {
                let start = self.i;
                self.take();
                let Some(m) = self.digits() else {
                    return Err(self.fail("invalid_repetition", "{ 后必须紧跟数字（作字面须转义）"));
                };
                let quant = match self.peek() {
                    Some('}') => {
                        self.take();
                        Quant {
                            text: self.text(start, self.i),
                            factor: m,
                        }
                    }
                    Some(',') => {
                        self.take();
                        let n = self.digits();
                        if self.peek() != Some('}') {
                            return Err(
                                self.fail("invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}")
                            );
                        }
                        self.take();
                        if let Some(n) = n {
                            if m > n {
                                return Err(self.fail("invalid_repetition", "量词下界不得大于上界"));
                            }
                        }
                        Quant {
                            text: self.text(start, self.i),
                            factor: n.unwrap_or(m),
                        }
                    }
                    _ => {
                        return Err(
                            self.fail("invalid_repetition", "量词形式只允许 {m}、{m,}、{m,n}")
                        )
                    }
                };
                self.reject_suffix()?;
                Ok(Some(quant))
            }
            Some('}') => Err(self.fail("invalid_repetition", "} 作字面须转义")),
            _ => Ok(None),
        }
    }

    fn reject_suffix(&mut self) -> Result<(), PatternError> {
        match self.peek() {
            Some('?') => Err(self.fail("lazy_quantifier", "不支持惰性量词")),
            Some('+') => Err(self.fail("possessive_quantifier", "不支持占有量词")),
            Some('*') | Some('{') | Some('}') => {
                Err(self.fail("stacked_quantifier", "同一原子至多一个量词"))
            }
            _ => Ok(()),
        }
    }

    /// 十进制计数；超出 u64 的位数饱和（上界判定只看是否越过展开规模，饱和不改变结论）。
    fn digits(&mut self) -> Option<u64> {
        let start = self.i;
        let mut value: u64 = 0;
        while let Some(c) = self.peek() {
            let Some(d) = c.to_digit(10) else { break };
            value = value.saturating_mul(10).saturating_add(u64::from(d));
            self.i += 1;
        }
        (self.i != start).then_some(value)
    }

    fn atom(&mut self) -> Result<Atom, PatternError> {
        let c = self.take();
        match c {
            '(' => self.group(),
            '[' => self.char_class(),
            '.' => Ok(Atom::AnyChar),
            '\\' => self.escape(),
            '{' | '}' => Err(self.fail(
                "invalid_repetition",
                format!("{c} 作字面须转义，或作为量词紧跟在原子之后"),
            )),
            c if METACHARS.contains(c) => {
                Err(self.fail("syntax", format!("元字符 {c:?} 使用位置不合法")))
            }
            // char 恒为 Unicode 标量值（Rust 的 String 不承载孤立代理项）；
            // 参考实现与 Python SDK 在此另有 not_scalar_value 判定，Rust 的类型系统已排除。
            c => Ok(Atom::Literal(c as u32)),
        }
    }

    fn group(&mut self) -> Result<Atom, PatternError> {
        if self.depth >= MAX_NESTING {
            return Err(self.fail("nesting_depth", format!("分组嵌套深度超过 {MAX_NESTING}")));
        }
        self.depth += 1;
        let body = self.group_body();
        self.depth -= 1;
        Ok(Atom::Group(body?))
    }

    fn group_body(&mut self) -> Result<Alt, PatternError> {
        if self.peek() == Some('?') {
            self.take();
            match self.peek() {
                Some(':') => {
                    self.take();
                    let body = self.alt(false)?;
                    self.expect_close()?;
                    Ok(body)
                }
                Some('=') | Some('!') => Err(self.fail("lookaround", "不支持 lookaround")),
                Some('<') => {
                    if matches!(self.s.get(self.i + 1), Some('=') | Some('!')) {
                        Err(self.fail("lookaround", "不支持 lookaround"))
                    } else {
                        Err(self.fail("named_group", "不支持命名/捕获组语法"))
                    }
                }
                Some('P') => Err(self.fail("named_group", "不支持命名/捕获组语法")),
                Some('#') => Err(self.fail("comment_group", "不支持 (?#…) 注释组")),
                _ => Err(self.fail("inline_flag", "不支持内联标志或其他组扩展")),
            }
        } else {
            let body = self.alt(false)?;
            self.expect_close()?;
            Ok(body)
        }
    }

    fn expect_close(&mut self) -> Result<(), PatternError> {
        if self.peek() != Some(')') {
            return Err(self.fail("syntax", "括号不配对"));
        }
        self.take();
        Ok(())
    }

    fn escape(&mut self) -> Result<Atom, PatternError> {
        let Some(c) = self.peek() else {
            return Err(self.fail("syntax", "悬空反斜杠"));
        };
        self.take();
        if "dDwWsS".contains(c) {
            return Ok(Atom::Shorthand(c));
        }
        if c == 'x' {
            return Ok(Atom::Literal(u32::from(self.hex_escape()?)));
        }
        if let Some(cp) = control_escape(c) {
            return Ok(Atom::Literal(cp));
        }
        if ESCAPABLE.contains(c) {
            return Ok(Atom::Literal(c as u32));
        }
        if c == 'p' || c == 'P' {
            return Err(self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}"));
        }
        if c.is_ascii_digit() {
            return Err(self.fail("backreference", "不支持反向引用"));
        }
        Err(self.fail("unknown_escape", format!("不允许的转义 \\{c}")))
    }

    fn hex_escape(&mut self) -> Result<u8, PatternError> {
        let digits: Vec<char> = self.s.iter().skip(self.i).take(2).copied().collect();
        if digits.len() != 2 || digits.iter().any(|c| !c.is_ascii_hexdigit()) {
            return Err(self.fail("unknown_escape", "\\x 必须紧跟两位十六进制"));
        }
        self.i += 2;
        let hex: String = digits.into_iter().collect();
        Ok(u8::from_str_radix(&hex, 16).expect("两位十六进制已校验"))
    }

    // class = "[" ["^"] ("-" / class-item) *class-item ["-"] "]"
    fn char_class(&mut self) -> Result<Atom, PatternError> {
        let negated = if self.peek() == Some('^') {
            self.take();
            true
        } else {
            false
        };
        let mut items: Vec<ClassItem> = Vec::new();
        loop {
            let Some(c) = self.peek() else {
                return Err(self.fail("syntax", "字符类未闭合"));
            };
            if c == ']' {
                self.take();
                if items.is_empty() {
                    return Err(self.fail("empty_class", "字符类不得为空"));
                }
                return Ok(Atom::CharClass(CharClass { items, negated }));
            }
            if c == '-' {
                // 类首、类尾的 - 是字面字符；中间位置不合法
                let next_is_close = matches!(self.s.get(self.i + 1), Some(']'));
                if !items.is_empty() && !next_is_close {
                    if matches!(items.last(), Some(ClassItem::Shorthand(_))) {
                        return Err(self.fail(
                            "class_range_endpoint",
                            "区间端点必须是单字符，简写类不可作端点",
                        ));
                    }
                    return Err(self.fail(
                        "class_dash_position",
                        "- 只能是类首/类尾的字面、转义或区间分隔符",
                    ));
                }
                self.take();
                items.push(ClassItem::Range(0x2D, 0x2D));
                continue;
            }
            let item = self.class_item()?;
            items.push(item);
        }
    }

    fn class_item(&mut self) -> Result<ClassItem, PatternError> {
        let item = self.class_char_or_shorthand()?;
        let (cp1, cp2) = match item {
            ClassItem::Shorthand(kind) => return Ok(ClassItem::Shorthand(kind)),
            ClassItem::Range(a, b) => (a, b),
        };
        if self.peek() == Some('-') && !matches!(self.s.get(self.i + 1), Some(']')) {
            self.take();
            match self.class_char_or_shorthand()? {
                ClassItem::Shorthand(_) => {
                    Err(self.fail("class_range_endpoint", "区间端点必须是单字符"))
                }
                ClassItem::Range(e1, e2) if e1 != e2 => {
                    Err(self.fail("class_range_endpoint", "区间端点必须是单字符"))
                }
                ClassItem::Range(end, _) => {
                    if cp1 > end {
                        return Err(self.fail("class_range_order", "区间的起点不得大于终点"));
                    }
                    Ok(ClassItem::Range(cp1, end))
                }
            }
        } else {
            Ok(ClassItem::Range(cp1, cp2))
        }
    }

    fn class_char_or_shorthand(&mut self) -> Result<ClassItem, PatternError> {
        let Some(c) = self.peek() else {
            return Err(self.fail("syntax", "字符类未闭合"));
        };
        if c == '[' || c == ']' {
            return Err(self.fail("class_unescaped_bracket", "类内 [ 与 ] 必须转义"));
        }
        if c == '-' {
            return Err(self.fail(
                "class_dash_position",
                "- 只能是类首/类尾的字面、转义或区间分隔符",
            ));
        }
        if c == '\\' {
            self.take();
            let Some(e) = self.peek() else {
                return Err(self.fail("syntax", "类内悬空反斜杠"));
            };
            self.take();
            if "dDwWsS".contains(e) {
                return Ok(ClassItem::Shorthand(e));
            }
            if e == 'x' {
                let cp = u32::from(self.hex_escape()?);
                return Ok(ClassItem::Range(cp, cp));
            }
            if let Some(cp) = control_escape(e) {
                return Ok(ClassItem::Range(cp, cp));
            }
            if ESCAPABLE.contains(e) {
                return Ok(ClassItem::Range(e as u32, e as u32));
            }
            if e == 'p' || e == 'P' {
                return Err(self.fail("unicode_property_escape", "不支持 \\p{…} / \\P{…}"));
            }
            return Err(self.fail("unknown_escape", format!("类内不允许的转义 \\{e}")));
        }
        self.take();
        Ok(ClassItem::Range(c as u32, c as u32))
    }
}

/// 类内 MUST NOT 出现相邻的 `&&`、`--`、`~~`（按字面文本判定：紧邻的两个同字符一律违例，
/// 无论前一个是否被转义——`[a\--b]` 也违例；写出相邻字符请用 `\xHH`）。
fn check_written_sequences(pattern: &[char]) -> Result<(), PatternError> {
    for pair in [['&', '&'], ['-', '-'], ['~', '~']] {
        for idx in 0..pattern.len().saturating_sub(1) {
            if pattern[idx] == pair[0] && pattern[idx + 1] == pair[1] && inside_class(pattern, idx)
            {
                let text: String = pair.iter().collect();
                return Err(PatternError::new(
                    "class_set_operation",
                    format!("类内不得出现字面序列 {text:?}"),
                ));
            }
        }
    }
    Ok(())
}

fn is_escaped(text: &[char], index: usize) -> bool {
    let mut backslashes = 0usize;
    let mut j = index;
    while j > 0 && text[j - 1] == '\\' {
        backslashes += 1;
        j -= 1;
    }
    backslashes % 2 == 1
}

fn inside_class(text: &[char], index: usize) -> bool {
    let mut inside = false;
    for (j, &c) in text.iter().enumerate().take(index + 1) {
        if is_escaped(text, j) {
            continue;
        }
        if c == '[' && !inside {
            inside = true;
        } else if c == ']' && inside {
            inside = false;
        }
    }
    inside
}

// ---------------------------------------------------------------------------
// 展开规模（§4.1.1：按书写形式逐项计数，不去重）
// ---------------------------------------------------------------------------

fn size_alt(alt: &Alt) -> u64 {
    alt.branches
        .iter()
        .flatten()
        .map(size_piece)
        .fold(0, u64::saturating_add)
}

fn size_piece(piece: &Piece) -> u64 {
    size_atom(&piece.atom).saturating_mul(piece.quant.as_ref().map_or(1, |q| q.factor))
}

fn size_atom(atom: &Atom) -> u64 {
    match atom {
        // 字符类的取反 `^` 不计，其余按写出的每一项计 1
        Atom::CharClass(class) => (class.items.len() as u64).max(1),
        // 分组至少计 1：空组计 0 会让「零尺寸原子 × 大计数」绕过展开规模上界
        Atom::Group(body) => size_alt(body).max(1),
        Atom::Anchor { .. } => 0,
        _ => 1,
    }
}

/// 完整校验（长度、类内序列、文法、展开规模）并返回 AST。
fn parse_checked(pattern: &str) -> Result<Alt, PatternError> {
    let chars: Vec<char> = pattern.chars().collect();
    if chars.len() > MAX_LENGTH {
        return Err(PatternError::new(
            "length",
            format!("长度超过 {MAX_LENGTH}"),
        ));
    }
    check_written_sequences(&chars)?;
    let mut parser = Parser {
        s: chars,
        i: 0,
        depth: 0,
    };
    let alt = parser.alt(true)?;
    if parser.i != parser.s.len() {
        return Err(parser.fail("syntax", "解析未到结尾"));
    }
    if size_alt(&alt) > MAX_EXPANSION {
        return Err(PatternError::new(
            "expansion",
            format!("展开规模超过 {MAX_EXPANSION}"),
        ));
    }
    Ok(alt)
}

/// 校验 pattern 属于 §4.1.1 子集；不属于时返回 [`PatternError`]。
pub fn check_pattern(pattern: &str) -> Result<(), PatternError> {
    parse_checked(pattern).map(|_| ())
}

// ---------------------------------------------------------------------------
// 转译（显式码点区间 → regex crate 语法）
// ---------------------------------------------------------------------------

fn normalize(mut ranges: Vec<(u32, u32)>) -> Vec<(u32, u32)> {
    ranges.sort_unstable();
    let mut merged: Vec<(u32, u32)> = Vec::new();
    for (lo, hi) in ranges {
        match merged.last_mut() {
            Some(last) if lo <= last.1.saturating_add(1) => last.1 = last.1.max(hi),
            _ => merged.push((lo, hi)),
        }
    }
    merged
}

/// 去掉落在代理码位（U+D800–U+DFFF）上的片段——子集按 Unicode 标量值定义。
fn only_scalars(ranges: &[(u32, u32)]) -> Vec<(u32, u32)> {
    let mut out: Vec<(u32, u32)> = Vec::new();
    for &(lo, hi) in ranges {
        if hi < 0xD800 || lo > 0xDFFF {
            out.push((lo, hi));
        } else {
            if lo < 0xD800 {
                out.push((lo, 0xD7FF));
            }
            if hi > 0xDFFF {
                out.push((0xE000, hi));
            }
        }
    }
    out
}

/// 在全体 Unicode 标量值上取补。
fn complement(ranges: &[(u32, u32)]) -> Vec<(u32, u32)> {
    let mut out: Vec<(u32, u32)> = Vec::new();
    let mut cursor: u32 = 0;
    for (lo, hi) in normalize(ranges.to_vec()) {
        if lo > cursor {
            out.push((cursor, lo - 1));
        }
        cursor = hi + 1;
    }
    if cursor <= 0x10FFFF {
        out.push((cursor, 0x10FFFF));
    }
    normalize(only_scalars(&out))
}

/// 简写类的码点区间（§4.1.1 语义表；ASCII）；大写为标量值宇宙上的补。
fn shorthand_ranges(kind: char) -> Vec<(u32, u32)> {
    let base: &[(u32, u32)] = match kind.to_ascii_lowercase() {
        'd' => &[(0x30, 0x39)],
        'w' => &[(0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)],
        's' => &[(0x09, 0x0D), (0x20, 0x20)],
        _ => unreachable!("解析器只产出 d/D/w/W/s/S"),
    };
    if kind.is_ascii_lowercase() {
        base.to_vec()
    } else {
        complement(base)
    }
}

fn emit_cp(cp: u32) -> String {
    format!("\\x{{{cp:x}}}")
}

fn emit_class(ranges: &[(u32, u32)]) -> String {
    if ranges.is_empty() {
        // 语义上不匹配任何标量值的类（如取反后为空，`[^\d\D]`）。Python 侧转译为 `(?!)`，
        // Rust regex 无 lookaround，用「取补全宇宙」的取反类——编译后同样不接受任何输入。
        return "[^\\x{0}-\\x{10ffff}]".to_string();
    }
    let mut out = String::from("[");
    for &(lo, hi) in ranges {
        out.push_str(&emit_cp(lo));
        if lo != hi {
            out.push('-');
            out.push_str(&emit_cp(hi));
        }
    }
    out.push(']');
    out
}

fn emit_atom(atom: &Atom) -> String {
    match atom {
        Atom::Literal(cp) => emit_cp(*cp),
        // `.`：除 U+000A、U+000D 外的任意单个 Unicode 标量值
        Atom::AnyChar => emit_class(&[
            (0x00, 0x09),
            (0x0B, 0x0C),
            (0x0E, 0xD7FF),
            (0xE000, 0x10FFFF),
        ]),
        Atom::Shorthand(kind) => emit_class(&shorthand_ranges(*kind)),
        Atom::CharClass(class) => {
            let mut ranges: Vec<(u32, u32)> = Vec::new();
            for item in &class.items {
                match item {
                    ClassItem::Shorthand(kind) => ranges.extend(shorthand_ranges(*kind)),
                    ClassItem::Range(lo, hi) => ranges.push((*lo, *hi)),
                }
            }
            // 匹配宇宙是 Unicode 标量值：正类区间同样剔除代理区，与取反 / `.` / 补集一致
            let mut merged = normalize(only_scalars(&ranges));
            if class.negated {
                merged = complement(&merged);
            }
            emit_class(&merged)
        }
        Atom::Group(body) => format!("(?:{})", emit_alt(body)),
        Atom::Anchor { start: true } => "\\A".to_string(),
        Atom::Anchor { start: false } => "\\z".to_string(),
    }
}

fn emit_alt(alt: &Alt) -> String {
    let branches: Vec<String> = alt
        .branches
        .iter()
        .map(|branch| {
            let mut parts = String::new();
            for piece in branch {
                parts.push_str(&emit_atom(&piece.atom));
                if let Some(quant) = &piece.quant {
                    parts.push_str(&quant.text);
                }
            }
            parts
        })
        .collect();
    branches.join("|")
}

/// 转译为显式码点区间（regex crate 语法；语义即 §4.1.1）。自身做完整校验，不依赖调用序。
///
/// 空 pattern 语义为「匹配任意串」，转译为 `(?:)`（空组，匹配空串即匹配任意串）。
fn translate(pattern: &str) -> Result<String, PatternError> {
    if pattern.is_empty() {
        return Ok("(?:)".to_string());
    }
    Ok(emit_alt(&parse_checked(pattern)?))
}

/// 已编译的子集内 pattern（§4.1.1 语义的匹配）。
#[derive(Debug)]
pub struct Pattern {
    regex: Regex,
}

impl Pattern {
    /// 校验并编译 pattern；不属于子集时返回 [`PatternError`]。
    ///
    /// 子集内的 pattern 在 `regex` 默认编译限额内必须可编译（`scripts/pattern_budget` 探针
    /// 复测了最贵形状的余量）：编译失败是内部缺陷，不是「清单不合法」，因此直接 panic 而不是
    /// 映射成拒绝——§4.1.1 不允许实现以引擎资源上限为由拒绝子集内的 pattern。调用方（运行器）
    /// 不得依赖 `Result` 或 `catch_unwind` 把此类失败收束为可恢复错误：它表示实现或引擎与规范
    /// 漂移，应当响亮地暴露（提示：带上 panic 消息并按 `scripts/pattern_budget` 复测引擎）。
    pub fn new(pattern: &str) -> Result<Pattern, PatternError> {
        let translated = translate(pattern)?;
        let regex = Regex::new(&translated).unwrap_or_else(|e| {
            panic!(
                "§4.1.1 子集内的 pattern 转译结果在 regex 默认限额内必须可编译\
                 （余量见 scripts/pattern_budget 探针）：{e}"
            )
        });
        Ok(Pattern { regex })
    }

    /// 按 §4.1.1 语义在 `value` 中检索（未锚定）：串中存在一处匹配即通过。
    pub fn is_match(&self, value: &str) -> bool {
        self.regex.is_match(value)
    }
}

/// 便捷入口：校验 pattern 后在 `value` 中检索（未锚定）。
pub fn search(pattern: &str, value: &str) -> Result<bool, PatternError> {
    Ok(Pattern::new(pattern)?.is_match(value))
}
