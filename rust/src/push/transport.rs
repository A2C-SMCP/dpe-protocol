//! 可替换的 HTTP 传输层。客户端只依赖 [`Transport`]，便于接入任意 HTTP 栈或内存实现。

use async_trait::async_trait;

use crate::error::DpeError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
}

#[derive(Debug, Clone)]
pub struct HttpRequest {
    pub method: Method,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
}

impl HttpRequest {
    /// 按名称（大小写不敏感）取请求头。
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }

    /// URL 中的路径部分。
    pub fn path(&self) -> &str {
        let after_scheme = self.url.split_once("://").map_or(self.url.as_str(), |(_, rest)| rest);
        after_scheme.find('/').map_or("/", |i| &after_scheme[i..])
    }
}

#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

#[async_trait]
pub trait Transport: Send + Sync {
    /// 发送请求。只有网络层失败返回 `Err`（会被重试）；任何 HTTP 状态码都应以 `Ok` 返回。
    async fn send(&self, request: HttpRequest) -> Result<HttpResponse, DpeError>;
}

#[async_trait]
impl<T: Transport + ?Sized> Transport for std::sync::Arc<T> {
    async fn send(&self, request: HttpRequest) -> Result<HttpResponse, DpeError> {
        (**self).send(request).await
    }
}

#[cfg(feature = "reqwest")]
pub use reqwest_impl::ReqwestTransport;

#[cfg(feature = "reqwest")]
mod reqwest_impl {
    use std::time::Duration;

    use async_trait::async_trait;

    use super::{HttpRequest, HttpResponse, Method, Transport};
    use crate::error::DpeError;

    #[derive(Debug, Clone)]
    pub struct ReqwestTransport {
        client: reqwest::Client,
    }

    impl ReqwestTransport {
        pub fn new(timeout: Duration) -> Result<Self, DpeError> {
            let client =
                reqwest::Client::builder().timeout(timeout).build().map_err(|e| DpeError::Transport(e.to_string()))?;
            Ok(Self { client })
        }

        pub fn from_client(client: reqwest::Client) -> Self {
            Self { client }
        }
    }

    #[async_trait]
    impl Transport for ReqwestTransport {
        async fn send(&self, request: HttpRequest) -> Result<HttpResponse, DpeError> {
            let method = match request.method {
                Method::Get => reqwest::Method::GET,
                Method::Post => reqwest::Method::POST,
            };
            let mut builder = self.client.request(method, &request.url);
            for (name, value) in &request.headers {
                builder = builder.header(name, value);
            }
            if let Some(body) = request.body {
                builder = builder.body(body);
            }
            let resp = builder.send().await.map_err(|e| DpeError::Transport(e.to_string()))?;
            let status = resp.status().as_u16();
            let headers = resp
                .headers()
                .iter()
                .filter_map(|(k, v)| v.to_str().ok().map(|v| (k.as_str().to_string(), v.to_string())))
                .collect();
            let body = resp.bytes().await.map_err(|e| DpeError::Transport(e.to_string()))?.to_vec();
            Ok(HttpResponse { status, headers, body })
        }
    }
}
