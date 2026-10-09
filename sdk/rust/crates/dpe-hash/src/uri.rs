//! file_uri 的语法规范化（core.md §1.1）。
//!
//! 与生成器 `scripts/gen_vectors.py` 的参考实现、Python dpe-hash 行为一致，由
//! `vectors/file_uri_normalization.json` 逐例固定（合法输入的输出、非法输入的错误码、幂等）。
//! 纯 ASCII 操作（非 ASCII 字符一律拒绝，见规范）。

use crate::error::{Error, ErrorKind, Result};

/// unreserved（RFC 3986 §2.3）。
fn is_unreserved(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~')
}

/// reserved（RFC 3986 §2.2）。
fn is_reserved(b: u8) -> bool {
    matches!(
        b,
        b':' | b'/'
            | b'?'
            | b'#'
            | b'['
            | b']'
            | b'@'
            | b'!'
            | b'$'
            | b'&'
            | b'\''
            | b'('
            | b')'
            | b'*'
            | b'+'
            | b','
            | b';'
            | b'='
    )
}

fn invalid(message: String) -> Error {
    Error::new(ErrorKind::Validation, message, "")
}

/// scheme（RFC 3986 §3.1）：`ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )` 后接 `:`；
/// 返回 `:` 之后的下标。
fn scheme_end(uri: &[u8]) -> Option<usize> {
    if !uri.first()?.is_ascii_alphabetic() {
        return None;
    }
    let len = uri[1..]
        .iter()
        .take_while(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
        .count();
    (uri.get(1 + len) == Some(&b':')).then_some(len + 2)
}

/// 返回 `uri` 的语法规范化形式（core.md §1.1）；它就是身份的比较形式。
///
/// 合法性判定（封闭清单）：scheme 文法、全串字符集（`pct-encoded` / `unreserved` /
/// `reserved`）、`%` 后两位 HEXDIG；不校验更深的成分文法（host 是否合法 IP 等）。
/// 不合法（含非 ASCII、空格、控制字符、坏百分号三元组）返回 [`ErrorKind::Validation`]
/// （`DPE_VALIDATION`）。
///
/// 变换三步：解码表示 unreserved 的百分号三元组 → scheme 与 host 中三元组之外的 ASCII 字母
/// 小写 → 其余三元组的 hex 位大写（reserved 字符不解码）。不做点段移除与基于 scheme / 协议
/// 的规范化。结果幂等：`normalize(normalize(x)) == normalize(x)`。
pub fn normalize_file_uri(uri: &str) -> Result<String> {
    let bytes = uri.as_bytes();
    let scheme_end =
        scheme_end(bytes).ok_or_else(|| invalid(format!("file_uri 缺少合法 scheme：{uri:?}")))?;

    // host 成分的范围：':' 后紧跟 "//" 才有 authority；authority 到第一个 "/"、"?"、"#" 为止，
    // host 是最后一个 "@"（userinfo 之后）到 port 之前的成分。三元组解码不改变这些边界
    // （unreserved 不含定界符），所以下标可以直接取自原串。按字节处理：非 ASCII 字节不是
    // 定界符，且随后在字符集检查中被拒绝。
    let (mut host_start, mut host_end) = (scheme_end, scheme_end);
    if bytes[scheme_end..].starts_with(b"//") {
        let start = scheme_end + 2;
        let authority_end = bytes[start..]
            .iter()
            .position(|b| matches!(b, b'/' | b'?' | b'#'))
            .map_or(bytes.len(), |i| start + i);
        let authority = &bytes[start..authority_end];
        host_start = authority
            .iter()
            .rposition(|&b| b == b'@')
            .map_or(start, |i| start + i + 1);
        let host = &bytes[host_start..authority_end];
        host_end = if host.first() == Some(&b'[') {
            host.iter()
                .position(|&b| b == b']')
                .map_or(authority_end, |i| host_start + i + 1)
        } else {
            host.iter()
                .position(|&b| b == b':')
                .map_or(authority_end, |i| host_start + i)
        };
    }
    let in_host = |i: usize| host_start <= i && i < host_end;

    let mut out = String::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'%' {
            let hex = bytes
                .get(i + 1..i + 3)
                .filter(|h| h.iter().all(u8::is_ascii_hexdigit));
            let Some(hex) = hex else {
                return Err(invalid(format!("file_uri 的百分号编码不合法：{uri:?}")));
            };
            let hex = std::str::from_utf8(hex).expect("HEXDIG 是 ASCII");
            let decoded = u8::from_str_radix(hex, 16).expect("两位 HEXDIG");
            if is_unreserved(decoded) {
                let decoded = if in_host(i) {
                    decoded.to_ascii_lowercase()
                } else {
                    decoded
                };
                out.push(decoded as char);
            } else {
                out.push('%');
                out.push_str(&hex.to_ascii_uppercase());
            }
            i += 3;
            continue;
        }
        if !(is_unreserved(b) || is_reserved(b)) {
            let ch = uri[i..].chars().next().expect("下标位于字符边界");
            return Err(invalid(format!(
                "file_uri 含不在 URI 字符集的字符 {ch:?}：{uri:?}"
            )));
        }
        let b = if i < scheme_end || in_host(i) {
            b.to_ascii_lowercase()
        } else {
            b
        };
        out.push(b as char);
        i += 1;
    }
    Ok(out)
}
