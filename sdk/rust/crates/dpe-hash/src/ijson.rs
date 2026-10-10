//! I-JSON 严格解析（RFC 7493；core §2.8 校验顺序第 0 步的解析部分）。
//!
//! 单遍自建解析器：拒绝重复键与孤立代理项，数值不在解析阶段拒绝（越界留到校验第 4 步在出错
//! 的值上报告；字面量经 serde_json 构造为 `Number`，拼写可能被规范化，如 `1e2` → `1e+2`、
//! `-0` → `0`，值不变、hash 不受影响）。**不复用 serde_json 的 `Value` 解析**：
//! `arbitrary_precision` 下数字以内部保留键 `$serde_json::private::Number` 的单键对象形态
//! 流传，直接解析会把源数据里恰好同形的对象静默读成数字、或把多键的同形对象误拒——源即内容
//! （北极星 P2），实现不得引入任何保留键。数值字面量在这里自行校验语法后，逐字面量交给
//! serde_json 构造 `Number`（该路径不含用户对象，不存在上述歧义）。
//!
//! 与 Python dpe-sdk 的 `_ijson.loads` 行为对等：违规同为 `DPE_VALIDATION`、位置为对象自身。
//! 已知偏差：嵌套深度上限 [`MAX_DEPTH`] 层（128 层通过、129 层拒绝；serde_json 默认在 128
//! 层即拒，本实现宽一层）；Python 标准库的对应边界约千层。独立的解析实现仍在
//! `tests/common/mod.rs`（只用于读向量夹具，不依赖本模块）。

use serde_json::{Map, Number, Value};

use crate::error::{Error, ErrorKind, Result};

/// 容器嵌套深度上限（顶层值为 1 层）。
///
/// 比 Python 标准库（约千层递归）保守；serde_json 默认在 128 层即拒，本实现宽一层
/// （128 层通过、129 层拒绝）。防解析递归耗尽栈。
const MAX_DEPTH: u32 = 128;

/// 把 JSON 文本严格解析为值：拒绝重复键与孤立代理项（core §2.8 第 0 步）。
///
/// 不复用 serde_json 的 `Value` 解析：`arbitrary_precision` 下其内部数字保留键
/// （`$serde_json::private::Number`）会把源数据里恰好同形的对象读成数字或误拒
/// （P2：源即内容，实现不得引入保留键）。
///
/// 不是合法 JSON（含孤立代理项、`NaN` 等非 JSON 数值、嵌套超深）或违反 I-JSON 时返回
/// [`ErrorKind::Validation`]（`DPE_VALIDATION`，位置为对象自身 `""`）。
pub fn parse_ijson(text: &str) -> Result<Value> {
    let mut parser = Parser::new(text);
    let value = parser.value(1)?;
    parser.skip_whitespace();
    if !parser.at_end() {
        return Err(parser.error("报文末尾有多余字符"));
    }
    Ok(value)
}

struct Parser<'a> {
    text: &'a str,
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(text: &'a str) -> Self {
        Parser { text, pos: 0 }
    }

    fn bytes(&self) -> &'a [u8] {
        self.text.as_bytes()
    }

    fn peek(&self) -> Option<u8> {
        self.bytes().get(self.pos).copied()
    }

    fn at_end(&self) -> bool {
        self.pos >= self.bytes().len()
    }

    /// 当前位置的行列（1 起，列按字节计），供诊断。
    fn position(&self) -> (usize, usize) {
        let consumed = &self.bytes()[..self.pos.min(self.bytes().len())];
        let line = consumed.iter().filter(|&&b| b == b'\n').count() + 1;
        let column = consumed.iter().rev().take_while(|&&b| b != b'\n').count() + 1;
        (line, column)
    }

    /// 语法错误：`DPE_VALIDATION`、位置为对象自身，消息带行号列号（供诊断）。
    fn error(&self, message: &str) -> Error {
        let (line, column) = self.position();
        Error::new(
            ErrorKind::Validation,
            format!("报文不是合法的 JSON：{message}（第 {line} 行第 {column} 列）"),
            "",
        )
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, byte: u8, what: &str) -> Result<()> {
        if self.peek() == Some(byte) {
            self.pos += 1;
            return Ok(());
        }
        Err(self.error(what))
    }

    /// 一个 JSON 值；`depth` 为当前值所处的嵌套层数（顶层为 1）。
    fn value(&mut self, depth: u32) -> Result<Value> {
        self.skip_whitespace();
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => self.string().map(Value::String),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => Err(self.error("不是合法的 JSON 值")),
            None => Err(self.error("报文意外结束")),
        }
    }

    fn literal(&mut self, literal: &str, value: Value) -> Result<Value> {
        match self.text[self.pos..].strip_prefix(literal) {
            Some(_) => {
                self.pos += literal.len();
                Ok(value)
            }
            None => Err(self.error("不是合法的 JSON 值")),
        }
    }

    fn object(&mut self, depth: u32) -> Result<Value> {
        if depth > MAX_DEPTH {
            return Err(self.error(&format!("嵌套深度超过上限 {MAX_DEPTH} 层")));
        }
        self.expect(b'{', "应为 `{`")?;
        let mut map = Map::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.skip_whitespace();
            if self.peek() != Some(b'"') {
                return Err(self.error("对象的键必须是字符串"));
            }
            let key = self.string()?;
            self.skip_whitespace();
            self.expect(b':', "键后应为 `:`")?;
            let value = self.value(depth + 1)?;
            if map.insert(key.clone(), value).is_some() {
                let (line, column) = self.position();
                return Err(Error::new(
                    ErrorKind::Validation,
                    format!("报文不是 I-JSON：同一对象内有重复的键（第 {line} 行第 {column} 列）"),
                    "",
                ));
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Value::Object(map));
                }
                _ => return Err(self.error("对象里应为 `,` 或 `}`")),
            }
        }
    }

    fn array(&mut self, depth: u32) -> Result<Value> {
        if depth > MAX_DEPTH {
            return Err(self.error(&format!("嵌套深度超过上限 {MAX_DEPTH} 层")));
        }
        self.expect(b'[', "应为 `[`")?;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value(depth + 1)?);
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Value::Array(items));
                }
                _ => return Err(self.error("数组里应为 `,` 或 `]`")),
            }
        }
    }

    /// 字符串值（当前位置在开引号上）。字符串内的字节扫描与切片都在 ASCII 边界（引号、反斜杠
    /// 与控制字符都 < 0x80，UTF-8 续字节 ≥ 0x80 不会误判），无转义时直接借用原文切片。
    fn string(&mut self) -> Result<String> {
        self.pos += 1; // 开引号
        let start = self.pos;
        loop {
            match self.peek() {
                None => return Err(self.error("字符串未闭合")),
                Some(b'"') => {
                    let text = &self.text[start..self.pos];
                    self.pos += 1;
                    return Ok(text.to_owned());
                }
                Some(b'\\') => break,
                Some(b) if b < 0x20 => {
                    return Err(self.error("字符串里不能包含未转义的控制字符"));
                }
                Some(_) => self.pos += 1,
            }
        }
        // 慢路径：从第一个转义开始逐字符解码（保留已扫描的前缀）
        let mut out = String::from(&self.text[start..self.pos]);
        loop {
            match self.peek() {
                None => return Err(self.error("字符串未闭合")),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    self.escape(&mut out)?;
                }
                Some(b) if b < 0x20 => {
                    return Err(self.error("字符串里不能包含未转义的控制字符"));
                }
                Some(_) => {
                    let ch = self.text[self.pos..]
                        .chars()
                        .next()
                        .expect("pos 在字符边界（见 string 的扫描不变量）");
                    out.push(ch);
                    self.pos += ch.len_utf8();
                }
            }
        }
    }

    /// 一个转义序列（当前位置在反斜杠之后）。
    fn escape(&mut self, out: &mut String) -> Result<()> {
        let Some(byte) = self.peek() else {
            return Err(self.error("转义序列未完成"));
        };
        self.pos += 1;
        match byte {
            b'"' => out.push('"'),
            b'\\' => out.push('\\'),
            b'/' => out.push('/'),
            b'b' => out.push('\u{0008}'),
            b'f' => out.push('\u{000C}'),
            b'n' => out.push('\n'),
            b'r' => out.push('\r'),
            b't' => out.push('\t'),
            b'u' => out.push(self.unicode_escape()?),
            _ => return Err(self.error("非法的转义字符")),
        }
        Ok(())
    }

    /// `\uXXXX` 转义（当前位置在 `u` 之后）：解码 UTF-16 码元，代理对成对处理。
    fn unicode_escape(&mut self) -> Result<char> {
        let hi = self.hex4()?;
        match hi {
            0xD800..=0xDBFF => {
                if self.peek() != Some(b'\\') || self.bytes().get(self.pos + 1) != Some(&b'u') {
                    return Err(self.error("孤立的高代理项转义"));
                }
                self.pos += 2;
                let lo = self.hex4()?;
                if !(0xDC00..=0xDFFF).contains(&lo) {
                    return Err(self.error("高代理项后必须跟低代理项转义"));
                }
                let code = 0x1_0000u32 + (u32::from(hi - 0xD800) << 10) + u32::from(lo - 0xDC00);
                char::from_u32(code).ok_or_else(|| self.error("非法的 Unicode 码点"))
            }
            0xDC00..=0xDFFF => Err(self.error("孤立的低代理项转义")),
            _ => char::from_u32(u32::from(hi)).ok_or_else(|| self.error("非法的 Unicode 码点")),
        }
    }

    fn hex4(&mut self) -> Result<u16> {
        let mut value: u16 = 0;
        for _ in 0..4 {
            let Some(byte) = self.peek() else {
                return Err(self.error("`\\u` 转义需要 4 位十六进制数字"));
            };
            let digit = match byte {
                b'0'..=b'9' => byte - b'0',
                b'a'..=b'f' => byte - b'a' + 10,
                b'A'..=b'F' => byte - b'A' + 10,
                _ => return Err(self.error("`\\u` 转义需要 4 位十六进制数字")),
            };
            value = value << 4 | u16::from(digit);
            self.pos += 1;
        }
        Ok(value)
    }

    /// 数字字面量：按 RFC 8259 语法逐字符校验后，整段交给 serde_json 构造 `Number`
    /// （`arbitrary_precision` 下按字面量构造，拼写可能被规范化但值不变；纯数字文本无对象，
    /// 不经过保留键判定）。
    fn number(&mut self) -> Result<Value> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(self.error("数字缺少整数部分")),
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.error("小数点后必须有数字"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.error("指数必须有数字"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let literal = &self.text[start..self.pos];
        // 依赖 arbitrary_precision（Cargo.toml，硬开）：超越 double 的字面量在 Number 中保留、
        // 不在此拒绝（core §2.8 第 4 步；拼写可能被规范化，值不变）；语法已校验，
        // 此处的失败分支只是兜底
        let number: Number =
            serde_json::from_str(literal).map_err(|_| self.error("数字字面量不被支持"))?;
        Ok(Value::Number(number))
    }
}
