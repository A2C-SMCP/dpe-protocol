//! 对 Python 参考服务端（`python -m dpe_sdk.testing`）的集成测试辅助（#25）。
//!
//! 与 Python 侧 `tests/server_helpers.spawn_server` 同一真实设施：子进程绑定空闲端口，stdout
//! 第一行公布 `{"url": …, "hooks": …}`，之后由客户端驱动。
//!
//! **门控**：启动命令取自环境变量 `DPE_REFERENCE_SERVER`（按空白切分、不支持引号；工作目录为
//! `sdk/rust`），如 `uv run --project ../python --frozen python -m dpe_sdk.testing`。未设置时：
//! `DPE_REFERENCE_REQUIRED=1`（CI 的 reference job）直接失败，杜绝静默跳过；否则打印跳过提示、
//! 用例直接返回。本地先在 `sdk/python` 下 `uv sync`。子进程放进独立进程组、收尾对整组发
//! SIGTERM（`uv run` 把 python 起成子进程）；这只在 Unix 上完整成立，其他平台请把解释器本身
//! （而不是 `uv run`）配置为启动命令。
//!
//! **故障注入**（Python 的 `Lossy` / `Expire` / `Tick` / `Recorder` 的对等物）：参考服务端没有
//! 「丢响应」钩子，丢响应在客户端做——[`drive`] 自己跑 `Operation` 的循环，经公开的
//! [`DpeClient::send`] 发出请求，按 [`Injection`] 在发出前拨钟 / 让会话过期，或在收到响应后把它
//! 换成传输错误回送给操作（服务端已生效、客户端收不到结果）。

#![allow(dead_code)]

use std::fmt;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use dpe_sdk::credentials::StaticCredentialProvider;
use dpe_sdk::http::DpeClient;
use dpe_sdk::protocol::{Operation, Request, Step, TransportError};
use dpe_sdk::{Error, ExpandedDocument, Response};
use serde_json::{json, Value};

/// 子进程公布地址的超时（`uv run` 首次解析环境可能较慢）。
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);

/// 小限额（Python `small_engine`）：内联放不下中等文档、blob 按 100 字节分块。
pub const SMALL_LIMITS: &[&str] = &[
    "--max-payload-bytes",
    "200",
    "--blob-chunk-bytes",
    "100",
    "--page-max-bytes",
    "4096",
    "--blob-max-bytes",
    "4096",
];

/// 子进程守卫：析构时先对整个进程组发 SIGTERM、限时等待，再 SIGKILL（同 Python
/// `spawn_server` 的 terminate + wait）。
///
/// 推荐的启动命令是 `uv run … python -m dpe_sdk.testing`：`uv` 把 python 起成**子进程**，
/// 只转发 SIGINT / SIGTERM——直接 `Child::kill()`（SIGKILL）只杀掉 `uv`，真正监听端口的
/// python 会变成孤儿。因此子进程放进独立进程组，收尾对整组发信号。启动失败的路径也经它回收。
///
/// 只在组长（直接子进程）尚未被回收时向进程组发信号：组长未回收时 pgid 不会被系统复用，
/// 不会误伤无关进程组。`uv run` 在 python 退出后才退出，因此组长退出即整组已结束。非 Unix
/// 平台只能结束直接子进程——那里请把解释器本身（而不是 `uv run`）配置为启动命令。
struct ChildGuard(Option<Child>);

impl ChildGuard {
    fn terminate(&mut self) {
        let Some(mut child) = self.0.take() else {
            return; // 幂等：已收尾
        };
        #[cfg(unix)]
        {
            let group = format!("-{}", child.id());
            let _ = Command::new("kill").args(["-TERM", "--", &group]).status();
            let deadline = std::time::Instant::now() + Duration::from_secs(10);
            while std::time::Instant::now() < deadline {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return; // 组长已退出并被回收：整组已结束，不再向（可能被复用的）pgid 发信号
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            // 超时：组长仍未回收（pgid 仍属于本组），对整组 SIGKILL
            let _ = Command::new("kill").args(["-KILL", "--", &group]).status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }

    fn get_mut(&mut self) -> &mut Child {
        self.0.as_mut().expect("子进程尚未收尾")
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        self.terminate();
    }
}

/// 一个参考服务端子进程（析构时结束）。
pub struct ReferenceServer {
    child: ChildGuard,
    url: String,
    hooks: Option<String>,
    stderr: PathBuf,
    http: reqwest::Client,
}

impl ReferenceServer {
    /// 按 `args` 启动（`--port 0` 由这里追加）；未配置启动命令时返回 `None`（见模块说明）。
    pub fn spawn(args: &[&str]) -> Option<ReferenceServer> {
        let command = match std::env::var("DPE_REFERENCE_SERVER") {
            Ok(command) if !command.trim().is_empty() => command,
            _ => {
                assert!(
                    std::env::var("DPE_REFERENCE_REQUIRED").as_deref() != Ok("1"),
                    "DPE_REFERENCE_REQUIRED=1 但未设置 DPE_REFERENCE_SERVER（参考服务端启动命令）"
                );
                eprintln!(
                    "跳过：未设置 DPE_REFERENCE_SERVER（如 `uv run --project ../python --frozen \
                     python -m dpe_sdk.testing`，先在 sdk/python 下 uv sync）"
                );
                return None;
            }
        };
        let mut parts = command.split_whitespace();
        let program = parts.next().expect("非空命令");
        let stderr = std::env::temp_dir().join(format!(
            "dpe-reference-{}-{}.log",
            std::process::id(),
            next_id()
        ));
        let log = std::fs::File::create(&stderr).expect("建 stderr 日志文件");
        // 工作目录 sdk/rust：命令里的相对路径（如 --project ../python）以它为准
        let workdir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut builder = Command::new(program);
        builder
            .args(parts)
            .args(["--port", "0"])
            .args(args)
            .current_dir(workdir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(log);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            builder.process_group(0); // 独立进程组：收尾对整组发信号（见 ChildGuard）
        }
        let mut child =
            ChildGuard(Some(builder.spawn().unwrap_or_else(|error| {
                panic!("启动参考服务端失败（{command}）：{error}")
            })));
        let stdout = child.get_mut().stdout.take().expect("stdout 已接管道");
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let _ = BufReader::new(stdout).read_line(&mut line);
            let _ = sender.send(line);
        });
        // 以下任何失败都在 panic 展开时经 ChildGuard 回收子进程，并附上 stderr 便于诊断
        let line = match receiver.recv_timeout(STARTUP_TIMEOUT) {
            Ok(line) if !line.trim().is_empty() => line,
            _ => panic!("参考服务端未公布地址：{}", read_log(&stderr)),
        };
        let info: Value = serde_json::from_str(&line).unwrap_or_else(|error| {
            panic!("公布行不是 JSON（{error}）：{line}\n{}", read_log(&stderr))
        });
        let url = info["url"]
            .as_str()
            .unwrap_or_else(|| panic!("公布行缺 url：{line}\n{}", read_log(&stderr)))
            .to_owned();
        Some(ReferenceServer {
            child,
            url,
            hooks: info["hooks"].as_str().map(str::to_owned),
            stderr,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .expect("HTTP 客户端"),
        })
    }

    /// remote 的 base URL（含 `--prefix`）。
    pub fn url(&self) -> &str {
        &self.url
    }

    /// 该服务端的客户端（不带凭证）。
    pub fn client(&self) -> DpeClient {
        DpeClient::builder(self.url.clone())
            .timeout(Some(Duration::from_secs(30)))
            .build()
            .expect("客户端")
    }

    /// 带 `Authorization` 原值的客户端（`--grant` 场景）。
    pub fn client_as(&self, authorization: &str) -> DpeClient {
        DpeClient::builder(self.url.clone())
            .credential(std::sync::Arc::new(StaticCredentialProvider::new(
                authorization,
            )))
            .timeout(Some(Duration::from_secs(30)))
            .build()
            .expect("客户端")
    }

    /// 截至此刻的子进程 stderr（失败诊断用）。
    pub fn stderr(&self) -> String {
        read_log(&self.stderr)
    }

    /// 钩子：把服务端时钟向前拨 `seconds` 秒（需 `--hooks`）。返回拨后的 `now`。
    pub async fn advance_clock(&self, seconds: u64) -> String {
        let body = self
            .hook("clock/advance", json!({"advance_seconds": seconds}))
            .await;
        body["now"].as_str().expect("now").to_owned()
    }

    /// 钩子：让一个暂存会话立即过期（需 `--hooks`）。返回它此前是否可用。
    pub async fn expire_session(&self, session_id: &str) -> bool {
        let body = self
            .hook("expire-session", json!({"session_id": session_id}))
            .await;
        body["expired"].as_bool().expect("expired")
    }

    async fn hook(&self, name: &str, body: Value) -> Value {
        let base = self.hooks.as_deref().expect("需以 --hooks 启动参考服务端");
        // SDK 不开 reqwest 的 json feature：手工序列化
        let response = self
            .http
            .post(format!("{base}/{name}"))
            .header("Content-Type", "application/json")
            .body(serde_json::to_vec(&body).expect("JSON 值总能序列化"))
            .send()
            .await
            .expect("钩子请求");
        let status = response.status();
        let bytes = response.bytes().await.expect("钩子响应体");
        let body: Value = serde_json::from_slice(&bytes).expect("钩子响应是 JSON");
        assert!(status.is_success(), "钩子 {name} 失败：{status} {body}");
        body
    }

    /// 原始 HTTP 请求（`target` 相对 remote，如 `staging/st-0/blobs/…`）；默认带
    /// `DPE-Hash-Contract: dpe1`（同 Python `Remote.raw`）。
    pub async fn raw(
        &self,
        method: &str,
        target: &str,
        headers: &[(&str, &str)],
        body: Vec<u8>,
    ) -> RawResponse {
        let mut request = self
            .http
            .request(
                reqwest::Method::from_bytes(method.as_bytes()).expect("方法"),
                format!("{}/{}", self.url, target),
            )
            .header("DPE-Hash-Contract", "dpe1");
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        let response = request.body(body).send().await.expect("原始请求");
        let status = response.status().as_u16();
        let headers = response
            .headers()
            .iter()
            .map(|(name, value)| {
                (
                    name.as_str().to_ascii_lowercase(),
                    value.to_str().unwrap_or_default().to_owned(),
                )
            })
            .collect();
        let body = response.bytes().await.expect("响应体").to_vec();
        RawResponse {
            status,
            headers,
            body,
        }
    }
}

impl Drop for ReferenceServer {
    fn drop(&mut self) {
        // 子进程由字段 `child`（ChildGuard）析构时收尾；Unix 上先删已打开的日志文件无碍
        let _ = std::fs::remove_file(&self.stderr);
    }
}

/// 原始请求的响应（头字段名小写）。
#[derive(Debug)]
pub struct RawResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl RawResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value.as_str())
    }

    /// 断言是 DPE 错误响应并返回 code（同 Python `http_helpers.problem`）：problem+json、
    /// `DPE-Error-Code` 与体中 `code` 一致、体中 `status` 等于状态码，并顺带钉住 HTTP 绑定 §5 的
    /// `type` / `title` 规则——`code` 去掉 `DPE_` 前缀转 kebab-case 即 `type` 的末段，`title`
    /// 是它把连接线换成空格的小写短语。
    pub fn code(&self) -> String {
        assert_eq!(
            self.header("Content-Type"),
            Some("application/problem+json")
        );
        let body: Value = serde_json::from_slice(&self.body).expect("problem 体是 JSON");
        let code = body["code"].as_str().expect("problem 体带 code").to_owned();
        assert_eq!(self.header("DPE-Error-Code"), Some(code.as_str()));
        assert_eq!(body["status"], json!(self.status));
        let slug = code
            .trim_start_matches("DPE_")
            .to_ascii_lowercase()
            .replace('_', "-");
        assert_eq!(body["type"], json!(format!("urn:dpe:error:{slug}")));
        assert_eq!(body["title"], json!(slug.replace('-', " ")));
        code
    }
}

fn read_log(path: &std::path::Path) -> String {
    let mut text = String::new();
    if let Ok(mut file) = std::fs::File::open(path) {
        let _ = file.seek(SeekFrom::Start(0));
        let _ = file.read_to_string(&mut text);
    }
    text
}

fn next_id() -> usize {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

// ---------------------------------------------------------------------- 驱动与故障注入

/// 请求匹配谓词。
pub type Matcher = Box<dyn Fn(&Request) -> bool + Send>;

/// 一次驱动中的故障注入与记录（Python `Lossy` / `Expire` / `Tick` / `Recorder` 的组合）。
#[derive(Default)]
pub struct Injection {
    /// 命中的**第一个**请求照常送达，收到响应后换成传输错误（响应丢失）。
    pub lose: Option<Matcher>,
    /// 命中的**第一个**请求发出前把服务端时钟拨快这么多秒。
    pub expire_before: Option<(Matcher, u64)>,
    /// 每个请求发出前都拨快这么多秒。
    pub tick: Option<u64>,
    /// 发出的请求（按顺序；含被丢弃响应的那个）。
    pub sent: Vec<Request>,
    /// 收到的响应（状态码与 `DPE-Upload-Offset` / `DPE-Error-Code`），与 `sent` 一一对应；
    /// 传输错误为 `None`。
    pub responses: Vec<Option<Seen>>,
    pub dropped: usize,
    pub expired: usize,
    /// 累计拨钟秒数。
    pub advanced: u64,
}

/// 记录下来的响应要点。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Seen {
    pub status: u16,
    pub offset: Option<String>,
    pub code: Option<String>,
}

impl Injection {
    pub fn new() -> Injection {
        Injection::default()
    }

    pub fn lose(mut self, matcher: impl Fn(&Request) -> bool + Send + 'static) -> Injection {
        self.lose = Some(Box::new(matcher));
        self
    }

    pub fn expire_before(
        mut self,
        matcher: impl Fn(&Request) -> bool + Send + 'static,
        seconds: u64,
    ) -> Injection {
        self.expire_before = Some((Box::new(matcher), seconds));
        self
    }

    pub fn tick(mut self, seconds: u64) -> Injection {
        self.tick = Some(seconds);
        self
    }

    /// 发出的请求方法。
    pub fn methods(&self) -> Vec<String> {
        self.sent
            .iter()
            .map(|request| request.method.as_str().to_owned())
            .collect()
    }

    /// 发出的 `(方法, target)`。
    pub fn methods_and_targets(&self) -> Vec<(String, String)> {
        self.sent
            .iter()
            .map(|request| (request.method.as_str().to_owned(), request.target.clone()))
            .collect()
    }
}

/// 模拟响应丢失的传输错误（请求已到达服务端、客户端收不到结果）。
#[derive(Debug)]
pub struct Lost;

impl fmt::Display for Lost {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("响应丢失（服务端已处理）")
    }
}

impl std::error::Error for Lost {}

/// 驱动一个操作直到完成，按 `injection` 注入故障并记录请求与响应。
pub async fn drive<O: Operation>(
    server: &ReferenceServer,
    client: &DpeClient,
    mut op: O,
    injection: &mut Injection,
) -> Result<O::Output, Error> {
    loop {
        match op.poll()? {
            Step::Done(output) => return Ok(output),
            Step::Send(request) => {
                if let Some(seconds) = injection.tick {
                    server.advance_clock(seconds).await;
                    injection.advanced += seconds;
                }
                let expire = matches!(&injection.expire_before, Some((matcher, _)) if injection.expired == 0 && matcher(&request));
                if expire {
                    let seconds = injection.expire_before.as_ref().expect("已匹配").1;
                    server.advance_clock(seconds).await;
                    injection.advanced += seconds;
                    injection.expired += 1;
                }
                let lose = matches!(&injection.lose, Some(matcher) if injection.dropped == 0 && matcher(&request));
                injection.sent.push(request.clone());
                let result: Result<Response, TransportError> = client.send(request).await;
                injection
                    .responses
                    .push(result.as_ref().ok().map(|response| Seen {
                        status: response.status(),
                        offset: response.header("DPE-Upload-Offset").map(str::to_owned),
                        code: response.header("DPE-Error-Code").map(str::to_owned),
                    }));
                match result {
                    Ok(_) if lose => {
                        injection.dropped += 1;
                        op.on_transport_error(Box::new(Lost))?;
                    }
                    Ok(response) => op.on_response(response)?,
                    Err(error) => op.on_transport_error(error)?,
                }
            }
        }
    }
}

// ---------------------------------------------------------------------- 文档构造与解析

/// 一页、每段文本一个 `NarrativeText` 元素（Python `text_doc([...])`）。
pub fn doc(texts: &[&str]) -> ExpandedDocument {
    pages(&[texts])
}

/// 逐页给出文本列表（Python `text_doc(*pages)`）。
pub fn pages(pages: &[&[&str]]) -> ExpandedDocument {
    let pages: Vec<Value> = pages
        .iter()
        .map(|texts| {
            json!({"elements": texts
                .iter()
                .map(|text| json!({"category": "NarrativeText", "text": text}))
                .collect::<Vec<_>>()})
        })
        .collect();
    ExpandedDocument::from_value(
        &json!({"file_type": "md", "pages": pages}),
        dpe_sdk::CONTRACT,
    )
    .expect("测试文档必须合法")
}

/// 由 JSON 值构造展开文档。
pub fn from_value(value: Value) -> ExpandedDocument {
    ExpandedDocument::from_value(&value, dpe_sdk::CONTRACT).expect("测试文档必须合法")
}

pub fn doc_hash(document: &ExpandedDocument) -> String {
    document.doc_hash(dpe_sdk::CONTRACT).expect("文档 hash")
}

/// 请求里的 `Content-Range` 解析为 `(起点, 终点, 总量)`（Python `chunk_spans`）。
pub fn chunk_spans(requests: &[Request]) -> Vec<(u64, u64, u64)> {
    requests
        .iter()
        .filter_map(|request| request.headers.get("Content-Range"))
        .map(|value| {
            let (span, total) = value
                .strip_prefix("bytes ")
                .and_then(|rest| rest.split_once('/'))
                .expect("Content-Range 形如 bytes a-b/n");
            let (first, last) = span.split_once('-').expect("区间形如 a-b");
            (
                first.parse().unwrap(),
                last.parse().unwrap(),
                total.parse().unwrap(),
            )
        })
        .collect()
}

/// 请求里出现过的暂存会话 id（`staging/{sid}/…` 的第二段，Python `session_ids`）。
pub fn session_ids(requests: &[Request]) -> std::collections::BTreeSet<String> {
    requests
        .iter()
        .filter_map(|request| request.target.strip_prefix("staging/"))
        .filter_map(|rest| rest.split('/').next())
        .map(str::to_owned)
        .collect()
}

/// blob 分块上传中从 `first` 字节起的那一块（`PUT …/blobs/…` 且 `Content-Range: bytes first-`）。
pub fn is_blob_chunk_from(request: &Request, first: u64) -> bool {
    request.method.as_str() == "PUT"
        && request.target.contains("/blobs/")
        && request
            .headers
            .get("Content-Range")
            .is_some_and(|value| value.starts_with(&format!("bytes {first}-")))
}

/// Python 测试共用的 250 字节图片字节（`b"\x89PNG" + b"x" * 246`）。
pub fn image_bytes() -> Vec<u8> {
    let mut bytes = b"\x89PNG".to_vec();
    bytes.extend([b'x'; 246]);
    bytes
}

/// 请求体的 JSON（体不是 JSON 时为 `Null`）。
pub fn body_json(request: &Request) -> Value {
    request
        .body_bytes()
        .ok()
        .flatten()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or(Value::Null)
}
