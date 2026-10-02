# DPE v1 HTTP 绑定

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../../docs/plan/v1-plan.md) §9，经 Issue #4、#6（评审）修订
> 本文把 [core.md](../core.md) 的抽象操作映射到 HTTP。v1 只有这一种规范性绑定。

## 1. Remote 与路径

一个 remote 就是一个 base URL（例如 `https://example.com/anything/r/42`），协议不关心其内部结构，**不使用**域名根路径下的 `.well-known`。下文所有路径都相对于 remote，remote 自身路径不带尾部 `/`。

| Core 操作 | HTTP |
| --- | --- |
| `capabilities` | `GET {remote}/capabilities` |
| `head` | `HEAD {remote}/documents?uri={file_uri}` |
| `batch_head` | `POST {remote}/heads` |
| `get_skeleton` | `GET {remote}/documents?uri={file_uri}` |
| `list` | `GET {remote}/documents?prefix={p}&cursor={c}&limit={n}` |
| `negotiate` | `POST {remote}/negotiate` |
| `upload`（页对象） | `PUT {remote}/staging/{sid}/pages/{page_hash}` |
| `upload`（内容对象） | `PUT {remote}/staging/{sid}/objects/{content_hash}` |
| `upload`（blob） | `PUT {remote}/staging/{sid}/blobs/{sha256}` |
| `commit` | `PUT {remote}/documents?uri={file_uri}` |
| `delete` | `DELETE {remote}/documents?uri={file_uri}` |
| `move` | `POST {remote}/move` |

`uri` / `prefix` 查询参数按 RFC 3986 百分号编码。`documents?uri=` 就是**文档资源**本身：读（GET / HEAD）、写（PUT）、删（DELETE）作用在同一个目标资源上，条件请求头因此恰好约束被 CAS 保护的文档（RFC 9110 §13）。

## 2. 媒体类型

- JSON 主体（含页对象与内容对象的上传）一律 `application/json`；错误为 `application/problem+json`（§5）；blob 上传用 blob 自身的媒体类型（未知时 `application/octet-stream`）。
- 媒体类型不承载版本：协议版本以 capabilities 的 `protocol` 为准，hash 契约由 `DPE-Hash-Contract` 头声明（§3.1）。
- 命名保持中性：媒体类型、头字段、错误码中 MUST NOT 出现具体产品名。

## 3. 并发控制：doc_hash 即 ETag

### 3.1 契约声明与版本令牌

- **契约声明**：每个请求 MUST 带 `DPE-Hash-Contract: <契约>`（取值为 capabilities 的 `hash_contracts` 之一，如 `dpe1`；`GET capabilities` 除外）。请求体中的 hash 按它计算，响应中的 hash 按它给出。未声明或不受支持 → `DPE_CONTRACT_UNSUPPORTED`。文档资源的响应 MUST 带 `Vary: DPE-Hash-Contract`。
- **版本令牌**：文档的版本令牌就是 doc_hash（core.md §1）。文档资源的响应带 `DPE-Doc-Hash: <契约>:…`（**权威值**）与 `ETag: "<契约>:…"`（强校验器，HTTP 便利），均按 `DPE-Hash-Contract` 给出。
- **弱 ETag**：中间层（如做 gzip 的反向代理）可能把 ETag 改写为弱校验器 `W/"…"`。客户端构造条件头时 MUST 取 `DPE-Doc-Hash` 或响应体中的 `doc_hash`，自行写成 `"<doc_hash>"`；MUST NOT 原样回传收到的 `ETag`。
- **If-Match 的比较**：服务端对 `If-Match` 的值逐字符比较（`W/` 前缀的值永不匹配），比较对象是当前内容在该值前缀所示契约下的 doc_hash（契约 1 §6），而不是本次响应的 ETag。因此过渡期内新旧契约的值都能匹配，与请求声明的契约无关。

### 3.2 前置条件

| Core（§5.1） | commit（`PUT documents?uri=`）/ delete（`DELETE documents?uri=`） | move（`POST move`） |
| --- | --- | --- |
| `base_hash = H` | `If-Match: "H"` | 请求体 `"base_hash": "H"`（必填） |
| `if_absent` | `If-None-Match: *`（仅 commit） | — |
| `force: true` | 请求体 `"force": true`，且不带条件头（仅 commit） | — |
| 条件不满足 | `412 Precondition Failed`（含文档不存在时的 `If-Match`，code 为 `DPE_NOT_FOUND`，与 RFC 9110 §13.1.1 一致） | `409 Conflict` |
| 缺少前置条件 | `428 Precondition Required` | `428 Precondition Required` |

- move 的 CAS 对象是请求体中的 `from_uri`，不是 `/move` 这个目标资源，因此前置条件放在请求体中，不使用条件头；条件不满足返回 `409`（`412` 专指条件头求值失败）。
- 带 `force: true` 的请求同时带条件头时返回 `DPE_VALIDATION`。
- **commit 的 unchanged 先于条件求值**（core.md §3.3 的求值顺序）：提交内容的 doc_hash 等于当前 doc_hash 时，服务端返回 `200` + `unchanged`，即使条件头不满足。对 `If-Match`，这符合 RFC 9110 §13.1.1：状态变更请求所要求的结果已经生效时，源服务器可以返回 2xx 而不是 412。对 `If-None-Match: *`，RFC 9110 §13.1.2 要求条件不满足时返回 412，这里是**有意偏离**：内容已经是提交的值时，DPE 以内容幂等（core.md §5.2）为准返回 `unchanged`。服务端 MUST 在应用层求值条件头，不得交给会先行返回 412 的通用中间件。
- 同一个 `code` 在不同端点可能映射到不同状态码（如 `DPE_PRECONDITION_FAILED` 在 PUT 上是 412、在 move 上是 409）。**客户端 MUST 依据 problem 体中的 `code` 分派**，MUST NOT 依据状态码分派。
- 若仍收到不带 problem 体的 `412`（例如网关自行求值），客户端 MUST 先 `head` 该文档再判定：
  - PUT：doc_hash 等于提交内容 → 成功；不存在 → `DPE_NOT_FOUND`；否则 → `DPE_PRECONDITION_FAILED`（`If-None-Match: *` 时为 `DPE_ALREADY_EXISTS`）。
  - DELETE：不存在 → 成功（§5.2）；doc_hash 等于 `If-Match` 的值 → 中间层误判，可原样重试一次，仍如此则上报；否则 → `DPE_PRECONDITION_FAILED`。

### 3.3 响应头

- `head` 用 `HEAD documents?uri=`：`200` 带 `DPE-Doc-Hash` 与 `ETag`；不存在返回 `404`（不带 problem 体）。
- `get_skeleton` 的 `200` 响应带同样的头，客户端 MAY 用 `If-None-Match` 走 `304`（弱比较，`W/` 不影响）。
- commit 的成功响应带文档当前的 `DPE-Doc-Hash` 与 `ETag`；move 的成功响应带目标文档的 `DPE-Doc-Hash` 与指向目标文档的 `Content-Location`，不带 `ETag`（`/move` 不是文档资源）；delete 的 `204` 不带二者。

## 4. 各端点报文

### 4.1 `GET {remote}/capabilities`

```json
{
  "protocol": "dpe/1",
  "hash_contracts": ["dpe1"],
  "limits": {
    "max_payload_bytes": 8388608,
    "page_max_bytes": 268435456,
    "staging_ttl_seconds": 86400,
    "blob_max_bytes": 104857600,
    "blob_chunk_bytes": 8388608,
    "batch_head_max": 500,
    "list_page_max": 1000
  },
  "content_encodings": ["gzip"],
  "features": ["move"]
}
```

- `staging_ttl_seconds`：暂存会话从最近一次成功操作起算的过期时间，MUST ≥ 3600（core.md §3.4）。
- 去重范围不在此声明（core.md §3.3）。
- `features` 列出可选能力；v1 内 `move` 为 MUST 实现，此处保留位置供扩展。

### 4.2 `POST {remote}/heads`（batch_head）

```json
// 请求
{ "uris": ["feishu://doc/a", "s3://bucket/b.md"] }
// 响应 200
{ "heads": [ { "doc_hash": "dpe1:…" }, null ] }
```

响应数组与请求一一对应，不存在的文档为 `null`。数量超过 `batch_head_max` 返回 `DPE_VALIDATION`。

### 4.3 `GET {remote}/documents?uri=…`（get_skeleton）

```json
{
  "file_uri": "feishu://doc/a",
  "doc_hash": "dpe1:…",
  "root": {
    "file_type": "md",
    "title": "季度报告",
    "doc_metadata": { "created_at": "2026-09-01T02:03:04Z", "author": "…" },
    "pages": [ "dpe1:…", "dpe1:…" ]
  },
  "pages": [
    { "title": "第一页", "page_metadata": { "page_label": "i" }, "elements": [ "dpe1:…", "dpe1:…" ] },
    { "page_metadata": {}, "elements": [ "dpe1:…" ] }
  ]
}
```

`pages` 按根对象 `pages` 的顺序给出全部页对象（重复的页 hash 对应重复的页对象）。返回值与最近一次写入的值内容等价（core.md §2.7），不含任何服务端衍生数据。

### 4.4 `GET {remote}/documents?prefix=…&cursor=…&limit=…`（list）

```json
{ "documents": [ { "file_uri": "feishu://doc/a", "doc_hash": "dpe1:…" } ],
  "next_cursor": "…" }
```

`next_cursor` 为 null 表示结束。cursor 不透明。

### 4.5 `POST {remote}/negotiate`

```json
// 请求
{ "file_uri": "feishu://doc/a",
  "root": { …同 4.3 的 root… },
  "pages": [ { …页对象… } ] }        // 可选：附带部分或全部页对象
// 响应 200
{ "missing_pages": ["dpe1:…"],
  "missing_content_hashes": ["dpe1:…"],
  "staging_session": { "id": "st-…", "expires_at": "2026-10-01T00:00:00Z" } }
```

- negotiate 不携带也不校验任何 CAS 前置条件（CAS 只在 commit 时裁决）；会话绑定 `(file_uri, 调用者身份)`（core.md §3.4）。
- `missing_pages`：根对象引用、既未附带也不在去重范围内的页对象。`missing_content_hashes`：附带的页对象中引用、而不可得的内容对象（未附带的页由 §4.6 的上传响应给出）。附带的页对象存入会话。
- 请求体超过 `max_payload_bytes` 时，少附带页对象（只提交根对象即可，根对象只含页 hash 列表）。
- 缺失清单按去重范围计算（core.md §3.3）。

### 4.6 `PUT {remote}/staging/{sid}/pages/{page_hash}` 与 `…/objects/{content_hash}`

请求体为一个页对象或内容对象 JSON，即该 hash 的原像对象（core.md §2.2、§2.3）。服务端依次：

1. 校验字段：出现未定义字段 → `DPE_VALIDATION`；未知 category → `DPE_CATEGORY_UNKNOWN`；
2. 重算 hash，与路径不符 → `DPE_HASH_MISMATCH`。

响应 `201`（本会话内新写入）或 `200`（本会话内重复），体为下一层的缺失清单：

```json
// 页对象的响应
{ "missing_content_hashes": ["dpe1:…"] }
// 内容对象的响应
{ "missing_blobs": ["sha256:…"] }
```

201 / 200 MUST 只按本会话已收到的内容判定，不反映服务端其他位置是否已存该对象；缺失清单按去重范围计算（core.md §3.3、§8）。

会话的每个成功操作都会续期（core.md §3.4）：negotiate 响应的 `expires_at` 与 upload 各响应（含 `202` 中间块）的 `DPE-Session-Expires: <RFC 3339 UTC>` 头给出续期后的过期时间。

**页对象可分块**：单个页对象超过 `max_payload_bytes` 时，按 §4.7 的方式分块上传与断点续传，总大小不超过 `page_max_bytes`。全部字节到齐后，服务端把它们解析为页对象，按契约 1 重算 page_hash 并与路径比较（不是对原始字节算 sha256）；最后一块的响应体为缺失清单。内容对象不分块，超限返回 `DPE_PAYLOAD_TOO_LARGE`（core.md §3.2）。

### 4.7 `PUT {remote}/staging/{sid}/blobs/{sha256}`（分块与断点续传）

- 整体上传：不带 `Content-Range` 的 `PUT`，体为完整字节。
- 分块上传：`PUT` + `Content-Range: bytes {from}-{to}/{total}`，块大小不超过 `blob_chunk_bytes`，MUST 按序追加；全部字节到齐后服务端校验 sha256。
- 中间块：响应 `202`，带 `DPE-Upload-Offset: {n}`（已收字节数），无体；最后一块到齐并校验通过后，按 §4.6 返回 `201` / `200`。
- 断点查询：`HEAD` 同一 URL，响应头 `DPE-Upload-Offset: {n}` 表示**本会话**已收字节数（同样不反映会话外是否已存该对象），并带 `DPE-Session-Expires` 给出会话当前的过期时间。断点查询只读，**不续期**（core.md §3.4）。
- 校验失败返回 `DPE_HASH_MISMATCH` 并丢弃已收内容。blob 校验的是原始字节的 sha256。
- 以上分块规则同样适用于 §4.6 的页对象，只是校验方式不同（见 §4.6），总大小上限为 `page_max_bytes`。

### 4.8 `PUT {remote}/documents?uri=…`（commit）

```json
{ "root": { …同 4.3 的 root… },
  "pages": [ { …内联页对象… } ],       // 可选
  "objects": [ { …内联内容对象… } ],   // 可选
  "staging_session": "st-…",          // 可选
  "force": false }
```

- 快路径：内联根对象引用的全部页对象与内容对象，一次往返完成。大文档：只带根对象并引用会话。

- 文档身份取自查询参数 `uri`，请求体不重复 `file_uri`。
- CAS 前置条件走 `If-Match` / `If-None-Match: *`（§3.2）。
- 响应 `201`（created）或 `200`（updated / unchanged），体为 core.md §3.3 的 commit 响应，头带文档当前的 `DPE-Doc-Hash` 与 `ETag`。
- 失败的 commit 不消费 `staging_session`（core.md §3.4）。

### 4.9 `DELETE {remote}/documents?uri=…` 与 `POST {remote}/move`

- delete：CAS 走 `If-Match`，成功 `204`。
- move：

  ```json
  { "from_uri": "…", "to_uri": "…", "base_hash": "dpe1:…" }
  ```

  按 core.md §5.2 的求值顺序：源不存在且目标 doc_hash 等于 `base_hash` → 视为已完成，返回成功；源不存在的其他情况 → `404` + `DPE_NOT_FOUND`；源 doc_hash 不符 → `409` + `DPE_PRECONDITION_FAILED`；目标已存在 → `409` + `DPE_ALREADY_EXISTS`。成功 `200`，体为 `{ "doc_hash": "dpe1:…" }`，头带 `DPE-Doc-Hash` 与 `Content-Location: {remote}/documents?uri={to_uri}`（§3.3）。

写操作不需要幂等键：幂等由内容保证（core.md §5.2）。

## 5. 错误格式（RFC 9457）

```json
{ "type": "urn:dpe:error:precondition-failed",
  "title": "precondition failed",
  "status": 412,
  "detail": "expected dpe1:3f…, current dpe1:9a…",
  "code": "DPE_PRECONDITION_FAILED",
  "retryable": false }
```

- `type` 是规范中稳定的 URI。草案使用 `urn:dpe:error:<kebab-case>`；定稿后若规范发布到固定域名，MAY 改为该域名下的 URL（plan §16）。
- 扩展成员：`code`（core.md §6 的错误码）与 `retryable`（语义见 core.md §6：仅表示原样重试可能成功）。`DPE_MISSING_CONTENT` 另带 `missing: {pages: […], content_hashes: […], blobs: […]}` 与 `missing_truncated: true|false`（core.md §3.3）。
- HTTP 状态映射（每个 code 在给定端点上只有一个状态码；客户端按 `code` 分派，§3.2）：

| code | 状态码 |
| --- | --- |
| `DPE_VALIDATION`、`DPE_CONTRACT_UNSUPPORTED`、`DPE_CATEGORY_UNKNOWN`、`DPE_HASH_MISMATCH`、`DPE_MISSING_CONTENT` | 400 |
| `DPE_FORBIDDEN` | 403（所有写操作都先判定授权，core.md §5 总则） |
| `DPE_NOT_FOUND` | 404；带 `If-Match` 的 PUT / DELETE documents 为 412（§3.2） |
| `DPE_PRECONDITION_FAILED`、`DPE_ALREADY_EXISTS` | 412（条件头：PUT / DELETE documents）；409（请求体前置条件：move） |
| `DPE_SESSION_EXPIRED` | 410（会话过期、已消费、不存在、不属于调用者或不属于该 file_uri，一律如此） |
| `DPE_PAYLOAD_TOO_LARGE` | 413 |
| `DPE_PRECONDITION_REQUIRED` | 428 |
| `DPE_RATE_LIMITED` | 429 |
| `DPE_UNAVAILABLE` | 503 |

429 与 503 MUST 带 `Retry-After`。

## 6. 鉴权

协议不定义鉴权方案：

- 客户端凭证放在标准 `Authorization` 头；具体方案（PAT、OAuth、JWT、mTLS）由服务端决定。
- 未认证返回 `401` + `WWW-Authenticate`；无权限返回 `403`（体为 `DPE_FORBIDDEN` problem）。
- SDK 通过可插拔的 `CredentialProvider` 注入凭证，不感知方案细节。

## 7. 传输细节

- 请求 MAY 使用 `Content-Encoding: gzip`（在 capabilities 的 `content_encodings` 内协商）；`max_payload_bytes` **按压缩前计算**。
- 服务端 SHOULD 支持 HTTP/2；大量小文档场景依赖多路复用并发快路径。
- 协议不暴露服务端处理进度：commit 成功响应就是协议的终点。
