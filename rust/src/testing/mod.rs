//! 测试工具（`testing` feature）：内存版 Robot 服务端与文档一致性检查。

pub mod conformance;
mod fake_server;

pub use conformance::{check_documents, CheckOptions, ConformanceIssue, ConformanceReport};
pub use fake_server::{FakeRobotServer, FakeState, Fault, StoredDocument, COMMIT_PATH, NEGOTIATE_PATH};
