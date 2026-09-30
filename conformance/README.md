# 服务端一致性跑分器（M2）

黑盒 HTTP 跑分器：对着任意实现了 [core.md](../spec/core.md) + [HTTP 绑定](../spec/bindings/http.md) 的 remote 跑一遍协议语义（CAS、暂存原子切换、delta 如实回报、错误格式等），供各服务端在自己的 CI 中执行。

M1 只保留占位；实现随 M2 交付（plan §14）。

## 通用检查面

- capabilities 协商与限额如实声明（`dedup_scope` 取值合法）；
- 快路径 / 暂存路径的 commit 语义与原子性（分批期间读接口不可见中间态）；
- delta 与 `get_skeleton` 读回结果互相印证（不制造伪变更）；
- `get_skeleton` 读回的内容与推送内容一致，不含任何服务端衍生数据；服务端回报的 doc_hash 等于按契约 1 对推送内容算出的值；
- CAS 全矩阵（if_absent / base_hash / 缺前置 / force）与删除复活防护；
- delete / move 语义（move 后目标 doc_hash 与源一致；move 缺 base_hash 返回 428）；
- RFC 9457 错误格式与 `code` / `retryable` / `Retry-After`；每个 code 与 HTTP §5 的状态码一致；
- 增量推送、删除、move 收敛后与全量推送一致（判据 plan §13）。

## 字段不被静默丢弃（#4 A1 / A2）

- **源即内容**：只改元素 metadata 中的任一键（例如坐标、图片 url），content_hash、page_hash、doc_hash 都会变化；`batch_head` 发现变化，commit 返回 `updated`，`get_skeleton` 读回新值。
- **unchanged 判定**：完全相同的重复 commit 返回 `unchanged`，doc_hash 不变。
- **报文校验**：以下情况都返回 `DPE_VALIDATION`：
  - 未定义的字段；
  - 内容对象带其 category 未规定的字段（如 NarrativeText 带 `text_as_html`）；
  - pages 未按 number 严格升序；
  - 整数字面量超过 2^53−1。

## 幂等与重试（#4 B1 / B4 / B7）

- **commit 幂等**：同一请求重复提交得到 `unchanged`。即使条件头已不满足（首次成功后 doc_hash 已变为提交值），也必须返回 200 + `unchanged`，而不是 412。
- **暂存路径幂等**：引用暂存会话的 commit 成功后，原样重试（同一 `staging_session`）返回 200 + `unchanged`，不得返回 410。
- **求值顺序**：内容未变但不带任何前置条件的 commit 返回 428；无 force 权限者提交内容未变的 force commit 返回 403。
- **move 幂等**：move 成功后原样重试，返回成功（源不存在、目标 doc_hash 等于 `base_hash`）；首次成功后目标被他人改写，重试返回 `DPE_NOT_FOUND`。
- **delete**：删除后原样重试，返回 `DPE_NOT_FOUND`。
- **会话失效**：过期、已消费、伪造 id、他人会话一律返回 `410` + `DPE_SESSION_EXPIRED`，`retryable: false`；会话不存在不得返回 404。
- **会话跨版本复用**：开会话并上传后，他人写入该文档；原 commit 得到 `DPE_PRECONDITION_FAILED`，以新 `If-Match` 引用同一会话重新 commit 成功，无需重传内容。
- **同级写入**：服务端经自身入口修改内容后，`head` 的 doc_hash 变化；来源基于新 doc_hash 的 commit 成功覆盖。

## 契约升级期

- 服务端同时声明 dpe1 与 dpe2 时，以 `DPE-Hash-Contract: dpe1` 调用 `batch_head` 得到 dpe1 值，未变文档与本地值相等（不触发重推）；以 dpe2 调用得到 dpe2 值；两种前缀的 `If-Match` 都能匹配当前内容；响应带 `Vary: DPE-Hash-Contract`。

## HTTP 绑定（#4 B5 / B6）

- 条件头只出现在文档资源上（`PUT` / `DELETE documents?uri=`）；move 的前置条件在请求体中，冲突返回 `409`。
- 响应带 `DPE-Doc-Hash` 与 `ETag: "<doc_hash>"`；用 `W/"…"` 形式的 If-Match 写入必然返回 412；按 `DPE-Doc-Hash` 构造的 If-Match 写入成功。
- 对已删除文档带 `If-Match` 的 PUT / DELETE 返回 `412` + `DPE_NOT_FOUND`；move 响应带指向目标文档的 `Content-Location`，不带 `ETag`。

## 安全（#4 B9）

- 调用者 A 只对前缀 P 有写授权，前缀 Q 下的文档含内容 X：
  - A 对 P 下文档 negotiate 时，X 必须出现在 `missing_*` 中；
  - A 在 commit 中引用 X 而不上传，必须返回 `DPE_MISSING_CONTENT`；
  - A 在自己的会话中上传 X，首次必须返回 `201`（不因 X 存在于 Q 而返回 `200`）；`HEAD` blob 的 `DPE-Upload-Offset` 只反映本会话。
- 对无写授权的 URI 提交与其当前内容相同的 commit，必须返回 `403`，而不是 `unchanged`。
