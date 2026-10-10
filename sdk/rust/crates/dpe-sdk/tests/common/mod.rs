//! 测试辅助：定位并读取仓库根目录的一致性向量（不复制进 SDK），以及拒绝重复键与孤立代理项的
//! 严格解析（vectors/README.md 对 `pattern_json` / `value_json` 的约定）。与
//! `dpe-hash/tests/common/mod.rs` 对等——两个 crate 是独立发布的包，不共享测试代码。

#![allow(dead_code)] // 各测试文件只用到其中一部分

use std::collections::HashSet;
use std::fmt;
use std::path::PathBuf;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::Value;

/// 定位仓库的 `vectors/`：`DPE_VECTORS_DIR` 可覆盖（如在仓库之外运行测试），
/// 否则从 crate 目录逐级上溯找 `vectors/manifest.json`（与 Python SDK 的 conftest.py 对等）。
pub fn vectors_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("DPE_VECTORS_DIR") {
        let path = PathBuf::from(dir);
        assert!(
            path.join("manifest.json").is_file(),
            "DPE_VECTORS_DIR 下没有 manifest.json：{}",
            path.display()
        );
        return path;
    }
    let mut dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    loop {
        let candidate = dir.join("vectors");
        if candidate.join("manifest.json").is_file() {
            return candidate;
        }
        assert!(
            dir.pop(),
            "找不到仓库的 vectors/ 目录，请设置 DPE_VECTORS_DIR"
        );
    }
}

pub fn read(name: &str) -> Value {
    let path = vectors_dir().join(name);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// 严格解析器在解析阶段拒绝了输入（I-JSON：孤立代理项；或重复键）。
#[derive(Debug)]
pub struct Rejected;

/// 拒绝重复键与孤立代理项的严格解析。
///
/// 孤立代理项：`str` 无法承载，serde_json 解析时报错；这里先显式扫描 `\u` 转义判定，
/// 不依赖 serde_json 的报错文本。重复键：serde_json 默认保留最后一个，所以用只校验键、
/// 不构建值的 visitor 扫描一遍。其余解析错误（坏 JSON）直接 panic，不会被静默当作通过。
pub fn parse_strict(text: &str) -> Result<Value, Rejected> {
    if has_lone_surrogate_escape(text) {
        return Err(Rejected);
    }
    match serde_json::from_str::<NoDuplicateKeys>(text) {
        Ok(_) => {}
        Err(e) if e.to_string().starts_with(DUPLICATE) => return Err(Rejected),
        Err(e) => panic!("向量的 json 文本不是 JSON：{e}"),
    }
    Ok(serde_json::from_str(text).expect("已通过严格扫描"))
}

/// JSON 文本中是否有未配对的 UTF-16 代理项转义（`\uD800`–`\uDFFF`）。
fn has_lone_surrogate_escape(text: &str) -> bool {
    let bytes = text.as_bytes();
    let unit = |at: usize| -> Option<u16> {
        let hex = bytes.get(at..at + 6)?;
        (hex[0] == b'\\' && hex[1] == b'u')
            .then(|| u16::from_str_radix(std::str::from_utf8(&hex[2..]).ok()?, 16).ok())
            .flatten()
    };
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b'\\' {
            i += 1;
            continue;
        }
        match unit(i) {
            Some(0xD800..=0xDBFF) => match unit(i + 6) {
                Some(0xDC00..=0xDFFF) => i += 12,
                _ => return true,
            },
            Some(0xDC00..=0xDFFF) => return true,
            Some(_) => i += 6,
            None => i += 2, // 其他转义（含 `\\`）整体跳过
        }
    }
    false
}

const DUPLICATE: &str = "duplicate object key";

struct NoDuplicateKeys;

impl<'de> Deserialize<'de> for NoDuplicateKeys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(NoDuplicateKeysVisitor)
    }
}

struct NoDuplicateKeysVisitor;

impl<'de> Visitor<'de> for NoDuplicateKeysVisitor {
    type Value = NoDuplicateKeys;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("JSON 值")
    }

    fn visit_bool<E>(self, _: bool) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_i64<E>(self, _: i64) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_u64<E>(self, _: u64) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_f64<E>(self, _: f64) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_str<E>(self, _: &str) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }
    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(NoDuplicateKeys)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        while seq.next_element::<NoDuplicateKeys>()?.is_some() {}
        Ok(NoDuplicateKeys)
    }

    // arbitrary_precision 下数字也以单键 map 的形式到达，同样走这里，无需特判
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
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
