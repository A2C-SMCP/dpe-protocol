//! 工程骨架冒烟测试：能读到规范仓库根目录的一致性向量（不复制进 SDK）。
//!
//! #19 会在本 crate 上补齐全部 document / jcs / 拒绝类向量的逐字节校验。

use std::path::PathBuf;

/// 定位仓库的 `vectors/`：`DPE_VECTORS_DIR` 可覆盖（如在仓库之外运行测试），
/// 否则从 crate 目录逐级上溯找 `vectors/manifest.json`（与 Python SDK 的 conftest.py 对等）。
fn vectors_dir() -> PathBuf {
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

#[test]
fn reads_repository_vectors() {
    let manifest = std::fs::read_to_string(vectors_dir().join("manifest.json")).unwrap();
    let manifest: serde_json::Value = serde_json::from_str(&manifest).unwrap();
    assert_eq!(manifest["contract"], "dpe1");
}
