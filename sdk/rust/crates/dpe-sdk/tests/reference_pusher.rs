//! 增量推送器对参考服务端（#25）：与 Python `tests/test_pusher.py` 的 parity 场景
//! （`_scenario` / `test_sync_and_async_agree_against_the_same_server`）逐步对等。
//!
//! Python 那里是同步 / 异步两个推送器的自我对照；Rust 只有异步推送器，这里对同一序列逐步断言
//! 完整的回报视图（同 Python `_view` 的字段：op、file_uri、status、precondition、delta、remote、
//! to_uri、side、error、remote_error、unknown）。他人的写入由第二个客户端经公开协议完成，不需要
//! 注入。

#![cfg(feature = "http")]

mod reference_common;

use std::collections::HashMap;
use std::sync::Arc;

use dpe_sdk::protocol::Precondition;
use dpe_sdk::pusher::{MemoryStateStore, PushOp, PushResult, PushStatus, Side, StatefulPusher};

use reference_common::{doc, doc_hash, ReferenceServer};

/// 一条回报的可比较视图（字段同 Python `_view`；`error` 归约为「是否有错 + code」，
/// `remote_error` 归约为是否存在）。
#[derive(Debug, Clone, PartialEq, Eq)]
struct View {
    op: PushOp,
    file_uri: String,
    status: PushStatus,
    precondition: Option<Precondition>,
    delta: Option<(u64, u64, u64)>,
    remote: Option<Option<String>>,
    to_uri: Option<String>,
    side: Option<Side>,
    error: Option<Option<String>>,
    remote_error: bool,
    unknown: bool,
}

impl View {
    fn of(result: &PushResult) -> View {
        View {
            op: result.op,
            file_uri: result.file_uri.clone(),
            status: result.status,
            precondition: result.precondition.clone(),
            delta: result
                .delta
                .map(|delta| (delta.added, delta.removed, delta.retained)),
            remote: result.remote.as_ref().map(|remote| remote.doc_hash.clone()),
            to_uri: result.to_uri.clone(),
            side: result.side,
            error: result
                .error
                .as_ref()
                .map(|error| error.code().map(str::to_owned)),
            remote_error: result.remote_error.is_some(),
            unknown: result.unknown,
        }
    }

    /// 期望视图：其余字段缺省为空 / false，按步骤逐项补上。
    fn expect(op: PushOp, file_uri: &str, status: PushStatus) -> View {
        View {
            op,
            file_uri: file_uri.to_owned(),
            status,
            precondition: None,
            delta: None,
            remote: None,
            to_uri: None,
            side: None,
            error: None,
            remote_error: false,
            unknown: false,
        }
    }

    fn pre(mut self, precondition: Precondition) -> View {
        self.precondition = Some(precondition);
        self
    }

    fn delta(mut self, added: u64, removed: u64, retained: u64) -> View {
        self.delta = Some((added, removed, retained));
        self
    }

    fn remote(mut self, doc_hash: Option<String>) -> View {
        self.remote = Some(doc_hash);
        self
    }

    fn to(mut self, to_uri: &str) -> View {
        self.to_uri = Some(to_uri.to_owned());
        self
    }

    fn side(mut self, side: Side) -> View {
        self.side = Some(side);
        self
    }

    fn error(mut self, code: &str) -> View {
        self.error = Some(Some(code.to_owned()));
        self
    }
}

fn views(results: &[PushResult]) -> Vec<View> {
    results.iter().map(View::of).collect()
}

fn none() -> HashMap<String, Option<String>> {
    HashMap::new()
}

#[tokio::test]
async fn pusher_scenario_agrees_with_the_python_parity_run() {
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1"]) else {
        return;
    };
    let (client, foreign) = (server.client(), server.client());
    let mut pusher =
        StatefulPusher::with_store(&client, Arc::new(MemoryStateStore::new()), 4).unwrap();
    let base = "dpe://parity/rust";
    let (uri, moved, occupied) = (
        format!("{base}/a.md"),
        format!("{base}/moved.md"),
        format!("{base}/occupied.md"),
    );
    let (one, two, three, theirs, occupy) = (
        doc(&["one"]),
        doc(&["two"]),
        doc(&["three"]),
        doc(&["theirs"]),
        doc(&["occupy"]),
    );
    let batch = |document: &dpe_sdk::ExpandedDocument, target: &str| {
        vec![(target.to_owned(), document.clone())]
    };
    use PushOp::{Commit, Delete, Move};

    // ① 新建 → created（if_absent）
    let results = pusher
        .push(&batch(&one, &uri), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &uri, PushStatus::Created)
            .pre(Precondition::IfAbsent)
            .delta(1, 0, 0)]
    );
    // ② 同内容 → achieved（R == N，不发写）
    let results = pusher
        .push(&batch(&one, &uri), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &uri, PushStatus::Achieved).remote(Some(doc_hash(&one)))]
    );
    // ③ 更新 → updated，以 L 作 base
    let results = pusher
        .push(&batch(&two, &uri), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &uri, PushStatus::Updated)
            .pre(Precondition::BaseHash(doc_hash(&one)))
            .delta(1, 1, 0)]
    );
    // ④ 他人改写 → 冲突（不发写，带读到的 remote）
    let current = pusher.snapshot().synced[&uri].clone();
    foreign
        .commit(&uri, &theirs, Precondition::BaseHash(current), None)
        .await
        .unwrap();
    let conflicted = pusher
        .push(&batch(&three, &uri), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&conflicted),
        [View::expect(Commit, &uri, PushStatus::Conflict).remote(Some(doc_hash(&theirs)))]
    );
    // ⑤ 以重读值作 expected_base 重提 → updated
    let reread = conflicted[0]
        .remote
        .as_ref()
        .and_then(|remote| remote.doc_hash.clone())
        .expect("冲突带读到的 remote");
    let expected = HashMap::from([(uri.clone(), Some(reread.clone()))]);
    let results = pusher
        .push(&batch(&three, &uri), &expected, None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &uri, PushStatus::Updated)
            .pre(Precondition::BaseHash(reread))
            .delta(1, 1, 0)]
    );
    // ⑥ move → moved（迁条目）
    let pair = vec![(uri.clone(), moved.clone())];
    let results = pusher.move_documents(&pair, &none()).await.unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Move, &uri, PushStatus::Moved)
            .pre(Precondition::BaseHash(doc_hash(&three)))
            .to(&moved)]
    );
    // ⑦ 重放自己的 move：已达成、不发写
    let results = pusher.move_documents(&pair, &none()).await.unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Move, &uri, PushStatus::Moved)
            .remote(None)
            .to(&moved)]
    );
    // ⑧ 目标处推同内容 → achieved（证明条目已迁移）
    let results = pusher
        .push(&batch(&three, &moved), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &moved, PushStatus::Achieved).remote(Some(doc_hash(&three)))]
    );
    // ⑨ 占住另一个目标
    let results = pusher
        .push(&batch(&occupy, &occupied), &none(), None)
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Commit, &occupied, PushStatus::Created)
            .pre(Precondition::IfAbsent)
            .delta(1, 0, 0)]
    );
    // ⑩ 目标被占（源未变、目标内容不同）→ 条目失败 side=to，带重读到的目标
    let results = pusher
        .move_documents(&[(moved.clone(), occupied.clone())], &none())
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Move, &moved, PushStatus::Failed)
            .pre(Precondition::BaseHash(doc_hash(&three)))
            .remote(Some(doc_hash(&occupy)))
            .to(&occupied)
            .side(Side::To)
            .error("DPE_ALREADY_EXISTS")]
    );
    // 目标已持有与源相同的内容（#165）→ 不发 move、以 base 删源 → moved；源删除、目标保留
    let (same_from, same_to) = (format!("{base}/same-from.md"), format!("{base}/same-to.md"));
    let same = doc(&["same"]);
    let results = pusher
        .push(
            &[
                (same_from.clone(), same.clone()),
                (same_to.clone(), same.clone()),
            ],
            &none(),
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [
            View::expect(Commit, &same_from, PushStatus::Created)
                .pre(Precondition::IfAbsent)
                .delta(1, 0, 0),
            View::expect(Commit, &same_to, PushStatus::Created)
                .pre(Precondition::IfAbsent)
                .delta(1, 0, 0),
        ]
    );
    let results = pusher
        .move_documents(&[(same_from.clone(), same_to.clone())], &none())
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Move, &same_from, PushStatus::Moved)
            .pre(Precondition::BaseHash(doc_hash(&same)))
            .remote(Some(doc_hash(&same)))
            .to(&same_to)]
    );
    assert_eq!(foreign.head(&same_from).await.unwrap(), None);
    assert_eq!(foreign.head(&same_to).await.unwrap(), Some(doc_hash(&same)));
    // ⑪ delete → deleted；⑫ 再删同 URI → 已达成（零写入）
    let results = pusher
        .delete(std::slice::from_ref(&moved), &none())
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Delete, &moved, PushStatus::Deleted)
            .pre(Precondition::BaseHash(doc_hash(&three)))]
    );
    let results = pusher
        .delete(std::slice::from_ref(&moved), &none())
        .await
        .unwrap();
    assert_eq!(
        views(&results),
        [View::expect(Delete, &moved, PushStatus::Deleted).remote(None)]
    );
    assert_eq!(client.head(&moved).await.unwrap(), None);
}
