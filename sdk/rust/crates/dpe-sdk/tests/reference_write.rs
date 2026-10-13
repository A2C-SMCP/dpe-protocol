//! 写操作对参考服务端（#25）：与 Python `tests/test_protocol_write_http.py`（#50）逐条对等。
//!
//! 对手是 Python 参考服务端子进程（见 `reference_common`）；未配置 `DPE_REFERENCE_SERVER` 时
//! 跳过（CI 的 reference job 强制）。

#![cfg(feature = "http")]

mod reference_common;

use dpe_sdk::protocol::Precondition;
use dpe_sdk::wire::CommitStatus;
use dpe_sdk::{Error, ErrorKind};
use serde_json::json;

use reference_common::{doc, doc_hash, drive, from_value, image_bytes, Injection, ReferenceServer};

const URI: &str = "test://docs/a";
const TARGET: &str = "test://docs/b";
const THIRD: &str = "test://docs/c";
const FRESH: &str = "test://docs/fresh";
const TARGET_QUERY: &str = "documents?uri=test%3A%2F%2Fdocs%2Fa";

fn image() -> Vec<u8> {
    image_bytes()
}

/// 引用一个 blob 的文档：blob 永远不能内联（core §6），快路径提交必报缺内容。
fn image_doc() -> dpe_sdk::ExpandedDocument {
    from_value(json!({
        "file_type": "png",
        "pages": [{"elements": [
            {"category": "Image", "blob": dpe_hash::blob_ref(&image()), "mime_type": "image/png"}
        ]}],
    }))
}

fn kind<T: std::fmt::Debug>(result: Result<T, Error>) -> ErrorKind {
    result.expect_err("应失败").kind()
}

#[tokio::test]
async fn cas_matrix_over_the_reference_server() {
    // CAS 全矩阵（core §3.3 第 4–5 步、§5）：三种前置条件的成功与失败分支，失败不改变内容
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let (first, changed, other) = (doc(&["a", "b"]), doc(&["a", "c"]), doc(&["z"]));

    // 快路径：一条 PUT 内联全部对象；IfAbsent 新建
    let mut recorded = Injection::new();
    let created = drive(
        &server,
        &client,
        core.commit(URI, &first, Precondition::IfAbsent, None)
            .unwrap(),
        &mut recorded,
    )
    .await
    .unwrap();
    assert_eq!(
        (created.status, created.delta.added),
        (CommitStatus::Created, 2)
    );
    assert_eq!(created.doc_hash, doc_hash(&first));
    assert_eq!(
        recorded.methods_and_targets(),
        [("PUT".to_owned(), TARGET_QUERY.to_owned())]
    );

    // IfAbsent + 不同内容 → ALREADY_EXISTS，服务端内容未被覆盖
    assert_eq!(
        kind(
            client
                .commit(URI, &changed, Precondition::IfAbsent, None)
                .await
        ),
        ErrorKind::AlreadyExists
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, created.doc_hash);

    // IfAbsent + 相同内容 → unchanged（第 4 步先于前置条件求值）
    let replay = client
        .commit(URI, &first, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(
        (
            replay.status,
            replay.delta.added,
            replay.delta.removed,
            replay.delta.retained
        ),
        (CommitStatus::Unchanged, 0, 0, 2)
    );

    // BaseHash 命中 → updated，delta 按元素多重集
    let updated = client
        .commit(
            URI,
            &changed,
            Precondition::BaseHash(created.doc_hash.clone()),
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        (
            updated.status,
            updated.delta.added,
            updated.delta.removed,
            updated.delta.retained
        ),
        (CommitStatus::Updated, 1, 1, 1)
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, doc_hash(&changed));

    // BaseHash 过期 → PRECONDITION_FAILED，内容未被覆盖
    assert_eq!(
        kind(
            client
                .commit(
                    URI,
                    &other,
                    Precondition::BaseHash(created.doc_hash.clone()),
                    None
                )
                .await
        ),
        ErrorKind::PreconditionFailed
    );
    let skeleton = client.get_skeleton(URI).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, updated.doc_hash);

    // BaseHash 指向不存在的文档 → NOT_FOUND
    let missing = "test://docs/missing";
    assert_eq!(
        kind(
            client
                .commit(
                    missing,
                    &other,
                    Precondition::BaseHash(doc_hash(&other)),
                    None
                )
                .await
        ),
        ErrorKind::NotFound
    );
    assert_eq!(client.head(missing).await.unwrap(), None);

    // Force：不看现状直接覆盖；相同内容仍是 unchanged；到不存在的文档是 created
    let forced = client
        .commit(URI, &other, Precondition::Force, None)
        .await
        .unwrap();
    assert_eq!(
        (forced.status, forced.doc_hash),
        (CommitStatus::Updated, doc_hash(&other))
    );
    let again = client
        .commit(URI, &other, Precondition::Force, None)
        .await
        .unwrap();
    assert_eq!(again.status, CommitStatus::Unchanged);
    let fresh = client
        .commit(missing, &other, Precondition::Force, None)
        .await
        .unwrap();
    assert_eq!(
        (fresh.status, fresh.doc_hash),
        (CommitStatus::Created, doc_hash(&other))
    );
}

#[tokio::test]
async fn commit_evaluation_order_over_the_reference_server() {
    // commit 求值顺序（core §3.3）：unchanged 先于会话，前置条件先于会话与可得性。
    // Python 用进程内 FakeClock 拨过 TTL；这里用 expire-session 钩子让同一个会话过期（等价）
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1", "--hooks", "/__hooks__"])
    else {
        return;
    };
    let client = server.client();
    let (current, changed, image) = (doc(&["a", "b"]), doc(&["a", "c"]), image_doc());
    let created = client
        .commit(URI, &current, Precondition::IfAbsent, None)
        .await
        .unwrap();

    // 开一个真实会话再让它过期：后续引用它时只有第 6 步会碰到会话
    let negotiated = client.negotiate(URI, &changed).await.unwrap();
    assert!(server.expire_session(&negotiated.session.id).await);
    let session = Some(&negotiated.session);

    // 第 4 步先于第 6 步：内容不变时即使引用过期会话也是 unchanged（不 410）
    let replay = client
        .commit(
            URI,
            &current,
            Precondition::BaseHash(created.doc_hash.clone()),
            session,
        )
        .await
        .unwrap();
    assert_eq!(replay.status, CommitStatus::Unchanged);

    // 第 5 步先于第 6 步：前置条件失败（陈旧 base_hash）优先于会话过期
    let stale = doc_hash(&doc(&["z"]));
    assert_eq!(
        kind(
            client
                .commit(URI, &changed, Precondition::BaseHash(stale), session)
                .await
        ),
        ErrorKind::PreconditionFailed
    );

    // 顺序确实走到了第 6 步：前置条件成立、内容有变时，同一会话报过期
    assert_eq!(
        kind(
            client
                .commit(
                    URI,
                    &changed,
                    Precondition::BaseHash(created.doc_hash.clone()),
                    session
                )
                .await
        ),
        ErrorKind::SessionExpired
    );

    // 第 5 步先于缺内容：已存在时 IfAbsent 先失败，缺 blob 不参与
    assert_eq!(
        kind(
            client
                .commit(URI, &image, Precondition::IfAbsent, None)
                .await
        ),
        ErrorKind::AlreadyExists
    );

    // 前置条件成立时才轮到第 6 步：缺 blob 如实报缺（带分层清单）
    let error = client
        .commit(FRESH, &image, Precondition::IfAbsent, None)
        .await
        .expect_err("缺 blob");
    assert_eq!(error.kind(), ErrorKind::MissingContent);
    assert_eq!(
        error.missing().expect("带缺失清单").blobs,
        [dpe_hash::blob_ref(&self::image())]
    );
}

#[tokio::test]
async fn commit_retries_over_the_reference_server() {
    // 响应丢失后的重发（core §5.2）：CAS 提交重发得 unchanged；force 不重放、先 head 判定
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let document = doc(&["a", "b"]);

    // 首次已生效、响应丢失 → 原样重发 → unchanged（首次的 delta 不可得）
    let mut lossy = Injection::new().lose(|request| request.method.as_str() == "PUT");
    let created = drive(
        &server,
        &client,
        core.commit(URI, &document, Precondition::IfAbsent, None)
            .unwrap(),
        &mut lossy,
    )
    .await
    .unwrap();
    assert_eq!(
        (created.status, created.doc_hash.clone()),
        (CommitStatus::Unchanged, doc_hash(&document))
    );
    assert_eq!(lossy.methods(), ["PUT", "PUT"]);
    assert_eq!(client.head(URI).await.unwrap(), Some(doc_hash(&document)));

    // force：响应丢失后不重放，先 head；等于提交内容即成功（不重发 PUT）
    let mut lossy = Injection::new().lose(|request| request.method.as_str() == "PUT");
    let confirmed = drive(
        &server,
        &client,
        core.commit(URI, &document, Precondition::Force, None)
            .unwrap(),
        &mut lossy,
    )
    .await
    .unwrap();
    assert_eq!(
        (confirmed.status, confirmed.doc_hash),
        (CommitStatus::Unchanged, doc_hash(&document))
    );
    assert_eq!(lossy.methods(), ["PUT", "HEAD"]);
}

#[tokio::test]
async fn delete_and_move_retries_over_the_reference_server() {
    // delete / move 的响应丢失重试与前置条件（core §4、§5.2），以最终内容断言
    let Some(server) = ReferenceServer::spawn(&["--prefix", "/r/1"]) else {
        return;
    };
    let client = server.client();
    let core = client.core().await.unwrap();
    let (document, other) = (doc(&["a", "b"]), doc(&["z"]));
    let created = client
        .commit(URI, &document, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(created.status, CommitStatus::Created);

    // delete：首次已生效、响应丢失 → 重发得 NOT_FOUND → 视为成功
    let mut lossy = Injection::new().lose(|request| request.method.as_str() == "DELETE");
    drive(
        &server,
        &client,
        core.delete(URI, &created.doc_hash).unwrap(),
        &mut lossy,
    )
    .await
    .unwrap();
    assert_eq!(lossy.methods(), ["DELETE", "DELETE"]);
    assert_eq!(client.head(URI).await.unwrap(), None);

    // 首次 delete 就 NOT_FOUND：如实抛出
    assert_eq!(
        kind(client.delete(URI, &created.doc_hash).await),
        ErrorKind::NotFound
    );

    // delete 前置条件不符 → 412，文档仍在（未被删）
    let recreated = client
        .commit(URI, &document, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(
        kind(client.delete(URI, &doc_hash(&other)).await),
        ErrorKind::PreconditionFailed
    );
    assert_eq!(
        client.head(URI).await.unwrap(),
        Some(recreated.doc_hash.clone())
    );

    // move：首次已生效、响应丢失 → 重发命中「目标状态已达成」→ 成功且内容完整
    let mut lossy = Injection::new().lose(|request| request.target == "move");
    let moved = drive(
        &server,
        &client,
        core.move_document(URI, TARGET, &recreated.doc_hash)
            .unwrap(),
        &mut lossy,
    )
    .await
    .unwrap();
    assert_eq!(moved.doc_hash, recreated.doc_hash);
    let targets: Vec<&str> = lossy
        .sent
        .iter()
        .map(|request| request.target.as_str())
        .collect();
    assert_eq!(targets, ["move", "move"]);
    assert_eq!(client.head(URI).await.unwrap(), None);
    let skeleton = client.get_skeleton(TARGET).await.unwrap().unwrap();
    assert_eq!(skeleton.doc_hash, recreated.doc_hash);

    // move 前置条件不符（由请求体承载）→ 409 → PreconditionFailed；两侧都未变
    assert_eq!(
        kind(client.move_document(TARGET, THIRD, &doc_hash(&other)).await),
        ErrorKind::PreconditionFailed
    );
    assert_eq!(
        client.head(TARGET).await.unwrap(),
        Some(recreated.doc_hash.clone())
    );
    assert_eq!(client.head(THIRD).await.unwrap(), None);

    // move 到已存在的目标 → ALREADY_EXISTS（409），两侧都未变
    client
        .commit(THIRD, &other, Precondition::IfAbsent, None)
        .await
        .unwrap();
    assert_eq!(
        kind(
            client
                .move_document(TARGET, THIRD, &recreated.doc_hash)
                .await
        ),
        ErrorKind::AlreadyExists
    );
    assert_eq!(
        client.head(TARGET).await.unwrap(),
        Some(recreated.doc_hash.clone())
    );
    assert_eq!(client.head(THIRD).await.unwrap(), Some(doc_hash(&other)));
}
