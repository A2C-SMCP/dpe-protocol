//! 增量推送器（#23，与 Python `dpe_sdk.pusher` 对等）：把一批文档变成「只推变化的部分」，
//! 本地状态只作缓存；删除与移动意图按 connector 契约 §7.2 执行与回报。
//!
//! **分层**：判定表、意图对账、状态维护与结果构造是纯逻辑（本模块不带 feature 门控的部分，
//! `--no-default-features` 下照常编译）；[`StatefulPusher`]（默认 feature `http`）建在
//! [`DpeClient`](crate::http::DpeClient) 与核心的 `deliver` / `batch_head` / `delete` /
//! `move_document` 之上，只做编排——CAS、412、响应丢失重发全部由核心与传输层承担，这里不另写
//! 一套，也**绝不自动 force**（move 本就不支持）。
//!
//! **缓存语义**（connector 契约 §7.2，本模块实现其机制、策略归调用方）：
//!
//! - **同步状态**：每篇文档最近一次确认的 remote doc_hash（类比 Git 的 remote-tracking ref），
//!   键为 core §1.1 规范化后的 file_uri；只作缓存，丢失时退化为以即时读到的 R 作 base、
//!   冲突保护只覆盖读写之间的竞态窗口，结果仍然正确。
//! - **意图日志**：写入请求发出前先持久化本次写入预期的 remote 状态——commit 记 `uri → N`、
//!   delete 记 `uri → 不存在`、move 同时记两侧 `from_uri → 不存在`、`to_uri → base_hash`——
//!   推送器**在 [`StateStore::save`] 返回 `Ok` 之后才发出写请求**（`save` 失败则整批中止、
//!   不发写）。结果一旦确定（成功、失败、含冲突）即清除对应意图；只有结果未知才保留——判据是
//!   核心的 [`Error::outcome_unknown`]：写请求本身任何一次尝试结果不确定（传输异常未带「确定
//!   未发出」底标、或无 DPE 错误码的任何响应——含校验失败的 2xx、未化解的裸 412、无码
//!   4xx/5xx）即未知，**包括「首发丢响应、重发得到 DPE 错误或建连失败」这类首发可能已生效的
//!   序列**。下一轮判定前先对账：R 等于待定意图值（含「不存在」）即视为本实例自己已生效的
//!   写入，按意图更新同步状态后再查判定表。
//! - **条目维护**：成功或已达成时——commit 记 `uri → doc_hash`；delete 删除该条目；move 把
//!   条目从 `from_uri` 迁到 `to_uri`（值不变）。冲突与失败不更新。
//! - **按 remote 隔离**：状态经 [`StateStore`] 以 remote 为命名空间存取，同一 store 服务多个
//!   remote 也不会串；缺省 [`MemoryStateStore`] 只在进程内、**不保证崩溃持久**（持久化实现
//!   属于运行器的状态目录，connector §4.4）。
//!
//! **前置条件判定表**（L = 同步状态，R = `batch_head` 读到的 remote 值，N = 本地内容 doc_hash；
//! `expected_base` 存在时覆盖 L，值为 `None` 即调用方确认「不存在」）：
//!
//! - commit：R == N → 已达成，跳过（[`PushStatus::Achieved`]）——这一行先于 `expected_base`
//!   的判定：内容已相同，即使调用方声明「不存在」也记 achieved；无 L（含契约不一致的条目按
//!   缺失）、R 不存在 → `if_absent`；R 存在 → 以 R 作 base；有 L、R == L → 以 L 作 base；
//!   有 L、R ≠ L 或 R 不存在；以及 `expected_base` 声明「不存在」而 R 存在 → **冲突**，不发写、
//!   如实回报（不自动重提；上层可用 `expected_base` 覆盖后重提）。
//! - delete / move 用同一张表（move 取 `from_uri` 的 L 与 R，并以 `head(to_uri)` 判已达成）：
//!   - delete：R 不存在 → 目标状态已达成（`deleted`，删条目、不发写）；无 L、R 存在 → 以 R
//!     作 base 删除；有 L、R == L → 以 L 作 base 删除；有 L、R ≠ L → **冲突**；写后服务端报
//!     「文档不存在」同样计 `deleted`（已达成）。
//!   - move：源 R 不存在时读目标——`head(to_uri)` 等于 base 即 **已达成**（`moved`，迁条目、
//!     不发写），否则**条目失败**（源已不存在、目标未达成，§7.2）；源 R ≠ base → **冲突**
//!     （[`Side::From`]）；写后目标被占（`DPE_ALREADY_EXISTS`）→ **条目失败**（[`Side::To`]，
//!     §7.2 列为不可由策略重判定）、源被改写（412 前置条件不符）→ **冲突**（`Side::From`）。
//!     源可写入而目标 R 也等于 base（目标已持有同一内容，如失败轮已建出目标，#165）→ 不发
//!     move，以 base **删除源**、判 `moved`（§7.2）；删源遇 412 → 冲突（`Side::From`），遇
//!     「不存在」→ 已达成。
//!     源不存在、源无 L 且调用方未确认「不存在」时，以**目标侧的 L** 作 base 候选：move 成功后
//!     条目已从源迁到目标，迁移后的条目就是本实例完成过这次 move 的记录，重放据此判已达成，与
//!     服务端对同一请求的裁决一致（core §5.2 第 4 步）。
//! - 比较 MUST 在同一 hash 契约下进行：同步状态中与当前契约不一致的条目按缺失处理。
//!
//! **写后冲突的重读先套用「已达成」行**（§7.2 冲突处置表，与策略无关，#140）：写入得到 CAS
//! 冲突后推送器重读该 URI——commit 重读值等于本次内容 N 记 `achieved`（确认 L = N、清意图），
//! delete 重读不存在记 `deleted`（删条目、清意图），二者都带重读值与原 CAS 错误、不重提；重读
//! 本身失败时仍回报 `conflict`（不臆测）。move 不在此列：源被改写时源仍存在。
//!
//! 判定表判出的冲突（未发写）不产生新意图、也**不清除**既有待定意图：那条待定写入的结果仍然
//! 未知（R ≠ 意图值既兼容「未落地」也兼容「落地后被覆盖」），按 §7.2 只有结果确定才清除。
//!
//! **回报**（[`PushResult`]，每条意图/条目一条）：冲突与失败的 `error` 保留服务端原样的错误
//! （含 `code`），供运行器按 §7.1 填 `remote` 来源；仅当写前判定（未发写、无服务端裁决）时
//! 给出同码的本地构造（如「源已不存在、目标未达成」给 `DPE_NOT_FOUND`）。
//!
//! **边界（交给运行器 #52）**：本模块**不设 `retryable`、不在内部重试冲突**——§7.1 的
//! 「本可自动重新判定的冲突因超过重试上限而失败时取 true」由运行器按自己的轮内计数决定；
//! 推送器只如实上报单次结果。同一批 move 里任何 URI 在 from∪to 中至多出现一次（链式、共用
//! 目标的生效依赖顺序，connector §6.5），保序拆批归调用方。
//!
//! **批量读被拒时逐条定位**（#145，connector §7.1）：判定前的 `batch_head` 被确定、不可重试地
//! 拒绝（如其中一个 URI 不在授权前缀内）时，推送器对本批每个 URI 各 `head` 一次（并发至多
//! `concurrency`），只让读被拒的条目以该条 `Failed` 返回（`error` 是那次读的原错误，code 原样，
//! `side` 为 `None`），其余条目照常判定与写入；逐条读都通过（拒绝与具体 URI 无关）时，全部条目
//! 同样按逐条读到的值判定与写入（写入仍经 CAS）。两种情况都记一条 warn（`log` 门面，target
//! `dpe_sdk::pusher`，带原错误码与计数，不带远端文本），使对一切批量读都拒绝的服务端可被察觉。
//! 定位在任何写入与意图持久化之前完成。网络失败、可重试错误耗尽
//! 与不合规响应仍整批返回 `Err`（定位过程中遇到同样如此）。与 Python 侧行为对等。
//!
//! **快/暂存分界**：完全复用核心 `deliver` 的判据，本模块不引入任何阈值；`concurrency` 只限制
//! 并发度（写操作在**同一个任务内**并发执行，每条互不拖累，不经 `tokio::spawn`；三个操作的
//! future 本身是 `Send`，调用方可以整体交给 `tokio::spawn`）。

// 判定逻辑的唯一调用方是 `http` 下的驱动；关掉默认 feature 时它们照常编译（sans-IO 断言）
#![cfg_attr(not(feature = "http"), allow(dead_code))]

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::sync::Mutex;

use crate::errors::{Error, ErrorKind};
use crate::models::{hashes_from_value, ExpandedDocument};
use crate::protocol::{Precondition, PreparedDocument};
use crate::wire::{CommitResult, CommitStatus, Delta};

#[cfg(feature = "http")]
pub use self::driver::{StatefulPusher, DEFAULT_CONCURRENCY};

// ---------------------------------------------------------------------------
// 公开类型
// ---------------------------------------------------------------------------

/// 读到的一篇文档的 remote 状态：`doc_hash` 为 `None` 表示文档不存在。
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct RemoteState {
    pub doc_hash: Option<String>,
}

/// 推送器本地状态的一次快照。
///
/// - `synced`：同步状态（规范化 file_uri → 最近一次确认的 remote doc_hash）；
/// - `intents`：待定意图（规范化 file_uri → doc_hash 或 `None`＝「不存在」）。
///
/// store 实现经 [`StateSnapshot::new`]（或 `Default`）构造，字段可直接读写。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct StateSnapshot {
    pub synced: BTreeMap<String, String>,
    pub intents: BTreeMap<String, Option<String>>,
}

impl StateSnapshot {
    /// 由同步状态与待定意图构造（store 实现从持久化介质读回时用）。
    pub fn new(
        synced: BTreeMap<String, String>,
        intents: BTreeMap<String, Option<String>>,
    ) -> Self {
        StateSnapshot { synced, intents }
    }
}

/// [`StateStore`] 的错误：实现自定的任意错误（推送器包装为 [`ErrorKind::StateStore`]，
/// `source()` 可取回）。
pub type StoreError = Box<dyn std::error::Error + Send + Sync + 'static>;

/// 推送器状态的持久化缝（按 remote 命名空间存取）。
///
/// `save` 返回 `Ok` 即表示**这一批意图已经持久化**（按其实现承诺的强度），推送器保证在它返回
/// 之后才发出对应的写请求；实现负责在崩溃后仍能读回（落盘实现属运行器状态目录，§4.4）。
///
/// **同步调用约定**：推送器在异步方法里**同步**调用 `load` / `save`（与 Python 侧对等），
/// 调用期间占住当前 worker 线程（转交别的线程再同步等待也一样），实现应尽量缩短耗时。
/// 多线程运行时上，重 I/O（文件写入、`fsync`）可用 `tokio::task::block_in_place` 包住，让
/// 运行时把其他任务挪走；它在 current_thread 运行时上会 panic，那里无法避免阻塞。
pub trait StateStore: Send + Sync {
    /// 读回该 remote 的状态；从未保存过时返回 `Ok(None)`。
    fn load(&self, remote: &str) -> Result<Option<StateSnapshot>, StoreError>;

    /// 整批保存该 remote 的状态。
    fn save(&self, remote: &str, snapshot: &StateSnapshot) -> Result<(), StoreError>;
}

/// 进程内的缺省实现：**不保证崩溃持久**，仅供单次运行与测试使用。
#[derive(Debug, Default)]
pub struct MemoryStateStore {
    states: Mutex<HashMap<String, StateSnapshot>>,
}

impl MemoryStateStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl StateStore for MemoryStateStore {
    fn load(&self, remote: &str) -> Result<Option<StateSnapshot>, StoreError> {
        let states = self
            .states
            .lock()
            .map_err(|_| "MemoryStateStore 的锁已中毒")?;
        Ok(states.get(remote).cloned())
    }

    fn save(&self, remote: &str, snapshot: &StateSnapshot) -> Result<(), StoreError> {
        let mut states = self
            .states
            .lock()
            .map_err(|_| "MemoryStateStore 的锁已中毒")?;
        states.insert(remote.to_owned(), snapshot.clone());
        Ok(())
    }
}

/// 一条回报对应的操作。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum PushOp {
    Commit,
    Delete,
    Move,
}

impl PushOp {
    /// 拼写（同 Python 的 `PushOp`）。
    pub fn as_str(self) -> &'static str {
        match self {
            PushOp::Commit => "commit",
            PushOp::Delete => "delete",
            PushOp::Move => "move",
        }
    }
}

/// 一条回报的状态；`Deleted` / `Moved` 含按 core §5.2 视为已达成的情形（§7.3 计数口径）。
///
/// 来源：`Achieved`＝commit 判定表初判 R == N（推送器自己判定，跳过写入），或写后 CAS 冲突、
/// 重读值等于本次内容（带重读值与原 CAS 错误）；`Unchanged`＝如实转述服务端 commit 的结果
/// （本模块不自行产出）；`Created` / `Updated` 同；`Deleted` / `Moved`＝delete / move 成功，
/// 或目标状态已达成（含写后服务端报「不存在」的 delete、写后 CAS 冲突且重读不存在的 delete，
/// §7.2）；`Conflict`＝判定表判定，或服务端 CAS 裁决且重读不是目标状态（后者带重读到的
/// `remote`）；`Failed`＝失败（[`PushResult::unknown`] 区分结果是否已确定）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum PushStatus {
    Created,
    Updated,
    Unchanged,
    Achieved,
    Deleted,
    Moved,
    Conflict,
    Failed,
}

impl PushStatus {
    /// 拼写（同 Python 的 `PushStatus`）。
    pub fn as_str(self) -> &'static str {
        match self {
            PushStatus::Created => "created",
            PushStatus::Updated => "updated",
            PushStatus::Unchanged => "unchanged",
            PushStatus::Achieved => "achieved",
            PushStatus::Deleted => "deleted",
            PushStatus::Moved => "moved",
            PushStatus::Conflict => "conflict",
            PushStatus::Failed => "failed",
        }
    }
}

impl From<CommitStatus> for PushStatus {
    fn from(status: CommitStatus) -> Self {
        match status {
            CommitStatus::Created => PushStatus::Created,
            CommitStatus::Updated => PushStatus::Updated,
            CommitStatus::Unchanged => PushStatus::Unchanged,
        }
    }
}

/// move 的问题发生在源侧还是目标侧。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum Side {
    From,
    To,
}

impl Side {
    /// 拼写（同 Python 的 `ConflictSide`）。
    pub fn as_str(self) -> &'static str {
        match self {
            Side::From => "from",
            Side::To => "to",
        }
    }
}

/// 一条意图/条目的推送结果（按操作回报，connector 契约 §7.2）。
///
/// 每条回报对应一个意图/条目，含涉及的 URI：commit 与 delete 以 `file_uri` 标识，move 的源在
/// `file_uri`、目标在 `to_uri`。
///
/// - `precondition`：本次写入使用的前置条件（commit 的 `BaseHash` / `IfAbsent`，delete / move
///   的 `BaseHash`）；未发写时为 `None`；
/// - `remote`：判定表读到的（写后 CAS 冲突时重读到的）remote 状态——重读即已达成的
///   `Achieved` / `Deleted` 同样带重读值；`None` 表示未读到（正常写入成功、未发写的判定不
///   重读、或重读本身失败）；move 为源侧（目标被占时为重读到的目标侧）；
/// - `delta`：commit 成功时服务端给出的元素级差值；
/// - `to_uri`：move 专用——归一化后的目标 URI；
/// - `side`：move 专用——问题发生在源侧还是目标侧；非侧向问题为 `None`；
/// - `error`：失败/冲突时的错误（冲突时为裁决它的 CAS 错误，其 `code` 可原样上报，§7.1）。
///   **已达成也可能携带**：delete 的「服务端报文档不存在」、写后 CAS 冲突而重读即目标状态的
///   `Achieved` / `Deleted`，都带原服务端错误供归因——**成败一律看 `status`，不看 `error`
///   是否为空**；
/// - `remote_error`：冲突后重读失败的错误（此时 `remote` 为 `None`、`error` 仍是 CAS 错误）；
/// - `unknown`：结果未确定（响应丢失、请求可能已发出的传输失败等）——对应的待定意图被保留，
///   下一轮按意图对账恢复；为 `false` 的失败都是已确定的（服务端已裁决、确定未发出、或本地
///   判定未发出）。
///
/// 不实现 `Clone`：[`Error`] 携带的原始传输错误不可克隆。
#[derive(Debug)]
#[non_exhaustive]
pub struct PushResult {
    pub op: PushOp,
    pub file_uri: String,
    pub status: PushStatus,
    pub precondition: Option<Precondition>,
    pub delta: Option<Delta>,
    pub remote: Option<RemoteState>,
    pub to_uri: Option<String>,
    pub side: Option<Side>,
    pub error: Option<Error>,
    pub remote_error: Option<Error>,
    pub unknown: bool,
}

impl PushResult {
    fn new(op: PushOp, file_uri: &str, status: PushStatus) -> Self {
        PushResult {
            op,
            file_uri: file_uri.to_owned(),
            status,
            precondition: None,
            delta: None,
            remote: None,
            to_uri: None,
            side: None,
            error: None,
            remote_error: None,
            unknown: false,
        }
    }
}

// ---------------------------------------------------------------------------
// 纯逻辑：校验、判定表、意图对账、状态维护与结果构造（不做 I/O）
// ---------------------------------------------------------------------------

/// commit 的已校验输入条目（uri 已归一化）。
#[derive(Debug)]
pub(crate) struct Entry<'d> {
    pub(crate) uri: String,
    pub(crate) document: &'d ExpandedDocument,
    pub(crate) new_hash: String,
    pub(crate) prepared: PreparedDocument,
}

/// 判定阶段的动作。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Action {
    /// 已达成，跳过。
    Achieved,
    /// 发写。
    Write,
    /// 判定表判出的冲突（未发写）。
    Conflict,
    /// 判定判出的条目失败（未发写，如 move 的「源已不存在、目标未达成」）。
    Failed,
}

/// 一次写操作的判定结果（commit / delete / move 共用；字段按操作取用）。写后的结局在
/// [`Outcome`]。
#[derive(Debug)]
pub(crate) struct Plan<'d> {
    pub(crate) op: PushOp,
    pub(crate) action: Action,
    /// commit / delete 的文档 uri；move 的 from_uri（均归一化）。
    pub(crate) uri: String,
    /// 判定表读到的 R（move 为源侧）。
    pub(crate) remote: RemoteState,
    /// 本次写入使用的前置条件；未发写或判定失败为 `None`。
    pub(crate) precondition: Option<Precondition>,
    /// commit：展开文档（write 时投递）与本地内容 hash（已达成时确认 L=N）。
    pub(crate) document: Option<&'d ExpandedDocument>,
    pub(crate) new_hash: Option<String>,
    /// commit write：推送器预算的三层 hash 与 dump（242a 内部通道）。
    pub(crate) prepared: Option<PreparedDocument>,
    /// move：目标 uri（归一化）与本次使用的 base_hash（已达成 / 迁移条目时也用）。
    pub(crate) to_uri: Option<String>,
    pub(crate) base: Option<String>,
    /// move：本次 move 将达成的内容在请求声明契约下的 doc_hash（迁移条目、意图对账用；
    /// 旧契约的 base 不能用于这个值）。
    pub(crate) to_hash: Option<String>,
    /// move：base 是服务端仍接受的旧契约值，无法与 R 直接比较；写后目标被占时重读源与目标。
    pub(crate) cross_contract: bool,
    /// move：目标已持有与源相同的内容（目标 R == base）——不发 move，以 base 删除源
    /// （§7.2「move 的目标已持有同一内容」，#165）。
    pub(crate) delete_source: bool,
    /// `Action::Failed` 时的判定错误（未发写）。
    pub(crate) error: Option<Error>,
}

impl<'d> Plan<'d> {
    fn new(op: PushOp, action: Action, uri: &str, remote: RemoteState) -> Self {
        Plan {
            op,
            action,
            uri: uri.to_owned(),
            remote,
            precondition: None,
            document: None,
            new_hash: None,
            prepared: None,
            to_uri: None,
            base: None,
            to_hash: None,
            cross_contract: false,
            delete_source: false,
            error: None,
        }
    }
}

/// 出错时的归类。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OutcomeKind {
    /// 成功（无 error）。
    Ok,
    /// CAS 裁决。
    Conflict,
    /// 服务端报「不存在」的 delete（目标状态已达成）。
    Achieved,
    /// 其余失败（是否已确定见 `unknown`）。
    Failed,
}

/// 一次写入的执行结果（由驱动产出，`finish_*` 消费）。
#[derive(Debug)]
pub(crate) struct Outcome {
    pub(crate) result: Option<CommitResult>,
    pub(crate) error: Option<Error>,
    pub(crate) kind: OutcomeKind,
    pub(crate) unknown: bool,
    pub(crate) side: Option<Side>,
    /// 冲突后的重读结果；`None` 表示重读失败或未重读。
    pub(crate) remote: Option<RemoteState>,
    pub(crate) remote_error: Option<Error>,
    /// move 成功时目标文档在请求声明契约下的 doc_hash（旧契约 base 不能当这个值）。
    pub(crate) move_hash: Option<String>,
    /// 运行期为「目标已持有同一内容」改为删源（#165 的跨契约路径）。
    pub(crate) delete_source: bool,
}

impl Outcome {
    /// 写入成功（commit 带结果，delete / move 为 `None`）。
    pub(crate) fn ok(result: Option<CommitResult>) -> Self {
        Outcome {
            result,
            error: None,
            kind: OutcomeKind::Ok,
            unknown: false,
            side: None,
            remote: None,
            remote_error: None,
            move_hash: None,
            delete_source: false,
        }
    }

    /// 写入失败：`kind` 归类，其余字段按需以链式方法补。
    pub(crate) fn failure(error: Error, kind: OutcomeKind) -> Self {
        Outcome {
            result: None,
            error: Some(error),
            kind,
            unknown: false,
            side: None,
            remote: None,
            remote_error: None,
            move_hash: None,
            delete_source: false,
        }
    }

    /// 一切非冲突失败都按核心的结果标记归类：只有核心明确标记「确定未生效」才清除意图，
    /// 无标记一律按结果未知（保留）。
    pub(crate) fn undetermined(error: Error) -> Self {
        let unknown = error.outcome_unknown();
        Outcome {
            unknown,
            ..Outcome::failure(error, OutcomeKind::Failed)
        }
    }

    /// 删除的写后结局是否就是「文档不存在」这一目标状态：服务端裁决不存在，或 CAS 冲突后
    /// 重读不存在（§7.2 冲突处置表 delete 行；move 改为删源时同理，#165）。
    pub(crate) fn reached_absent(&self) -> bool {
        self.kind == OutcomeKind::Achieved
            || (self.kind == OutcomeKind::Conflict
                && self
                    .remote
                    .as_ref()
                    .is_some_and(|remote| remote.doc_hash.is_none()))
    }

    pub(crate) fn with_side(mut self, side: Side) -> Self {
        self.side = Some(side);
        self
    }

    pub(crate) fn with_move_hash(mut self, value: String) -> Self {
        self.move_hash = Some(value);
        self
    }

    pub(crate) fn with_delete_source(mut self) -> Self {
        self.delete_source = true;
        self
    }

    /// 附上重读结果（成功为 `remote`，失败为 `remote_error`）。
    pub(crate) fn with_reread(mut self, reread: Result<Option<String>, Error>) -> Self {
        match reread {
            Ok(doc_hash) => self.remote = Some(RemoteState { doc_hash }),
            Err(error) => self.remote_error = Some(error),
        }
        self
    }
}

/// 值是否属于本次会话支持的任何契约。
fn matches_any_contract(value: &str, contracts: &[&str]) -> bool {
    dpe_hash::parse_hash_with(value, contracts).is_ok()
}

/// 合法 hash 值的前缀契约（值已由 `check_base_hash` / `base_of` 校验过）。
fn contract_of(value: &str) -> &str {
    dpe_hash::parse_hash_with(value, dpe_hash::KNOWN_CONTRACTS)
        .map(|(contract, _)| contract)
        .expect("base_of 已保证值属于受支持契约")
}

/// core §1.1 规范化（错误按 dpe-hash 的同码协议错误上报，同核心各入口）。
fn normalize(uri: &str) -> Result<String, Error> {
    dpe_hash::normalize_file_uri(uri).map_err(|error| Error::from_hash_error(&error, ""))
}

/// 归一化后的 `expected_base`（file_uri → doc_hash 或 `None`＝确认「不存在」）。
pub(crate) type Expected = HashMap<String, Option<String>>;

/// 一批意图（file_uri → 预期的 remote doc_hash 或 `None`＝「不存在」）。
pub(crate) type Intents = BTreeMap<String, Option<String>>;

/// 推送器的内存状态（同步状态 + 待定意图）与全部纯逻辑；驱动负责持久化与 I/O。
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct PusherState {
    pub(crate) synced: BTreeMap<String, String>,
    pub(crate) intents: BTreeMap<String, Option<String>>,
}

impl PusherState {
    pub(crate) fn from_snapshot(snapshot: Option<StateSnapshot>) -> Self {
        let snapshot = snapshot.unwrap_or_default();
        PusherState {
            synced: snapshot.synced,
            intents: snapshot.intents,
        }
    }

    pub(crate) fn snapshot(&self) -> StateSnapshot {
        StateSnapshot {
            synced: self.synced.clone(),
            intents: self.intents.clone(),
        }
    }

    // ------------------------------------------------------------------
    // 批准备：本地校验与归一化（任何一项不合法都在发请求前整批拒绝）
    // ------------------------------------------------------------------

    /// commit 的输入校验：uri 文法（core §1.1）、归一化后重复、`expected_base` 的范围与取值。
    pub(crate) fn prepare<'d>(
        documents: &'d [(String, ExpandedDocument)],
        expected_base: &HashMap<String, Option<String>>,
        contract: &str,
        contracts: &[&str],
    ) -> Result<(Vec<Entry<'d>>, Expected), Error> {
        let mut entries = Vec::with_capacity(documents.len());
        let mut seen: HashMap<String, &str> = HashMap::new();
        for (uri, document) in documents {
            let normalized = normalize(uri)?;
            if let Some(previous) = seen.get(&normalized) {
                return Err(Error::invalid_argument(format!(
                    "归一化后重复的 file_uri：{previous:?} 与 {uri:?}"
                )));
            }
            let data = serde_json::to_value(document).expect("数据模型总能序列化");
            let hashes = hashes_from_value(&data, contract)?;
            let prepared = PreparedDocument {
                contract: contract.to_owned(),
                hashes,
                data,
            };
            seen.insert(normalized.clone(), uri);
            entries.push(Entry {
                uri: normalized,
                document,
                new_hash: prepared.hashes.doc_hash.clone(),
                prepared,
            });
        }
        let allowed: HashSet<String> = seen.into_keys().collect();
        let expected = Self::prepare_expected(expected_base, &allowed, contracts)?;
        Ok((entries, expected))
    }

    /// delete 的输入校验与归一化（同一口径：整批拒绝）。
    pub(crate) fn prepare_uris(
        uris: &[String],
        expected_base: &HashMap<String, Option<String>>,
        contracts: &[&str],
    ) -> Result<(Vec<String>, Expected), Error> {
        let mut normalized = Vec::with_capacity(uris.len());
        let mut seen: HashMap<String, &str> = HashMap::new();
        for uri in uris {
            let value = normalize(uri)?;
            if let Some(previous) = seen.get(&value) {
                return Err(Error::invalid_argument(format!(
                    "归一化后重复的 file_uri：{previous:?} 与 {uri:?}"
                )));
            }
            seen.insert(value.clone(), uri);
            normalized.push(value);
        }
        let allowed: HashSet<String> = seen.into_keys().collect();
        let expected = Self::prepare_expected(expected_base, &allowed, contracts)?;
        Ok((normalized, expected))
    }

    /// move 的输入校验与归一化；`expected_base` 以 from_uri 为键（§7.2 取源的 L）。
    ///
    /// 同一 URI 在本批的 from 与 to 合集中至多出现一次：链式（a→b、b→c）或共用目标（a→c、
    /// b→c）的生效依赖顺序（connector §6.5），而一批内的写请求并发执行，因此整批拒绝；保序
    /// 拆批归调用方（#52 运行器）。
    pub(crate) fn prepare_moves(
        moves: &[(String, String)],
        expected_base: &HashMap<String, Option<String>>,
        contracts: &[&str],
    ) -> Result<(Vec<(String, String)>, Expected), Error> {
        let mut pairs = Vec::with_capacity(moves.len());
        let mut sources: HashSet<String> = HashSet::new();
        let mut seen: HashMap<String, &str> = HashMap::new();
        for (from_uri, to_uri) in moves {
            let source = normalize(from_uri)?;
            let target = normalize(to_uri)?;
            if source == target {
                return Err(Error::invalid_argument(format!(
                    "move 的源与目标是同一篇文档：{source:?}"
                )));
            }
            for (value, raw) in [(&source, from_uri), (&target, to_uri)] {
                if let Some(previous) = seen.get(value) {
                    return Err(Error::invalid_argument(format!(
                        "同一批 move 中重复的 URI（归一化后）：{previous:?} 与 {raw:?}；\
                         链式或共用目标须由调用方保序拆批"
                    )));
                }
                seen.insert(value.clone(), raw);
            }
            sources.insert(source.clone());
            pairs.push((source, target));
        }
        let expected = Self::prepare_expected(expected_base, &sources, contracts)?;
        Ok((pairs, expected))
    }

    /// 校验 `expected_base`：键必须属于本次操作的目标，值必须是任一受支持契约的 hash 或
    /// `None`（过渡期内旧契约的同步状态值照常可用）。
    fn prepare_expected(
        expected_base: &HashMap<String, Option<String>>,
        allowed: &HashSet<String>,
        contracts: &[&str],
    ) -> Result<Expected, Error> {
        let mut expected = HashMap::with_capacity(expected_base.len());
        for (uri, value) in expected_base {
            let normalized = normalize(uri)?;
            if !allowed.contains(&normalized) {
                return Err(Error::invalid_argument(format!(
                    "expected_base 的 {uri:?} 不在本次操作的目标里（归一化后 {normalized:?}）"
                )));
            }
            if let Some(value) = value {
                if !matches_any_contract(value, contracts) {
                    return Err(Error::invalid_argument(format!(
                        "expected_base[{uri:?}] 不是受支持契约的 hash：{value:?}"
                    )));
                }
            }
            expected.insert(normalized, value.clone());
        }
        Ok(expected)
    }

    // ------------------------------------------------------------------
    // 判定：对账 → 解 L → 查表；三个操作共用对账与解 L
    // ------------------------------------------------------------------

    /// 意图对账（§7.2）：R 等于待定意图值（含「不存在」）＝ 本实例自己已生效的写入。
    pub(crate) fn reconcile(&mut self, uri: &str, head: Option<&str>) {
        if self.intents.get(uri).map(Option::as_deref) == Some(head) {
            match self.intents.remove(uri).flatten() {
                None => {
                    self.synced.remove(uri);
                }
                Some(confirmed) => {
                    self.synced.insert(uri.to_owned(), confirmed);
                }
            }
        }
    }

    /// 解出 L：`expected_base` 覆盖同步状态（`None` = 确认「不存在」）；服务端仍接受其契约的
    /// 旧条目照常作 L，其余按缺失（connector 契约 §7.2）。
    ///
    /// 返回 `(base, known_absent)`：`base` 为 `None` 时可能是「无 L」也可能是「确认不存在」，
    /// 由 `known_absent` 区分（后者在 R 存在时判冲突，不静默覆盖）。
    pub(crate) fn base_of(
        &self,
        uri: &str,
        expected: &Expected,
        contracts: &[&str],
    ) -> (Option<String>, bool) {
        if let Some(base) = expected.get(uri) {
            return (base.clone(), base.is_none());
        }
        let base = self
            .synced
            .get(uri)
            .filter(|value| matches_any_contract(value, contracts))
            .cloned();
        (base, false)
    }

    /// commit 的判定表（§7.2）：只更新内存状态，持久化归驱动。
    pub(crate) fn plan_commit<'d>(
        &mut self,
        entries: Vec<Entry<'d>>,
        heads: &[Option<String>],
        expected: &Expected,
        contract: &str,
        contracts: &[&str],
    ) -> Vec<Plan<'d>> {
        debug_assert_eq!(entries.len(), heads.len());
        let mut plans = Vec::with_capacity(entries.len());
        for (entry, head) in entries.into_iter().zip(heads) {
            let uri = entry.uri.as_str();
            self.reconcile(uri, head.as_deref());
            let (base, known_absent) = self.base_of(uri, expected, contracts);
            let remote = RemoteState {
                doc_hash: head.clone(),
            };
            let (action, precondition) = if head.as_deref() == Some(entry.new_hash.as_str()) {
                (Action::Achieved, None)
            } else if known_absent || base.is_none() {
                // 无 L（或调用方确认不存在）：R 不存在即 if_absent，否则以 R 作 base；调用方
                // 确认不存在而 R 存在＝与确认矛盾，判冲突（不静默覆盖）
                match head {
                    None => (Action::Write, Some(Precondition::IfAbsent)),
                    Some(_) if known_absent => (Action::Conflict, None),
                    Some(value) => (Action::Write, Some(Precondition::BaseHash(value.clone()))),
                }
            } else {
                let base = base.expect("上一分支已排除 base 缺失");
                if head.is_none() {
                    (Action::Conflict, None)
                } else if contract_of(&base) != contract {
                    // 跨契约：R 与 L 不可比；以 L 为 base 写入，由服务端 CAS 裁决（§7.2）
                    (Action::Write, Some(Precondition::BaseHash(base)))
                } else if head.as_deref() == Some(base.as_str()) {
                    (Action::Write, Some(Precondition::BaseHash(base)))
                } else {
                    (Action::Conflict, None)
                }
            };
            let mut plan = Plan::new(PushOp::Commit, action, uri, remote);
            plan.precondition = precondition;
            plan.document = Some(entry.document);
            plan.new_hash = Some(entry.new_hash);
            if action == Action::Write {
                plan.prepared = Some(entry.prepared);
            }
            plans.push(plan);
        }
        plans
    }

    /// delete 的判定表（§7.2）：R 不存在＝目标状态已达成（不发写）。
    pub(crate) fn plan_delete(
        &mut self,
        uris: &[String],
        heads: &[Option<String>],
        expected: &Expected,
        contract: &str,
        contracts: &[&str],
    ) -> Vec<Plan<'static>> {
        debug_assert_eq!(uris.len(), heads.len());
        let mut plans = Vec::with_capacity(uris.len());
        for (uri, head) in uris.iter().zip(heads) {
            self.reconcile(uri, head.as_deref());
            let (base, known_absent) = self.base_of(uri, expected, contracts);
            let remote = RemoteState {
                doc_hash: head.clone(),
            };
            let (action, precondition) = match head {
                None => (Action::Achieved, None),
                Some(_) if known_absent => (Action::Conflict, None), // 确认不存在而存在
                Some(value) => match base {
                    None => (Action::Write, Some(Precondition::BaseHash(value.clone()))),
                    Some(base) if contract_of(&base) != contract => {
                        // 跨契约：R 与 L 不可比；以 L 为 base 删除，由服务端 CAS 裁决（§7.2）
                        (Action::Write, Some(Precondition::BaseHash(base)))
                    }
                    Some(base) if &base == value => {
                        (Action::Write, Some(Precondition::BaseHash(base)))
                    }
                    Some(_) => (Action::Conflict, None),
                },
            };
            let mut plan = Plan::new(PushOp::Delete, action, uri, remote);
            plan.precondition = precondition;
            plans.push(plan);
        }
        plans
    }

    /// move 的判定表（§7.2「取 from_uri 的 L 与 R」；源不存在时以目标侧判已达成）。
    ///
    /// 源已不存在而目标未达成／base 不可知 → 判定失败（条目失败，§7.2 列为不可由策略重判定）；
    /// 调用方确认源「不存在」而实际存在 → 与确认矛盾，判冲突（同 commit / delete 的表口径）；
    /// 源被改写 → 冲突（`Side::From`）。
    ///
    /// 源不存在且源无 L 时，以目标侧的 L 作 base 候选：迁移后的条目就是本实例完成过的 move
    /// （成功后条目已从源迁到目标；未确认的 to 侧意图也已由上面的对账并入），重放据此判已达成，
    /// 与服务端对同一请求的裁决一致（core §5.2）。回退只在源无 L 时生效，同 §7.2「无 L」的
    /// 退化口径。因此「已达成」只表示「源不存在、目标内容等于本实例确认过的值」，不担保这次
    /// 移动由本实例发生（与 core §5.2 第 4 步只保证状态同义）。
    pub(crate) fn plan_move(
        &mut self,
        moves: &[(String, String)],
        from_heads: &[Option<String>],
        to_heads: &[Option<String>],
        expected: &Expected,
        contract: &str,
        contracts: &[&str],
    ) -> Vec<Plan<'static>> {
        debug_assert_eq!(moves.len(), from_heads.len());
        debug_assert_eq!(moves.len(), to_heads.len());
        let mut plans = Vec::with_capacity(moves.len());
        for (((from_uri, to_uri), from_head), to_head) in moves.iter().zip(from_heads).zip(to_heads)
        {
            self.reconcile(from_uri, from_head.as_deref());
            self.reconcile(to_uri, to_head.as_deref());
            let (mut base, known_absent) = self.base_of(from_uri, expected, contracts);
            if from_head.is_none() && base.is_none() && !known_absent {
                base = self.base_of(to_uri, &HashMap::new(), contracts).0;
            }
            let remote = RemoteState {
                doc_hash: from_head.clone(),
            };
            let mut plan = Plan::new(PushOp::Move, Action::Conflict, from_uri, remote);
            plan.to_uri = Some(to_uri.clone());
            if known_absent && from_head.is_some() {
                // 调用方确认「不存在」而源实际存在：与确认矛盾，判冲突（不静默覆盖）
            } else if from_head.is_none() || known_absent {
                if base.is_some() && to_head == &base {
                    plan.action = Action::Achieved;
                    plan.to_hash = to_head.clone();
                    plan.base = base;
                } else {
                    plan.action = Action::Failed;
                    plan.error = Some(Error::protocol(
                        ErrorKind::NotFound,
                        format!("{from_uri} 已不存在，且目标 {to_uri} 未达成（未发写）"),
                    ));
                }
            } else {
                let from_head = from_head.clone().expect("上一分支已排除源不存在");
                // 无 L 时以读到的 R 为 base（§7.2「无 L，R 存在」的退化）；有 L 而 R ≠ L 即源被
                // 改写：冲突。L 是旧契约值时直接发 move，目标侧待写后重读（§7.2）。
                match base {
                    None => {
                        plan.action = Action::Write;
                        plan.precondition = Some(Precondition::BaseHash(from_head.clone()));
                        // 目标已持有同一内容：不发 move（服务端会以 ALREADY_EXISTS 拒绝、重放永不
                        // 收敛），改为以 base 删除源（#165）
                        plan.delete_source = to_head.as_deref() == Some(from_head.as_str());
                        plan.base = Some(from_head.clone());
                        plan.to_hash = Some(from_head);
                    }
                    Some(base) if contract_of(&base) != contract => {
                        plan.action = Action::Write;
                        plan.precondition = Some(Precondition::BaseHash(base.clone()));
                        plan.base = Some(base);
                        plan.to_hash = Some(from_head);
                        plan.cross_contract = true;
                    }
                    Some(base) if Some(base.as_str()) == Some(from_head.as_str()) => {
                        plan.action = Action::Write;
                        plan.precondition = Some(Precondition::BaseHash(base.clone()));
                        plan.delete_source = to_head.as_deref() == Some(from_head.as_str());
                        plan.base = Some(base);
                        plan.to_hash = Some(from_head);
                    }
                    Some(_) => {}
                }
            }
            plans.push(plan);
        }
        plans
    }

    // ------------------------------------------------------------------
    // 意图与状态维护
    // ------------------------------------------------------------------

    /// commit 写前的一批意图：`uri → N`。
    pub(crate) fn commit_intents(plans: &[Plan<'_>]) -> Intents {
        plans
            .iter()
            .filter(|plan| plan.action == Action::Write)
            .map(|plan| (plan.uri.clone(), plan.new_hash.clone()))
            .collect()
    }

    /// delete 写前的一批意图：`uri → 不存在`。
    pub(crate) fn delete_intents(plans: &[Plan<'_>]) -> Intents {
        plans
            .iter()
            .filter(|plan| plan.action == Action::Write)
            .map(|plan| (plan.uri.clone(), None))
            .collect()
    }

    /// move 写前的一批意图：两侧一起记（`from → 不存在`、`to → 将达成的值`）。
    ///
    /// `to` 记请求声明契约下的值（跨契约时不能记旧契约的 base）：对账拿它与 R 比较。
    pub(crate) fn move_intents(plans: &[Plan<'_>]) -> Intents {
        let mut intents = BTreeMap::new();
        for plan in plans.iter().filter(|plan| plan.action == Action::Write) {
            let to_uri = plan.to_uri.clone().expect("move 的 write 计划必有目标");
            intents.insert(plan.uri.clone(), None);
            intents.insert(
                to_uri,
                Some(
                    plan.to_hash
                        .clone()
                        .or_else(|| plan.base.clone())
                        .expect("move 的 write 计划必有目标值"),
                ),
            );
        }
        intents
    }

    /// 并入一批意图后的**新状态**（本状态不变）；没有意图时返回 `None`（无需持久化）。
    ///
    /// 驱动先持久化新状态、`save` 返回 `Ok` 后才替换内存状态并发写：持久化失败时本批意图
    /// 不进入内存——不留下未持久化的意图（否则下一轮对账会把他人恰好写下的同值误认为自身写入、
    /// 以它作 base 静默覆盖），也不误删同键上原有的结果未知的待定意图。判定阶段的对账结果
    /// 不在此列：它来自真实读到的远端值，已经生效，不回滚。
    pub(crate) fn with_intents(&self, intents: Intents) -> Option<PusherState> {
        if intents.is_empty() {
            return None;
        }
        let mut staged = self.clone();
        staged.intents.extend(intents);
        Some(staged)
    }

    /// 确认一篇文档的同步状态并清意图：`doc_hash` 为 `None` 即删除条目（不存在）。
    fn confirm(&mut self, uri: &str, doc_hash: Option<String>) {
        match doc_hash {
            None => {
                self.synced.remove(uri);
            }
            Some(value) => {
                self.synced.insert(uri.to_owned(), value);
            }
        }
        self.intents.remove(uri);
    }

    /// 清除一条计划涉及的意图（move 两侧一起）。
    fn clear_intents(&mut self, plan: &Plan<'_>) {
        self.intents.remove(&plan.uri);
        if let Some(to_uri) = &plan.to_uri {
            self.intents.remove(to_uri);
        }
    }

    /// move 成功/已达成：条目从 from 迁到 to，两侧意图清除。
    ///
    /// 值取请求声明契约下的目标 doc_hash（`value` 优先，其次计划里的 `to_hash`，最后才回退到
    /// base）；跨契约时 base 是旧契约的值，不能写进同步状态。
    fn migrate(&mut self, plan: &Plan<'_>, value: Option<String>) {
        let to_uri = plan.to_uri.clone().expect("move 计划必有目标");
        let target = value
            .or_else(|| plan.to_hash.clone())
            .or_else(|| plan.base.clone())
            .expect("迁移的 move 计划必有目标值");
        self.synced.remove(&plan.uri);
        self.synced.insert(to_uri, target);
        self.clear_intents(plan);
    }

    /// 写后失败的公共归类（三个操作共用）：冲突、未确定（保留意图）、已确定失败（清意图）。
    fn outcome_failure(&mut self, plan: &Plan<'_>, outcome: Outcome) -> PushResult {
        let status = if outcome.kind == OutcomeKind::Conflict {
            PushStatus::Conflict
        } else {
            PushStatus::Failed
        };
        let unknown = outcome.kind != OutcomeKind::Conflict && outcome.unknown;
        if !unknown {
            self.clear_intents(plan);
        }
        let mut result = PushResult::new(plan.op, &plan.uri, status);
        result.precondition = plan.precondition.clone();
        result.remote = outcome.remote;
        result.to_uri = plan.to_uri.clone();
        result.side = outcome.side;
        result.error = outcome.error;
        result.remote_error = outcome.remote_error;
        result.unknown = unknown;
        result
    }

    // ------------------------------------------------------------------
    // 收尾：把执行结果并入状态并构造回报
    // ------------------------------------------------------------------

    /// commit 的收尾（意图：结果确定即清除，未确定（`unknown`）才保留）。
    pub(crate) fn finish_commit(
        &mut self,
        plans: Vec<Plan<'_>>,
        outcomes: Vec<Option<Outcome>>,
    ) -> Vec<PushResult> {
        let mut results = Vec::with_capacity(plans.len());
        for (plan, outcome) in plans.into_iter().zip(outcomes) {
            match plan.action {
                Action::Achieved => {
                    self.confirm(&plan.uri, plan.new_hash.clone());
                    let mut result =
                        PushResult::new(PushOp::Commit, &plan.uri, PushStatus::Achieved);
                    result.remote = Some(plan.remote);
                    results.push(result);
                }
                Action::Conflict => {
                    // 未发写：不产生新意图，也不清除既有待定意图（那条写入的结果仍未知）
                    let mut result =
                        PushResult::new(PushOp::Commit, &plan.uri, PushStatus::Conflict);
                    result.remote = Some(plan.remote);
                    results.push(result);
                }
                // commit 的判定表当前不产出判定失败；一旦产出，如实报 Failed（不混作冲突）
                Action::Failed => results.push(judged_failure(plan)),
                Action::Write => {
                    let outcome = outcome.expect("write 计划必有执行结果");
                    debug_assert!(plan.new_hash.is_some(), "commit 的 write 计划必有 N");
                    if outcome.kind == OutcomeKind::Conflict
                        && outcome.remote.as_ref().map(|remote| &remote.doc_hash)
                            == Some(&plan.new_hash)
                    {
                        // §7.2 冲突处置表 commit 行：重读值等于本次内容＝已达成（与策略无关）；
                        // 确认 L = N、清意图、不重提，原 CAS 错误一并带上供归因
                        self.confirm(&plan.uri, plan.new_hash.clone());
                        let mut result =
                            PushResult::new(PushOp::Commit, &plan.uri, PushStatus::Achieved);
                        result.precondition = plan.precondition;
                        result.remote = outcome.remote;
                        result.error = outcome.error;
                        results.push(result);
                        continue;
                    }
                    if outcome.error.is_some() {
                        results.push(self.outcome_failure(&plan, outcome));
                        continue;
                    }
                    let committed = outcome.result.expect("无 error 必有 commit 结果");
                    self.confirm(&plan.uri, Some(committed.doc_hash));
                    let mut result =
                        PushResult::new(PushOp::Commit, &plan.uri, committed.status.into());
                    result.precondition = plan.precondition;
                    result.delta = Some(committed.delta);
                    results.push(result);
                }
            }
        }
        results
    }

    /// delete 的收尾：成功、「文档不存在」（服务端裁决）与写后 CAS 冲突而重读不存在都计
    /// `Deleted`（已达成）。
    pub(crate) fn finish_delete(
        &mut self,
        plans: Vec<Plan<'_>>,
        outcomes: Vec<Option<Outcome>>,
    ) -> Vec<PushResult> {
        let mut results = Vec::with_capacity(plans.len());
        for (plan, outcome) in plans.into_iter().zip(outcomes) {
            match plan.action {
                Action::Achieved => {
                    self.confirm(&plan.uri, None); // 目标状态已达成：删条目、清意图
                    let mut result =
                        PushResult::new(PushOp::Delete, &plan.uri, PushStatus::Deleted);
                    result.remote = Some(plan.remote);
                    results.push(result);
                }
                Action::Conflict => {
                    let mut result =
                        PushResult::new(PushOp::Delete, &plan.uri, PushStatus::Conflict);
                    result.remote = Some(plan.remote);
                    results.push(result);
                }
                // delete 的判定表当前不产出判定失败；一旦产出，如实报 Failed（不混作冲突）
                Action::Failed => results.push(judged_failure(plan)),
                Action::Write => {
                    let outcome = outcome.expect("write 计划必有执行结果");
                    match &outcome.error {
                        Some(_) if outcome.reached_absent() => {
                            // 服务端裁决「文档不存在」、或 CAS 冲突后重读不存在＝目标状态已达成
                            // （§7.2 冲突处置表 delete 行，与策略无关）；原错误一并带上（含
                            // code）供运行器按 §7.1 归因
                            self.confirm(&plan.uri, None);
                            let mut result =
                                PushResult::new(PushOp::Delete, &plan.uri, PushStatus::Deleted);
                            result.precondition = plan.precondition;
                            result.remote = outcome.remote;
                            result.error = outcome.error;
                            results.push(result);
                        }
                        Some(_) => results.push(self.outcome_failure(&plan, outcome)),
                        None => {
                            self.confirm(&plan.uri, None); // 删除成功
                            let mut result =
                                PushResult::new(PushOp::Delete, &plan.uri, PushStatus::Deleted);
                            result.precondition = plan.precondition;
                            results.push(result);
                        }
                    }
                }
            }
        }
        results
    }

    /// move 的收尾：成功与已达成都计 `Moved` 并迁移条目；问题带 `side` 标明侧别；每条回报都带
    /// 目标 `to_uri`（「涉及的 URI」）。
    pub(crate) fn finish_move(
        &mut self,
        plans: Vec<Plan<'_>>,
        outcomes: Vec<Option<Outcome>>,
    ) -> Vec<PushResult> {
        let mut results = Vec::with_capacity(plans.len());
        for (plan, outcome) in plans.into_iter().zip(outcomes) {
            let mut result = match plan.action {
                Action::Achieved => {
                    self.migrate(&plan, None);
                    let mut result = PushResult::new(PushOp::Move, &plan.uri, PushStatus::Moved);
                    result.remote = Some(plan.remote);
                    result
                }
                Action::Conflict => {
                    let mut result = PushResult::new(PushOp::Move, &plan.uri, PushStatus::Conflict);
                    result.remote = Some(plan.remote);
                    result.side = Some(Side::From);
                    result
                }
                Action::Failed => {
                    // 判定失败（源已不存在、目标未达成）：问题在源侧
                    let to_uri = plan.to_uri.clone();
                    let mut result = judged_failure(plan);
                    result.side = Some(Side::From);
                    result.to_uri = to_uri;
                    results.push(result);
                    continue;
                }
                Action::Write => {
                    let outcome = outcome.expect("write 计划必有执行结果");
                    if outcome.error.is_some() {
                        if !((plan.delete_source || outcome.delete_source)
                            && outcome.reached_absent())
                        {
                            results.push(self.outcome_failure(&plan, outcome));
                            continue;
                        }
                        // 删源时服务端裁决「源不存在」、或 CAS 冲突后重读源不存在：move 的目标
                        // 状态（源不存在、目标为 base）已达成，同 delete 的口径；原错误一并带上
                        self.migrate(&plan, outcome.move_hash.clone());
                        let mut result =
                            PushResult::new(PushOp::Move, &plan.uri, PushStatus::Moved);
                        result.precondition = plan.precondition;
                        result.remote = outcome.remote;
                        result.error = outcome.error;
                        result
                    } else {
                        // 写入成功：迁移条目；删源路径带判定时读到的源侧 R
                        self.migrate(&plan, outcome.move_hash.clone());
                        let mut result =
                            PushResult::new(PushOp::Move, &plan.uri, PushStatus::Moved);
                        result.precondition = plan.precondition;
                        if plan.delete_source || outcome.delete_source {
                            result.remote = Some(plan.remote);
                        }
                        result
                    }
                }
            };
            result.to_uri = plan.to_uri;
            results.push(result);
        }
        results
    }
}

/// 判定失败（未发写）的回报：`Failed`，带判定错误与读到的 remote；不产生新意图、也不清除
/// 既有待定意图（三个操作共用，move 另补侧别与目标）。
fn judged_failure(plan: Plan<'_>) -> PushResult {
    let mut result = PushResult::new(plan.op, &plan.uri, PushStatus::Failed);
    result.remote = Some(plan.remote);
    result.error = plan.error;
    result
}

impl fmt::Display for PushStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// 驱动（默认 feature `http`）：经 DpeClient 读、写与持久化
// ---------------------------------------------------------------------------

#[cfg(feature = "http")]
mod driver {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

    use futures_util::stream::{self, StreamExt};

    use super::{
        Action, MemoryStateStore, Outcome, OutcomeKind, Plan, PushOp, PushResult, PushStatus,
        PusherState, Side, StateStore,
    };
    use crate::errors::{Error, ErrorKind};
    use crate::http::DpeClient;
    use crate::models::ExpandedDocument;
    use crate::protocol::{BlobProvider, Precondition};

    /// 本批是否有发写的计划（收尾保存失败时据此置 `outcome_unknown`）。
    fn has_writes(plans: &[Plan<'_>]) -> bool {
        plans.iter().any(|plan| plan.action == Action::Write)
    }

    /// 服务端确定、不可重试地拒绝了这次读（带规范码且不可重试；不合规响应与传输错误不算）。
    ///
    /// 契约不兼容除外：它不针对具体 URI，按 connector §7.1 是实例级，整批返回，不逐条定位。
    fn determinate(error: &Error) -> bool {
        error.code().is_some()
            && !error.is_retryable()
            && error.kind() != ErrorKind::ContractUnsupported
    }

    /// 读被拒的条目的回报（`side` 为空：不是 move 的源/目标侧问题，按读失败归类）。
    fn read_failure(op: PushOp, uri: &str, to_uri: Option<&str>, error: Error) -> PushResult {
        let mut result = PushResult::new(op, uri, PushStatus::Failed);
        result.to_uri = to_uri.map(str::to_owned);
        result.error = Some(error);
        result
    }

    /// 把读被拒的条目与其余条目的回报按输入顺序合并。
    fn merge(
        count: usize,
        mut failures: HashMap<usize, PushResult>,
        rest: Vec<PushResult>,
    ) -> Vec<PushResult> {
        let mut rest = rest.into_iter();
        (0..count)
            .map(|index| match failures.remove(&index) {
                Some(failure) => failure,
                None => rest.next().expect("其余回报与未失败的条目一一对应"),
            })
            .collect()
    }

    /// `push` 的 blob 来源在本批并发投递之间共享。
    ///
    /// **借用不变式**：provider 只在核心操作的同步 `poll` 步骤里被调用（`BlobProvider` 的调用
    /// 约束），而 `run_all` 的并发在同一个任务内、只在 await 点交错——锁只在一次回调期间持有、
    /// 从不跨 await，因此单任务内不会争锁、也不会自锁。用 `Mutex` 而不是 `RefCell`，是为了让
    /// 持有它的 `push` future 是 `Send`（#131）。
    type SharedBlobs<'p> = Mutex<Option<BlobProvider<'p>>>;

    /// 取共享回调的锁。锁中毒只可能来自回调自身 panic：那次 `push` 已在展开，取回内部值即可、
    /// 不再放大。
    fn lock_blobs<'g, 'p>(cell: &'g SharedBlobs<'p>) -> MutexGuard<'g, Option<BlobProvider<'p>>> {
        cell.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 写操作的缺省并发度（同 Python 侧的 `concurrency=4`）。
    pub const DEFAULT_CONCURRENCY: usize = 4;

    /// 异步增量推送器：[`push`](StatefulPusher::push) 只推变化的部分，遇冲突如实回报、绝不
    /// 自动 force；语义见[模块说明](super)。
    ///
    /// - 状态在构造时从 store 载入一次、每次操作结束时整批保存回去；同一 store 上并发使用多个
    ///   推送器不在支持范围（每个 remote 一个实例）。三个操作都取 `&mut self`，同一实例上的
    ///   操作天然串行；
    /// - 写操作在**调用方的任务内**并发执行（至多 `concurrency` 条），不经 `tokio::spawn`；
    ///   操作的 future 是 `Send`（`push` 经 `SharedBlobs` 的 `Mutex` 共享 blob 来源），调用方
    ///   可以把整次操作交给 `tokio::spawn`；
    /// - 批级失败（输入校验、判定前读取的网络失败 / 可重试错误耗尽 / 不合规响应、store 读写）以
    ///   `Err` 返回、不产生逐条回报；判定前的 `batch_head` 被确定拒绝时不在此列（逐条定位，见
    ///   [模块说明](super)）；store 的
    ///   错误包装为 [`ErrorKind::StateStore`]，原错误经 `source()` 取回：
    ///   - 构造时载入失败：未发任何请求（`outcome_unknown()` 为假）；
    ///   - 写前持久化意图失败：写请求确定未发出（`outcome_unknown()` 为假），本批意图不进入
    ///     内存（判定阶段按真实读到的远端值完成的对账保留，下次保存时一并落盘）；
    ///   - 收尾保存失败（本批发过写请求时 `outcome_unknown()` 为真，否则为假）：整批报错，本轮的逐条回报
    ///     随之丢失，调用方不应据此推进游标；已确定的结果已并入内存，同一实例继续可用，只有
    ///     结果未知的写入在下一轮按意图对账。
    ///
    /// **store 与内存不一致**：任何一次保存失败后，store 都可能与内存不一致——收尾失败时
    /// store **落后**于内存（仍留着内存里已清除的意图，如已确定失败的写入）；落盘实现若在写入
    /// 完成后才报错（如目录 `fsync` 失败），写前那次失败也会让 store **领先**于内存（留着从未
    /// 发出的意图）。**继续使用同一个实例**，直到下一次保存成功（它会整份写入内存状态）；在那
    /// 之前**不要用同一个 store 新建实例**——读回的过期意图会在他人恰好写下同值时被误认为自身
    /// 写入。进程崩溃后重新载入属于 §7.2 本就接受的崩溃窗口；跨实例的保证属于运行器的职责。
    ///
    /// ```no_run
    /// use std::collections::HashMap;
    /// use std::sync::Arc;
    ///
    /// use dpe_sdk::http::DpeClient;
    /// use dpe_sdk::pusher::{MemoryStateStore, StatefulPusher};
    /// use dpe_sdk::{ExpandedDocument, CONTRACT};
    ///
    /// # async fn example() -> Result<(), dpe_sdk::Error> {
    /// let client = DpeClient::new("https://example.com/r/42")?;
    /// let mut pusher = StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), 4)?;
    /// let document = ExpandedDocument::parse(
    ///     r#"{"file_type": "md", "pages": [{"elements": [{"category": "Title", "text": "季度报告"}]}]}"#,
    ///     CONTRACT,
    /// )?;
    /// let results = pusher
    ///     .push(&[("s3://bucket/a.md".to_owned(), document)], &HashMap::new(), None)
    ///     .await?;
    /// println!("{}", results[0].status);
    /// pusher
    ///     .move_documents(&[("s3://bucket/a.md".to_owned(), "s3://bucket/b.md".to_owned())], &HashMap::new())
    ///     .await?;
    /// pusher.delete(&["s3://bucket/b.md".to_owned()], &HashMap::new()).await?;
    /// # Ok(())
    /// # }
    /// ```
    pub struct StatefulPusher<'c> {
        client: &'c DpeClient,
        store: Arc<dyn StateStore>,
        concurrency: usize,
        state: PusherState,
    }

    impl std::fmt::Debug for StatefulPusher<'_> {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.debug_struct("StatefulPusher")
                .field("remote", &self.client.remote())
                .field("concurrency", &self.concurrency)
                .field("state", &self.state)
                .finish_non_exhaustive()
        }
    }

    impl<'c> StatefulPusher<'c> {
        /// 以缺省配置构造：[`MemoryStateStore`]、并发度 [`DEFAULT_CONCURRENCY`]。
        pub fn new(client: &'c DpeClient) -> Self {
            Self::with_store(
                client,
                Arc::new(MemoryStateStore::new()),
                DEFAULT_CONCURRENCY,
            )
            .expect("内存 store 的 load 不会失败、缺省并发度合法")
        }

        /// 指定 store 与并发度构造：从 store 载入该 remote 的状态（失败如实返回）；
        /// `concurrency` 为 0 返回 [`ErrorKind::InvalidArgument`]。
        pub fn with_store(
            client: &'c DpeClient,
            store: Arc<dyn StateStore>,
            concurrency: usize,
        ) -> Result<Self, Error> {
            if concurrency == 0 {
                return Err(Error::invalid_argument(
                    "concurrency 必须是正整数，实际为 0",
                ));
            }
            // 构造阶段不发任何写请求：结果确定（未生效）
            let snapshot = store
                .load(client.remote())
                .map_err(|error| Error::state_store(error).with_outcome_unknown(false))?;
            Ok(StatefulPusher {
                client,
                store,
                concurrency,
                state: PusherState::from_snapshot(snapshot),
            })
        }

        /// 推送器所用的客户端（构造时传入的同一实例）：运行器在轮首经它取得 remote 能力
        /// （#137），不另建客户端。
        pub fn client(&self) -> &'c DpeClient {
            self.client
        }

        /// 把内存中的同步状态与意图整批写入 store（补存入口，connector §7.2 / #137）。
        ///
        /// 任何一次保存失败后 store 都可能与内存不一致（见类型说明）；调用方（如运行器）在结束
        /// 使用前用**同一实例**调用它补存，成功后两者重新一致。本身不发写请求：失败为
        /// [`ErrorKind::StateStore`]、`outcome_unknown()` 为假（同收尾保存「按本批是否发写置位」
        /// 的口径）。
        pub fn flush(&self) -> Result<(), Error> {
            self.save(false)
        }

        /// 绑定的 remote（即 store 的命名空间）。
        pub fn remote(&self) -> &str {
            self.client.remote()
        }

        /// 当前内存状态的快照（诊断用；持久化以 store 为准）。
        pub fn snapshot(&self) -> super::StateSnapshot {
            self.state.snapshot()
        }

        /// 推送一批文档（`(file_uri, 展开文档)`）：只推变化的部分，返回与输入同序的逐条回报。
        ///
        /// `expected_base` 覆盖同步状态中的 L（值为 `None` 即调用方确认「不存在」）；上层可在
        /// 冲突后以重读到的 remote 值重提（source_wins 等策略归上层，本方法不自动重提）。
        ///
        /// `blobs` 原样交给每条写入的 `deliver`：按 `sha256:` 引用给出服务端缺少的 blob 字节
        /// （可用 [`crate::media`] 的来源适配构造）；不发写的条目（如 R == N 已达成）不会调用它。
        /// 为 `None` 时服务端缺字节由核心如实报 `DPE_MISSING_CONTENT`，推送器不自行解引用任何
        /// 来源。本批并发投递共享同一个回调（同一任务内交错，不并行调用）。
        pub async fn push(
            &mut self,
            documents: &[(String, ExpandedDocument)],
            expected_base: &HashMap<String, Option<String>>,
            blobs: Option<BlobProvider<'_>>,
        ) -> Result<Vec<PushResult>, Error> {
            let core = self.client.core().await?;
            let contract = core.contract().to_owned();
            let contracts = core.accepted_contracts();
            let (entries, expected) =
                PusherState::prepare(documents, expected_base, &contract, &contracts)?;
            if entries.is_empty() {
                return Ok(Vec::new());
            }
            let uris: Vec<String> = entries.iter().map(|entry| entry.uri.clone()).collect();
            let (heads, mut denied) = self.read(&uris).await?;
            let count = entries.len();
            let mut failures = HashMap::new();
            let mut kept = Vec::new();
            let mut kept_heads = Vec::new();
            for (index, (entry, head)) in entries.into_iter().zip(heads).enumerate() {
                match denied.remove(&index) {
                    Some(error) => {
                        failures
                            .insert(index, read_failure(PushOp::Commit, &entry.uri, None, error));
                    }
                    None => {
                        kept.push(entry);
                        kept_heads.push(head);
                    }
                }
            }
            let mut wrote = false;
            let mut results = Vec::new();
            if !kept.is_empty() {
                let plans =
                    self.state
                        .plan_commit(kept, &kept_heads, &expected, &contract, &contracts);
                self.persist(PusherState::commit_intents(&plans))?;
                wrote = has_writes(&plans);
                let outcomes = self.run_all(&plans, Some(&Mutex::new(blobs))).await;
                results = self.state.finish_commit(plans, outcomes);
            }
            self.save(wrote)?;
            Ok(merge(count, failures, results))
        }

        /// 删除一批文档（按 URI）：CAS 走 `If-Match`，返回与输入同序的逐条回报。
        ///
        /// - 远端已不存在＝目标状态已达成，记 `Deleted`、不发写；
        /// - `expected_base` 覆盖同步状态中的 L（值为 `None` 即确认「不存在」）；
        /// - 冲突（他人改写）如实回报，绝不自动 force；`source_wins` 等策略归上层（用重读到的
        ///   值作 `expected_base` 重提）。
        pub async fn delete(
            &mut self,
            uris: &[String],
            expected_base: &HashMap<String, Option<String>>,
        ) -> Result<Vec<PushResult>, Error> {
            let core = self.client.core().await?;
            let contract = core.contract().to_owned();
            let contracts = core.accepted_contracts();
            let (normalized, expected) =
                PusherState::prepare_uris(uris, expected_base, &contracts)?;
            if normalized.is_empty() {
                return Ok(Vec::new());
            }
            let (heads, mut denied) = self.read(&normalized).await?;
            let mut failures = HashMap::new();
            let mut kept = Vec::new();
            let mut kept_heads = Vec::new();
            for (index, (uri, head)) in normalized.iter().zip(heads).enumerate() {
                match denied.remove(&index) {
                    Some(error) => {
                        failures.insert(index, read_failure(PushOp::Delete, uri, None, error));
                    }
                    None => {
                        kept.push(uri.clone());
                        kept_heads.push(head);
                    }
                }
            }
            let mut wrote = false;
            let mut results = Vec::new();
            if !kept.is_empty() {
                let plans =
                    self.state
                        .plan_delete(&kept, &kept_heads, &expected, &contract, &contracts);
                self.persist(PusherState::delete_intents(&plans))?;
                wrote = has_writes(&plans);
                let outcomes = self.run_all(&plans, None).await;
                results = self.state.finish_delete(plans, outcomes);
            }
            self.save(wrote)?;
            Ok(merge(normalized.len(), failures, results))
        }

        /// 按 URI 批量原子改名（`(from_uri, to_uri)` 序列）：CAS 走源文档的 `base_hash`。
        ///
        /// - 源已不存在而目标内容等于 base＝已达成，记 `Moved`、不发写；源已不存在而目标未达成
        ///   为条目失败（`Side::From`）；
        /// - 成功与已达成都把同步状态条目从源迁到目标；
        /// - `expected_base` 以 `from_uri` 为键（值为 `None` 即确认源「不存在」）；
        /// - 同一 URI 在本批 from 与 to 中至多出现一次（链式、共用目标整批拒绝，保序拆批归
        ///   调用方）；
        /// - 目标被占（`DPE_ALREADY_EXISTS`）为条目失败（`Side::To`），源被改写为冲突
        ///   （`Side::From`），都如实回报、绝不自动退化；
        /// - 例外：目标已持有与源相同的内容（读到的目标值等于 base）时不发 move，以 base 为
        ///   前置条件删除源，判 `Moved`（§7.2，#165）；删源遇源被改写为冲突（`Side::From`），
        ///   遇源已不存在为已达成。
        pub async fn move_documents(
            &mut self,
            moves: &[(String, String)],
            expected_base: &HashMap<String, Option<String>>,
        ) -> Result<Vec<PushResult>, Error> {
            let core = self.client.core().await?;
            let contract = core.contract().to_owned();
            let contracts = core.accepted_contracts();
            let (pairs, expected) = PusherState::prepare_moves(moves, expected_base, &contracts)?;
            if pairs.is_empty() {
                return Ok(Vec::new());
            }
            // 一次 batch_head 读全部 move 的两侧：先全部源、再全部目标（本批 URI 两两不同）
            let sides: Vec<String> = pairs
                .iter()
                .map(|(from, _)| from.clone())
                .chain(pairs.iter().map(|(_, to)| to.clone()))
                .collect();
            let (mut heads, mut denied) = self.read(&sides).await?;
            let count = pairs.len();
            let to_heads = heads.split_off(count);
            let mut failures = HashMap::new();
            let mut kept = Vec::new();
            let mut kept_from = Vec::new();
            let mut kept_to = Vec::new();
            for (index, ((pair, from_head), to_head)) in
                pairs.iter().zip(heads).zip(to_heads).enumerate()
            {
                // 任一侧读被拒都归到该 move
                match denied
                    .remove(&index)
                    .or_else(|| denied.remove(&(index + count)))
                {
                    Some(error) => {
                        failures.insert(
                            index,
                            read_failure(PushOp::Move, &pair.0, Some(&pair.1), error),
                        );
                    }
                    None => {
                        kept.push(pair.clone());
                        kept_from.push(from_head);
                        kept_to.push(to_head);
                    }
                }
            }
            let mut wrote = false;
            let mut results = Vec::new();
            if !kept.is_empty() {
                let plans = self.state.plan_move(
                    &kept, &kept_from, &kept_to, &expected, &contract, &contracts,
                );
                self.persist(PusherState::move_intents(&plans))?;
                wrote = has_writes(&plans);
                let outcomes = self.run_all(&plans, None).await;
                results = self.state.finish_move(plans, outcomes);
            }
            self.save(wrote)?;
            Ok(merge(count, failures, results))
        }

        /// 判定前的读：一次 `batch_head`；被确定、不可重试地拒绝时逐 URI `head` 定位（至多
        /// `concurrency` 个并发，同一任务内），返回（读到的值，被拒的 URI 下标 → 原错误）。逐条
        /// 读都通过（拒绝与具体 URI 无关）时没有被拒的 URI，全部条目按读到的值继续（connector
        /// §7.1）；批量读被拒这件事不被吞掉，记一条 warn（target `dpe_sdk::pusher`，带原错误码
        /// 与计数，不带远端文本）。网络失败、可重试错误耗尽、不合规响应与契约不兼容（含定位途中
        /// 遇到的）整批返回 `Err`；此时尚未写入、未持久化意图。
        async fn read(
            &self,
            uris: &[String],
        ) -> Result<(Vec<Option<String>>, HashMap<usize, Error>), Error> {
            let rejected = match self.client.batch_head(uris).await {
                Ok(heads) => return Ok((heads, HashMap::new())),
                Err(error) if determinate(&error) => error,
                Err(error) => return Err(error),
            };
            let mut heads: Vec<Option<String>> = vec![None; uris.len()];
            let mut denied = HashMap::new();
            // 按下标迭代（同 run_all）：闭包只捕获下标，future 才能保持 Send（#131）
            let mut probes = stream::iter(0..uris.len())
                .map(|index| async move { (index, self.client.head(&uris[index]).await) })
                .buffer_unordered(self.concurrency);
            while let Some((index, outcome)) = probes.next().await {
                match outcome {
                    Ok(head) => heads[index] = head,
                    Err(error) if determinate(&error) => {
                        denied.insert(index, error);
                    }
                    // 丢弃 stream 即取消其余读
                    Err(error) => return Err(error),
                }
            }
            log::warn!(
                target: "dpe_sdk::pusher",
                "批量读被拒（{}），逐条读定位：{} 个 URI 中 {} 个读被拒",
                rejected.code().unwrap_or("?"),
                uris.len(),
                denied.len()
            );
            Ok((heads, denied))
        }

        /// 写前先把这一批意图持久化；`save` 返回 `Ok` 之后才替换内存状态，调用方随后才允许
        /// 发出写请求（§7.2）。失败时本批意图不进入内存（判定阶段的对账结果保留）、写请求
        /// 确定未发出。
        fn persist(&mut self, intents: super::Intents) -> Result<(), Error> {
            let Some(staged) = self.state.with_intents(intents) else {
                return Ok(());
            };
            self.store
                .save(self.client.remote(), &staged.snapshot())
                .map_err(|error| Error::state_store(error).with_outcome_unknown(false))?;
            self.state = staged;
            Ok(())
        }

        /// 收尾保存：失败时整批报错，本轮的逐条回报随之丢失；`outcome_unknown` 显式置位——
        /// 本批发过写请求（`wrote`）为真，否则为假（确定未生效）。store 与内存不一致（见类型
        /// 说明），同一实例的下一次成功保存会整份补上。
        fn save(&self, wrote: bool) -> Result<(), Error> {
            self.store
                .save(self.client.remote(), &self.state.snapshot())
                .map_err(|error| Error::state_store(error).with_outcome_unknown(wrote))
        }

        /// 对全部 write 计划按 `concurrency` 有界并发地执行（同一任务内），结果按序回填。
        async fn run_all(
            &self,
            plans: &[Plan<'_>],
            blobs: Option<&SharedBlobs<'_>>,
        ) -> Vec<Option<Outcome>> {
            let mut outcomes: Vec<Option<Outcome>> = plans.iter().map(|_| None).collect();
            let indexes: Vec<usize> = plans
                .iter()
                .enumerate()
                .filter(|(_, plan)| plan.action == Action::Write)
                .map(|(index, _)| index)
                .collect();
            let mut running = stream::iter(indexes)
                .map(|index| async move { (index, self.run_one(&plans[index], blobs).await) })
                .buffer_unordered(self.concurrency);
            while let Some((index, outcome)) = running.next().await {
                outcomes[index] = Some(outcome);
            }
            outcomes
        }

        /// `blobs` 只有 commit 用到（delete / move 传 `None`）。
        async fn run_one(&self, plan: &Plan<'_>, blobs: Option<&SharedBlobs<'_>>) -> Outcome {
            match plan.op {
                super::PushOp::Commit => self.commit_one(plan, blobs).await,
                super::PushOp::Delete => self.delete_one(plan).await,
                super::PushOp::Move => self.move_one(plan).await,
            }
        }

        async fn commit_one(&self, plan: &Plan<'_>, blobs: Option<&SharedBlobs<'_>>) -> Outcome {
            let precondition = plan.precondition.clone().expect("write 计划必有前置条件");
            let document = plan.document.expect("commit 的 write 计划必有文档");
            let prepared = plan
                .prepared
                .as_ref()
                .expect("commit 的 write 计划必有预算");
            // 每条 deliver 拿一个转发到共享 provider 的局部闭包（见 `SharedBlobs` 的借用不变式）；
            // 调用方没给 provider 时仍传 None，保持核心「无来源即如实报缺失」的语义
            let shared = blobs.filter(|cell| lock_blobs(cell).is_some());
            let mut lookup = |reference: &str| {
                shared.and_then(|cell| lock_blobs(cell).as_mut().and_then(|f| f(reference)))
            };
            let provider: Option<BlobProvider<'_>> = shared.map(|_| {
                let lookup: BlobProvider<'_> = &mut lookup;
                lookup
            });
            match self
                .client
                .deliver_prepared(&plan.uri, document, prepared, precondition, provider, None)
                .await
            {
                Ok(result) => Outcome::ok(Some(result)),
                Err(error) => match error.kind() {
                    ErrorKind::PreconditionFailed
                    | ErrorKind::AlreadyExists
                    | ErrorKind::NotFound => Outcome::failure(error, OutcomeKind::Conflict)
                        .with_reread(self.client.head(&plan.uri).await),
                    // 首发丢响应、重发得到 429 耗尽、重发全建连失败等序列里首次尝试都可能已
                    // 生效；只有核心明确标记「确定未生效」才清除意图
                    _ => Outcome::undetermined(error),
                },
            }
        }

        async fn delete_one(&self, plan: &Plan<'_>) -> Outcome {
            let base = match &plan.precondition {
                Some(Precondition::BaseHash(base)) => base.as_str(),
                _ => unreachable!("delete 的 write 计划必有 base"),
            };
            match self.client.delete(&plan.uri, base).await {
                Ok(()) => Outcome::ok(None),
                Err(error) => match error.kind() {
                    ErrorKind::PreconditionFailed => Outcome::failure(error, OutcomeKind::Conflict)
                        .with_reread(self.client.head(&plan.uri).await),
                    // 文档不存在即目标状态已达成（§7.2 冲突处置表 delete 行），不是失败
                    ErrorKind::NotFound => Outcome::failure(error, OutcomeKind::Achieved),
                    _ => Outcome::undetermined(error),
                },
            }
        }

        async fn move_one(&self, plan: &Plan<'_>) -> Outcome {
            let to_uri = plan.to_uri.as_deref().expect("move 的 write 计划必有目标");
            let base = plan.base.as_deref().expect("move 的 write 计划必有 base");
            if plan.delete_source {
                // 目标已持有同一内容（#165）：以 base 删除源，结局归类同 delete，只有 CAS 冲突
                // 标源侧（侧别专指 move 的问题侧，其余错误不标，运行器据此分级）
                let outcome = self.delete_one(plan).await;
                let outcome = if outcome.kind == OutcomeKind::Conflict {
                    outcome.with_side(Side::From)
                } else {
                    outcome
                };
                return outcome
                    .with_move_hash(plan.to_hash.clone().expect("删源的 move 计划必有目标值"));
            }
            match self.client.move_document(&plan.uri, to_uri, base).await {
                Ok(result) => Outcome::ok(None).with_move_hash(result.doc_hash),
                Err(error) => match error.kind() {
                    // 源被改写：重读源侧
                    ErrorKind::PreconditionFailed => Outcome::failure(error, OutcomeKind::Conflict)
                        .with_side(Side::From)
                        .with_reread(self.client.head(&plan.uri).await),
                    // 目标被占：重读目标侧
                    ErrorKind::AlreadyExists => {
                        let target = self.client.head(to_uri).await;
                        if plan.cross_contract {
                            let source = self.client.head(&plan.uri).await;
                            if let (Ok(Some(target_hash)), Ok(Some(source_hash))) =
                                (&target, &source)
                            {
                                if target_hash == source_hash {
                                    // 目标已持有与源相同的内容：仍用旧契约的 L 删源（不用
                                    // 重读到的源值），源被他人改写时由服务端 CAS 判冲突（§7.2）
                                    let outcome = self.delete_one(plan).await;
                                    let outcome = if outcome.kind == OutcomeKind::Conflict {
                                        outcome.with_side(Side::From)
                                    } else {
                                        outcome
                                    };
                                    return outcome
                                        .with_delete_source()
                                        .with_move_hash(target_hash.clone());
                                }
                            }
                            if matches!(&source, Ok(None)) {
                                // 重读时源已不存在：沿用「源已不存在、目标未达成」的条目失败
                                return Outcome::failure(
                                    Error::protocol(
                                        ErrorKind::NotFound,
                                        format!(
                                            "{} 已不存在，且目标 {to_uri} 未达成（重读）",
                                            plan.uri
                                        ),
                                    ),
                                    OutcomeKind::Failed,
                                )
                                .with_side(Side::From)
                                .with_reread(target);
                            }
                            if let Err(source_error) = source {
                                let mut outcome = Outcome::failure(error, OutcomeKind::Failed)
                                    .with_side(Side::To)
                                    .with_reread(target);
                                // 与 Python 口径一致：目标侧重读失败时保留目标侧错误，
                                // 只有目标侧成功才用源侧错误补位
                                if outcome.remote_error.is_none() {
                                    outcome.remote_error = Some(source_error);
                                }
                                return outcome;
                            }
                        }
                        Outcome::failure(error, OutcomeKind::Failed)
                            .with_side(Side::To)
                            .with_reread(target)
                    }
                    // 源已不存在、目标未达成（§7.2 条目失败）
                    ErrorKind::NotFound => {
                        Outcome::failure(error, OutcomeKind::Failed).with_side(Side::From)
                    }
                    _ => Outcome::undetermined(error),
                },
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 纯逻辑单测：判定表逐格、对账、条目维护、意图的保留与清除、批校验（I/O 场景见 tests/pusher.rs）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const C: &str = dpe_hash::CONTRACT;

    fn h(digit: char) -> String {
        format!("{C}:{}", digit.to_string().repeat(64))
    }

    fn drill(digit: char) -> String {
        format!(
            "{}:{}",
            dpe_hash::DRILL_CONTRACT,
            digit.to_string().repeat(64)
        )
    }

    fn doc(text: &str) -> ExpandedDocument {
        ExpandedDocument::parse(
            &format!(
                r#"{{"file_type": "md", "pages": [{{"elements": [{{"category": "NarrativeText", "text": "{text}"}}]}}]}}"#
            ),
            C,
        )
        .expect("合法文档")
    }

    fn prepared_doc(document: &ExpandedDocument) -> PreparedDocument {
        let hashes = document.hashes(C).expect("合法文档");
        PreparedDocument {
            contract: C.to_owned(),
            hashes,
            data: serde_json::to_value(document).expect("数据模型总能序列化"),
        }
    }

    fn none() -> Expected {
        HashMap::new()
    }

    /// 驱动的持久化成功路径：并入意图后的新状态替换旧状态。
    fn stage(state: &mut PusherState, intents: Intents) {
        if let Some(staged) = state.with_intents(intents) {
            *state = staged;
        }
    }

    #[test]
    fn with_intents_leaves_the_current_state_untouched() {
        let mut state = with_synced(&[(A, h('1'))]);
        state.intents.insert(A.to_owned(), Some(h('2'))); // 结果未知的待定意图
        let before = state.clone();
        let staged = state
            .with_intents(BTreeMap::from([
                (A.to_owned(), Some(h('3'))),
                (B.to_owned(), None),
            ]))
            .expect("有意图");
        assert_eq!(state, before, "持久化之前内存状态不变");
        assert_eq!(staged.intents.get(A), Some(&Some(h('3'))));
        assert_eq!(staged.intents.get(B), Some(&None));
        assert!(state.with_intents(BTreeMap::new()).is_none());
    }

    fn expected(pairs: &[(&str, Option<String>)]) -> Expected {
        pairs
            .iter()
            .map(|(uri, value)| ((*uri).to_owned(), value.clone()))
            .collect()
    }

    const A: &str = "dpe://docs/a.md";
    const B: &str = "dpe://docs/b.md";
    const X: &str = "dpe://docs/x.md";

    /// commit 单条判定：返回 (动作, 前置条件)。
    fn commit_plan(
        state: &mut PusherState,
        document: &ExpandedDocument,
        head: Option<String>,
        expected: &Expected,
    ) -> (Action, Option<Precondition>) {
        commit_plan_with(state, document, head, expected, &[C])
    }

    fn commit_plan_with(
        state: &mut PusherState,
        document: &ExpandedDocument,
        head: Option<String>,
        expected: &Expected,
        contracts: &[&str],
    ) -> (Action, Option<Precondition>) {
        let prepared = prepared_doc(document);
        let entry = Entry {
            uri: A.to_owned(),
            document,
            new_hash: prepared.hashes.doc_hash.clone(),
            prepared,
        };
        let plan = state
            .plan_commit(vec![entry], &[head], expected, C, contracts)
            .pop()
            .unwrap();
        (plan.action, plan.precondition)
    }

    fn with_synced(pairs: &[(&str, String)]) -> PusherState {
        PusherState {
            synced: pairs
                .iter()
                .map(|(uri, value)| ((*uri).to_owned(), value.clone()))
                .collect(),
            intents: BTreeMap::new(),
        }
    }

    // ------------------------------------------------------------------ commit

    #[test]
    fn commit_table() {
        let document = doc("正文");
        let n = document.doc_hash(C).unwrap();
        // R == N → 已达成（先于 expected_base：确认「不存在」也记已达成）
        let declared_absent = expected(&[(A, None)]);
        assert_eq!(
            commit_plan(
                &mut PusherState::default(),
                &document,
                Some(n.clone()),
                &declared_absent
            ),
            (Action::Achieved, None)
        );
        // 无 L、R 不存在 → if_absent
        assert_eq!(
            commit_plan(&mut PusherState::default(), &document, None, &none()),
            (Action::Write, Some(Precondition::IfAbsent))
        );
        // 无 L、R 存在 → 以 R 作 base
        assert_eq!(
            commit_plan(
                &mut PusherState::default(),
                &document,
                Some(h('1')),
                &none()
            ),
            (Action::Write, Some(Precondition::BaseHash(h('1'))))
        );
        // 有 L、R == L → 以 L 作 base
        assert_eq!(
            commit_plan(
                &mut with_synced(&[(A, h('1'))]),
                &document,
                Some(h('1')),
                &none()
            ),
            (Action::Write, Some(Precondition::BaseHash(h('1'))))
        );
        // 有 L、R ≠ L / R 不存在 → 冲突
        assert_eq!(
            commit_plan(
                &mut with_synced(&[(A, h('1'))]),
                &document,
                Some(h('2')),
                &none()
            ),
            (Action::Conflict, None)
        );
        assert_eq!(
            commit_plan(&mut with_synced(&[(A, h('1'))]), &document, None, &none()),
            (Action::Conflict, None)
        );
        // 调用方确认「不存在」：R 不存在 → if_absent；R 存在 → 冲突（与确认矛盾）
        assert_eq!(
            commit_plan(
                &mut with_synced(&[(A, h('1'))]),
                &document,
                None,
                &declared_absent
            ),
            (Action::Write, Some(Precondition::IfAbsent))
        );
        assert_eq!(
            commit_plan(
                &mut PusherState::default(),
                &document,
                Some(h('1')),
                &declared_absent
            ),
            (Action::Conflict, None)
        );
        // expected_base 覆盖 L
        assert_eq!(
            commit_plan(
                &mut with_synced(&[(A, h('1'))]),
                &document,
                Some(h('2')),
                &expected(&[(A, Some(h('2')))])
            ),
            (Action::Write, Some(Precondition::BaseHash(h('2'))))
        );
    }

    #[test]
    fn old_contract_entries_are_missing_when_not_accepted() {
        let document = doc("正文");
        // 服务端只接受 dpe1：dpex 的 L 按无 L，R 存在即以 R 作 base（而不是判冲突）
        assert_eq!(
            commit_plan(
                &mut with_synced(&[(A, drill('1'))]),
                &document,
                Some(h('2')),
                &none()
            ),
            (Action::Write, Some(Precondition::BaseHash(h('2'))))
        );
    }

    #[test]
    fn old_contract_entries_are_used_when_accepted() {
        let document = doc("正文");
        // 服务端仍接受 dpex：旧 L 直接作 base，由服务端 CAS 裁决
        assert_eq!(
            commit_plan_with(
                &mut with_synced(&[(A, drill('1'))]),
                &document,
                Some(h('2')),
                &none(),
                &[C, dpe_hash::DRILL_CONTRACT],
            ),
            (Action::Write, Some(Precondition::BaseHash(drill('1'))))
        );
    }

    #[test]
    fn reconcile_merges_only_a_matching_pending_intent() {
        let mut state = PusherState::default();
        state.intents.insert(A.to_owned(), Some(h('1')));
        state.intents.insert(B.to_owned(), None);
        state.synced.insert(B.to_owned(), h('9'));
        state.reconcile(A, Some(&h('2'))); // 对不上：保留
        assert_eq!(state.intents.get(A), Some(&Some(h('1'))));
        state.reconcile(A, Some(&h('1'))); // 对上：并入同步状态
        assert_eq!(state.synced.get(A), Some(&h('1')));
        assert!(!state.intents.contains_key(A));
        state.reconcile(B, None); // 「不存在」意图对上：删条目
        assert!(!state.synced.contains_key(B));
        assert!(state.intents.is_empty());
    }

    #[test]
    fn commit_lost_write_reconciles_into_achieved() {
        // 上一轮写入结果未知（意图保留）、实际已生效：R == N 先对账再判已达成
        let document = doc("正文");
        let n = document.doc_hash(C).unwrap();
        let mut state = with_synced(&[(A, h('1'))]);
        state.intents.insert(A.to_owned(), Some(n.clone()));
        let (action, _) = commit_plan(&mut state, &document, Some(n.clone()), &none());
        assert_eq!(action, Action::Achieved);
        assert_eq!(state.synced.get(A), Some(&n));
    }

    // ------------------------------------------------------------------ delete

    fn delete_plan(
        state: &mut PusherState,
        head: Option<String>,
        expected: &Expected,
    ) -> Plan<'static> {
        delete_plan_with(state, head, expected, &[C])
    }

    fn delete_plan_with(
        state: &mut PusherState,
        head: Option<String>,
        expected: &Expected,
        contracts: &[&str],
    ) -> Plan<'static> {
        state
            .plan_delete(&[A.to_owned()], &[head], expected, C, contracts)
            .pop()
            .unwrap()
    }

    #[test]
    fn delete_table() {
        let plan = delete_plan(&mut with_synced(&[(A, h('1'))]), None, &none());
        assert_eq!(plan.action, Action::Achieved);
        let plan = delete_plan(&mut PusherState::default(), Some(h('1')), &none());
        assert_eq!(
            (plan.action, plan.precondition),
            (Action::Write, Some(Precondition::BaseHash(h('1'))))
        );
        let plan = delete_plan(&mut with_synced(&[(A, h('1'))]), Some(h('1')), &none());
        assert_eq!(
            (plan.action, plan.precondition),
            (Action::Write, Some(Precondition::BaseHash(h('1'))))
        );
        let plan = delete_plan(&mut with_synced(&[(A, h('1'))]), Some(h('2')), &none());
        assert_eq!(plan.action, Action::Conflict);
        let plan = delete_plan(
            &mut PusherState::default(),
            Some(h('1')),
            &expected(&[(A, None)]),
        );
        assert_eq!(plan.action, Action::Conflict);
    }

    #[test]
    fn delete_cross_contract_table() {
        let contracts = [C, dpe_hash::DRILL_CONTRACT];
        let plan = delete_plan_with(
            &mut with_synced(&[(A, drill('1'))]),
            Some(h('1')),
            &none(),
            &contracts,
        );
        assert_eq!(
            (plan.action, plan.precondition),
            (Action::Write, Some(Precondition::BaseHash(drill('1'))))
        );
        let plan = delete_plan_with(
            &mut with_synced(&[(A, drill('1'))]),
            None,
            &none(),
            &contracts,
        );
        assert_eq!(plan.action, Action::Achieved);
    }

    // ------------------------------------------------------------------ move

    fn move_plan(
        state: &mut PusherState,
        from_head: Option<String>,
        to_head: Option<String>,
        expected: &Expected,
    ) -> Plan<'static> {
        move_plan_with(state, from_head, to_head, expected, &[C])
    }

    fn move_plan_with(
        state: &mut PusherState,
        from_head: Option<String>,
        to_head: Option<String>,
        expected: &Expected,
        contracts: &[&str],
    ) -> Plan<'static> {
        state
            .plan_move(
                &[(A.to_owned(), B.to_owned())],
                &[from_head],
                &[to_head],
                expected,
                C,
                contracts,
            )
            .pop()
            .unwrap()
    }

    #[test]
    fn move_table() {
        // 无 L、源存在 → 以 R 作 base
        let plan = move_plan(&mut PusherState::default(), Some(h('1')), None, &none());
        assert_eq!(
            (plan.action, plan.precondition, plan.base),
            (
                Action::Write,
                Some(Precondition::BaseHash(h('1'))),
                Some(h('1'))
            )
        );
        // 有 L、R == L → 以 L 作 base；R ≠ L → 冲突
        let plan = move_plan(
            &mut with_synced(&[(A, h('1'))]),
            Some(h('1')),
            None,
            &none(),
        );
        assert_eq!(plan.action, Action::Write);
        let plan = move_plan(
            &mut with_synced(&[(A, h('1'))]),
            Some(h('2')),
            None,
            &none(),
        );
        assert_eq!(plan.action, Action::Conflict);
        // 源不存在：目标等于 base → 已达成；否则条目失败（同码 DPE_NOT_FOUND）
        let plan = move_plan(
            &mut with_synced(&[(A, h('1'))]),
            None,
            Some(h('1')),
            &none(),
        );
        assert_eq!((plan.action, plan.base), (Action::Achieved, Some(h('1'))));
        let plan = move_plan(
            &mut with_synced(&[(A, h('1'))]),
            None,
            Some(h('2')),
            &none(),
        );
        assert_eq!(plan.action, Action::Failed);
        assert_eq!(
            plan.error.as_ref().and_then(Error::code),
            Some("DPE_NOT_FOUND")
        );
        // 调用方确认源「不存在」而源实际存在 → 冲突
        let plan = move_plan(
            &mut PusherState::default(),
            Some(h('1')),
            None,
            &expected(&[(A, None)]),
        );
        assert_eq!(plan.action, Action::Conflict);
        // 确认不存在且源不存在：不借目标侧的 L（base 不可知）→ 条目失败
        let plan = move_plan(
            &mut with_synced(&[(B, h('1'))]),
            None,
            Some(h('1')),
            &expected(&[(A, None)]),
        );
        assert_eq!(plan.action, Action::Failed);
    }

    #[test]
    fn move_cross_contract_table() {
        let contracts = [C, dpe_hash::DRILL_CONTRACT];
        // 旧契约 L：直接发 move，目标侧待写后重读；to_hash 取源在请求声明契约下的值
        let plan = move_plan_with(
            &mut with_synced(&[(A, drill('1'))]),
            Some(h('1')),
            Some(h('1')),
            &none(),
            &contracts,
        );
        assert_eq!(
            (plan.action, plan.precondition, plan.base, plan.to_hash),
            (
                Action::Write,
                Some(Precondition::BaseHash(drill('1'))),
                Some(drill('1')),
                Some(h('1')),
            )
        );
        assert!(plan.cross_contract && !plan.delete_source);
        // 跨契约且源不存在：沿用条目失败（不新增语义）
        let plan = move_plan_with(
            &mut with_synced(&[(A, drill('1'))]),
            None,
            Some(h('1')),
            &none(),
            &contracts,
        );
        assert_eq!(plan.action, Action::Failed);
    }

    #[test]
    fn move_intents_record_the_declared_contract_value() {
        let contracts = [C, dpe_hash::DRILL_CONTRACT];
        let plans = vec![move_plan_with(
            &mut with_synced(&[(A, drill('1'))]),
            Some(h('1')),
            Some(h('1')),
            &none(),
            &contracts,
        )];
        assert_eq!(
            PusherState::move_intents(&plans).get(B),
            Some(&Some(h('1')))
        );
    }

    #[test]
    fn move_onto_target_holding_same_content_plans_source_delete() {
        // #165：源可写入且目标 R == base → 仍为 Write，但删源而不发 move（有 L / 无 L 两种 base）
        for state in [with_synced(&[(A, h('1'))]), PusherState::default()] {
            let plan = move_plan(&mut state.clone(), Some(h('1')), Some(h('1')), &none());
            assert_eq!(
                (plan.action, plan.delete_source, plan.base),
                (Action::Write, true, Some(h('1')))
            );
        }
        // 目标内容不同、目标不存在：照常发 move
        for to_head in [Some(h('2')), None] {
            let plan = move_plan(
                &mut with_synced(&[(A, h('1'))]),
                Some(h('1')),
                to_head,
                &none(),
            );
            assert_eq!((plan.action, plan.delete_source), (Action::Write, false));
        }
        // 源被改写（R ≠ L）即使目标等于 R 也是冲突，不删源
        let plan = move_plan(
            &mut with_synced(&[(A, h('1'))]),
            Some(h('2')),
            Some(h('2')),
            &none(),
        );
        assert_eq!((plan.action, plan.delete_source), (Action::Conflict, false));
    }

    #[test]
    fn move_replay_after_own_success_is_achieved() {
        // B1：成功后条目已迁到目标；源不存在、源无 L 时以目标侧的 L 作 base 候选
        let mut state = PusherState::default();
        let plans = state.plan_move(
            &[(A.to_owned(), B.to_owned())],
            &[Some(h('1'))],
            &[None],
            &none(),
            C,
            &[C],
        );
        stage(&mut state, PusherState::move_intents(&plans));
        let results = state.finish_move(plans, vec![Some(Outcome::ok(None))]);
        assert_eq!(results[0].status, PushStatus::Moved);
        assert_eq!(state.synced, BTreeMap::from([(B.to_owned(), h('1'))]));
        assert!(state.intents.is_empty());
        // 重放（同一状态；读同一 store 的新实例等价于从同一快照起步）
        for mut replay in [
            state.clone(),
            PusherState::from_snapshot(Some(state.snapshot())),
        ] {
            let plan = move_plan(&mut replay, None, Some(h('1')), &none());
            assert_eq!((plan.action, plan.precondition), (Action::Achieved, None));
        }
    }

    #[test]
    fn move_target_pending_intent_counts_only_after_reconcile() {
        // 目标侧残留意图对不上（R ≠ 意图值）：不当 base → 条目失败，意图原样保留
        let mut state = PusherState::default();
        state.intents.insert(B.to_owned(), Some(h('1')));
        let plan = move_plan(&mut state, None, Some(h('2')), &none());
        assert_eq!(plan.action, Action::Failed);
        let results = state.finish_move(vec![plan], vec![None]);
        assert_eq!(
            (results[0].status, results[0].side),
            (PushStatus::Failed, Some(Side::From))
        );
        assert_eq!(state.intents.get(B), Some(&Some(h('1'))));
        // 对得上（R == 意图值）：对账并入 L → 源不存在且无 L → 已达成
        let mut state = PusherState::default();
        state.intents.insert(B.to_owned(), Some(h('1')));
        let plan = move_plan(&mut state, None, Some(h('1')), &none());
        assert_eq!(plan.action, Action::Achieved);
    }

    #[test]
    fn move_intents_record_both_sides() {
        let mut state = PusherState::default();
        let plans = state.plan_move(
            &[(A.to_owned(), B.to_owned())],
            &[Some(h('1'))],
            &[None],
            &none(),
            C,
            &[C],
        );
        assert_eq!(
            PusherState::move_intents(&plans),
            BTreeMap::from([(A.to_owned(), None), (B.to_owned(), Some(h('1')))])
        );
    }

    // ------------------------------------------------------------------ 收尾：意图的保留与清除

    fn transport_failure(unknown: bool) -> Error {
        Error::transport("boom".into()).with_outcome_unknown(unknown)
    }

    #[test]
    fn unknown_outcome_keeps_the_intent_and_determined_failure_clears_it() {
        for (unknown, kept) in [(true, true), (false, false)] {
            let mut state = PusherState::default();
            let plans = state.plan_delete(&[A.to_owned()], &[Some(h('1'))], &none(), C, &[C]);
            stage(&mut state, PusherState::delete_intents(&plans));
            let results = state.finish_delete(
                plans,
                vec![Some(Outcome::undetermined(transport_failure(unknown)))],
            );
            assert_eq!(
                (results[0].status, results[0].unknown),
                (PushStatus::Failed, unknown)
            );
            assert_eq!(state.intents.contains_key(A), kept);
        }
    }

    #[test]
    fn server_conflict_clears_the_intent_but_plan_conflict_keeps_pending_ones() {
        // 服务端 CAS 裁决：结果已确定 → 清意图，回报带重读值
        let mut state = PusherState::default();
        let plans = state.plan_delete(&[A.to_owned()], &[Some(h('1'))], &none(), C, &[C]);
        stage(&mut state, PusherState::delete_intents(&plans));
        let outcome = Outcome::failure(
            Error::protocol(ErrorKind::PreconditionFailed, "x"),
            OutcomeKind::Conflict,
        )
        .with_reread(Ok(Some(h('2'))));
        let results = state.finish_delete(plans, vec![Some(outcome)]);
        assert_eq!(results[0].status, PushStatus::Conflict);
        assert_eq!(
            results[0].remote,
            Some(RemoteState {
                doc_hash: Some(h('2'))
            })
        );
        assert!(state.intents.is_empty());
        // 判定表判出的冲突（未发写）：既有待定意图保留（那条写入的结果仍未知）
        let mut state = with_synced(&[(A, h('1'))]);
        state.intents.insert(A.to_owned(), Some(h('3')));
        let plans = state.plan_delete(&[A.to_owned()], &[Some(h('2'))], &none(), C, &[C]);
        let results = state.finish_delete(plans, vec![None]);
        assert_eq!(results[0].status, PushStatus::Conflict);
        assert_eq!(state.intents.get(A), Some(&Some(h('3'))));
    }

    /// 写后 CAS 冲突的收尾（#140）：`reread` 为重读结果（`None` 表示重读失败）。
    fn conflicted(code: ErrorKind, reread: Option<Option<String>>) -> Outcome {
        let outcome = Outcome::failure(Error::protocol(code, "cas"), OutcomeKind::Conflict);
        match reread {
            Some(doc_hash) => outcome.with_reread(Ok(doc_hash)),
            None => outcome.with_reread(Err(Error::invalid_argument("reread failed"))),
        }
    }

    #[test]
    fn commit_conflict_whose_reread_equals_the_content_is_achieved() {
        let document = doc("v2");
        let n = document.doc_hash(C).unwrap();
        let mut state = with_synced(&[(A, h('1'))]);
        let prepared = prepared_doc(&document);
        let entry = Entry {
            uri: A.to_owned(),
            document: &document,
            new_hash: n.clone(),
            prepared,
        };
        let plans = state.plan_commit(vec![entry], &[Some(h('1'))], &none(), C, &[C]);
        stage(&mut state, PusherState::commit_intents(&plans));
        let outcome = conflicted(ErrorKind::PreconditionFailed, Some(Some(n.clone())));

        let results = state.finish_commit(plans, vec![Some(outcome)]);

        assert_eq!(results[0].status, PushStatus::Achieved);
        assert_eq!(
            results[0].precondition,
            Some(Precondition::BaseHash(h('1')))
        );
        assert_eq!(
            results[0].remote,
            Some(RemoteState {
                doc_hash: Some(n.clone())
            })
        );
        assert_eq!(
            results[0].error.as_ref().and_then(Error::code),
            Some("DPE_PRECONDITION_FAILED"),
            "已达成也带原 CAS 错误"
        );
        assert_eq!(state.synced.get(A), Some(&n), "确认 L = N");
        assert!(state.intents.is_empty());
    }

    #[test]
    fn commit_conflict_whose_reread_differs_stays_a_conflict() {
        let document = doc("v2");
        let mut state = with_synced(&[(A, h('1'))]);
        let prepared = prepared_doc(&document);
        let entry = Entry {
            uri: A.to_owned(),
            document: &document,
            new_hash: prepared.hashes.doc_hash.clone(),
            prepared,
        };
        let plans = state.plan_commit(vec![entry], &[Some(h('1'))], &none(), C, &[C]);
        stage(&mut state, PusherState::commit_intents(&plans));

        let results = state.finish_commit(
            plans,
            vec![Some(conflicted(
                ErrorKind::PreconditionFailed,
                Some(Some(h('9'))),
            ))],
        );

        assert_eq!(results[0].status, PushStatus::Conflict);
        assert_eq!(state.synced.get(A), Some(&h('1')), "冲突不更新同步状态");
        assert!(state.intents.is_empty(), "结果确定：清意图");
    }

    #[test]
    fn delete_conflict_whose_reread_is_absent_is_deleted() {
        let mut state = with_synced(&[(A, h('1'))]);
        let plans = state.plan_delete(&[A.to_owned()], &[Some(h('1'))], &none(), C, &[C]);
        stage(&mut state, PusherState::delete_intents(&plans));

        let results = state.finish_delete(
            plans,
            vec![Some(conflicted(ErrorKind::PreconditionFailed, Some(None)))],
        );

        assert_eq!(results[0].status, PushStatus::Deleted);
        assert_eq!(results[0].remote, Some(RemoteState { doc_hash: None }));
        assert_eq!(
            results[0].error.as_ref().and_then(Error::code),
            Some("DPE_PRECONDITION_FAILED")
        );
        assert_eq!(state, PusherState::default(), "删条目、清意图");
    }

    #[test]
    fn delete_conflict_with_failed_reread_stays_a_conflict() {
        let mut state = with_synced(&[(A, h('1'))]);
        let plans = state.plan_delete(&[A.to_owned()], &[Some(h('1'))], &none(), C, &[C]);
        stage(&mut state, PusherState::delete_intents(&plans));

        let results = state.finish_delete(
            plans,
            vec![Some(conflicted(ErrorKind::PreconditionFailed, None))],
        );

        assert_eq!(results[0].status, PushStatus::Conflict);
        assert!(results[0].remote.is_none() && results[0].remote_error.is_some());
        assert_eq!(
            state.synced.get(A),
            Some(&h('1')),
            "条目保留（未确认任何结果）"
        );
    }

    #[test]
    fn delete_server_not_found_is_achieved_with_the_error() {
        let mut state = with_synced(&[(A, h('1'))]);
        let plans = state.plan_delete(&[A.to_owned()], &[Some(h('1'))], &none(), C, &[C]);
        let outcome = Outcome::failure(
            Error::protocol(ErrorKind::NotFound, "gone"),
            OutcomeKind::Achieved,
        );
        let results = state.finish_delete(plans, vec![Some(outcome)]);
        assert_eq!(results[0].status, PushStatus::Deleted);
        assert!(
            results[0].remote.is_none(),
            "写后 NotFound 不重读：remote 为 None（与 Python 对等）"
        );
        assert_eq!(
            results[0].error.as_ref().and_then(Error::code),
            Some("DPE_NOT_FOUND")
        );
        assert!(state.synced.is_empty());
    }

    #[test]
    fn commit_success_confirms_the_server_hash() {
        let document = doc("正文");
        let mut state = PusherState::default();
        let prepared = prepared_doc(&document);
        let entry = Entry {
            uri: A.to_owned(),
            document: &document,
            new_hash: prepared.hashes.doc_hash.clone(),
            prepared,
        };
        let plans = state.plan_commit(vec![entry], &[None], &none(), C, &[C]);
        stage(&mut state, PusherState::commit_intents(&plans));
        let committed = CommitResult {
            status: CommitStatus::Created,
            doc_hash: h('7'),
            delta: Delta {
                added: 1,
                removed: 0,
                retained: 0,
            },
        };
        let results = state.finish_commit(plans, vec![Some(Outcome::ok(Some(committed)))]);
        assert_eq!(
            (results[0].status, results[0].precondition.clone()),
            (PushStatus::Created, Some(Precondition::IfAbsent))
        );
        assert_eq!(state.synced.get(A), Some(&h('7')));
        assert!(state.intents.is_empty());
    }

    // ------------------------------------------------------------------ 批校验（整批拒绝）

    fn invalid(result: Result<impl fmt::Debug, Error>) -> ErrorKind {
        result.expect_err("应整批拒绝").kind()
    }

    #[test]
    fn local_validation_rejects_the_whole_batch() {
        let document = doc("正文");
        let dup = vec![
            (A.to_owned(), document.clone()),
            ("DPE://docs/a.md".to_owned(), document),
        ];
        assert_eq!(
            invalid(PusherState::prepare(&dup, &none(), C, &[C]).map(|_| ())),
            ErrorKind::InvalidArgument
        );
        // uri 文法错误：同 dpe-hash 的同码协议错误
        assert_eq!(
            invalid(PusherState::prepare_uris(
                &["not a uri".to_owned()],
                &none(),
                &[C]
            )),
            ErrorKind::Validation
        );
        // expected_base：键在批外、值不是受支持契约的 hash
        let uris = [A.to_owned()];
        assert_eq!(
            invalid(PusherState::prepare_uris(
                &uris,
                &expected(&[(B, None)]),
                &[C]
            )),
            ErrorKind::InvalidArgument
        );
        assert_eq!(
            invalid(PusherState::prepare_uris(
                &uris,
                &expected(&[(A, Some(drill('1')))]),
                &[C]
            )),
            ErrorKind::InvalidArgument
        );
    }

    #[test]
    fn expected_base_accepts_a_still_supported_old_contract() {
        // 与 Python 的 test_expected_base_accepts_a_still_supported_old_contract_value 对等：
        // expected_base 是 L 的覆盖值，口径同 base_of——服务端仍接受其契约时照常可用
        let document = doc("正文");
        let documents = vec![(A.to_owned(), document)];
        let contracts = [C, dpe_hash::DRILL_CONTRACT];
        assert!(PusherState::prepare(
            &documents,
            &expected(&[(A, Some(drill('1')))]),
            C,
            &contracts,
        )
        .is_ok());
        let uris = [A.to_owned()];
        assert!(
            PusherState::prepare_uris(&uris, &expected(&[(A, Some(drill('1')))]), &contracts,)
                .is_ok()
        );
        let moves = vec![(A.to_owned(), B.to_owned())];
        assert!(PusherState::prepare_moves(
            &moves,
            &expected(&[(A, Some(drill('1')))]),
            &contracts,
        )
        .is_ok());
    }

    #[test]
    fn overlapping_moves_are_rejected() {
        let pairs = |list: &[(&str, &str)]| -> Vec<(String, String)> {
            list.iter()
                .map(|(from, to)| ((*from).to_owned(), (*to).to_owned()))
                .collect()
        };
        for moves in [
            pairs(&[(A, B), (B, X)]),                 // 链式
            pairs(&[(A, X), (B, X)]),                 // 共用目标
            pairs(&[(A, B), (A, X)]),                 // 重复源
            pairs(&[(A, A)]),                         // 源＝目标
            pairs(&[(A, X), ("DPE://docs/b.md", B)]), // 归一化后才重叠
            pairs(&[("DPE://docs/a.md", A)]),         // 归一化后源＝目标
        ] {
            assert_eq!(
                invalid(PusherState::prepare_moves(&moves, &none(), &[C])),
                ErrorKind::InvalidArgument,
                "{moves:?}"
            );
        }
        // expected_base 以 from_uri 为键：目标作键属批外
        assert_eq!(
            invalid(PusherState::prepare_moves(
                &pairs(&[(A, B)]),
                &expected(&[(B, None)]),
                &[C]
            )),
            ErrorKind::InvalidArgument
        );
        assert!(PusherState::prepare_moves(
            &pairs(&[(A, B), (X, "dpe://docs/y.md")]),
            &none(),
            &[C]
        )
        .is_ok());
    }
}
