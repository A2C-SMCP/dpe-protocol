//! 公开错误：调用方按 [`ErrorKind`] 或 [`Error::code`] 分派，不解析消息字符串。
//!
//! `code` 取规范错误码（core.md §6），服务端可直接映射为 problem 体；`path` 是出错位置的
//! RFC 6901 JSON Pointer，相对于传入函数的那个对象（如 `/pages/0/elements/2/text_as_html`），
//! 出错位置就是对象本身时为空串。与 Python dpe-hash 的异常子类一一对应。

use std::fmt;

/// 错误类别，与 Python dpe-hash 的异常子类一一对应（规范错误码见 [`ErrorKind::code`]）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    /// 输入不合法（`DPE_VALIDATION`）：类型错误、非法 hash / blob 引用、契约混用等。
    /// 对应 Python `ValidationError`。
    Validation,
    /// 封闭 schema 违例：对象出现规范未定义（或其 category 未允许）的字段（core.md §2）。
    /// 码为 `DPE_VALIDATION`，对应 Python `UndefinedFieldError`。
    UndefinedField,
    /// file_type 不合 core.md §2.5 的语法（取值不在推荐表内不算违例）。码为 `DPE_VALIDATION`，
    /// 对应 Python `FileTypeInvalidError`。
    FileTypeInvalid,
    /// 报文不是 I-JSON：字符串或对象键含孤立代理项（core §2.8 第 0 步）。码为
    /// `DPE_VALIDATION`，对应 Python `InvalidUnicodeError`。
    ///
    /// Rust 的 `str` 无法承载孤立代理项（serde_json 在解析阶段即拒绝），本 crate 的入口
    /// 不会产生该类别；保留它只为与 Python 的异常子类一一对应。
    InvalidUnicode,
    /// 整数字面量绝对值超过 2^53−1（契约 1 §3.3、core.md §2.6）。码为 `DPE_VALIDATION`，
    /// 对应 Python `IntegerOutOfRangeError`。
    IntegerOutOfRange,
    /// category 不在契约的封闭枚举内（`DPE_CATEGORY_UNKNOWN`，契约 1 §4）。
    CategoryUnknown,
    /// hash 契约不被支持，或 hash 值缺少 / 带未知契约前缀（`DPE_CONTRACT_UNSUPPORTED`）。
    ContractUnsupported,
}

impl ErrorKind {
    /// 规范错误码（core.md §6）。
    pub fn code(self) -> &'static str {
        match self {
            ErrorKind::CategoryUnknown => "DPE_CATEGORY_UNKNOWN",
            ErrorKind::ContractUnsupported => "DPE_CONTRACT_UNSUPPORTED",
            ErrorKind::Validation
            | ErrorKind::UndefinedField
            | ErrorKind::FileTypeInvalid
            | ErrorKind::InvalidUnicode
            | ErrorKind::IntegerOutOfRange => "DPE_VALIDATION",
        }
    }
}

/// dpe-hash 的错误：类别、可读消息与出错位置。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    kind: ErrorKind,
    message: String,
    path: String,
}

impl Error {
    pub(crate) fn new(
        kind: ErrorKind,
        message: impl Into<String>,
        path: impl Into<String>,
    ) -> Self {
        Error {
            kind,
            message: message.into(),
            path: path.into(),
        }
    }

    /// 把相对路径挂到 `prefix` 之下（内部先按相对位置报错，出错时才拼出完整路径，
    /// 免得在合法输入的热路径上为每个元素、每个子 hash 预先拼路径）。
    ///
    /// 约定：内部函数的 `at` 参数一律是绝对路径；只有热路径上的调用传 `""`，且必须紧跟
    /// `.map_err(|e| e.under(..))` 补上前缀。对已传绝对 `at` 的调用再接 `under` 会重复前缀。
    pub(crate) fn under(mut self, prefix: &str) -> Self {
        self.path.insert_str(0, prefix);
        self
    }

    /// 错误类别。
    pub fn kind(&self) -> ErrorKind {
        self.kind
    }

    /// 规范错误码（core.md §6），如 `DPE_VALIDATION`。
    pub fn code(&self) -> &'static str {
        self.kind.code()
    }

    /// 可读消息（不含位置）。
    pub fn message(&self) -> &str {
        &self.message
    }

    /// 出错位置的 RFC 6901 JSON Pointer，相对于传入的对象；对象本身为 `""`。
    pub fn path(&self) -> &str {
        &self.path
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.path.is_empty() {
            write!(f, "{}", self.message)
        } else {
            write!(f, "{}（位置 {}）", self.message, self.path)
        }
    }
}

impl std::error::Error for Error {}

/// 本 crate 的 `Result` 别名。
pub type Result<T, E = Error> = std::result::Result<T, E>;
