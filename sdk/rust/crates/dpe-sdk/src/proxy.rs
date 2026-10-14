//! 代理只来自环境变量（#132）：按固定规则判定一个 remote 走哪个代理，不读操作系统的代理设置。
//!
//! reqwest 缺省按环境变量取代理，开了 `system-proxy` feature 还会读 macOS / Windows 的系统代理
//! 设置——feature 在依赖图里是合并的，别的 crate 打开它就会悄悄改变本客户端的行为。所以两个
//! SDK 都不依赖 HTTP 库的缺省，而是按同一套规则自行解析环境变量（Python 侧为
//! `dpe_sdk._proxy`，用同一张用例表 `sdk/testdata/proxy_env.json` 测试）。规则全部按字符写死、
//! 不借助 URL 库的解析——两个 SDK 所用的库在边界上口径不同（端口范围、IDNA、空白与大小写的
//! 字符集），借用就会产生分歧：
//!
//! 1. 只读环境变量，从不读 OS 设置；
//! 2. 按 remote 的 scheme 取值：https 用 `HTTPS_PROXY`，http 用 `HTTP_PROXY`，都没有时用
//!    `ALL_PROXY`；每个名字先大写、后小写；值去掉首尾的 ASCII 空白（空格、`\t`、`\n`、`\r`、
//!    `\v`、`\f`）后为空即视为未设置；
//! 3. 环境中存在 `REQUEST_METHOD`（CGI，httpoxy）时忽略全部代理变量；
//! 4. `NO_PROXY`（同样先大写、后小写）：逗号分隔、条目去掉 ASCII 空白；`*` 匹配一切；IP 与
//!    CIDR（`addr/前缀长度`）按地址匹配，IPv6 可带方括号；其余条目是域名，按 ASCII 不区分大小写
//!    匹配自身及其子域，前导 `.` 可有可无（`example.com` 不匹配 `notexample.com`）；条目不带
//!    端口；命中即直连（此时不再校验代理 URL）；
//! 5. 代理 URL 不带 `://` 时按 `http://` 补上，随后必须符合：整体只含 RFC 3986 允许的 ASCII
//!    字符且不超过 4096 个（`%` 只能后跟两位十六进制；`\`、空白、控制字符、非 ASCII 一律不合法——WHATWG 的
//!    URL 解析把 `\` 当作 `/`、删掉 TAB 与换行，httpx 不这样做）；scheme 为 `http` / `https`
//!    （ASCII 不区分大小写；其他合法 scheme 如 `socks5` 报不受支持）；authority 截到第一个 `/`
//!    `?` `#`，其后只允许空或单个 `/`；userinfo（作为代理的 Basic 认证）只含 RFC 3986 userinfo
//!    字符（不含 `@`，密码里的 `@` 须写成 `%40`），因此 authority 里至多一个 `@`；主机为
//!    方括号内的 IPv6 地址（不带 zone）、点分十进制 IPv4，或由 ASCII 字母、数字、`-` 组成的
//!    非空标签以 `.` 连接的域名（不接受 `xn--` 开头的标签；末标签全为数字或以 `0x` 开头时必须
//!    是合法的点分十进制 IPv4）；端口可省略，写了就是 1–5 位十进制数字且不超过 65535。不符合即
//!    在构造客户端时报错，错误消息只含变量名（与通过文法的 scheme），不回显值。
//!
//! 一个客户端只绑定一个 remote 且不跟随重定向，所以在构造客户端时判定一次即可。环境变量的值
//! 不是 UTF-8 时，Rust 替换为 U+FFFD、Python 按 `surrogateescape` 解码：两者都是非 ASCII 字符，
//! 在代理 URL 里一律不合法、在 `NO_PROXY` 条目里不会匹配任何（ASCII 的）remote 主机，结果一致。

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// 判定出的代理：来源变量名（报错时用，不回显可能带凭证的值）与代理 URL。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProxyChoice {
    pub(crate) variable: String,
    pub(crate) url: String,
}

/// 两个 SDK 共用的空白字符集（Rust 的 `trim()` 按 Unicode，Python 的 `strip()` 另含
/// `\x1c`–`\x1f` 等，二者都不用）。
const WHITESPACE: &[char] = &[' ', '\t', '\n', '\r', '\x0b', '\x0c'];

/// 按模块说明的规则判定 `remote` 应经的代理；直连返回 `Ok(None)`。`lookup` 按名字取环境变量
/// （存在即 `Some`，空值也是 `Some("")`）。代理 URL 不合法或 scheme 不受支持时返回错误消息
/// （不回显可能带凭证的值）。
pub(crate) fn proxy_for(
    remote: &str,
    lookup: impl Fn(&str) -> Option<String>,
) -> Result<Option<ProxyChoice>, String> {
    if lookup("REQUEST_METHOD").is_some() {
        return Ok(None);
    }
    let (scheme, host) = remote_scheme_and_host(remote);
    let found =
        first_set(&lookup, &format!("{scheme}_proxy")).or_else(|| first_set(&lookup, "all_proxy"));
    let Some((variable, value)) = found else {
        return Ok(None);
    };
    if let Some((_, no_proxy)) = first_set(&lookup, "no_proxy") {
        if bypass(&host, &no_proxy) {
            return Ok(None);
        }
    }
    let url = if value.contains("://") {
        value
    } else {
        format!("http://{value}")
    };
    check_proxy_url(&variable, &url)?;
    Ok(Some(ProxyChoice { variable, url }))
}

/// 先大写、后小写，取第一个非空值：返回（变量名, 去掉首尾 ASCII 空白的值）。
fn first_set(lookup: &impl Fn(&str) -> Option<String>, name: &str) -> Option<(String, String)> {
    [name.to_ascii_uppercase(), name.to_ascii_lowercase()]
        .into_iter()
        .find_map(|key| {
            let value = lookup(&key)?.trim_matches(WHITESPACE).to_owned();
            (!value.is_empty()).then_some((key, value))
        })
}

/// 已按 HTTP 绑定 §1 校验过的 remote 的 scheme 与主机（IPv6 去掉方括号）：纯字符切分，不经
/// URL 库（`Url::parse` 会按 UTS46 拒绝 `check_remote_url` 接受的部分 `xn--` 主机）。
fn remote_scheme_and_host(remote: &str) -> (String, String) {
    let (scheme, rest) = remote.split_once("://").unwrap_or(("", remote));
    let authority = rest.split('/').next().unwrap_or_default();
    let host = match authority.strip_prefix('[') {
        Some(inner) => inner.split(']').next().unwrap_or_default(),
        None => authority.split(':').next().unwrap_or_default(),
    };
    (scheme.to_ascii_lowercase(), host.to_ascii_lowercase())
}

fn is_scheme(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '.' | '-'))
}

/// 代理主机：方括号内的 IPv6、点分十进制 IPv4，或 LDH 标签组成的域名（见模块说明）。
fn valid_host(host: &str) -> bool {
    if let Some(rest) = host.strip_prefix('[') {
        let Some(inner) = rest.strip_suffix(']') else {
            return false;
        };
        return !inner.is_empty() && !inner.contains('%') && inner.parse::<Ipv6Addr>().is_ok();
    }
    let labels: Vec<&str> = host.split('.').collect();
    let ldh = |label: &&str| {
        !label.is_empty()
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    };
    if !labels.iter().all(ldh) {
        return false; // 含空标签（空主机、尾点、连续点）与 LDH 之外的字符
    }
    if labels
        .iter()
        .any(|label| label.to_ascii_lowercase().starts_with("xn--"))
    {
        return false;
    }
    let last = labels.last().copied().unwrap_or_default();
    let numeric = last.bytes().all(|b| b.is_ascii_digit())
        || last.starts_with("0x")
        || last.starts_with("0X");
    !numeric || host.parse::<Ipv4Addr>().is_ok()
}

/// 按模块说明第 5 条校验补全 scheme 之后的代理 URL。
/// 代理 URL 的长度上限（字节，此时都是 ASCII）：reqwest 连接时经 `http::Uri` 重新解析，超过其
/// 65534 字节上限会静默直连——两侧都在构造时拒绝过长的值。
const MAX_PROXY_URL: usize = 4096;

/// RFC 3986 的字符序列：每个字符满足 `allowed`，或是 `%` 后跟两位十六进制（pct-encoded）。
fn rfc3986(text: &str, allowed: impl Fn(u8) -> bool) -> bool {
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = |j: usize| bytes.get(j).is_some_and(u8::is_ascii_hexdigit);
            if !(hex(i + 1) && hex(i + 2)) {
                return false;
            }
            i += 3;
        } else if allowed(bytes[i]) {
            i += 1;
        } else {
            return false;
        }
    }
    true
}

fn unreserved_or_sub_delim(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"-._~!$&'()*+,;=".contains(&b)
}

/// 按模块说明第 5 条校验补全 scheme 之后的代理 URL，返回（主机, 端口）（IPv6 带方括号，端口
/// 没写为 `None`）。
fn check_proxy_url(variable: &str, url: &str) -> Result<(String, Option<u16>), String> {
    let invalid = || format!("环境变量 {variable} 的代理 URL 不合法");
    let uri_char = |b: u8| unreserved_or_sub_delim(b) || b":/?#[]@".contains(&b);
    if url.len() > MAX_PROXY_URL || !rfc3986(url, uri_char) {
        return Err(invalid());
    }
    let (scheme, rest) = url.split_once("://").unwrap_or(("", url));
    if !is_scheme(scheme) {
        return Err(invalid()); // 不回显：不合法的「scheme」里可能夹着凭证
    }
    let lowered = scheme.to_ascii_lowercase();
    if lowered != "http" && lowered != "https" {
        return Err(format!(
            "环境变量 {variable} 的代理 scheme {scheme:?} 不受支持（只支持 http / https）"
        ));
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(end);
    if !tail.is_empty() && tail != "/" {
        return Err(invalid());
    }
    let (userinfo, hostport) = match authority.rsplit_once('@') {
        Some((userinfo, hostport)) => (Some(userinfo), hostport),
        None => (None, authority),
    };
    if userinfo.is_some_and(|u| !rfc3986(u, |b| unreserved_or_sub_delim(b) || b == b':')) {
        return Err(invalid());
    }
    let (host, port_part) = if hostport.starts_with('[') {
        match hostport.find(']') {
            Some(close) => hostport.split_at(close + 1),
            None => ("", ""),
        }
    } else {
        match hostport.find(':') {
            Some(colon) => hostport.split_at(colon),
            None => (hostport, ""),
        }
    };
    if !valid_host(host) {
        return Err(invalid());
    }
    if port_part.is_empty() {
        return Ok((host.to_owned(), None));
    }
    let port = port_part.strip_prefix(':').unwrap_or("x");
    let digits = (1..=5).contains(&port.len()) && port.bytes().all(|b| b.is_ascii_digit());
    match port.parse::<u16>() {
        Ok(number) if digits => Ok((host.to_owned(), Some(number))),
        _ => Err(invalid()),
    }
}

fn address(text: &str) -> Option<IpAddr> {
    let text = text
        .strip_prefix('[')
        .and_then(|t| t.strip_suffix(']'))
        .unwrap_or(text);
    text.parse().ok() // 带 zone 的 IPv6（`%`）不能解析为地址，按域名处理（两个 SDK 同一口径）
}

/// IP 或 `addr/前缀长度`（前缀只接受十进制数字，不接受掩码写法）：返回（地址, 前缀长度）。
fn network(entry: &str) -> Option<(IpAddr, u32)> {
    match entry.split_once('/') {
        None => {
            let addr = address(entry)?;
            Some((addr, max_prefix(addr)))
        }
        Some((text, prefix)) => {
            let addr = address(text)?;
            if prefix.is_empty() || !prefix.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let prefix: u32 = prefix.parse().ok()?;
            (prefix <= max_prefix(addr)).then_some((addr, prefix))
        }
    }
}

fn max_prefix(addr: IpAddr) -> u32 {
    match addr {
        IpAddr::V4(_) => 32,
        IpAddr::V6(_) => 128,
    }
}

/// 地址是否落在网络内（主机位不为零的条目也按网络匹配；地址族不同即不匹配）。
fn contains((net, prefix): (IpAddr, u32), host: IpAddr) -> bool {
    match (net, host) {
        (IpAddr::V4(net), IpAddr::V4(host)) => {
            let mask = u32::MAX.checked_shl(32 - prefix).unwrap_or(0);
            u32::from(net) & mask == u32::from(host) & mask
        }
        (IpAddr::V6(net), IpAddr::V6(host)) => {
            let mask = u128::MAX.checked_shl(128 - prefix).unwrap_or(0);
            u128::from(net) & mask == u128::from(host) & mask
        }
        _ => false,
    }
}

/// remote 的主机是否命中 `NO_PROXY`。
fn bypass(host: &str, no_proxy: &str) -> bool {
    let host_address = address(host);
    for raw in no_proxy.split(',') {
        let entry = raw.trim_matches(WHITESPACE).to_ascii_lowercase();
        if entry.is_empty() {
            continue;
        }
        if entry == "*" {
            return true;
        }
        if let Some(net) = network(&entry) {
            if host_address.is_some_and(|addr| contains(net, addr)) {
                return true;
            }
            continue;
        }
        let domain = entry.strip_prefix('.').unwrap_or(&entry);
        if !domain.is_empty()
            && host_address.is_none()
            && (host == domain || host.ends_with(&format!(".{domain}")))
        {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// 两个 SDK 共用的用例表（Python 侧 `tests/test_proxy.py` 同表）。
    #[test]
    fn the_shared_proxy_table() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../testdata/proxy_env.json"
        );
        let text = std::fs::read_to_string(path).expect("读取 sdk/testdata/proxy_env.json");
        let table: serde_json::Value = serde_json::from_str(&text).expect("用例表是 JSON");
        let cases = table["cases"].as_array().expect("cases 是数组");
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().expect("name");
            let remote = case["remote"].as_str().expect("remote");
            let env: HashMap<String, String> =
                serde_json::from_value(case["env"].clone()).expect("env 是字符串表");
            let got = proxy_for(remote, |key| env.get(key).cloned());
            match &case["expected"] {
                serde_json::Value::Null => assert_eq!(got, Ok(None), "{name}"),
                serde_json::Value::String(s) if s == "error" => {
                    let message = got.expect_err(name);
                    for value in env.values().filter(|v| v.contains("://")) {
                        assert!(!message.contains(value.as_str()), "{name}：消息回显了值");
                    }
                }
                serde_json::Value::String(url) => {
                    assert_eq!(
                        got.map(|c| c.map(|c| c.url)),
                        Ok(Some(url.clone())),
                        "{name}"
                    );
                    assert_reqwest_agrees(name, url);
                }
                other => panic!("{name}：不认识的 expected {other}"),
            }
        }
    }

    /// 文法接受的代理 URL，reqwest 也必须接受，且（按 WHATWG 的 `Url::parse`）解析出的主机与
    /// 端口与文法切出的一致——否则同一个值在两个 SDK 里可能连到不同的代理（如 `\` 被当作 `/`）。
    fn assert_reqwest_agrees(name: &str, url: &str) {
        assert!(reqwest::Proxy::all(url).is_ok(), "{name}：reqwest 拒绝");
        let (host, port) = check_proxy_url("HTTPS_PROXY", url).expect("文法接受");
        let parsed = reqwest::Url::parse(url).expect("url 解析");
        let parsed_host = parsed.host_str().expect("有主机");
        if let Some(inner) = host.strip_prefix('[') {
            let ours: Ipv6Addr = inner.trim_end_matches(']').parse().expect("IPv6");
            let theirs: Ipv6Addr = parsed_host.trim_matches(['[', ']']).parse().expect("IPv6");
            assert_eq!(ours, theirs, "{name}");
        } else {
            assert_eq!(parsed_host, host.to_ascii_lowercase(), "{name}");
        }
        let default = if url.to_ascii_lowercase().starts_with("https://") {
            443
        } else {
            80
        };
        assert_eq!(
            parsed.port_or_known_default(),
            Some(port.unwrap_or(default)),
            "{name}"
        );
    }

    #[test]
    fn the_unsupported_scheme_message_does_not_echo_credentials() {
        let message = proxy_for("https://dpe.example.com/r", |key| {
            (key == "HTTPS_PROXY").then(|| "socks5://user:s3cr3t@p:1080".to_owned())
        })
        .expect_err("socks5 不受支持");
        assert!(message.contains("HTTPS_PROXY") && !message.contains("s3cr3t"));
        // 「scheme」位置不合文法（夹着 userinfo）：只报不合法，不回显
        let message = proxy_for("https://dpe.example.com/r", |key| {
            (key == "HTTPS_PROXY").then(|| "user:s3cr3t@socks5://p:1080".to_owned())
        })
        .expect_err("scheme 不合文法");
        assert!(message.contains("HTTPS_PROXY") && !message.contains("s3cr3t"));
    }
}
