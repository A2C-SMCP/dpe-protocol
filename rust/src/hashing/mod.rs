//! DPE 三层内容寻址 hash（hash-contract-v1 的符合性实现）与策略 URI。
//!
//! 策略 URI 形如 `hash-strategy://{name}?algo_version={v}[&k=v...]`，
//! 比较时按「名称 + 参数集合」判等，参数顺序不影响语义。

pub mod primitives;
mod v1;

use std::collections::{BTreeMap, HashSet};
use std::fmt;
use std::str::FromStr;

use url::Url;

use crate::error::{DpeError, Result};
use crate::schema::Document;

pub const HASH_STRATEGY_SCHEME: &str = "hash-strategy";

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct HashStrategyRef {
    pub name: String,
    /// 已按键排序
    pub params: Vec<(String, String)>,
}

impl HashStrategyRef {
    pub fn algo_version(&self) -> Option<&str> {
        self.params.iter().find(|(k, _)| k == "algo_version").map(|(_, v)| v.as_str())
    }

    pub fn default_v1() -> Self {
        Self { name: "default".into(), params: vec![("algo_version".into(), "v1".into())] }
    }

    pub fn to_uri(&self) -> String {
        let mut uri = format!("{HASH_STRATEGY_SCHEME}://{}", self.name);
        if !self.params.is_empty() {
            let query = url::form_urlencoded::Serializer::new(String::new()).extend_pairs(&self.params).finish();
            uri.push('?');
            uri.push_str(&query);
        }
        uri
    }
}

impl FromStr for HashStrategyRef {
    type Err = DpeError;

    fn from_str(uri: &str) -> Result<Self> {
        let parsed = Url::parse(uri).map_err(|e| DpeError::InvalidUri(format!("{uri:?}: {e}")))?;
        let name = parsed.host_str().filter(|h| !h.is_empty());
        match (parsed.scheme(), name) {
            (HASH_STRATEGY_SCHEME, Some(name)) => {
                let mut params: Vec<(String, String)> = parsed.query_pairs().into_owned().collect();
                params.sort();
                Ok(Self { name: name.to_string(), params })
            }
            _ => Err(DpeError::InvalidUri(format!("not a hash-strategy URI: {uri:?}"))),
        }
    }
}

impl fmt::Display for HashStrategyRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_uri())
    }
}

/// SDK 已实现的策略，用于与服务端能力文档求交集。
/// `image-source` 需要源文件字节，dpe-push/1 明确不支持，故不列入。
pub fn supported_hash_strategies() -> HashSet<HashStrategyRef> {
    HashSet::from([HashStrategyRef::default_v1()])
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageHashes {
    pub number: i64,
    pub title: Option<String>,
    pub page_hash: String,
    /// 与 `page.elements` 数组顺序一一对应
    pub content_hashes: Vec<String>,
}

/// 一次计算得到的三层 hash 骨架，始终携带其 `strategy`，防止跨策略误用。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentHashes {
    pub strategy: HashStrategyRef,
    pub doc_hash: String,
    /// 与 `document.pages` 数组顺序一一对应
    pub pages: Vec<PageHashes>,
}

impl DocumentHashes {
    pub fn all_content_hashes(&self) -> HashSet<&str> {
        self.pages.iter().flat_map(|p| p.content_hashes.iter().map(String::as_str)).collect()
    }
}

/// 按 `strategy` 分派到对应算法版本，计算三层 hash。
pub fn compute_hashes(doc: &Document, strategy: &HashStrategyRef) -> Result<DocumentHashes> {
    if !supported_hash_strategies().contains(strategy) {
        return Err(DpeError::UnsupportedHashStrategy(strategy.to_uri()));
    }
    let pages: Vec<PageHashes> = doc
        .pages
        .iter()
        .map(|page| {
            let content_hashes: Vec<String> = page.elements.iter().map(v1::element_hash).collect();
            PageHashes {
                number: page.number,
                title: page.title.clone(),
                page_hash: v1::page_hash(page.title.as_deref(), &content_hashes),
                content_hashes,
            }
        })
        .collect();
    let by_number: BTreeMap<i64, String> = pages.iter().map(|p| (p.number, p.page_hash.clone())).collect();
    Ok(DocumentHashes { strategy: strategy.clone(), doc_hash: v1::doc_hash(doc, &by_number), pages })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strategy_ref_equality_ignores_param_order() {
        let a: HashStrategyRef = "hash-strategy://tabular?algo_version=v3&include_coords=true".parse().unwrap();
        let b: HashStrategyRef = "hash-strategy://tabular?include_coords=true&algo_version=v3".parse().unwrap();
        assert_eq!(a, b);
        assert_eq!(a.algo_version(), Some("v3"));
        assert_eq!(HashStrategyRef::default_v1().to_uri(), "hash-strategy://default?algo_version=v1");
        assert!("https://default?algo_version=v1".parse::<HashStrategyRef>().is_err());
    }
}
