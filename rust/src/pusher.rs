//! 有状态推送器：在 [`DpePushClient`] 之上记住每个 `file_uri` 的投递状态。
//!
//! 本 crate 以 **Document** 为边界：何时拉取、拉取哪些、如何编排，都由上层决定。
//! 推送器只负责两件与协议相关、且每个调用方都需要的事：
//!
//! - 自动携带上次投递成功时服务端返回的 `doc_hash`（作为 `base_doc_hash`）；
//! - 记录调用方给出的源侧 `fingerprint`，供 [`StatefulPusher::needs_push`] 判断是否可以跳过拉取。
//!
//! ```ignore
//! let pusher = StatefulPusher::new(&client, &state);
//! for item in feishu.list_changed().await? {
//!     let uri = make_dpe_uri("feishu-acme", &format!("wiki/{}", item.token))?;
//!     if pusher.needs_push(uri.as_str(), Some(&item.revision)).await? {
//!         pusher.push(&feishu.build_document(&item).await?, Some(item.revision.clone())).await?;
//!     }
//! }
//! ```

use chrono::Utc;

use crate::error::Result;
use crate::push::{DpePushClient, PushResult, Transport};
use crate::schema::Document;
use crate::state::{SyncRecord, SyncStateStore};

pub struct StatefulPusher<'a, T: Transport> {
    client: &'a DpePushClient<T>,
    state: &'a dyn SyncStateStore,
}

impl<'a, T: Transport> StatefulPusher<'a, T> {
    pub fn new(client: &'a DpePushClient<T>, state: &'a dyn SyncStateStore) -> Self {
        Self { client, state }
    }

    /// `fingerprint` 与上次成功投递时记录的一致即返回 `false`，调用方可跳过拉取与推送。
    /// `fingerprint` 为 `None` 表示调用方无法廉价判断变更，总是返回 `true`（由 hash 协商兜底）。
    pub async fn needs_push(&self, file_uri: &str, fingerprint: Option<&str>) -> Result<bool> {
        let Some(fingerprint) = fingerprint else { return Ok(true) };
        let previous = self.state.get(file_uri).await?;
        Ok(previous.and_then(|p| p.source_fingerprint).as_deref() != Some(fingerprint))
    }

    /// 投递完整文档，并在成功后记录服务端权威 `doc_hash` 与 `fingerprint`。
    pub async fn push(&self, doc: &Document, fingerprint: Option<String>) -> Result<PushResult> {
        let file_uri = doc.file_uri.to_string();
        let previous = self.state.get(&file_uri).await?;
        let result = self.client.push(doc, previous.map(|p| p.doc_hash)).await?;
        self.state
            .put(SyncRecord {
                file_uri,
                source_fingerprint: fingerprint,
                doc_hash: result.doc_hash.clone(),
                hash_strategy_uri: result.hash_strategy_uri.clone(),
                synced_at: Utc::now(),
            })
            .await?;
        Ok(result)
    }

    /// 清除本地状态（例如上层发现源文档已删除）。不会删除 Robot 中的文档：dpe-push/1 尚无删除语义。
    pub async fn forget(&self, file_uri: &str) -> Result<()> {
        self.state.delete(file_uri).await
    }
}
