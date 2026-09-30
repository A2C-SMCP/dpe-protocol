# 服务端一致性跑分器（M2）

黑盒 HTTP 跑分器：对着任意实现了 [core.md](../spec/core.md) + [HTTP 绑定](../spec/bindings/http.md) 的 remote 跑一遍协议语义（CAS、暂存原子切换、delta 如实回报、错误格式等），供各服务端在自己的 CI 中执行。

M1 只保留占位；实现随 M2 交付（plan §14）。计划中的检查面：

- capabilities 协商与限额如实声明（含 `idempotency_window_seconds` 不低于下限、`dedup_scope` 取值合法）；
- 快路径 / 暂存路径的 commit 语义与原子性（分批期间读接口不可见中间态）；
- delta 与 `get_skeleton` 读回结果互相印证（不制造伪变更）；
- 服务端回报的 doc_hash / state_hash 等于按契约 1 对推送内容算出的值（私有排除键不得影响结果，契约 1 §2.1）；
- CAS 全矩阵（if_absent / base_revision / 缺前置 / force）与删除复活防护；
- delete / move 语义（move 后目标 doc_hash / state_hash 与源一致）；
- RFC 9457 错误格式与 `code` / `retryable` / `Retry-After`；每个 code 与 HTTP §6 的状态码一致；
- 增量推送、删除、move 收敛后与全量推送一致（判据 plan §13）。

## 字段不被静默丢弃（#4 A1 / A2）

- **只改不进 hash 的字段**：分别只改某个 entry 的 `occurrence.coordinates`、`occurrence.image_url`、`occurrence.parent_id`，以及只改 `attributes`：
  - `batch_head` 返回的 state_hash 变化、doc_hash 不变；
  - 快路径 commit 返回 `updated`、新 revision、delta 全为 retained；
  - `get_skeleton` 读回新值。
- **重复内容、不同坐标**：同一内容出现在两个位置、各带不同 `occurrence`，读回后两处分别保留；对调两处坐标后 commit 为 `updated`。
- **unchanged 判定**：完全相同的重复 commit 返回 `unchanged`，revision 不变。
- **报文校验**：以下情况都返回 `DPE_VALIDATION`：
  - metadata（任一层）中出现任一保留键；
  - 内容对象带其 category 未规定的字段（如 NarrativeText 带 `text_as_html`、任何内容对象带 `image_url`）；
  - occurrence 出现未知键，或非 Image entry 带 `image_url`；
  - pages 未按 number 严格升序。

## 重试与会话（#4 B1 / B4 / B7）

- **幂等**：
  - 同键同请求在窗口内重放首次响应（含 status 与 delta），且不产生新 revision；
  - 同键不同请求返回 `422` + `DPE_IDEMPOTENCY_MISMATCH`；
  - 同键并发返回 `409` + `DPE_IDEMPOTENCY_IN_PROGRESS` 并带 `Retry-After`；
  - 首次得到 `429` / `503` 的请求，同键重试会被真正执行，不重放 429 / 503；
  - 缺少 `Idempotency-Key` 的写请求返回 `400` + `DPE_VALIDATION`。
- **状态恢复判定前提**：构造 commit（base_revision / if_absent / 引用会话）、delete、move 首次成功但客户端视为失败的场景，窗口外重试分别得到 `DPE_REVISION_CONFLICT` / `DPE_ALREADY_EXISTS` / `DPE_SESSION_EXPIRED` / `DPE_NOT_FOUND` 等 core.md §5.2 所列错误，且 `head` 的结果满足判定条件。
- **同级写入**：服务端经自身入口修改 attributes 后，`head` 的 revision 与 state_hash 均变化（core.md §2.5）。
- **会话失效**：过期、已消费、伪造 id、他人会话一律返回 `410` + `DPE_SESSION_EXPIRED`，`retryable: false`；会话不存在不得返回 404。
- **会话跨 revision 复用**：开会话并上传后，并发修改 attributes；原 commit 得到 `DPE_REVISION_CONFLICT`，以新 `If-Match` 引用同一会话重新 commit 成功，无需重传内容。

## HTTP 绑定（#4 B5 / B6）

- 条件头只出现在文档资源上（`PUT` / `DELETE documents?uri=`）；move 的前置条件在请求体中，冲突返回 `409`。
- 响应带 `DPE-Revision`；用 `W/"…"` 形式的 If-Match 写入必然返回 412；按 `DPE-Revision` 构造的 If-Match 写入成功。
- 对已删除文档带 `If-Match` 的 PUT / DELETE 返回 `412` + `DPE_NOT_FOUND`；move 响应带指向目标文档的 `Content-Location`，不带 `ETag`。

## 安全（#4 B9）

- 调用者 A 只对前缀 P 有写授权，前缀 Q 下的文档含内容 X：
  - A 对 P 下文档 negotiate 时，X 必须出现在 `missing_*` 中；
  - A 在 commit 中引用 X 而不上传，必须返回 `DPE_MISSING_CONTENT`；
  - A 在自己的会话中上传 X，首次必须返回 `201`（不因 X 存在于 Q 而返回 `200`）；`HEAD` blob 的 `DPE-Upload-Offset` 只反映本会话。
