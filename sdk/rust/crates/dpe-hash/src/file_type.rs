//! file_type 的语法校验（core.md §2.5）：唯一实现，各服务端实现直接引用。
//!
//! file_type 是开放取值（推荐登记表见 [`RECOMMENDED_FILE_TYPES`](crate::RECOMMENDED_FILE_TYPES)）：
//! 协议对取值的唯一约束是语法 `^[a-z0-9][a-z0-9_]{0,31}$`——ASCII 小写字母、数字与下划线，
//! 首字符是字母或数字，长度不超过 32。接收方 MUST NOT 因取值未登记而拒收；语法校验因此与
//! 推荐表无关，未登记的取值同样合法。

use crate::error::{Error, ErrorKind, Result};

/// 语法（core.md §2.5）的长度上界。
const MAX_LEN: usize = 32;

/// `value` 是否满足 core.md §2.5 的 file_type 语法。只看语法，不看推荐登记表。
///
/// 按字节判定：合法字符集本来就是 ASCII，非 ASCII 字节必定使它返回 `false`，
/// 因此字节长度与字符长度在这里等价。
pub fn is_valid_file_type(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.len() > MAX_LEN {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

/// 校验 core.md §2.5 的 file_type 语法；不合法返回 [`ErrorKind::FileTypeInvalid`]
/// （`DPE_VALIDATION`，与 hash 入口对同一输入的结论一致）。
///
/// 入口形态供写入路径直接引用：只按语法校验，不按取值集合拒收。
pub fn validate_file_type(value: &str) -> Result<()> {
    if !is_valid_file_type(value) {
        return Err(Error::new(
            ErrorKind::FileTypeInvalid,
            format!("file_type 不合语法：{value:?}"),
            "",
        ));
    }
    Ok(())
}
