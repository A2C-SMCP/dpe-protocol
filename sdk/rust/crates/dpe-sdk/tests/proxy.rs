//! 代理只来自环境变量（#132）：`DpeClient` 经真实 TCP 的代理行为，与 Python 侧
//! `tests/test_proxy.py` 逐条对等（判定规则的用例表见 `src/proxy.rs` 的单元测试）。
//!
//! remote 是 `http_common` 的假服务端，代理是本文件起的本地 HTTP 代理（转发、拒绝隧道、回 502
//! 三种模式，记录收到的请求行）。环境变量是进程级的：客户端只在 `build` 时读取，用例经
//! [`build_with_env`] 设置、构造、复原；全部场景在一个测试函数里依次运行（见文件末尾）。
//!
//! 「系统设了代理、环境变量没设」在 Rust 侧无从构造（依赖图未打开 reqwest 的 `system-proxy`），
//! 由构造时显式 `no_proxy()` 保证，见 `src/http.rs` 的模块说明；Python 侧有对应的模拟用例。

#![cfg(feature = "http")]

mod http_common;

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::{Body, Headers, Method, NotSent, Request};
use dpe_sdk::{Error, ErrorKind};

use http_common::{capabilities_reply, MockServer};

/// 判定会读到的全部变量。
const PROXY_VARS: [&str; 9] = [
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "REQUEST_METHOD",
];

/// 清掉全部代理变量、设置 `vars`、构造客户端、再清掉——客户端只在构造时读环境。
fn build_with_env(
    remote: &str,
    vars: &[(&str, &str)],
    max_retries: u32,
) -> Result<DpeClient, Error> {
    let clear = || {
        PROXY_VARS
            .iter()
            .for_each(|name| std::env::remove_var(name))
    };
    clear();
    for (name, value) in vars {
        std::env::set_var(name, value);
    }
    let client = DpeClient::builder(remote)
        .max_retries(max_retries)
        .timeout(Some(Duration::from_secs(5)))
        .build();
    clear();
    client
}

fn capabilities_request() -> Request {
    Request {
        method: Method::Get,
        target: "capabilities".to_owned(),
        headers: Headers::new(),
        body: Body::None,
        retry_safe: true,
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    /// 明文转发给上游（绝对形式的请求目标改写为原始形式）
    Forward,
    /// 对一切请求回 502
    BadGateway,
}

/// 本地 HTTP 代理：`CONNECT` 一律以 403 拒绝；其余请求按 [`Mode`] 处理。
struct LocalProxy {
    port: u16,
    seen: Arc<Mutex<Vec<String>>>,
}

impl LocalProxy {
    fn start(mode: Mode) -> LocalProxy {
        let listener = TcpListener::bind("127.0.0.1:0").expect("绑定代理端口");
        let port = listener.local_addr().expect("代理地址").port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorder = Arc::clone(&seen);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let recorder = Arc::clone(&recorder);
                thread::spawn(move || {
                    let _ = serve(stream, mode, &recorder);
                });
            }
        });
        LocalProxy { port, seen }
    }

    fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    fn seen(&self) -> Vec<String> {
        self.seen.lock().expect("记录锁").clone()
    }
}

fn serve(stream: TcpStream, mode: Mode, seen: &Mutex<Vec<String>>) -> std::io::Result<()> {
    let mut writer = stream.try_clone()?;
    let mut reader = BufReader::new(stream);
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let mut headers = Vec::new();
        loop {
            let mut header = String::new();
            if reader.read_line(&mut header)? == 0 || header == "\r\n" || header == "\n" {
                break;
            }
            headers.push(header);
        }
        let mut parts = line.split(' ');
        let method = parts.next().unwrap_or_default().to_owned();
        let target = parts.next().unwrap_or_default().to_owned();
        seen.lock()
            .expect("记录锁")
            .push(format!("{method} {target}"));
        if method == "CONNECT" {
            // 隧道一律拒绝：请求随后才会经隧道发出
            writer.write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")?;
            return Ok(());
        }
        let length = headers
            .iter()
            .find_map(|h| {
                let (name, value) = h.split_once(':')?;
                name.trim()
                    .eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())?
            })
            .unwrap_or(0);
        let mut body = vec![0; length];
        reader.read_exact(&mut body)?;
        if mode == Mode::BadGateway {
            writer.write_all(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")?;
            continue;
        }
        // 转发：http://host:port/path → 原始形式，带 Connection: close 交给上游
        let rest = target
            .strip_prefix("http://")
            .expect("明文转发的目标是绝对形式");
        let (authority, path) = rest.split_at(rest.find('/').unwrap_or(rest.len()));
        let mut upstream = TcpStream::connect(authority)?;
        let mut request = format!(
            "{method} {} HTTP/1.1\r\n",
            if path.is_empty() { "/" } else { path }
        );
        for header in &headers {
            let name = header.split(':').next().unwrap_or_default().trim();
            if !name.eq_ignore_ascii_case("connection")
                && !name.eq_ignore_ascii_case("proxy-connection")
            {
                request.push_str(header);
            }
        }
        request.push_str("Connection: close\r\n\r\n");
        upstream.write_all(request.as_bytes())?;
        upstream.write_all(&body)?;
        // 按 Content-Length 读完应答即转回：假服务端宣告 `Connection: close` 后等对端先关，
        // 读到 EOF 会一直等到它的超时
        writer.write_all(&read_response(upstream)?)?;
        return Ok(());
    }
}

/// 读一个带 `Content-Length` 的 HTTP/1.1 应答（头与体的原始字节）。
fn read_response(upstream: TcpStream) -> std::io::Result<Vec<u8>> {
    let mut reader = BufReader::new(upstream);
    let mut raw = Vec::new();
    let mut length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Ok(raw);
        }
        raw.extend_from_slice(line.as_bytes());
        if line == "\r\n" || line == "\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    raw.extend_from_slice(&body);
    Ok(raw)
}

async fn the_env_proxy_is_used() {
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let proxy = LocalProxy::start(Mode::Forward);
    let client =
        build_with_env(&server.remote(), &[("HTTP_PROXY", &proxy.url())], 0).expect("构造");
    let response = client
        .send(capabilities_request())
        .await
        .expect("经代理成功");
    assert_eq!(response.status(), 200);
    assert_eq!(
        proxy.seen(),
        [format!("GET {}/capabilities", server.remote())]
    );
    assert_eq!(server.count(), 1);
}

async fn no_proxy_connects_directly() {
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let proxy = LocalProxy::start(Mode::Forward);
    let client = build_with_env(
        &server.remote(),
        &[("http_proxy", &proxy.url()), ("NO_PROXY", "127.0.0.1")],
        0,
    )
    .expect("构造");
    let response = client.send(capabilities_request()).await.expect("直连成功");
    assert_eq!(response.status(), 200);
    assert!(proxy.seen().is_empty());
    assert_eq!(server.count(), 1);
}

async fn reqwests_own_proxy_detection_is_off() {
    // 只按本 SDK 的规则判定：大写 `NO_PROXY` 为空视为未设置，取小写 `no_proxy` → 直连。
    // reqwest 自身的探测取第一个**存在**的变量（空的大写），不会绕过代理——若构造时没关掉
    // 它（`no_proxy()`），这个请求会被它送进代理。
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let proxy = LocalProxy::start(Mode::Forward);
    let client = build_with_env(
        &server.remote(),
        &[
            ("http_proxy", &proxy.url()),
            ("NO_PROXY", ""),
            ("no_proxy", "127.0.0.1"),
        ],
        0,
    )
    .expect("构造");
    let response = client.send(capabilities_request()).await.expect("直连成功");
    assert_eq!(response.status(), 200);
    assert!(proxy.seen().is_empty());
}

async fn an_unreachable_proxy_is_not_sent() {
    // 连不上代理是建连失败：确定未发出（端口 1 在测试机上没有监听者）
    let server = MockServer::start(vec![capabilities_reply(&[])]);
    let client =
        build_with_env(&server.remote(), &[("HTTP_PROXY", "http://127.0.0.1:1")], 0).expect("构造");
    let error = client
        .send(capabilities_request())
        .await
        .expect_err("连不上代理");
    assert!(error.downcast_ref::<NotSent>().is_some(), "{error}");
    assert_eq!(server.count(), 0);
}

async fn a_rejected_tunnel_is_not_sent() {
    // HTTPS 经代理、隧道被拒（CONNECT 应答 403）：请求还没到 remote，判为确定未发出；
    // 传输层按「确定未发出」原样重发（每次都先建隧道）。失败发生在 TLS 之前，不需要证书。
    let proxy = LocalProxy::start(Mode::Forward);
    let client = build_with_env(
        "https://dpe.invalid/r/1",
        &[("HTTPS_PROXY", &proxy.url())],
        1,
    )
    .expect("构造");
    let error = client
        .send(capabilities_request())
        .await
        .expect_err("隧道被拒");
    let marker = error
        .downcast_ref::<NotSent>()
        .expect("隧道被拒带「确定未发出」标记");
    assert!(marker.error().downcast_ref::<reqwest::Error>().is_some());
    assert_eq!(
        proxy.seen(),
        ["CONNECT dpe.invalid:443", "CONNECT dpe.invalid:443"]
    );
}

async fn a_bad_gateway_from_a_forwarding_proxy_is_a_response() {
    // 明文 HTTP 经代理转发时，代理的 502 是普通响应，不判为确定未发出（代理可能已把请求
    // 转给了 remote）
    let proxy = LocalProxy::start(Mode::BadGateway);
    let client =
        build_with_env("http://dpe.invalid/r/1", &[("HTTP_PROXY", &proxy.url())], 0).expect("构造");
    let response = client
        .send(capabilities_request())
        .await
        .expect("502 是响应");
    assert_eq!(response.status(), 502);
    assert_eq!(proxy.seen(), ["GET http://dpe.invalid/r/1/capabilities"]);
}

fn an_unsupported_proxy_scheme_is_rejected_at_build() {
    let Err(error) = build_with_env(
        "https://dpe.example.com/r",
        &[("HTTPS_PROXY", "socks5://user:s3cr3t@127.0.0.1:1080")],
        0,
    ) else {
        panic!("socks5 不受支持");
    };
    assert_eq!(error.kind(), ErrorKind::InvalidArgument);
    assert!(error.message().contains("HTTPS_PROXY"));
    assert!(!error.to_string().contains("s3cr3t"));
}

/// 全部场景在**一个**测试里依次运行：环境变量是进程级的，同一测试二进制里并发的测试在别处
/// 读环境（如 DNS 解析）时与这里的修改构成数据竞争；只有一个测试函数，就没有并发的读者。
#[tokio::test]
async fn proxy_scenarios_run_one_at_a_time() {
    the_env_proxy_is_used().await;
    no_proxy_connects_directly().await;
    reqwests_own_proxy_detection_is_off().await;
    an_unreachable_proxy_is_not_sent().await;
    a_rejected_tunnel_is_not_sent().await;
    a_bad_gateway_from_a_forwarding_proxy_is_a_response().await;
    an_unsupported_proxy_scheme_is_rejected_at_build();
}
