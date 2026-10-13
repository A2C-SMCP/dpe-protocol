//! 媒体来源适配（#24）：纯计算部分，与 Python 侧 `tests/test_media.py` 逐条对等。
//!
//! 引用与 `dpe_hash::blob_ref`（规范实现）对照；读取一律经 `BlobSource::read`（核心解析请求体
//! 时走的同一入口）。经核心分块上传、续传与惰性的场景见 `tests/media_staging.rs`。

use std::io::{self, Cursor, Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use dpe_sdk::media::{
    source_from_path, source_from_read_blob, source_from_stream, DEFAULT_CHUNK_BYTES,
};

fn media_bytes(size: usize) -> Vec<u8> {
    (0..size).map(|i| (i % 251) as u8).collect()
}

/// 测试用临时文件（进程内唯一名字，析构时删除）。
struct TempFile(PathBuf);

impl TempFile {
    fn with(data: &[u8]) -> TempFile {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "dpe-sdk-media-{}-{}.bin",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, data).expect("写临时文件");
        TempFile(path)
    }
}

impl Drop for TempFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

#[test]
fn default_chunk_is_one_mebibyte() {
    assert_eq!(DEFAULT_CHUNK_BYTES, 1 << 20);
}

#[test]
fn source_from_path_matches_the_normative_ref_and_serves_offsets() {
    let data = media_bytes(1000);
    let file = TempFile::with(&data);

    let source = source_from_path(&file.0, 64).unwrap();

    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(&data));
    assert_eq!(source.size(), 1000);
    assert_eq!(source.read(0, 10).unwrap(), data[..10]);
    assert_eq!(source.read(500, 300).unwrap(), data[500..800]);
    assert_eq!(source.read(990, 10).unwrap(), data[990..]);
    // 越过末尾：短读（长度不符由核心判定）
    assert_eq!(source.read(995, 10).unwrap(), data[995..]);
}

#[test]
fn source_from_path_of_an_empty_file() {
    let file = TempFile::with(b"");
    let source = source_from_path(&file.0, 64).unwrap();
    assert_eq!(
        (source.blob_ref(), source.size()),
        (dpe_hash::blob_ref(b"").as_str(), 0)
    );
}

#[test]
fn source_from_path_reports_a_missing_file() {
    let error = source_from_path("/definitely/not/here.bin", 64).expect_err("文件不存在");
    assert_eq!(error.kind(), io::ErrorKind::NotFound);
}

#[test]
fn source_from_path_rereads_the_file_so_changes_are_not_hidden() {
    // 读取器按偏移重新打开文件：投递期间文件变化会被读到（交给服务端的 HASH_MISMATCH 或核心的
    // 长度判定），而不是读到构造时的旧快照
    let file = TempFile::with(&media_bytes(100));
    let source = source_from_path(&file.0, 64).unwrap();
    std::fs::write(&file.0, vec![7u8; 40]).unwrap();
    assert_eq!(source.read(0, 100).unwrap(), vec![7u8; 40]);
}

/// 记录每次 `read` 请求缓冲大小的 `Read + Seek` 包装（计数型断言：哈希只做有界读）。
struct Counting {
    inner: Cursor<Vec<u8>>,
    sizes: Arc<Mutex<Vec<usize>>>,
}

impl Read for Counting {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.sizes.lock().unwrap().push(buf.len());
        self.inner.read(buf)
    }
}

impl Seek for Counting {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.inner.seek(pos)
    }
}

#[test]
fn source_from_stream_hashes_in_bounded_chunks_and_serves_offsets() {
    let data = media_bytes(1000);
    let sizes = Arc::new(Mutex::new(Vec::new()));
    let mut cursor = Cursor::new(data.clone());
    cursor.seek(SeekFrom::Start(300)).unwrap(); // 起始位置不在开头：从头哈希
    let stream = Counting {
        inner: cursor,
        sizes: Arc::clone(&sizes),
    };

    let source = source_from_stream(stream, 128).unwrap();

    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(&data));
    assert_eq!(source.size(), 1000);
    let hash_reads = sizes.lock().unwrap().clone();
    assert!(
        !hash_reads.is_empty() && hash_reads.iter().all(|&size| size == 128),
        "{hash_reads:?}"
    );
    assert_eq!(source.read(250, 100).unwrap(), data[250..350]);
    assert_eq!(source.read(0, 5).unwrap(), data[..5]);
}

/// `seek` 一律失败的流（管道之类）。
struct Seekless(Cursor<Vec<u8>>);

impl Read for Seekless {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}

impl Seek for Seekless {
    fn seek(&mut self, _pos: SeekFrom) -> io::Result<u64> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "illegal seek"))
    }
}

#[test]
fn source_from_stream_rejects_seekless_streams() {
    let Err(error) = source_from_stream(Seekless(Cursor::new(vec![1, 2, 3])), 64) else {
        panic!("不可 seek 的流应被拒绝");
    };
    assert_eq!(
        error.kind(),
        io::ErrorKind::InvalidInput,
        "调用方给错了来源"
    );
    assert!(error.to_string().contains("可 seek"), "{error}");
    let cause = std::error::Error::source(error.get_ref().expect("带包装的错误"))
        .expect("原错误经 source() 取回");
    assert_eq!(
        cause.downcast_ref::<io::Error>().map(io::Error::kind),
        Some(io::ErrorKind::Unsupported)
    );
    assert!(
        error.to_string().contains("illegal seek"),
        "带原因：{error}"
    );
}

/// `read_blob` 的调用记录：每次 `(offset, max_bytes)`。
type Calls = Arc<Mutex<Vec<(u64, u64)>>>;

/// 按 §6.7 形状服务一段字节，调用记入 `calls`。
fn read_blob_over(
    data: Vec<u8>,
    calls: &Calls,
) -> impl Fn(u64, u64) -> io::Result<(Vec<u8>, bool)> + Send + Sync + 'static {
    let log = Arc::clone(calls);
    move |offset: u64, max_bytes: u64| {
        log.lock().unwrap().push((offset, max_bytes));
        let start = (offset as usize).min(data.len());
        let end = (start + max_bytes as usize).min(data.len());
        Ok((data[start..end].to_vec(), end >= data.len()))
    }
}

#[test]
fn source_from_read_blob_hashes_and_serves_offsets() {
    let data = media_bytes(601);
    let calls = Calls::default();
    let read = read_blob_over(data.clone(), &calls);

    let source = source_from_read_blob(read, Some(601), 64).unwrap();

    assert_eq!(source.blob_ref(), dpe_hash::blob_ref(&data));
    assert_eq!(source.size(), 601);
    let hash_pass = calls.lock().unwrap().clone();
    assert_eq!(hash_pass.len(), 601usize.div_ceil(64), "一趟覆盖全部字节");
    assert!(hash_pass.iter().all(|&(_, max_bytes)| max_bytes == 64));
    // 第二趟：按偏移请求同一份快照，每次至多 chunk_bytes
    assert_eq!(source.read(100, 150).unwrap(), data[100..250]);
    let range_calls = calls.lock().unwrap()[hash_pass.len()..].to_vec();
    assert_eq!(range_calls, [(100, 64), (164, 64), (228, 22)]);
    // 边界：读到末尾、越过末尾（空块 + eof → 空）、零长度（不调用回调）
    assert_eq!(source.read(596, 5).unwrap(), data[596..]);
    assert_eq!(source.read(601, 10).unwrap(), Vec::<u8>::new());
    let before = calls.lock().unwrap().len();
    assert_eq!(source.read(0, 0).unwrap(), Vec::<u8>::new());
    assert_eq!(calls.lock().unwrap().len(), before, "零长度不调用回调");
}

#[test]
fn source_from_read_blob_without_expected_size() {
    let data = media_bytes(10);
    let read = read_blob_over(data.clone(), &Calls::default());
    let source = source_from_read_blob(read, None, 4).unwrap();
    assert_eq!(
        (source.blob_ref(), source.size()),
        (dpe_hash::blob_ref(&data).as_str(), 10)
    );
}

fn invalid(result: io::Result<dpe_sdk::protocol::BlobSource>) -> io::Error {
    match result {
        Ok(_) => panic!("应被拒绝"),
        Err(error) => error,
    }
}

#[test]
fn source_from_read_blob_rejects_contract_violations() {
    // 单次返回超过 max_bytes
    let error = invalid(source_from_read_blob(
        |_, max_bytes| Ok((vec![0; max_bytes as usize + 1], true)),
        None,
        8,
    ));
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("返回了"), "{error}");

    // 空块不带 eof
    let error = invalid(source_from_read_blob(
        |_, _| Ok((Vec::new(), false)),
        None,
        8,
    ));
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("eof"), "{error}");

    // 实算总长与声明不符（短于声明）
    let read = read_blob_over(media_bytes(10), &Calls::default());
    let error = invalid(source_from_read_blob(read, Some(11), 4));
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("与声明的"), "{error}");

    // 读超声明：不等 eof 即早停（回调始终不返回 eof 的违约来源）
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    let endless = move |_: u64, max_bytes: u64| {
        counter.fetch_add(1, Ordering::SeqCst);
        Ok((vec![1; max_bytes as usize], false))
    };
    let error = invalid(source_from_read_blob(endless, Some(10), 4));
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("超过声明的"), "{error}");
    assert_eq!(calls.load(Ordering::SeqCst), 3, "读到 12 > 10 即停");

    // 回调自身的 I/O 错误原样透传
    let error = invalid(source_from_read_blob(
        |_, _| Err(io::Error::new(io::ErrorKind::TimedOut, "slow")),
        None,
        8,
    ));
    assert_eq!(error.kind(), io::ErrorKind::TimedOut);
}

#[test]
fn chunk_bytes_must_be_positive() {
    let file = TempFile::with(b"abc");
    let errors = [
        invalid(source_from_path(&file.0, 0)),
        invalid(source_from_stream(Cursor::new(vec![1u8]), 0)),
        invalid(source_from_read_blob(
            |_, _| Ok((Vec::new(), true)),
            None,
            0,
        )),
    ];
    for error in errors {
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(error.to_string().contains("chunk_bytes"), "{error}");
    }
}

#[test]
fn read_blob_serving_a_changed_snapshot_returns_a_short_block() {
    // 哈希之后快照变短：读取器在 eof 处停下、返回短块（核心按长度不符如实拒绝）
    let data = Arc::new(Mutex::new(media_bytes(100)));
    let shared = Arc::clone(&data);
    let read = move |offset: u64, max_bytes: u64| {
        let data = shared.lock().unwrap();
        let start = (offset as usize).min(data.len());
        let end = (start + max_bytes as usize).min(data.len());
        Ok((data[start..end].to_vec(), end >= data.len()))
    };
    let source = source_from_read_blob(read, Some(100), 16).unwrap();
    data.lock().unwrap().truncate(50);

    let block = source.read(40, 30).unwrap();

    assert_eq!(block.len(), 10);
}
