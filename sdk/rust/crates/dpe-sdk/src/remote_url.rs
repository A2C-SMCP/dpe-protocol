//! remote URL 的文法（HTTP 绑定 §1）：判定实现在 dpe-hash（[`dpe_hash::check_remote_url`]），
//! 此处重新导出，传输层与实例定义经本模块调用（与 Python 的 `dpe_sdk.remote_url` 对等，#181）。

pub use dpe_hash::{check_remote_url, RemoteUrlError};
