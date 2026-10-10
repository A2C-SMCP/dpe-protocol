//! file_type 的语法校验与推荐登记表（core.md §2.5）：开放取值，语法是唯一约束。

use dpe_hash::{
    doc_hash, is_valid_file_type, validate_file_type, ErrorKind, RECOMMENDED_FILE_TYPES,
};
use serde_json::json;

/// 恰 32 字符（长度上界，界内合法）；33 字符的版本在拒绝用例里。
const MAX_LENGTH: &str = "max_length_value_0123456789abcde";

#[test]
fn syntax_accepts() {
    for value in [
        "md",
        "a",
        "a1",
        "a_b_1",
        "3mf",
        "markdown",
        "custom_format_v2",
        MAX_LENGTH,
    ] {
        assert!(is_valid_file_type(value), "{value:?}");
        assert!(validate_file_type(value).is_ok(), "{value:?}");
    }
}

#[test]
fn syntax_rejects() {
    let too_long = format!("{MAX_LENGTH}f");
    for value in [
        "",
        "Markdown",        // 大写
        "git-repo",        // 连字符
        "_internal",       // 首字符是下划线
        too_long.as_str(), // 33 字符，超过长度上界
        "md ",             // 尾随空格
        "md\n",            // 尾随换行：全串匹配，不得按行尾锚点放过
        "märkdown",        // 非 ASCII
        "md.",             // 点不在字符集内
    ] {
        assert!(!is_valid_file_type(value), "{value:?}");
        let err = validate_file_type(value).expect_err(value);
        assert_eq!(err.kind(), ErrorKind::FileTypeInvalid, "{value:?}");
        assert_eq!(err.code(), "DPE_VALIDATION", "{value:?}");
    }
}

#[test]
fn recommended_table_is_syntactically_valid() {
    assert!(!RECOMMENDED_FILE_TYPES.is_empty());
    for value in RECOMMENDED_FILE_TYPES {
        assert!(is_valid_file_type(value), "{value}");
    }
}

#[test]
fn git_repo_registered_and_legacy_repos_kept() {
    // #95：新增 `git_repo`；`*_repo` 历史取值保留（不影响已存数据），新文档改用 `git_repo`。
    assert!(RECOMMENDED_FILE_TYPES.contains(&"git_repo"));
    for legacy in [
        "java_repo",
        "python_repo",
        "javascript_repo",
        "typescript_repo",
    ] {
        assert!(RECOMMENDED_FILE_TYPES.contains(&legacy), "{legacy}");
    }
}

#[test]
fn hash_accepts_unregistered_value_and_rejects_invalid_syntax() {
    let no_pages: [&str; 0] = [];
    let registered = doc_hash(&json!({"file_type": "md"}), &no_pages, "dpe1").unwrap();
    let unregistered =
        doc_hash(&json!({"file_type": "custom_format_v2"}), &no_pages, "dpe1").unwrap();
    assert_ne!(registered, unregistered);

    let err = doc_hash(&json!({"file_type": "Markdown"}), &no_pages, "dpe1").expect_err("Markdown");
    assert_eq!(err.kind(), ErrorKind::FileTypeInvalid);
    assert_eq!(err.code(), "DPE_VALIDATION");
    assert_eq!(err.path(), "/file_type");
}
