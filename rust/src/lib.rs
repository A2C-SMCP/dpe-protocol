//! dpe-protocol：DPE 投递的协议层，把上层产出的 DPE 文档通过 dpe-push/1 增量投递到 TFRobot。
//!
//! 本 crate 以 **Document** 为边界：飞书、网盘、三方系统等对接，以及何时拉取、拉取哪些，都由上层负责。
//!
//! 分层（自底向上），与 Python SDK 一一对应：
//!
//! - [`schema`]     DPE 三层数据模型（Document / DocPage / DocElement）
//! - [`hashing`]    hash-contract-v1 三层内容寻址 hash
//! - [`push`]       dpe-push/1 客户端（发现 → 协商 → 投递，含自动分片），传输层可替换
//! - [`uri`]        `dpe://{tenant}/{path}` 命名空间
//! - [`validation`] 文档级校验
//! - [`pusher`]     有状态推送器（自动 base_doc_hash、fingerprint 跳过）
//! - `testing`      内存版 Robot 服务端与文档一致性检查（需启用 `testing` feature）

pub mod error;
pub mod hashing;
pub mod push;
pub mod pusher;
pub mod schema;
pub mod state;
#[cfg(feature = "testing")]
pub mod testing;
pub mod uri;
pub mod validation;

pub use error::{DpeError, PushError, Result};
pub use push::{DpePushClient, PushResult, MEDIA_TYPE, PROTOCOL_VERSION};
pub use pusher::StatefulPusher;
pub use uri::make_dpe_uri;
pub use validation::check_document;
