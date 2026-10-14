# 服务端一致性跑分器（M2）

黑盒 HTTP 跑分器：对着任意实现了 [core.md](../spec/core.md) + [HTTP 绑定](../spec/bindings/http.md) 的 remote 跑一遍协议语义（CAS、暂存原子切换、delta 如实回报、错误格式等），供各服务端在自己的 CI 中执行。

**当前状态**：骨架已落地（#26）——`dpe-conformance` CLI、中性配置与服务端钩子、JUnit XML 与「已执行 / 跳过」计数、冒烟章；子集 1（#27）已落地——capabilities / 读接口（head、batch_head、get_skeleton、list）/ commit 快路径与 CAS 基础 / 报文校验四章；全量 1/6（#54）已落地——大文档与逐层协商、暂存会话两章；全量 2/6（#55）已落地——写操作（delete / move 语义、CAS 与幂等重试、删除复活防护、同级写入）与 HTTP 绑定（条件头按值比较、响应头、HEAD 错误面、RFC 9457 与 code → 状态码逐码）两章；错误注入钩子（#123）已落地——`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE` 与「429/503 MUST 带 Retry-After」可经钩子黑盒覆盖。全量 3/6（#56）已落地——求值顺序与校验顺序（传输层、uri、条件头、暂存上传的组合场景，报文校验先于授权）一章，以及经 HTTP 消费 `invalid_objects` / `nesting_depth` 向量一章。入口在仓库的 `sdk/python/packages/dpe-conformance/`（用法与配置见该包 README）。全量 4/6（#57）已落地——安全与去重范围一章（内容存于他处仍按层列为缺失、上传状态只按本会话、无写授权一律 403、元素层与 blob 层的去重下界）。其余全量（#58–#59）随 M2 推进。`dpe-conformance` 发布在 PyPI，服务端仓库在 CI 中的用法见 [在 CI 中运行跑分器](ci.md)。

## 通用检查面

- capabilities 协商与限额如实声明（`staging_ttl_seconds` ≥ 3600）；
- 暂存会话续期：每次成功的 negotiate / upload（含中间块）都把过期时间推后到「此刻 + staging_ttl」，并由 `expires_at` / `DPE-Session-Expires` 如实给出（必带，RFC 3339 UTC，以 `Z` 结尾）；`now ≥ expires_at` 即过期（配置了 `clock-advance` 钩子时，先推进到 `expires_at` 之前断言会话仍可用，再推进到不早于 `expires_at`——钩子只接受整秒，可能超调不到 1 秒——断言得到 `DPE_SESSION_EXPIRED`；未配置时该项显式 skip）；断点查询（`HEAD`）带 `DPE-Session-Expires` 但不续期；闲置超过 TTL 的会话返回 `DPE_SESSION_EXPIRED`；
- 去重范围固定为本文档：只靠该文档当前状态（或本次暂存会话）即可得的对象不出现在缺失清单中；其他文档持有的对象一律视为缺失（不因它存在于别处而变化）；
- 快路径 / 暂存路径的 commit 语义与原子性（分批期间读接口不可见中间态）；
- delta 与 `get_skeleton` 读回结果互相印证（不制造伪变更）；
- `get_skeleton` 读回的内容与推送内容一致，不含任何服务端衍生数据；服务端回报的 doc_hash 等于按契约 1 对推送内容算出的值；
- CAS 全矩阵（if_absent / base_hash / 缺前置 / force）与删除复活防护；
- delete / move 语义（move 后目标 doc_hash 与源一致；move 缺 base_hash 返回 428）；
- RFC 9457 错误格式与 `code` / `retryable` / `Retry-After`；每个 code 与 HTTP §5 的状态码一致；每个错误响应带与 `code` 相等的 `DPE-Error-Code` 头；
- 增量推送、删除、move 收敛后与全量推送一致（判据 plan §13）。

## 字段不被静默丢弃（#4 A1 / A2）

- **源即内容**：只改元素 metadata 中的任一键（例如坐标、图片 url），content_hash、page_hash、doc_hash 都会变化；`batch_head` 发现变化，commit 返回 `updated`，`get_skeleton` 读回新值。
- **unchanged 判定**：完全相同的重复 commit 返回 `unchanged`，doc_hash 不变。
- **报文校验**：以下情况都返回 `DPE_VALIDATION`：
  - 未定义的字段；
  - 元素对象带其 category 未规定的字段（如 NarrativeText 带 `text_as_html`）；
  - 整数字面量超过 2^53−1；
  - 对象的嵌套深度超过 64（#94，core.md §2.8 第 0 步；逐条边界向量见 `vectors/nesting_depth.json`）。

## 大文档与逐层协商（#6 F2）

- 页数与元素数足以让展开骨架远超 `max_payload_bytes` 的文档，经「negotiate 只交文档对象 → 上传缺失页对象 → 上传缺失元素对象 → 上传缺失 blob → commit 只带文档对象」完成提交；negotiate 与 commit 的请求体都不超限。
- 单个页对象超过 `max_payload_bytes` 时，按分块上传与断点续传完成：中间块返回 `202` + `DPE-Upload-Offset`，最后一块的响应给出该页缺失的元素对象；分块页对象超过 `page_max_bytes` 返回 `DPE_PAYLOAD_TOO_LARGE`。
- 文档对象超过 `max_payload_bytes` 时，negotiate 与 commit 都返回 `413` + `DPE_PAYLOAD_TOO_LARGE`（v1 的规模边界，core.md §3.2）。
- commit 缺少对象时，`DPE_MISSING_CONTENT` 的 problem 体按层列出缺失的 hash；补传到同一会话后重新 commit 成功。
- 在中间插入一页后再次提交：`missing_pages` 只含新页，其余页对象不重传；doc_hash 变化。
- 对调两页：`missing_pages` 为空，commit 返回 `updated`，delta 为零。
- 元素对象上传响应的 `missing_blobs`、未分块页对象上传响应的 `missing_content_hashes` 与实际缺失一致（不截断）；分块上传的页对象：`missing_truncated` 为 `false` 时与实际缺失一致，为 `true` 时是实际缺失的非空子集，且未截断时的整个响应体按 JCS 序列化后的 UTF-8 字节数超过 `max_payload_bytes`（http §4.6）。
- negotiate 对无写授权的 URI → `403` + `DPE_FORBIDDEN`，不返回会话与缺失清单；upload 不重复判定授权（会话可用即放行，commit 才重新授权，core.md §3.4、§5 总则）。
- 分块违例的求值顺序（core.md §3.4、http §4.7）：会话不可用 → `410`（先于分块参数与请求体校验）；本会话内已完成的 hash 重复上传（含重发最后一块）→ `200` + 缺失清单，不校验 `Content-Range` 与请求体；`Content-Range` 非法 / 块长与范围不符 / `total` 与先前声明不一致 → `400` + `DPE_VALIDATION`；单块超过 `blob_chunk_bytes`、首块声明的 `total` 超过 `page_max_bytes` / `blob_max_bytes` → `413`（不接收字节）；偏移不连续 → `400` + `DPE_VALIDATION`，响应带 `DPE-Upload-Offset` 头；到齐校验失败（含 hash 不符）→ 对应错误码并丢弃该对象已收内容。
- 断点查询的各状态（http §4.7）：本会话无进度的 hash → `200` + `DPE-Upload-Offset: 0`（不得 `404`）；已完成 → `200` + 该对象已收的总字节数；到齐后校验失败并被丢弃 → 偏移回到 `0`（据此黑盒验证「丢弃」）。

## 幂等与重试（#4 B1 / B4 / B7）

- **commit 幂等**：同一请求重复提交得到 `unchanged`。即使条件头已不满足（首次成功后 doc_hash 已变为提交值），也必须返回 200 + `unchanged`，而不是 412。
- **暂存路径幂等**：引用暂存会话的 commit 成功后，原样重试（同一 `staging_session`）返回 200 + `unchanged`，不得返回 410。
- **求值顺序**：内容未变但不带任何前置条件的 commit 返回 428；无 force 权限者提交内容未变的 force commit 返回 403。
- **move 幂等**：move 成功后原样重试，返回成功（源不存在、目标 doc_hash 等于 `base_hash`）；首次成功后目标被他人改写，重试返回 `DPE_NOT_FOUND`。
- **delete**：删除后原样重试，返回 `DPE_NOT_FOUND`。
- **会话失效**：过期、已消费、伪造 id、他人会话一律返回 `410` + `DPE_SESSION_EXPIRED`，`retryable: false`；会话不存在不得返回 404。
- **会话跨版本复用**：开会话并上传后，他人写入该文档（未移除本次提交依赖的对象）；原 commit 得到 `DPE_PRECONDITION_FAILED`，以新 `If-Match` 引用同一会话重新 commit 成功，无需重传内容。
- **分块续传的响应不确定**（服务端断言）：中间块 `202` 响应丢失后原样重发 → `400` + `DPE-Upload-Offset` 头；断点查询不续期（`DPE-Session-Expires` 不变）。客户端「先断点查询再续传」是 SDK 侧义务（core.md §3.4），由两个 SDK 的对等测试覆盖（#49 / #50），非跑分项。
- 引用属于同一调用者、但属于另一 file_uri 的会话，返回 `410` + `DPE_SESSION_EXPIRED`。
- **同级写入**：服务端经自身入口修改内容后，`head` 的 doc_hash 变化；来源基于新 doc_hash 的 commit 成功覆盖。

## 契约升级期

- 服务端同时声明 dpe1 与 dpe2 时，以 `DPE-Hash-Contract: dpe1` 调用 `batch_head` 得到 dpe1 值，未变文档与本地值相等（不触发重推）；以 dpe2 调用得到 dpe2 值；两种前缀的 `If-Match` 都能匹配当前内容；响应带 `Vary: DPE-Hash-Contract`。

## HTTP 绑定（#4 B5 / B6）

- 条件头只出现在文档资源上（`PUT` / `DELETE documents?uri=`）；move 的前置条件在请求体中，冲突返回 `409`。
- 响应带 `DPE-Doc-Hash` 与 `ETag: "<doc_hash>"`；用 `W/"…"` 形式的 If-Match 写入必然返回 412；按 `DPE-Doc-Hash` 构造的 If-Match 写入成功。
- HEAD 出错时带 `DPE-Error-Code`：`head` 不存在的文档返回 `404` + `DPE_NOT_FOUND`，缺少契约头返回 `400` + `DPE_CONTRACT_UNSUPPORTED`；对失效会话做断点查询返回 `410` + `DPE_SESSION_EXPIRED`（#70）。
- 对已删除文档带 `If-Match` 的 PUT / DELETE 返回 `412` + `DPE_NOT_FOUND`；move 响应带指向目标文档的 `Content-Location`，不带 `ETag`。

## 安全（#4 B9）

- 调用者 A 提交的内容 X 已存于另一篇文档（无论 A 对该文档是否有写授权；去重范围固定为本文档）：
  - 缺失清单必须把 X 列为缺失，按 X 所在的层核对：X 是页对象时，出现在 negotiate 的 `missing_pages` 中；X 是元素对象时，出现在附带了引用它的页的 negotiate 响应、或该页对象上传响应的 `missing_content_hashes` 中（页对象分块上传时，仅当 `missing_truncated` 为 `false` 才作此断言，http §4.6）；X 是 blob 时，出现在引用它的元素对象上传响应的 `missing_blobs` 中；
  - A 在 commit 中引用 X 而不上传，必须返回 `DPE_MISSING_CONTENT`；
  - A 在自己的会话中上传 X，首次必须返回 `201`（不因 X 存在于别处而返回 `200`）；`HEAD` blob 的 `DPE-Upload-Offset` 只反映本会话。
- 上传状态只按本会话判定：同一文档、同一调用者的另一个未提交会话里已上传的元素与 blob，在新会话中首次上传仍为 `201`，blob 的断点偏移仍为 `0`，且仍列入元素上传响应的 `missing_blobs`。服务端对同一文档与调用者复用已打开的会话时，无法构造另一个会话，该项如实 skip。
- 去重范围下界：本文档当前状态可得的对象不列为缺失。页层见「大文档与逐层协商」（插入一页只缺新页）；元素层与 blob 层：新页复用当前状态已有的元素、并新增一个引用当前状态已有 blob 的元素时，页上传响应只列新元素，新元素上传响应的 `missing_blobs` 为空。
- 对无写授权的 URI 提交与其当前内容相同的 commit，必须返回 `403`，而不是 `unchanged`。
- 对无写授权的 URI 做 delete，无论文档是否存在都返回 `403`；对源或目标任一侧无写授权的 move，无论两侧状态如何都返回 `403`（#6 F3）。

## 求值顺序（#6 F5）

组合场景一律「同时违反两处，断言返回的是先判的那个」。限流与不可用（429 / 503）不在固定顺序内，不对它们下结论（HTTP §3.1）。

- **传输层**（HTTP §3.1、§6）：顺序为路由 → 认证 → 查询参数的形态 → 请求体。
  - 未知路径 → `404`；方法不符 → `405` + `Allow`。两者都不是 DPE 错误，没有 problem 体，也没有 `DPE-Error-Code`。
  - 未认证 → `401` + `WWW-Authenticate`，排在一切报文校验之前：重复 `uri`、缺契约头、非 JSON 体同时出现时仍是 `401`。路由先于认证：未知路径不带凭证也是 `404`。服务端接受匿名请求时这两项如实 skip。
  - 坏查询 + 超大请求体 → `DPE_VALIDATION`，不是 `413`。
- **uri 的位置**（HTTP §3.1）：commit、negotiate、delete、get_skeleton 缺契约头 + 非法 uri → `DPE_CONTRACT_UNSUPPORTED`；无写授权 + 非法 uri（含 move 的 `from_uri`）→ `DPE_VALIDATION`，不是 `403`。
- **条件头**（HTTP §3.2、§3.3）：
  - 写操作条件头的语法排在授权之前：无权 + 非法条件头的 commit / DELETE → `DPE_VALIDATION`。
  - unchanged 不放行语法违例：内容未变 + 非法 `If-Match` → `DPE_VALIDATION`。
  - GET 的 `If-None-Match` 语法排在契约 → uri 之后、文档状态之前：文档不存在 + 非法取值 → `DPE_VALIDATION`，不是 `404`；缺契约头时 → `DPE_CONTRACT_UNSUPPORTED`。
- **暂存上传**（HTTP §4.6、§4.7）：
  - 元素对象按有无 `Content-Range` 分流。非 JSON 体 + 缺契约头时，带它 → `DPE_CONTRACT_UNSUPPORTED`，不带它 → `DPE_VALIDATION`。伪造会话 + `Content-Range` → `DPE_VALIDATION`，不是 `410`。
  - 分块同时带 `Content-Encoding` → `DPE_VALIDATION`。编码不受支持时也不是 `415`；会话不可用时是 `410`；已完成后带它重发仍是 `200`。
  - 断点查询的顺序为契约 → 路径 hash 语法 → 会话：非法路径 hash 在有效会话下不得得到 `200` + `0`，在伪造会话下不是 `410`。
  - blob 整体上传的顺序为契约 → 路径引用语法 → sha256 → 会话：伪造会话 + 引用与字节不符 → `DPE_HASH_MISMATCH`。分块 `pages/{非法}` + 伪造会话 → `DPE_VALIDATION`。
- 第 0 步：超过 `max_payload_bytes` 的请求，无论调用者是否有写授权，都返回 `413`；缺少或声明不受支持的 `DPE-Hash-Contract` 返回 `DPE_CONTRACT_UNSUPPORTED`（第 1 步，先于授权）。
- 报文校验先于授权（#63）：无写授权的调用者提交不合法的报文（非 I-JSON、信封成员类型错误或未定义、对象违例、force 与条件头并存）时返回对应的校验错误，而不是 `403`；报文合法时一律 `403`，与文档状态无关。move 同理，源与目标两侧授权排在请求信封校验之后。
- commit 的每个错误码都只出自 core.md §3.3 中唯一的一步：例如同时存在「内联对象字段非法」与「前置条件不满足」时返回 `DPE_VALIDATION`；同时存在「前置条件不满足」与「内容缺失」时返回 `DPE_PRECONDITION_FAILED`。
- 内容未变的 commit 即使带有非法的内联对象也返回 `DPE_VALIDATION`（第 1 步先于第 4 步）；带有合法但多余的内联对象时返回 `unchanged`，且不处理这些对象。
- commit 的内联对象永远不会得到 `DPE_HASH_MISMATCH`；暂存上传中路径 hash 不符才会。

## 一致性向量（经 HTTP 消费）

- `vectors/invalid_objects.json` 的每条拒绝用例都嵌入一次 commit 后发送，错误码必须一致。
  - 只断言 `code`。违例位置 `path` 不经 HTTP 断言，因为 problem 体不携带它（core.md §2.8、HTTP §5），它由两个 SDK 的向量测试覆盖。
  - `expanded_document` 类用例是 SDK 的展开视图，不是线上形态，不经 HTTP 消费。
- `vectors/nesting_depth.json`：恰在上界的对象通过报文校验，多一层即 `DPE_VALIDATION`。
- 用例口径的「受支持契约」是向量主契约 ∪ 用例契约（`vectors/README.md`）。只有输入涉及的每个契约前缀在用例口径与服务端声明中受支持与否都一致时，期望码才适用，否则该用例如实 skip。
