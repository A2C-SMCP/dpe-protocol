# DPE v1 HTTP 绑定

> 状态：**草案**（M1，待评审定稿）｜ 依据：[docs/plan/v1-plan.md](../../docs/plan/v1-plan.md) §9
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
| `commit` | `POST {remote}/commit` |
| `delete` | `DELETE {remote}/documents?uri={file_uri}` |
| `move` | `POST {remote}/move` |

`uri` / `prefix` 查询参数按 RFC 3986 百分号编码。

## 2. 媒体类型

- 请求与响应主体：`application/dpe+json`（草案值；版本通过媒体类型参数 `; version=1` 或 capabilities 协商，定稿见 plan §16）。服务端 SHOULD 同时接受 `application/json`。
- 错误：`application/problem+json`（§6）。
- 内容对象与 blob 上传：`application/dpe.object+json` 与 blob 自身的媒体类型（未知时 `application/octet-stream`）。
- 命名保持中性：媒体类型、头字段、错误码中 MUST NOT 出现具体产品名。

## 3. 并发控制：映射到 HTTP 条件请求

| Core（§5） | HTTP |
| --- | --- |
| `revision` | `ETag`（强校验器，不透明带引号形式 `"…"`） |
| `base_revision = R` | `If-Match: "R"` |
| `if_absent` | `If-None-Match: *` |
| CAS 冲突 / move 目标已存在 | `412 Precondition Failed` |
| 缺少前置条件 | `428 Precondition Required` |
| `force: true` | 请求体字段 `force`（不映射到头；force 绕过条件请求语义，显式出现在报文里便于审计） |

- 每个成功写操作的响应 MUST 带新 `ETag`。
- `head` 用 `HEAD documents?uri=`：`200` 带 `ETag` 与 `DPE-Doc-Hash` 头；不存在返回 `404`（不带 problem 体）。
- `get_skeleton` 的 `200` 响应同样带 `ETag` 与 `DPE-Doc-Hash`，客户端 MAY 用 `If-None-Match` 走 `304`。

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
    "list_page_max": 1000
  },
  "content_encodings": ["gzip"],
  "attributes": ["tfrs/creator_id", "tfrs/group_id"],
  "features": ["move"]
}
```

`attributes` 列出该 remote 接受的键（示例中的 `tfrs/*` 只是某个服务端的声明，不属于规范）。`features` 列出可选能力；v1 内 `move` 为 MUST 实现，此处保留位置供扩展。

### 4.2 `POST {remote}/heads`（batch_head）

```json
// 请求
{ "uris": ["feishu://doc/a", "s3://bucket/b.md"] }
// 响应 200
{ "heads": [ { "revision": "r-9", "doc_hash": "dpe1:…" }, null ] }
```

响应数组与请求一一对应，不存在的文档为 `null`。数量超过 `batch_head_max` 返回 `DPE_VALIDATION`。

### 4.3 `GET {remote}/documents?uri=…`（get_skeleton）

```json
{
  "file_uri": "feishu://doc/a",
  "revision": "r-9",
  "doc_hash": "dpe1:…",
  "file_type": "md",
  "doc_metadata": { "created_at": "2026-09-01T02:03:04Z", "...": "…" },
  "attributes": { "tfrs/creator_id": "u-1" },
  "skeleton": {
    "title": null,
    "pages": [
      { "number": 1, "title": "第一页", "page_metadata": {},
        "elements": [ { "hash": "dpe1:…" }, { "hash": "dpe1:…", "metadata": { "lang": "zh" } } ] }
    ]
  }
}
```

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
  "base_revision": "r-9",            // 或 "if_absent": true
  "hash_contract": "dpe1",
  "skeleton": { …同 4.3… },
  "blobs": ["sha256:…"] }
// 响应 200
{ "missing_content_hashes": ["dpe1:…"],
  "missing_blobs": ["sha256:…"],
  "staging_session": { "id": "st-…", "expires_at": "2026-10-01T00:00:00Z" } }
```

negotiate 不校验 CAS 结果的持续有效性（commit 时才裁决），但 MUST 记录绑定的 `(file_uri, base_revision, 调用者身份)`。

### 4.6 `PUT {remote}/staging/{sid}/objects/{content_hash}`

请求体为内容对象 JSON（core.md §2.3 的内容字段，不含正交字段）。响应 `201`（新写入）或 `200`（幂等重复）。服务端 MUST 重算 hash，与路径不符返回 `DPE_HASH_MISMATCH`。

### 4.7 `PUT {remote}/staging/{sid}/blobs/{sha256}`（分块与断点续传）

- 整体上传：不带 `Content-Range` 的 `PUT`，体为完整字节。
- 分块上传：`PUT` + `Content-Range: bytes {from}-{to}/{total}`，块大小不超过 `blob_chunk_bytes`，MUST 按序追加；全部字节到齐后服务端校验 sha256。
- 断点查询：`HEAD` 同一 URL，响应头 `DPE-Upload-Offset: {n}` 表示已收字节数。
- 校验失败返回 `DPE_HASH_MISMATCH` 并丢弃已收内容。

### 4.8 `POST {remote}/commit`

```json
{ "file_uri": "feishu://doc/a",
  "hash_contract": "dpe1",
  "skeleton": { … },
  "file_type": "md",
  "doc_metadata": { … },
  "attributes": { … },
  "staging_session": "st-…",          // 可选
  "objects": [ { …内联内容对象… } ],   // 可选（快路径）
  "force": false }
```

CAS 前置条件走 `If-Match` / `If-None-Match: *` 头（§3）。响应 `200`（updated / unchanged）或 `201`（created），体为 core.md §3.3 的 commit 响应，头带新 `ETag`。

### 4.9 `DELETE {remote}/documents?uri=…` 与 `POST {remote}/move`

- delete：CAS 走 `If-Match`，成功 `204`。
- move：`{ "from_uri": "…", "to_uri": "…" }`，CAS（对 from）走 `If-Match`，目标已存在返回 `412` + `DPE_ALREADY_EXISTS`；成功 `200`，体带 `{ "revision": "…" }`，头带新 `ETag`。

## 5. 幂等键

commit / delete / move 请求 MUST 携带 `Idempotency-Key` 头：

- 键为客户端生成的唯一字符串（SHOULD 为 UUID）。**每个不同的请求体使用新键**；重试同一请求复用同一键。
- 服务端 SHOULD 在去重窗口内对同键请求返回首次结果（窗口下限属于 plan §16 未决项）。

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
- 扩展成员：`code`（core.md §6 的错误码）与 `retryable`。
- HTTP 状态映射：`DPE_VALIDATION` 等报文类 → 400；`DPE_FORBIDDEN` → 403；`DPE_NOT_FOUND` / 会话不存在 → 404；`DPE_ALREADY_EXISTS` / `DPE_REVISION_CONFLICT` → 412（`if_absent` 冲突亦为 412）；`DPE_PRECONDITION_REQUIRED` → 428；`DPE_PAYLOAD_TOO_LARGE` → 413；`DPE_SESSION_EXPIRED` → 410；限流 → 429；`DPE_UNAVAILABLE` → 503。429 与 503 MUST 带 `Retry-After`。

## 7. 鉴权

协议不定义鉴权方案：

- 客户端凭证放在标准 `Authorization` 头；具体方案（PAT、OAuth、JWT、mTLS）由服务端决定。
- 未认证返回 `401` + `WWW-Authenticate`；无权限返回 `403`（体为 `DPE_FORBIDDEN` problem）。
- SDK 通过可插拔的 `CredentialProvider` 注入凭证，不感知方案细节。

## 8. 传输细节

- 请求 MAY 使用 `Content-Encoding: gzip`（在 capabilities 的 `content_encodings` 内协商）；`max_payload_bytes` **按压缩前计算**。
- 服务端 SHOULD 支持 HTTP/2；大量小文档场景依赖多路复用并发快路径。
- 协议不暴露服务端处理进度：commit 成功响应就是协议的终点。
