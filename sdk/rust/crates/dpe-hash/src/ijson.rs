//! I-JSON 严格解析（RFC 7493；core §2.8 校验顺序第 0 步的解析部分）。
//!
//! serde_json 默认保留重复键的最后一个，本模块显式拒绝：先用只校验键、不构建值的 visitor
//! 扫描一遍，再用常规解析构建值。孤立代理项由 serde_json 在解析阶段拒绝（`str` 无法承载）。
//! 数值不在解析阶段拒绝（core §2.8 第 4 步）：`1e400` 这类越界字面量保留在值中，
//! 到校验第 4 步再在出错的值上报告（serde_json 已启用 `arbitrary_precision`）。
//!
//! 与 Python dpe-sdk 的 `_ijson.loads` 行为对等：违规同为 `DPE_VALIDATION`、位置为对象自身。
//! 独立的解析实现仍在 `tests/common/mod.rs`（只用于读向量夹具，不依赖本模块）。

use std::collections::HashSet;
use std::fmt;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::Value;

use crate::error::{Error, ErrorKind, Result};

/// 把 JSON 文本严格解析为值：拒绝重复键与孤立代理项（core §2.8 第 0 步）。
///
/// 不是合法 JSON（含孤立代理项、`NaN` 等非 JSON 数值）或违反 I-JSON 时返回
/// [`ErrorKind::Validation`]（`DPE_VALIDATION`，位置为对象自身 `""`）。
pub fn parse_ijson(text: &str) -> Result<Value> {
    match serde_json::from_str::<NoDuplicateKeys>(text) {
        Ok(_) => {}
        Err(e) if e.to_string().starts_with(DUPLICATE) => {
            return Err(Error::new(
                ErrorKind::Validation,
                "报文不是 I-JSON：同一对象内有重复的键",
                "",
            ));
        }
        Err(e) => return Err(not_json(&e)),
    }
    // 第一遍扫描已覆盖全部语法；此处不会失败，但仍按 Result 处理，不引入 panic 面
    serde_json::from_str(text).map_err(|e| not_json(&e))
}

fn not_json(e: &serde_json::Error) -> Error {
    Error::new(
        ErrorKind::Validation,
        format!("报文不是合法的 JSON：{e}"),
        "",
    )
}

const DUPLICATE: &str = "duplicate object key";

/// 只扫描语法、不构建值的解析目标；重复键时报 [`DUPLICATE`]。
struct NoDuplicateKeys;

impl<'de> Deserialize<'de> for NoDuplicateKeys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        deserializer.deserialize_any(NoDuplicateKeysVisitor)
    }
}

struct NoDuplicateKeysVisitor;

impl<'de> Visitor<'de> for NoDuplicateKeysVisitor {
    type Value = NoDuplicateKeys;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("JSON 值")
    }

    fn visit_bool<E>(self, _: bool) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_i64<E>(self, _: i64) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_u64<E>(self, _: u64) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_f64<E>(self, _: f64) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_str<E>(self, _: &str) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_unit<E>(self) -> std::result::Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }

    fn visit_seq<A: SeqAccess<'de>>(
        self,
        mut seq: A,
    ) -> std::result::Result<Self::Value, A::Error> {
        while seq.next_element::<NoDuplicateKeys>()?.is_some() {}
        Ok(NoDuplicateKeys)
    }

    // arbitrary_precision 下数字也以单键 map 的形式到达，同样走这里，无需特判
    fn visit_map<A: MapAccess<'de>>(
        self,
        mut map: A,
    ) -> std::result::Result<Self::Value, A::Error> {
        let mut seen = HashSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !seen.insert(key) {
                return Err(de::Error::custom(DUPLICATE));
            }
            map.next_value::<NoDuplicateKeys>()?;
        }
        Ok(NoDuplicateKeys)
    }
}
