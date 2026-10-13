//! 媒体上传辅助（#24，与 Python `dpe_sdk.media` 对等）：把调用方显式给出的字节来源适配成
//! 核心要的 [`BlobSource`]。
//!
//! 只做**来源适配**：给定本地路径、可 seek 的字节流或 connector 契约 §6.7 的 `read_blob`
//! 回调（句柄由调用方绑定），算出 `sha256:` 引用与总长，并给出按任意偏移读取的读取器。分块、
//! 断点续传与「400 + `DPE-Upload-Offset` 重同步」都在核心（`ProtocolCore::upload_blob` /
//! `deliver` 的 `blobs` 参数，见 [`crate::protocol`]），本模块不重复实现；内存字节直接用
//! [`BlobSource::from_bytes`]。
//!
//! **不解析 url、不识别 metadata 里的路径字符串**：来源一律由调用方显式给出——元素对象的
//! `blob` 字段只认 `sha256:` 引用（core §2.3），url 的解引用与凭证属于 connector（契约 1
//! §4.2）。建议在 `deliver` 的 `blobs` 回调里才构造来源：未变文档不会要求 blob，来源（大文件、
//! `read_blob` 往返）也就不会被读取。
//!
//! **内存不随文件大小增长**：路径与流分块流式哈希（内存 O(`chunk_bytes`)）；`read_blob` 回调
//! 的来源分两趟——先分块读一遍算出摘要与总长，读取器再按偏移请求同一份快照（§6.7 保证句柄
//! 支持任意偏移、同一偏移重复读取结果一致），不整读、不落盘。摘要与总长在上传开始前确定，
//! 符合 §6.7 对运行器的约束（上传路径含 sha256、`Content-Range` 含 total）。
//!
//! **错误**：一律 [`std::io::Error`]（这一层是本地 I/O 适配，产物直接交给
//! [`BlobReader`](crate::protocol::BlobReader) 的 `io::Result`）：参数不合法（含不可 seek 的流）为
//! [`InvalidInput`](io::ErrorKind::InvalidInput)，`read_blob` 违反契约或总长与声明不符为
//! [`InvalidData`](io::ErrorKind::InvalidData)（对应 Python 侧的 `ValueError`），其余 I/O 错误
//! 原样透传（对应 `OSError`）。

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sha2::{Digest, Sha256};

use crate::protocol::{BlobReader, BlobSource};

/// 流式哈希与 `read_blob` 单次请求的缺省块大小（1 MiB，同 Python 侧）。
pub const DEFAULT_CHUNK_BYTES: usize = 1 << 20;

/// blob 引用的前缀（契约 1 §1）；与 `dpe_hash::blob_ref` 的等价性由测试钉住。
const BLOB_PREFIX: &str = "sha256:";

/// 本地路径 → [`BlobSource`]：分块流式哈希算出引用与总长（不整读进内存）。
///
/// 读取器每次按偏移重新打开文件读取：要求文件在投递期间保持不变，内容变化会以
/// `DPE_HASH_MISMATCH` 如实上报（core §3.4），变短则被核心按「字节数与声明的区间不符」拒绝，
/// 都不会被静默接受。
///
/// ```no_run
/// use dpe_sdk::http::DpeClient;
/// use dpe_sdk::media::{source_from_path, DEFAULT_CHUNK_BYTES};
/// use dpe_sdk::protocol::Precondition;
/// use dpe_sdk::{ExpandedDocument, CONTRACT};
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// let client = DpeClient::new("https://example.com/r/42")?;
/// let path = "figures/cover.png";
/// let reference = source_from_path(path, DEFAULT_CHUNK_BYTES)?.blob_ref().to_owned();
/// let document = ExpandedDocument::parse(
///     &format!(
///         r#"{{"file_type": "md", "pages": [{{"elements": [{{"category": "Image", "blob": "{reference}", "mime_type": "image/png"}}]}}]}}"#
///     ),
///     CONTRACT,
/// )?;
/// // 来源在 blobs 回调里才构造（未变文档不会要求 blob）；回调只能返回 Option，I/O 错误先存下来
/// let mut source_error = None;
/// let mut blobs = |_: &str| {
///     source_from_path(path, DEFAULT_CHUNK_BYTES)
///         .map_err(|error| source_error = Some(error))
///         .ok()
/// };
/// let delivered = client
///     .deliver("s3://bucket/a.md", &document, Precondition::IfAbsent, Some(&mut blobs), None)
///     .await;
/// if let Some(error) = source_error {
///     return Err(error.into());
/// }
/// delivered?;
/// # Ok(())
/// # }
/// ```
pub fn source_from_path(path: impl AsRef<Path>, chunk_bytes: usize) -> io::Result<BlobSource> {
    check_chunk(chunk_bytes)?;
    let path = path.as_ref().to_path_buf();
    let (blob_ref, size) = hash_reader(&mut File::open(&path)?, chunk_bytes)?;
    Ok(BlobSource::new(blob_ref, size, Arc::new(PathReader(path))))
}

/// 可 seek 的二进制流（`File`、`Cursor` 等）→ [`BlobSource`]。
///
/// 从流的开头分块流式哈希后委托 [`BlobSource::from_seekable`]：流须支持任意偏移的 seek /
/// read（读取发生在驱动方解析请求体时，异步驱动方放在工作线程）。`seek` 失败（管道、已失效的
/// 句柄）是调用方给错了来源，报 [`InvalidInput`](io::ErrorKind::InvalidInput)、原错误经
/// `source()` 取回（同 Python 的 `ValueError ... from exc`）；不可 seek 的来源请改用
/// [`source_from_read_blob`] 或先落为临时文件。
pub fn source_from_stream<R>(mut stream: R, chunk_bytes: usize) -> io::Result<BlobSource>
where
    R: Read + Seek + Send + 'static,
{
    check_chunk(chunk_bytes)?;
    stream
        .seek(SeekFrom::Start(0))
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, NotSeekable(error)))?;
    let (blob_ref, size) = hash_reader(&mut stream, chunk_bytes)?;
    Ok(BlobSource::from_seekable(stream, blob_ref, size))
}

/// connector 契约 §6.7 的 `read_blob`（句柄已绑定）→ [`BlobSource`]。
///
/// `read(offset, max_bytes)` 返回 `(data, eof)`：`data` 不超过 `max_bytes`，空 `data` 必带
/// `eof = true`；`offset` 等于或超过总长时返回空块 + `eof = true`。
///
/// 先分块读一遍算出引用与总长（§6.7：摘要与总长确定后才开始上传），读取器再按偏移请求同一份
/// 快照。`expected_size` 给出时校验实算总长与它一致（§6.7 的 `size` 一致性），回调违反契约
/// （单次返回超过 `max_bytes`、空块不带 `eof`）同样报 [`InvalidData`](io::ErrorKind::InvalidData)。
/// 读取中途提前到末尾（快照变化）时读取器返回短块，由核心按「字节数与声明的区间不符」如实
/// 拒绝。
///
/// **句柄声明了 `size` 时（契约 §6.6）请一律传 `expected_size`**：既做 §6.7 的一致性校验，
/// 也让「回调始终不返回 `eof`」的违约来源在总长超过声明时立即失败；不带时本地没有上界可判，
/// 这类来源只能靠调用方自己的读取超时兜底（§6.7 把超时职责放在运行器）。
pub fn source_from_read_blob<F>(
    read: F,
    expected_size: Option<u64>,
    chunk_bytes: usize,
) -> io::Result<BlobSource>
where
    F: Fn(u64, u64) -> io::Result<(Vec<u8>, bool)> + Send + Sync + 'static,
{
    check_chunk(chunk_bytes)?;
    let chunk = chunk_bytes as u64;
    let mut hasher = Sha256::new();
    let mut total: u64 = 0;
    loop {
        let (data, eof) = read_once(&read, total, chunk)?;
        hasher.update(&data);
        total += data.len() as u64;
        if let Some(expected) = expected_size {
            if total > expected {
                return Err(invalid_data(format!(
                    "read_blob 读到的总长已超过声明的 {expected}"
                )));
            }
        }
        if eof {
            break;
        }
    }
    if let Some(expected) = expected_size {
        if total != expected {
            return Err(invalid_data(format!(
                "read_blob 读到的总长 {total} 与声明的 {expected} 不符"
            )));
        }
    }
    let blob_ref = format!("{BLOB_PREFIX}{}", hex(&hasher.finalize()));
    let reader = ReadBlobReader { read, chunk };
    Ok(BlobSource::new(blob_ref, total, Arc::new(reader)))
}

/// 流不可 seek：调用方给错了来源（`source()` 是 seek 的原错误）。
#[derive(Debug)]
struct NotSeekable(io::Error);

impl std::fmt::Display for NotSeekable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "流必须可 seek（任意偏移读取）；不可 seek 的流改用 read_blob 回调或先落为临时文件：{}",
            self.0
        )
    }
}

impl std::error::Error for NotSeekable {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.0)
    }
}

/// 按偏移重新打开文件读取的读取器（路径来源）。
struct PathReader(PathBuf);

impl BlobReader for PathReader {
    fn read(&self, offset: u64, length: u64) -> io::Result<Vec<u8>> {
        let mut file = File::open(&self.0)?;
        file.seek(SeekFrom::Start(offset))?;
        // 读满或到末尾（短读交给核心判定）；容量按实际读到的增长，不按声明长度预分配
        let mut block = Vec::new();
        file.take(length).read_to_end(&mut block)?;
        Ok(block)
    }
}

/// `read_blob` 回调的读取器：按偏移分块请求同一份快照（每次至多 `chunk` 字节）。
struct ReadBlobReader<F> {
    read: F,
    chunk: u64,
}

impl<F> BlobReader for ReadBlobReader<F>
where
    F: Fn(u64, u64) -> io::Result<(Vec<u8>, bool)> + Send + Sync,
{
    fn read(&self, offset: u64, length: u64) -> io::Result<Vec<u8>> {
        let mut block = Vec::new();
        while (block.len() as u64) < length {
            let want = self.chunk.min(length - block.len() as u64);
            let (data, eof) = read_once(&self.read, offset + block.len() as u64, want)?;
            block.extend_from_slice(&data);
            if eof {
                break; // 快照提前结束：返回短块，由核心如实拒绝
            }
        }
        Ok(block)
    }
}

/// 分块流式哈希：内存 O(chunk_bytes)，不整读（路径与流共用）。
fn hash_reader<R: Read>(reader: &mut R, chunk_bytes: usize) -> io::Result<(String, u64)> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; chunk_bytes];
    let mut total: u64 = 0;
    loop {
        let read = match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        hasher.update(&buffer[..read]);
        total += read as u64;
    }
    Ok((format!("{BLOB_PREFIX}{}", hex(&hasher.finalize())), total))
}

/// 一次 `read_blob` 调用并校验其契约（`data` ≤ `max_bytes`；空块必带 `eof`）。
fn read_once<F>(read: &F, offset: u64, max_bytes: u64) -> io::Result<(Vec<u8>, bool)>
where
    F: Fn(u64, u64) -> io::Result<(Vec<u8>, bool)>,
{
    let (data, eof) = read(offset, max_bytes)?;
    if data.len() as u64 > max_bytes {
        return Err(invalid_data(format!(
            "read_blob 在偏移 {offset} 上请求至多 {max_bytes} 字节，返回了 {} 字节",
            data.len()
        )));
    }
    if data.is_empty() && !eof {
        return Err(invalid_data(format!(
            "read_blob 在偏移 {offset} 返回空块且 eof 为 false（空块必带 eof: true）"
        )));
    }
    Ok((data, eof))
}

fn check_chunk(chunk_bytes: usize) -> io::Result<()> {
    if chunk_bytes == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "chunk_bytes 必须是正整数，实际为 0",
        ));
    }
    Ok(())
}

fn invalid_data(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// 小写十六进制（与 `dpe_hash::blob_ref` 同形）。
fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut out, byte| {
            let _ = write!(out, "{byte:02x}");
            out
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 第一次 `read` 返回 `Interrupted`、之后照常读的读取器。
    struct InterruptedOnce {
        inner: io::Cursor<Vec<u8>>,
        interrupted: bool,
    }

    impl Read for InterruptedOnce {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            if !self.interrupted {
                self.interrupted = true;
                return Err(io::Error::new(io::ErrorKind::Interrupted, "signal"));
            }
            self.inner.read(buf)
        }
    }

    #[test]
    fn hash_reader_retries_interrupted_reads() {
        let data = b"interrupted but complete".to_vec();
        let mut reader = InterruptedOnce {
            inner: io::Cursor::new(data.clone()),
            interrupted: false,
        };
        let (blob_ref, size) = hash_reader(&mut reader, 8).unwrap();
        assert!(reader.interrupted);
        assert_eq!(
            (blob_ref, size),
            (dpe_hash::blob_ref(&data), data.len() as u64)
        );
    }
}
