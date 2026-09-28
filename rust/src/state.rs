//! 同步状态：记录每个 `file_uri` 上次成功投递的结果。
//!
//! [`JsonFileStateStore`] 的文件格式与 Python SDK 的 `JsonFileStateStore` 相同，两者可互读。

use std::collections::BTreeMap;
use std::path::PathBuf;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::error::Result;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SyncRecord {
    pub file_uri: String,
    /// 调用方给出的源侧变更标记，未变时调用方可跳过拉取（见 [`crate::StatefulPusher`]）
    #[serde(default)]
    pub source_fingerprint: Option<String>,
    /// 服务端返回的权威 doc_hash，下次投递作为 `base_doc_hash`
    pub doc_hash: String,
    pub hash_strategy_uri: String,
    pub synced_at: DateTime<Utc>,
}

#[async_trait]
pub trait SyncStateStore: Send + Sync {
    async fn get(&self, file_uri: &str) -> Result<Option<SyncRecord>>;
    async fn put(&self, record: SyncRecord) -> Result<()>;
    async fn delete(&self, file_uri: &str) -> Result<()>;
}

#[derive(Debug, Default)]
pub struct MemoryStateStore {
    records: Mutex<BTreeMap<String, SyncRecord>>,
}

#[async_trait]
impl SyncStateStore for MemoryStateStore {
    async fn get(&self, file_uri: &str) -> Result<Option<SyncRecord>> {
        Ok(self.records.lock().await.get(file_uri).cloned())
    }

    async fn put(&self, record: SyncRecord) -> Result<()> {
        self.records.lock().await.insert(record.file_uri.clone(), record);
        Ok(())
    }

    async fn delete(&self, file_uri: &str) -> Result<()> {
        self.records.lock().await.remove(file_uri);
        Ok(())
    }
}

/// 单文件 JSON 存储，适合单进程的本地 / 定时任务场景。每次写入原子替换整个文件。
#[derive(Debug)]
pub struct JsonFileStateStore {
    path: PathBuf,
    records: Mutex<Option<BTreeMap<String, SyncRecord>>>,
}

impl JsonFileStateStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into(), records: Mutex::new(None) }
    }

    async fn load(&self, slot: &mut Option<BTreeMap<String, SyncRecord>>) -> Result<()> {
        if slot.is_none() {
            *slot = Some(match tokio::fs::read(&self.path).await {
                Ok(raw) => serde_json::from_slice(&raw)?,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
                Err(e) => return Err(e.into()),
            });
        }
        Ok(())
    }

    async fn flush(&self, records: &BTreeMap<String, SyncRecord>) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let tmp = self.path.with_extension("json.tmp");
        tokio::fs::write(&tmp, serde_json::to_vec_pretty(records)?).await?;
        tokio::fs::rename(&tmp, &self.path).await?;
        Ok(())
    }
}

#[async_trait]
impl SyncStateStore for JsonFileStateStore {
    async fn get(&self, file_uri: &str) -> Result<Option<SyncRecord>> {
        let mut slot = self.records.lock().await;
        self.load(&mut slot).await?;
        Ok(slot.as_ref().and_then(|r| r.get(file_uri).cloned()))
    }

    async fn put(&self, record: SyncRecord) -> Result<()> {
        let mut slot = self.records.lock().await;
        self.load(&mut slot).await?;
        let records = slot.as_mut().expect("loaded");
        records.insert(record.file_uri.clone(), record);
        self.flush(records).await
    }

    async fn delete(&self, file_uri: &str) -> Result<()> {
        let mut slot = self.records.lock().await;
        self.load(&mut slot).await?;
        let records = slot.as_mut().expect("loaded");
        if records.remove(file_uri).is_some() {
            self.flush(records).await?;
        }
        Ok(())
    }
}
