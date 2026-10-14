//! remote URL 的文法（HTTP 绑定 §1）：客户端接受 remote URL 的唯一判定。
//!
//! 传输层（`http::DpeClient` 构造，随 feature `http`）与 connector 运行器（实例定义的 `remote.url`，
//! connector 契约 §4.4）共用本模块。判定只看输入串本身，不依赖任何 URL 库；与生成器
//! `scripts/gen_vectors.py` 的参考实现行为一致，由 `vectors/remote_url.json` 逐例固定（同
//! Python 的 `dpe_sdk.remote_url`）。文法消除了同一地址的写法差异（大小写、编码、默认端口、
//! 尾部 `/`、点段、IP 的不同文本形式），因此两个 remote URL 是否相同按字符串相等比较即可；
//! DNS 别名这类字符串无法判定的重叠由服务端前缀授权兜底。

use std::fmt;

/// remote URL 不符合 HTTP 绑定 §1 的文法。
///
/// 消息只说明违反了哪一条，不回显 URL（可能把凭证误写在 userinfo 里，connector 契约 §4.3
/// 「不外泄」）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteUrlError {
    message: &'static str,
}

impl RemoteUrlError {
    const fn new(message: &'static str) -> Self {
        Self { message }
    }

    /// 违反的是哪一条（供人读）。
    pub fn message(&self) -> &str {
        self.message
    }
}

impl fmt::Display for RemoteUrlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message)
    }
}

impl std::error::Error for RemoteUrlError {}

fn is_unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'.' | b'_' | b'~')
}

fn is_sub_delim(c: u8) -> bool {
    matches!(
        c,
        b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'='
    )
}

/// 主机名（第 5 条）：RFC 3986 的 reg-name 去掉 pct-encoded。
fn is_host_char(c: u8) -> bool {
    is_unreserved(c) || is_sub_delim(c)
}

/// pchar 中除 pct-encoded 以外的字符（RFC 3986 §3.3）。
fn is_pchar(c: u8) -> bool {
    is_unreserved(c) || is_sub_delim(c) || matches!(c, b':' | b'@')
}

/// `dec-octet`（RFC 3986 §3.2.2）：0–255，无前导 0。
fn is_dec_octet(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() || bytes.len() > 3 || !bytes.iter().all(u8::is_ascii_digit) {
        return false;
    }
    if bytes.len() > 1 && bytes[0] == b'0' {
        return false;
    }
    text.parse::<u16>().is_ok_and(|value| value <= 255)
}

fn is_ipv4(text: &str) -> bool {
    let parts: Vec<&str> = text.split('.').collect();
    parts.len() == 4 && parts.iter().all(|part| is_dec_octet(part))
}

fn is_h16(text: &str) -> bool {
    (1..=4).contains(&text.len()) && text.bytes().all(|c| c.is_ascii_hexdigit())
}

/// RFC 3986 §3.2.2 的 IPv6address（不含内嵌 IPv4、IPvFuture 与 zone）解析为 8 个 16 位组。
fn ipv6_groups(text: &str) -> Option<[u16; 8]> {
    if text.matches("::").count() > 1 {
        return None;
    }
    let parts: Vec<&str> = match text.split_once("::") {
        Some((head, tail)) => {
            let left: Vec<&str> = if head.is_empty() {
                Vec::new()
            } else {
                head.split(':').collect()
            };
            let right: Vec<&str> = if tail.is_empty() {
                Vec::new()
            } else {
                tail.split(':').collect()
            };
            if left.len() + right.len() > 7 {
                return None;
            }
            let mut parts = left;
            parts.resize(8 - right.len(), "0");
            parts.extend(right);
            parts
        }
        None => text.split(':').collect(),
    };
    if parts.len() != 8 || !parts.iter().all(|part| is_h16(part)) {
        return None;
    }
    let mut groups = [0u16; 8];
    for (group, part) in groups.iter_mut().zip(&parts) {
        *group = u16::from_str_radix(part, 16).ok()?;
    }
    Some(groups)
}

/// RFC 5952 §4 的推荐文本：hex 小写无前导 0，只压缩最长（≥2，并列取首段）的零组串。
fn ipv6_text(groups: &[u16; 8]) -> String {
    let (mut best_start, mut best_len) = (None, 1);
    let mut i = 0;
    while i < 8 {
        if groups[i] == 0 {
            let mut j = i;
            while j < 8 && groups[j] == 0 {
                j += 1;
            }
            if j - i > best_len {
                best_start = Some(i);
                best_len = j - i;
            }
            i = j;
        } else {
            i += 1;
        }
    }
    let parts: Vec<String> = groups.iter().map(|group| format!("{group:x}")).collect();
    match best_start {
        None => parts.join(":"),
        Some(start) => format!(
            "{}::{}",
            parts[..start].join(":"),
            parts[start + best_len..].join(":")
        ),
    }
}

/// IPv6 字面量须恰为其 RFC 5952 §4 推荐文本（第 5 条）。
fn is_ipv6(text: &str) -> bool {
    ipv6_groups(text).is_some_and(|groups| ipv6_text(&groups) == text)
}

/// 数字形式的末标签（WHATWG URL 的 "ends in a number"）：全为十进制数字，或 `0x` 后接 hex。
fn is_numeric_label(label: &str) -> bool {
    if let Some(hex) = label.strip_prefix("0x") {
        return hex
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c));
    }
    !label.is_empty() && label.bytes().all(|c| c.is_ascii_digit())
}

/// 主机名（第 5 条）：非空标签以 `.` 连接；末标签是数字形式时须为严格 IPv4。
fn is_hostname(host: &str) -> bool {
    if !host.bytes().all(is_host_char) {
        return false;
    }
    let labels: Vec<&str> = host.split('.').collect();
    if labels.iter().any(|label| label.is_empty()) {
        return false;
    }
    match labels.last() {
        Some(last) if is_numeric_label(last) => is_ipv4(host),
        _ => true,
    }
}

/// 端口（第 6 条）：1–65535 的十进制数，无前导 0。
fn is_port(text: &str) -> bool {
    let bytes = text.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 5
        && bytes[0] != b'0'
        && bytes.iter().all(u8::is_ascii_digit)
        && text.parse::<u32>().is_ok_and(|value| value <= 65535)
}

/// 按 HTTP 绑定 §1 判定 remote URL；合法时返回 `Ok(())`。
pub fn check_remote_url(url: &str) -> Result<(), RemoteUrlError> {
    match dpe_hash::normalize_file_uri(url) {
        Err(_) => {
            return Err(RemoteUrlError::new(
                "remote URL 不是合法的 URI（core §1.1）",
            ))
        }
        Ok(normalized) if normalized != url => {
            return Err(RemoteUrlError::new(
                "remote URL 不是规范化形式（scheme 与 host 须小写、百分号编码的 hex 须大写、\
                 unreserved 字符不得编码，core §1.1）",
            ))
        }
        Ok(_) => {}
    }
    if url.contains(['?', '#']) {
        return Err(RemoteUrlError::new("remote URL 不得含查询串或片段"));
    }
    // 第 1 条已保证有合法 scheme，首个 ':' 即其分界
    let (scheme, rest) = url.split_once(':').unwrap_or((url, ""));
    let default_port = match scheme {
        "http" => "80",
        "https" => "443",
        _ => {
            return Err(RemoteUrlError::new(
                "remote URL 的 scheme 必须是 http 或 https",
            ))
        }
    };
    let Some(after) = rest.strip_prefix("//") else {
        return Err(RemoteUrlError::new(
            "remote URL 缺少 authority（须形如 http(s)://host…）",
        ));
    };
    let (authority, path) = match after.find('/') {
        Some(slash) => after.split_at(slash),
        None => (after, ""),
    };
    if authority.contains('@') {
        return Err(RemoteUrlError::new(
            "remote URL 不得含 userinfo（凭证经单独的凭证引用提供）",
        ));
    }
    let port = if let Some(bracketed) = authority.strip_prefix('[') {
        let Some(close) = bracketed.find(']') else {
            return Err(RemoteUrlError::new(
                "remote URL 的 IP 字面量须为 IPv6 地址的 RFC 5952 推荐文本\
                 （不接受 IPvFuture、zone 与内嵌 IPv4 的点分写法）",
            ));
        };
        if !is_ipv6(&bracketed[..close]) {
            return Err(RemoteUrlError::new(
                "remote URL 的 IP 字面量须为 IPv6 地址的 RFC 5952 推荐文本\
                 （不接受 IPvFuture、zone 与内嵌 IPv4 的点分写法）",
            ));
        }
        let after_host = &bracketed[close + 1..];
        if after_host.is_empty() {
            None
        } else if let Some(port) = after_host.strip_prefix(':') {
            Some(port)
        } else {
            return Err(RemoteUrlError::new("remote URL 的 host 不合法"));
        }
    } else {
        let (host, port) = match authority.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (authority, None),
        };
        if !is_hostname(host) {
            return Err(RemoteUrlError::new(
                "remote URL 的主机名不合法（须为非空标签、不得百分号编码；\
                 末标签为数字时须为严格的四段 IPv4）",
            ));
        }
        port
    };
    if let Some(port) = port {
        if !is_port(port) {
            return Err(RemoteUrlError::new(
                "remote URL 的端口须为 1–65535 的十进制数，且无前导 0",
            ));
        }
        if port == default_port {
            return Err(RemoteUrlError::new(
                "remote URL 不得显式写出 scheme 的默认端口（http 80、https 443）",
            ));
        }
    }
    if !path.is_empty() {
        if path.ends_with('/') {
            return Err(RemoteUrlError::new("remote URL 的路径不得以 / 结尾"));
        }
        for segment in path[1..].split('/') {
            // '%' 后的两位 HEXDIG 已由 core §1.1 的合法性判定保证
            if !segment.bytes().all(|c| is_pchar(c) || c == b'%') {
                return Err(RemoteUrlError::new("remote URL 的路径含不允许的字符"));
            }
            if segment == "." || segment == ".." {
                return Err(RemoteUrlError::new("remote URL 的路径不得含 . 或 .. 段"));
            }
        }
    }
    Ok(())
}
