//! HTTP 传输适配测试的辅助：极简 HTTP/1.1 假服务端（真实 TCP，零外部依赖）。
//!
//! 与 Python 侧 `httpx.MockTransport` 的分工相同：这里只模拟网络层（应答、断连、挂起），
//! 不替代承载协议契约的那一层——请求报文与响应报文都是手写的线格式，经真实的 reqwest /
//! tokio 客户端往返。
//!
//! 服务端对每个连接只处理一个请求、应答后带 `Connection: close` 关闭（客户端每次请求重连）；
//! 应答按脚本顺序消耗，脚本耗尽后重复最后一条（与 Python 测试的 `_scripted` 相同）。

#![allow(dead_code)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use dpe_sdk::credentials::CredentialProvider;
use dpe_sdk::wire::Capabilities;
use serde_json::{json, Value};

/// 计数凭证 provider：记录取用次数，固定返回一个值（`None` 表示不带凭证）。
#[derive(Default)]
pub struct CountingProvider {
    value: Option<String>,
    calls: AtomicUsize,
}

impl CountingProvider {
    /// 固定返回 `value`。
    pub fn new(value: impl Into<String>) -> Self {
        CountingProvider {
            value: Some(value.into()),
            calls: AtomicUsize::new(0),
        }
    }

    /// 到目前为止的取用次数。
    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl CredentialProvider for CountingProvider {
    fn authorization(&self) -> Option<String> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.value.clone()
    }
}

/// 服务端记录的一次请求（头字段名统一小写）。
#[derive(Debug, Clone)]
pub struct Recorded {
    pub method: String,
    /// 对端（客户端的临时端口）：诊断连接复用时看它是否相同。
    pub peer: std::net::SocketAddr,
    /// 请求行里的 target（路径 + 查询串）：remote 之后的已编码串。
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value.as_str())
    }

    /// 请求体的 JSON（体不是 JSON 时 panic）。
    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body).expect("请求体是 JSON")
    }

    /// 请求体解 gzip 后的字节。
    pub fn gunzip(&self) -> Vec<u8> {
        let mut decoder = flate2::read::GzDecoder::new(self.body.as_slice());
        let mut out = Vec::new();
        decoder.read_to_end(&mut out).expect("请求体是 gzip");
        out
    }
}

/// 一条脚本应答。
#[derive(Debug, Clone)]
pub enum Reply {
    /// 正常应答（读完请求后按此回复）。
    Response {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    },
    /// 读完请求后直接断连（响应丢失：请求已到服务端，响应回不去）。
    Close,
    /// 读完请求后挂起不应答（读超时：等待客户端超时或断开后退出，不拖住测试）。
    Stall,
}

impl Reply {
    /// 状态码 + 体（无附加头）。
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Reply {
        Reply::Response {
            status,
            headers: Vec::new(),
            body: body.into(),
        }
    }

    /// JSON 应答。
    pub fn json(status: u16, body: Value) -> Reply {
        Reply::Response {
            status,
            headers: vec![("Content-Type".to_owned(), "application/json".to_owned())],
            body: serde_json::to_vec(&body).expect("JSON 值总能序列化"),
        }
    }

    /// JSON 应答 + 附加头（`DPE-Doc-Hash` 等）。
    pub fn json_with(status: u16, body: Value, headers: &[(&str, &str)]) -> Reply {
        Reply::Response {
            status,
            headers: [&[("Content-Type", "application/json")][..], headers]
                .concat()
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect(),
            body: serde_json::to_vec(&body).expect("JSON 值总能序列化"),
        }
    }

    /// problem 应答（HTTP 绑定 §5）：体 + `DPE-Error-Code` 头。
    pub fn problem(code: &str, status: u16, headers: &[(&str, &str)]) -> Reply {
        let body = json!({"type": "urn:dpe:error:x", "title": "t", "status": status, "code": code});
        Reply::json_with(
            status,
            body,
            &[&[("DPE-Error-Code", code)][..], headers].concat(),
        )
    }
}

/// 极简 HTTP/1.1 假服务端：监听 127.0.0.1 的随机端口，按脚本应答并记录请求。
pub struct MockServer {
    addr: SocketAddr,
    requests: Arc<Mutex<Vec<Recorded>>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl MockServer {
    /// 在 127.0.0.1 的随机端口上启动，按脚本应答（脚本耗尽后重复最后一条）。
    pub fn start(script: Vec<Reply>) -> MockServer {
        MockServer::start_with(script, None)
    }

    /// 同 [`start`](MockServer::start)，但服务满 `close_after` 个请求后关闭监听——
    /// 其后的连接是**建连失败**（确定未发出），用来演练写请求全链路未发出的场景。
    pub fn start_closing_after(script: Vec<Reply>, close_after: usize) -> MockServer {
        MockServer::start_with(script, Some(close_after))
    }

    /// 按请求路由应答：每个请求交给 `responder` 决定回复（多步、并发场景用——应答取决于请求
    /// 本身而不是到达顺序）。仍只模拟网络层：应答由用例逐条给出，不实现服务端语义。
    pub fn start_fn(responder: impl Fn(&Recorded) -> Reply + Send + Sync + 'static) -> MockServer {
        MockServer::start_source(Source::Respond(Box::new(responder)), None)
    }

    /// 同 [`start_fn`](MockServer::start_fn)，但服务满 `close_after` 个请求后关闭监听（其后的
    /// 连接是建连失败）。
    pub fn start_fn_closing_after(
        responder: impl Fn(&Recorded) -> Reply + Send + Sync + 'static,
        close_after: usize,
    ) -> MockServer {
        MockServer::start_source(Source::Respond(Box::new(responder)), Some(close_after))
    }

    fn start_with(script: Vec<Reply>, close_after: Option<usize>) -> MockServer {
        MockServer::start_source(Source::Script(Mutex::new(script)), close_after)
    }

    fn start_source(source: Source, close_after: Option<usize>) -> MockServer {
        let listener = bind_outside_ephemeral();
        let addr = listener.local_addr().expect("取本地地址");
        listener.set_nonblocking(true).expect("非阻塞 accept");
        let script = Arc::new(source);
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let thread = {
            let script = Arc::clone(&script);
            let requests = Arc::clone(&requests);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                let mut served = 0usize;
                while !stop.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            // 脚本条目在**读到请求之后**才消耗（见 `handle`）：空连接
                            // （连上就断）不占脚本，避免打乱后续应答的顺序
                            served += 1;
                            let script = Arc::clone(&script);
                            let requests = Arc::clone(&requests);
                            thread::spawn(move || handle(stream, script, requests));
                            if Some(served) == close_after {
                                break; // 丢弃监听：其后的连接被拒绝（建连失败）
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(1));
                        }
                        Err(_) => break,
                    }
                }
            })
        };
        MockServer {
            addr,
            requests,
            stop,
            thread: Some(thread),
        }
    }

    /// 客户端用的 remote（`http://127.0.0.1:<port>`）。
    pub fn remote(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// 到目前为止收到的请求（按到达顺序）。
    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().expect("请求记录锁").clone()
    }

    /// 到目前为止收到的请求数。
    pub fn count(&self) -> usize {
        self.requests.lock().expect("请求记录锁").len()
    }

    /// 等待请求数达到 `expected`（并发用例消除时序抖动），超时后如实返回当前数量。
    pub fn wait_for_requests(&self, expected: usize, timeout: Duration) -> usize {
        let deadline = Instant::now() + timeout;
        loop {
            let count = self.count();
            if count >= expected || Instant::now() > deadline {
                return count;
            }
            thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for MockServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// 绑定一个 127.0.0.1 上的**非临时端口**（`21000 + 槽位`，槽位从全局计数器取）并监听。
///
/// 刻意避开操作系统的临时端口段（Linux ≥ 32768、macOS ≥ 49152）：[`MockServer::start`] 若用
/// `bind(0)` 拿端口，[`MockServer::start_closing_after`] 关闭监听后释放的那个端口可能被并行
/// 用例的新服务端抢走——被关方客户端的「建连失败」就落到别人身上，还会吃掉那一边的应答
/// 脚本（实测到的串扰）。槽位分配保证同一次运行内的服务端端口互不相同；槽位端口被别的
/// 进程占用时顺延。
fn bind_outside_ephemeral() -> TcpListener {
    static NEXT_SLOT: AtomicU16 = AtomicU16::new(0);
    static BASE: std::sync::OnceLock<u16> = std::sync::OnceLock::new();
    // 起始槽位随进程启动时刻错开：本地连跑多轮测试时，固定的一批端口会反复与上一轮残留的
    // TCP 状态（TIME_WAIT）照面；每轮换一批就绕开了。段内槽位递增保证同一次运行内各服务端
    // 端口互不相同。
    let base = *BASE.get_or_init(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| (elapsed.as_nanos() % 100) as u16)
            .unwrap_or(0)
    });
    for _ in 0..200 {
        let slot = (base + NEXT_SLOT.fetch_add(1, Ordering::Relaxed)) % 200;
        if let Ok(listener) = TcpListener::bind(("127.0.0.1", 21_000 + slot)) {
            return listener;
        }
    }
    panic!("21000–21199 段没有空闲端口");
}

/// 应答来源：按到达顺序消耗的脚本，或按请求路由的应答函数。
enum Source {
    Script(Mutex<Vec<Reply>>),
    Respond(Box<dyn Fn(&Recorded) -> Reply + Send + Sync>),
}

/// 处理一个连接：读一个请求、记录、按脚本应答，然后关闭。
///
/// 脚本条目在这里（读到请求之后）消耗：空连接不占脚本条目，也不记入请求列表。
fn handle(mut stream: TcpStream, script: Arc<Source>, requests: Arc<Mutex<Vec<Recorded>>>) {
    // macOS 上 `accept` 出的套接字**继承**监听套接字的非阻塞标志（实测：立刻 read 会返回
    // WouldBlock 而不是等待）。不显式切回阻塞模式，读一个还没到达的请求会立刻失败、处理器
    // 提前关掉还带着未读数据的连接——内核改用 RST 收尾，客户端看到随机的「连接被重置」。
    stream.set_nonblocking(false).expect("切回阻塞模式");
    let Some(request) = read_request(&stream) else {
        return;
    };
    let head_only = request.method == "HEAD";
    let reply = match script.as_ref() {
        Source::Script(script) => {
            let mut script = script.lock().expect("脚本锁");
            if script.len() > 1 {
                script.remove(0)
            } else {
                script[0].clone() // 脚本耗尽后重复最后一条（与 Python 侧 `_scripted` 相同）
            }
        }
        Source::Respond(responder) => responder(&request),
    };
    requests.lock().expect("请求记录锁").push(request);
    match reply {
        Reply::Close => {
            let _ = stream.shutdown(std::net::Shutdown::Both);
        }
        Reply::Stall => {
            // 挂住不应答：等客户端超时后断开（读到 EOF 或出错）即退出
            wait_for_client_close(stream);
        }
        Reply::Response {
            status,
            headers,
            body,
        } => {
            let mut head = format!(
                "HTTP/1.1 {status} {reason}\r\nContent-Length: {}\r\nConnection: close\r\n",
                body.len(),
                reason = reason_phrase(status)
            );
            for (name, value) in &headers {
                head.push_str(name);
                head.push_str(": ");
                head.push_str(value);
                head.push_str("\r\n");
            }
            head.push_str("\r\n");
            let _ = stream.write_all(head.as_bytes());
            if !head_only {
                let _ = stream.write_all(&body);
            }
            let _ = stream.flush();
            // 宣告了 `Connection: close`：等**客户端先关**（读到 EOF）再收尾。服务端先关会
            // 让自己这侧的槽位端口积累 TIME_WAIT（`netstat` 里可见），客户端先关则 TIME_WAIT
            // 落在它的临时端口上——由内核负责避开复用，测试端口保持干净。
            wait_for_client_close(stream);
        }
    }
}

/// 等客户端关闭连接（读到 EOF 或出错），最多 5 秒——不主动先关（见 `handle` 的说明）。
fn wait_for_client_close(mut stream: TcpStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut scratch = [0u8; 256];
    while Instant::now() < deadline {
        match stream.read(&mut scratch) {
            Ok(0) => break, // 客户端关了
            Ok(_) => continue,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                continue
            }
            Err(_) => break,
        }
    }
    let _ = stream.shutdown(std::net::Shutdown::Both);
}

/// 读一个 HTTP/1.1 请求（请求行 + 头 + 按 `Content-Length` 读体）。
fn read_request(stream: &TcpStream) -> Option<Recorded> {
    let peer = stream.peer_addr().ok()?;
    let clone = stream.try_clone().ok()?;
    let mut reader = BufReader::new(clone);
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let mut parts = line.trim_end().split(' ');
    let method = parts.next()?.to_owned();
    let target = parts.next()?.to_owned();
    let mut headers: Vec<(String, String)> = Vec::new();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).ok()? == 0 {
            return None; // 连接在半途断开
        }
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_ascii_lowercase(), value.trim().to_owned()));
        }
    }
    let length: usize = headers
        .iter()
        .find(|(name, _)| name == "content-length")
        .and_then(|(_, value)| value.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0u8; length];
    if length > 0 {
        reader.read_exact(&mut body).ok()?;
    }
    Some(Recorded {
        method,
        target,
        peer,
        headers,
        body,
    })
}

/// 状态码短语（不够用时给 "Status"；协议不读短语，只为报文可读）。
fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        302 => "Found",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        410 => "Gone",
        412 => "Precondition Failed",
        413 => "Payload Too Large",
        415 => "Unsupported Media Type",
        428 => "Precondition Required",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "Status",
    }
}

// ---------------------------------------------------------------------- 应答构造

/// capabilities 响应体模板（与 Python 侧的 `CAPABILITIES`、Rust 侧 protocol_common 同模板）。
pub fn capabilities_body() -> Value {
    json!({
        "protocol": "dpe/1",
        "hash_contracts": ["dpe1"],
        "limits": {
            "max_payload_bytes": 8388608,
            "page_max_bytes": 268435456,
            "staging_ttl_seconds": 86400,
            "blob_max_bytes": 104857600,
            "blob_chunk_bytes": 8388608,
            "batch_head_max": 2,
            "list_page_max": 1000,
        },
        "content_encodings": ["gzip"],
        "features": ["move"],
    })
}

/// `GET capabilities` 的 200 应答（`content_encodings` 可改）。
pub fn capabilities_reply(content_encodings: &[&str]) -> Reply {
    let mut body = capabilities_body();
    body["content_encodings"] = json!(content_encodings);
    Reply::json(200, body)
}

/// capabilities 响应解析用的类型（断言用）。
pub fn capabilities() -> Capabilities {
    Capabilities::from_value(&capabilities_body()).expect("能力模板必须合法")
}

/// commit 成功的应答（201 created / 200 updated + `DPE-Doc-Hash`）。
pub fn committed(
    document: &dpe_sdk::ExpandedDocument,
    status: &str,
    added: u64,
    removed: u64,
    retained: u64,
) -> Reply {
    let hash = document.doc_hash(dpe_sdk::CONTRACT).expect("文档 hash");
    Reply::json_with(
        if status == "created" { 201 } else { 200 },
        json!({
            "status": status,
            "doc_hash": hash,
            "delta": {"added": added, "removed": removed, "retained": retained},
        }),
        &[("DPE-Doc-Hash", &hash)],
    )
}

/// `batch_head` 的 200 应答（与入参一一对应，`None` 为不存在，线上是 `null`）。
pub fn heads_reply(heads: &[Option<String>]) -> Reply {
    let items: Vec<Value> = heads
        .iter()
        .map(|head| match head {
            Some(hash) => json!({"doc_hash": hash}),
            None => Value::Null,
        })
        .collect();
    Reply::json(200, json!({"heads": items}))
}

/// `dpe1:` + 64 位重复字符的 hash 字面量（与 protocol_common 一致）。
pub fn hash(digit: char) -> String {
    format!("dpe1:{}", digit.to_string().repeat(64))
}

/// 当前 UTC 时间 + `offset` 秒的 IMF-fixdate（RFC 9110 §5.6.7，如
/// `Sun, 06 Nov 1994 08:49:37 GMT`）：`Retry-After` 的 HTTP-date 形式用例。
pub fn http_date_in(offset: i64) -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("系统时钟晚于纪元")
        .as_secs() as i64
        + offset;
    let days = seconds.div_euclid(86_400);
    let time = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    // 1970-01-01 是周四
    let weekday = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"][days.rem_euclid(7) as usize];
    let month_name = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ][(month - 1) as usize];
    format!(
        "{weekday}, {day:02} {month_name} {year} {:02}:{:02}:{:02} GMT",
        time / 3600,
        (time / 60) % 60,
        time % 60
    )
}

/// 天数（1970-01-01 起）→ (年, 月, 日)（Howard Hinnant 的 civil_from_days）。
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if month <= 2 { year + 1 } else { year }, month, day)
}
