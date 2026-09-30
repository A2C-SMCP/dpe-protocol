# DPE v1 HTTP 绑定

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../../docs/plan/v1-plan.md) §9，经 Issue #4（评审）修订
> 本文把 [core.md](../core.md) 的抽象操作映射到 HTTP。v1 只有这一种规范性绑定。
> 具体路径与媒体类型命名属于 plan §16 未决项，本文给出草案值，定稿前可能调整。

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
| `upload`（内容对象） | `PUT {remote}/staging/{sid}/objects/{content_hash}` |
| `upload`（blob） | `PUT {remote}/staging/{sid}/blobs/{sha256}` |
| `commit` | `PUT {remote}/documents?uri={file_uri}` |
| `delete` | `DELETE {remote}/documents?uri={file_uri}` |
| `move` | `POST {remote}/move` |

`uri` / `prefix` 查询参数按 RFC 3986 百分号编码。`documents?uri=` 就是**文档资源**本身：读（GET / HEAD）、写（PUT）、删（DELETE）作用在同一个目标资源上，条件请求头因此恰好约束被 CAS 保护的文档（RFC 9110 §13）。

## 2. 媒体类型

- 请求与响应主体：`application/dpe+json`（草案值；版本通过媒体类型参数 `; version=1` 或 capabilities 协商，定稿见 plan §16）。服务端 SHOULD 同时接受 `application/json`。
- 错误：`application/problem+json`（§6）。
- 内容对象与 blob 上传：`application/dpe.object+json` 与 blob 自身的媒体类型（未知时 `application/octet-stream`）。
- 命名保持中性：媒体类型、头字段、错误码中 MUST NOT 出现具体产品名。

## 3. 并发控制：映射到 HTTP

### 3.1 revision 的承载

- **revision 的权威值**是响应体中的 `revision` 字段与 `DPE-Revision` 响应头（HEAD 等无体响应只有后者）。
- 文档资源的响应同时带 `ETag: "<revision>"`（强校验器），仅作 HTTP 便利；中间层（如做 gzip 的反向代理）可能把它改写为弱校验器 `W/"<revision>"`。
- 客户端构造条件头时 MUST 取 `DPE-Revision` 或响应体中的 revision，自行写成 `"<revision>"`；MUST NOT 原样回传收到的 `ETag`。服务端对 `If-Match` 按 RFC 9110 做强比较，`W/` 前缀的值永不匹配。
- revision MUST 只由可放进 entity-tag 的可见 ASCII 字符组成（`%x21 / %x23-7E`，即不含 `"`、空白与控制字符）。

### 3.2 前置条件

| Core（§5） | commit（`PUT documents?uri=`）/ delete（`DELETE documents?uri=`） | move（`POST move`） |
| --- | --- | --- |
| `base_revision = R` | `If-Match: "R"` | 请求体 `"base_revision": "R"` |
| `if_absent` | `If-None-Match: *`（仅 commit） | — |
| `force: true` | 请求体 `"force": true`，且不带条件头（delete 不支持 force） | 请求体 `"force": true` |
| 条件不满足 | `412 Precondition Failed`（含文档不存在时的 `If-Match`，code 为 `DPE_NOT_FOUND`，与 RFC 9110 §13.1.1 一致） | `409 Conflict` |
| 缺少前置条件 | `428 Precondition Required` | `428 Precondition Required` |

- move 的 CAS 对象是请求体中的 `from_uri`，不是 `/move` 这个目标资源，因此前置条件放在请求体中，不使用条件头；条件不满足返回 `409`（`412` 专指条件头求值失败）。
- force 显式出现在报文里便于审计；带 `force: true` 的请求同时带条件头或 `base_revision` 时返回 `DPE_VALIDATION`。
- 同一个 `code` 在不同端点可能映射到不同状态码（如 `DPE_REVISION_CONFLICT` 在 PUT 上是 412、在 move 上是 409）。**客户端 MUST 依据 problem 体中的 `code` 分派**，MUST NOT 依据状态码分派。
- 通用框架或网关可能在应用代码之前自行求值条件头，返回不带 problem 体的 `412`。客户端收到无 problem 体的 `412` 时 MUST 先 `head` 该文档：不存在按 `DPE_NOT_FOUND` 处理，否则按 `DPE_REVISION_CONFLICT`（或 `If-None-Match: *` 时的 `DPE_ALREADY_EXISTS`）处理。

### 3.3 读接口的状态头

- `head` 用 `HEAD documents?uri=`：`200` 带 `DPE-Revision`、`ETag`、`DPE-Doc-Hash` 与 `DPE-State-Hash` 头；不存在返回 `404`（不带 problem 体）。
- `get_skeleton` 的 `200` 响应带同样的头，客户端 MAY 用 `If-None-Match` 走 `304`（弱比较，`W/` 不影响）。
- commit 的成功响应 MUST 带文档当前的 `DPE-Revision` 与 `ETag`（`unchanged` 时即原 revision）；move 的成功响应带目标文档的 `DPE-Revision` 与指向目标文档的 `Content-Location`，不带 `ETag`（`/move` 不是文档资源，其 ETag 没有意义）；delete 的 `204` 不带二者。

## 4. 各端点报文

### 4.1 `GET {remote}/capabilities`

```json
{
  "protocol": "dpe/1",
  "hash_contracts": ["dpe1"],
  "limits": {
    "max_payload_bytes": 8388608,
    "staging_ttl_seconds": 86400,
    "blob_max_bytes": 104857600,
    "blob_chunk_bytes": 8388608,
    "batch_head_max": 500,
    "list_page_max": 1000,
    "idempotency_window_seconds": 86400
  },
  "dedup_scope": "document",
  "content_encodings": ["gzip"],
  "attributes": ["tfrs/creator_id", "tfrs/group_id"],
  "features": ["move"]
}
```

- `attributes` 列出该 remote 接受的键（示例中的 `tfrs/*` 只是某个服务端的声明，不属于规范）。
- `dedup_scope`：`document` 或 `writable`（core.md §3.3）。
- `idempotency_window_seconds`：幂等去重窗口（core.md §5.1），MUST 不小于规范下限。
- `features` 列出可选能力；v1 内 `move` 为 MUST 实现，此处保留位置供扩展。

### 4.2 `POST {remote}/heads`（batch_head）

```json
// 请求
{ "uris": ["feishu://doc/a", "s3://bucket/b.md"] }
// 响应 200
{ "heads": [ { "revision": "r-9", "doc_hash": "dpe1:…", "state_hash": "dpe1:…" }, null ] }
```

响应数组与请求一一对应，不存在的文档为 `null`。数量超过 `batch_head_max` 返回 `DPE_VALIDATION`。快路径筛选比对 `state_hash`（core.md §3.2）。

### 4.3 `GET {remote}/documents?uri=…`（get_skeleton）

```json
{
  "file_uri": "feishu://doc/a",
  "revision": "r-9",
  "doc_hash": "dpe1:…",
  "state_hash": "dpe1:…",
  "file_type": "md",
  "attributes": { "tfrs/creator_id": "u-1" },
  "skeleton": {
    "doc_metadata": { "created_at": "2026-09-01T02:03:04Z", "author": "…" },
    "pages": [
      { "number": 1, "title": "第一页", "page_metadata": { "source_block": "blk-1" },
        "elements": [
          { "hash": "dpe1:…", "occurrence": { "coordinates": { "points": [[0.1, 0.1], [0.4, 0.2]], "system": "PixelSpace" } } },
          { "hash": "dpe1:…" }
        ] }
    ]
  }
}
```

`pages` 按 `number` 严格升序；entry 的 `occurrence` 见 core.md §2.3。

### 4.4 `GET {remote}/documents?prefix=…&cursor=…&limit=…`（list）

```json
{ "documents": [ { "file_uri": "feishu://doc/a", "revision": "r-9" } ],
  "next_cursor": "…" }
```

`next_cursor` 为 null 表示结束。cursor 不透明。

### 4.5 `POST {remote}/negotiate`

```json
// 请求
{ "file_uri": "feishu://doc/a",
  "hash_contract": "dpe1",
  "skeleton": { …同 4.3… },
  "blobs": ["sha256:…"] }
// 响应 200
{ "missing_content_hashes": ["dpe1:…"],
  "missing_blobs": ["sha256:…"],
  "staging_session": { "id": "st-…", "expires_at": "2026-10-01T00:00:00Z" } }
```

- negotiate 不携带也不校验任何 CAS 前置条件（CAS 只在 commit 时裁决）；会话绑定 `(file_uri, 调用者身份)`（core.md §3.4）。
- `blobs` 列出骨架所引用内容对象中出现的全部 blob（服务端此时尚未收到缺失的内容对象，无法自行得知）。
- `missing_*` 按 `dedup_scope` 计算（core.md §3.3）。

### 4.6 `PUT {remote}/staging/{sid}/objects/{content_hash}`

请求体为一个内容对象 JSON，恰为 content_hash 的完整原像（core.md §2.3）：只含其 category 规定的字段与 `metadata`。服务端依次：

1. 校验字段：出现多余字段、metadata 中出现保留键 → `DPE_VALIDATION`；未知 category → `DPE_CATEGORY_UNKNOWN`；
2. 重算 content_hash，与路径不符 → `DPE_HASH_MISMATCH`。

响应 `201`（本会话内新写入）或 `200`（本会话内重复）。201 / 200 MUST 只按本会话已收到的内容判定，不反映服务端其他位置是否已存该内容（core.md §3.4、§9）。

### 4.7 `PUT {remote}/staging/{sid}/blobs/{sha256}`（分块与断点续传）

- 整体上传：不带 `Content-Range` 的 `PUT`，体为完整字节。
- 分块上传：`PUT` + `Content-Range: bytes {from}-{to}/{total}`，块大小不超过 `blob_chunk_bytes`，MUST 按序追加；全部字节到齐后服务端校验 sha256。
- 断点查询：`HEAD` 同一 URL，响应头 `DPE-Upload-Offset: {n}` 表示**本会话**已收字节数（同样不反映会话外是否已存该 blob）。
- 校验失败返回 `DPE_HASH_MISMATCH` 并丢弃已收内容。

### 4.8 `PUT {remote}/documents?uri=…`（commit）

```json
{ "hash_contract": "dpe1",
  "file_type": "md",
  "skeleton": { …同 4.3（含 doc / page metadata 与 entry occurrence）… },
  "attributes": { … },
  "staging_session": "st-…",          // 可选
  "objects": [ { …内联内容对象… } ],   // 可选（快路径）
  "force": false }
```

- 文档身份取自查询参数 `uri`，请求体不再重复 `file_uri`。
- CAS 前置条件走 `If-Match` / `If-None-Match: *`（§3.2）。
- 响应 `201`（created）或 `200`（updated / unchanged），体为 core.md §3.3 的 commit 响应，头带文档当前的 `DPE-Revision` 与 `ETag`（`unchanged` 时即原 revision，§3.3）。
- 失败的 commit 不消费 `staging_session`（core.md §3.4）。

### 4.9 `DELETE {remote}/documents?uri=…` 与 `POST {remote}/move`

- delete：CAS 走 `If-Match`，成功 `204`。
- move：

  ```json
  { "from_uri": "…", "to_uri": "…", "base_revision": "r-9" }   // 或 "force": true
  ```

  源 revision 不符 → `409` + `DPE_REVISION_CONFLICT`；源不存在 → `404` + `DPE_NOT_FOUND`；目标已存在 → `409` + `DPE_ALREADY_EXISTS`。成功 `200`，体为目标文档的 `{ "revision": "…", "doc_hash": "dpe1:…", "state_hash": "dpe1:…" }`，头带 `DPE-Revision` 与 `Content-Location: {remote}/documents?uri={to_uri}`（§3.3）。

## 5. 幂等键

commit / delete / move 请求 MUST 携带 `Idempotency-Key` 头（语义见 core.md §5.1），缺失 → `400` + `DPE_VALIDATION`：

- 键为客户端生成的唯一字符串（SHOULD 为 UUID）。**每个不同的请求使用新键**；重试 MUST 复用同一键，并发送逐字节相同的请求（方法、目标 URI、条件头与请求体）。
- 服务端 MUST 在 `idempotency_window_seconds` 内对同键请求重放首次**已完成**的响应（状态码、头与体），不再执行。只有 2xx 与带 `code` 且 `retryable: false` 的错误响应算已完成；`429`、`503`、`DPE_IDEMPOTENCY_IN_PROGRESS` 的 `409`，以及 `500` 等不带 `code` 的响应 MUST NOT 被记录或重放（core.md §5.1）。收到已完成响应后再次发起的请求 MUST 使用新键。
- 服务端按（方法、目标 URI、条件头、请求体字节）判定"同一请求"：同键不同请求 → `422` + `DPE_IDEMPOTENCY_MISMATCH`；同键首个请求仍在处理 → `409` + `DPE_IDEMPOTENCY_IN_PROGRESS`（带 `Retry-After`）。
- 幂等键的作用域是（调用者身份, 键）：不同调用者的同名键互不影响。

## 6. 错误格式（RFC 9457）

```json
{ "type": "urn:dpe:error:revision-conflict",
  "title": "revision conflict",
  "status": 412,
  "detail": "expected r-9, current r-12",
  "code": "DPE_REVISION_CONFLICT",
  "retryable": false }
```

- `type` 是规范中稳定的 URI。草案使用 `urn:dpe:error:<kebab-case>`；定稿后若规范发布到固定域名，MAY 改为该域名下的 URL（plan §16）。
- 扩展成员：`code`（core.md §6 的错误码）与 `retryable`（语义见 core.md §6：仅表示原样重试可能成功）。
- HTTP 状态映射（每个 code 在给定端点上只有一个状态码；客户端按 `code` 分派，§3.2）：

| code | 状态码 |
| --- | --- |
| `DPE_VALIDATION`、`DPE_CONTRACT_UNSUPPORTED`、`DPE_CATEGORY_UNKNOWN`、`DPE_ATTRIBUTE_UNSUPPORTED`、`DPE_HASH_MISMATCH`、`DPE_MISSING_CONTENT` | 400 |
| `DPE_FORBIDDEN` | 403 |
| `DPE_NOT_FOUND` | 404；带 `If-Match` 的 PUT / DELETE documents 为 412（§3.2） |
| `DPE_REVISION_CONFLICT`、`DPE_ALREADY_EXISTS` | 412（条件头：PUT / DELETE documents）；409（请求体前置条件：move） |
| `DPE_IDEMPOTENCY_IN_PROGRESS` | 409 |
| `DPE_SESSION_EXPIRED` | 410（会话过期、已消费、不存在或不属于调用者，一律如此） |
| `DPE_PAYLOAD_TOO_LARGE` | 413 |
| `DPE_IDEMPOTENCY_MISMATCH` | 422 |
| `DPE_PRECONDITION_REQUIRED` | 428 |
| `DPE_RATE_LIMITED` | 429 |
| `DPE_UNAVAILABLE` | 503 |

429、503 与 `DPE_IDEMPOTENCY_IN_PROGRESS` 的 409 MUST 带 `Retry-After`。

## 7. 鉴权

协议不定义鉴权方案：

- 客户端凭证放在标准 `Authorization` 头；具体方案（PAT、OAuth、JWT、mTLS）由服务端决定。
- 未认证返回 `401` + `WWW-Authenticate`；无权限返回 `403`（体为 `DPE_FORBIDDEN` problem）。
- SDK 通过可插拔的 `CredentialProvider` 注入凭证，不感知方案细节。

## 8. 传输细节

- 请求 MAY 使用 `Content-Encoding: gzip`（在 capabilities 的 `content_encodings` 内协商）；`max_payload_bytes` **按压缩前计算**。
- 服务端 SHOULD 支持 HTTP/2；大量小文档场景依赖多路复用并发快路径。
- 协议不暴露服务端处理进度：commit 成功响应就是协议的终点。
