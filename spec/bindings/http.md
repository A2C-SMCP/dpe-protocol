# DPE v1 HTTP 绑定

> 状态：**定稿**（M1，2026-10-06；此后的变更经 Issue 修订并发布新文档版本）｜ 依据：[docs/plan/v1-plan.md](../../docs/plan/v1-plan.md) §9，经 Issue #4、#6（评审）、#30、#31、#39、#63、#70、#73 修订
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
| `upload`（元素对象） | `PUT {remote}/staging/{sid}/objects/{content_hash}` |
| `upload`（blob） | `PUT {remote}/staging/{sid}/blobs/{sha256}` |
| `commit` | `PUT {remote}/documents?uri={file_uri}` |
| `delete` | `DELETE {remote}/documents?uri={file_uri}` |
| `move` | `POST {remote}/move` |

`uri` / `prefix` 查询参数按 RFC 3986 百分号编码。`documents?uri=` 就是**文档资源**本身：读（GET / HEAD）、写（PUT）、删（DELETE）作用在同一个目标资源上，条件请求头因此恰好约束被 CAS 保护的文档（RFC 9110 §13）。

## 2. 媒体类型

- JSON 主体（含页对象与元素对象的上传）一律 `application/json`；错误为 `application/problem+json`（§5）；blob 上传用 blob 自身的媒体类型（未知时 `application/octet-stream`）。
- 媒体类型不承载版本：协议版本以 capabilities 的 `protocol` 为准，hash 契约由 `DPE-Hash-Contract` 头声明（§3.1）。
- 命名保持中性：媒体类型、头字段、错误码中 MUST NOT 出现具体产品名。

## 3. 并发控制：doc_hash 即 ETag

### 3.1 契约声明与版本令牌

- **带 JSON 请求体的端点的校验顺序**（batch_head、negotiate、页对象与元素对象的**非分块** upload、commit、move；blob 上传与分块的页对象上传的请求体是原始字节，校验在字节到齐后进行（§4.6、§4.7），不适用本节顺序）：
  1. 整个请求体是 I-JSON（core.md §2.8 第 0 步），否则 `DPE_VALIDATION`；
  2. 契约声明（`DPE-Hash-Contract` 头），否则 `DPE_CONTRACT_UNSUPPORTED`；
  3. 请求信封（batch_head、negotiate、commit、move 的请求体顶层，形状见 §4 各端点）：请求体是 JSON 对象；顶层成员封闭，出现 §4 未定义的成员 → `DPE_VALIDATION`；必有成员必须出现，各成员的类型正确，否则 `DPE_VALIDATION`；可选成员为 null 视同缺省。upload 的请求体就是对象本身，没有信封；
  4. 按 core.md §2.8 校验请求体中的对象。

  以上都只看请求本身。写操作的授权排在这些校验之后、一切依赖文档状态的判定之前（core.md §3.3、§5.2，#63）。commit 的完整求值顺序见 core.md §3.3，move 见 core.md §5.2。
- **契约声明**：每个请求 MUST 带 `DPE-Hash-Contract: <契约>`（取值为 capabilities 的 `hash_contracts` 之一，如 `dpe1`；`GET capabilities` 除外）。请求体中的 hash 按它计算，响应中的 hash 按它给出。未声明或不受支持 → `DPE_CONTRACT_UNSUPPORTED`。文档资源的响应 MUST 带 `Vary: DPE-Hash-Contract`。
- **URI 的文法与规范化**（core.md §1.1）：`uri` 查询参数（GET / HEAD / PUT / DELETE）与请求体中的 URI 成员（batch_head 的 `uris`、negotiate 的 `file_uri`、move 的 `from_uri` / `to_uri`）MUST 符合 core.md §1.1 的文法，不合法 → `DPE_VALIDATION`；与本节的其余校验一样只看请求本身，先于一切依赖文档状态的判定。服务端以规范化形式作为身份；`get_skeleton`、`list` 返回的 `file_uri` 与 move 成功响应的 `Content-Location` MUST 为规范化形式。`list` 的 `prefix` 是例外：不规范化、不按 URI 文法校验（§4.4）。
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
- 同一个 `code` 在不同端点可能映射到不同状态码（如 `DPE_PRECONDITION_FAILED` 在 PUT 上是 412、在 move 上是 409）。**客户端 MUST 依据错误的 `code` 分派**（有 problem 体时取体中的 `code`，HEAD 取 `DPE-Error-Code` 头，§5），MUST NOT 依据状态码分派。
- 若仍收到既无 problem 体、也无 `DPE-Error-Code` 头的 `412`（例如网关自行求值），客户端 MUST 先 `head` 该文档再判定：
  - PUT：doc_hash 等于提交内容 → 成功；不存在 → `DPE_NOT_FOUND`；否则 → `DPE_PRECONDITION_FAILED`（`If-None-Match: *` 时为 `DPE_ALREADY_EXISTS`）。
  - DELETE：不存在 → 成功（§5.2）；doc_hash 等于 `If-Match` 的值 → 中间层误判，可原样重试一次，仍如此则上报；否则 → `DPE_PRECONDITION_FAILED`。

### 3.3 响应头

- `head` 用 `HEAD documents?uri=`：`200` 带 `DPE-Doc-Hash` 与 `ETag`；不存在返回 `404` + `DPE-Error-Code: DPE_NOT_FOUND`（HEAD 不带体，§5）。
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
  "document": {
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

`pages` 按文档对象 `pages` 的顺序给出全部页对象（重复的页 hash 对应重复的页对象）。返回值与最近一次写入的值内容等价（core.md §2.7），不含任何服务端衍生数据。

### 4.4 `GET {remote}/documents?prefix=…&cursor=…&limit=…`（list）

```json
{ "documents": [ { "file_uri": "feishu://doc/a", "doc_hash": "dpe1:…" } ],
  "next_cursor": "…" }
```

`next_cursor` 为 null 表示结束。cursor 不透明。

- `prefix` 缺省视同空串（列出全部文档）。前缀按原样与规范化后的 file_uri 做码点前缀匹配，服务端不对前缀做 core.md §1.1 的规范化（core.md §3）。
- `limit` 缺省取 capabilities 的 `list_page_max`；不是 1 到 `list_page_max` 之间的十进制整数 → `DPE_VALIDATION`（与 batch_head 超过 `batch_head_max` 一致）。
- `cursor` 只能取本端点此前返回的 `next_cursor`；服务端无法识别的 cursor → `DPE_VALIDATION`。

### 4.5 `POST {remote}/negotiate`

```json
// 请求
{ "file_uri": "feishu://doc/a",
  "document": { …同 4.3 的 document… },
  "pages": [ { …页对象… } ] }        // 可选：附带部分或全部页对象
// 响应 200
{ "missing_pages": ["dpe1:…"],
  "missing_content_hashes": ["dpe1:…"],
  "staging_session": { "id": "st-…", "expires_at": "2026-10-01T00:00:00Z" } }
```

- **求值顺序**（前一步失败即返回，后续步骤不执行；core.md §3.4）：
  0. 传输层：请求体超过 `max_payload_bytes` → `413` + `DPE_PAYLOAD_TOO_LARGE`；
  1. 报文校验（同 §3.1）：I-JSON → 契约头 → 请求信封（`file_uri` 必须出现、为字符串且符合 core.md §1.1 的 URI 文法，非法（如相对引用、空串、非 ASCII）→ `DPE_VALIDATION`；`document` 必须出现，`pages` 可选且为数组；未定义成员 → `DPE_VALIDATION`）→ 文档对象 → 附带的页对象（按数组顺序）；
  2. 授权：对 `file_uri` 的写授权 → `403` + `DPE_FORBIDDEN`；报文校验只看请求本身，授权排在其后、一切依赖文档状态的判定之前（core.md §5 总则）；
  3. 开会话、把附带的页对象存入会话、计算缺失清单；未通过授权的调用者不获得会话与缺失清单。会话绑定与去重范围使用 `file_uri` 按 core.md §1.1 语法规范化后的形式——同一篇文档的等价写法不会绑定出不同的会话。
- negotiate 不携带也不校验任何 CAS 前置条件（CAS 只在 commit 时裁决）；会话绑定 `(file_uri, 调用者身份)`（core.md §3.4）。
- `missing_pages`：文档对象引用、既未附带也不在去重范围内的页对象。`missing_content_hashes`：附带的页对象中引用、而不可得的元素对象（未附带的页由 §4.6 的上传响应给出）。附带的页对象存入会话。
- 请求体超过 `max_payload_bytes` 时，少附带页对象（只提交文档对象即可，文档对象只含页 hash 列表）。
- 缺失清单按去重范围计算（core.md §3.3）。

### 4.6 `PUT {remote}/staging/{sid}/pages/{page_hash}` 与 `…/objects/{content_hash}`

请求体为一个页对象或元素对象 JSON，即该 hash 的原像对象（core.md §2.2、§2.3）。服务端依次（前一步失败即返回，后续步骤不执行）：

0. 传输层：请求体超过 `max_payload_bytes` → `413` + `DPE_PAYLOAD_TOO_LARGE`；
1. 按 §3.1 的顺序校验：请求体是 I-JSON → 契约头 → 页对象整体超过 `page_max_bytes` → `413`（元素对象不分块，只有第 0 步的 `max_payload_bytes` 上限）→ 按 core.md §2.8 校验对象（`DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` / `DPE_CONTRACT_UNSUPPORTED`）；
2. 重算 hash，与路径不符 → `DPE_HASH_MISMATCH`；
3. 会话可用性（core.md §3.4）→ 不可用一律 `410` + `DPE_SESSION_EXPIRED`；
4. 本会话内该 hash 已完成 → `200`（重复）；否则登记入会话 → `201`。

第 1–2 步只看请求本身，先于一切依赖会话状态的判定（core.md §3.4）：同一路径 hash、但请求体的 hash 被篡改时得到 `DPE_HASH_MISMATCH`，不是 `200` 重复——篡改后的请求体已不是同一 hash 的上传。

响应 `201`（本会话内新写入）或 `200`（本会话内重复），体为下一层的缺失清单：

```json
// 页对象的响应
{ "missing_content_hashes": ["dpe1:…"] }
// 元素对象的响应
{ "missing_blobs": ["sha256:…"] }
```

201 / 200 MUST 只按本会话已收到的内容判定，不反映服务端其他位置是否已存该对象；缺失清单按去重范围计算（core.md §3.3、§8）。

会话的每个成功操作都会续期（core.md §3.4）：negotiate 响应的 `expires_at` 与 upload 各响应（含 `202` 中间块）的 `DPE-Session-Expires: <RFC 3339 UTC>` 头给出续期后的过期时间。

**页对象可分块**：单个页对象超过 `max_payload_bytes` 时，按 §4.7 的方式分块上传与断点续传（单块大小同样不超过 `blob_chunk_bytes`），总大小不超过 `page_max_bytes`。全部字节到齐后，服务端把它们解析为页对象，按契约 1 重算 page_hash 并与路径比较（不是对原始字节算 sha256）；最后一块的响应体为缺失清单。元素对象不分块，超限返回 `DPE_PAYLOAD_TOO_LARGE`（core.md §3.2）。

### 4.7 `PUT {remote}/staging/{sid}/blobs/{sha256}`（分块与断点续传）

- 整体上传：不带 `Content-Range` 的 `PUT`，体为完整字节。服务端依次：第 0 步传输层（超过 `max_payload_bytes` 或 `blob_max_bytes` → `413`）→ 契约头 → 校验原始字节的 sha256，与路径不符 → `400` + `DPE_HASH_MISMATCH` → 会话可用性（不可用一律 `410` + `DPE_SESSION_EXPIRED`）→ 本会话内该 hash 已完成 → `200`（重复），否则登记入会话 → `201`。整段上传通过校验后该对象即在本会话内完成；若此前有分块的部分进度，随之作废。
- 分块上传：`PUT` + `Content-Range: bytes {from}-{to}/{total}`，MUST 按序追加；全部字节到齐后服务端才校验。服务端依次（前一步失败即返回，后续步骤不执行；core.md §3.4）：
  0. 传输层：单块请求体超过 `max_payload_bytes` → `413` + `DPE_PAYLOAD_TOO_LARGE`；
  1. 契约头 → `400` + `DPE_CONTRACT_UNSUPPORTED`（请求体是原始字节，内容校验在到齐后进行）；
  2. 会话可用性 → 不可用一律 `410` + `DPE_SESSION_EXPIRED`；
  3. 本会话内该 hash 已完成 → `200`（重复）+ 缺失清单，不校验 `Content-Range` 与请求体——幂等（core.md §3.4）：同一 hash 重复上传 MUST 成功，重发最后一块同样命中；
  4. 分块一致性：`Content-Range` 格式非法、块长与范围不符、`total` 与本会话此前声明的不一致 → `400` + `DPE_VALIDATION`；
  5. 尺寸：单块超过 `blob_chunk_bytes`、`total` 超过 `blob_max_bytes`（页对象为 `page_max_bytes`）→ `413`，首块即判，不接收任何字节；
  6. 偏移：`from` ≠ 本会话已收字节数 → `400` + `DPE_VALIDATION`，响应 MUST 带 `DPE-Upload-Offset: {n}`（本会话已收字节数），客户端据它重新同步；
  7. 追加字节、续期；未到齐 → `202`，带 `DPE-Upload-Offset`（已收字节数）与 `DPE-Session-Expires`，无体。
- 到齐后：blob 校验原始字节的 sha256；页对象解析为页对象（I-JSON → core.md §2.8 → 按契约 1 重算 page_hash 与路径比较，不是对原始字节算 sha256）。校验失败（含 hash 不符，为 `400` + `DPE_HASH_MISMATCH`）MUST 丢弃该对象已收的全部内容，重传从零开始；通过后按 §4.6 返回 `201` / `200` + 缺失清单，并带 `DPE-Session-Expires`。
- 断点查询：`HEAD` 同一 URL，响应头 `DPE-Upload-Offset: {n}` 表示**本会话**已收字节数（同样不反映会话外是否已存该对象），并带 `DPE-Session-Expires` 给出会话当前的过期时间。断点查询只读，**不续期**（core.md §3.4）；会话不可用时返回 `410` + `DPE-Error-Code: DPE_SESSION_EXPIRED`（§5）。三种状态的返回：本会话没有该 hash 的任何进度 → `200` + `DPE-Upload-Offset: 0`（不是 `404`：「无进度」是正常状态，HEAD 的 `404` 也会与网关返回的非 DPE 错误混淆）；本会话内该 hash 已完成 → `200`，偏移为该对象已收的总字节数；到齐后校验失败、已收内容被丢弃后 → 偏移回到 `0`。
- **客户端义务**：任何一块的响应丢失或不确定时，客户端 MUST 先断点查询再按返回的已收偏移续传，MUST NOT 盲目重发（core.md §3.4）。查到的偏移等于自己声明的 `total` 即上传已完成：重发任意一块（如最后一块）即可按第 3 步得到 `200` + 缺失清单（该步不校验 `Content-Range` 与请求体）；MUST NOT 发送空区间（`bytes {total}-{total}/{total}` 本身不合法）。
- 以上分块规则同样适用于 §4.6 的页对象（单块上限同 `blob_chunk_bytes`），只是校验方式不同（见 §4.6），总大小上限为 `page_max_bytes`。

### 4.8 `PUT {remote}/documents?uri=…`（commit）

```json
{ "document": { …同 4.3 的 document… },
  "pages": [ { …内联页对象… } ],       // 可选
  "objects": [ { …内联元素对象… } ],   // 可选
  "staging_session": "st-…",          // 可选
  "force": false }
```

- 快路径：内联文档对象引用的全部页对象与元素对象，一次往返完成。大文档：只带文档对象并引用会话。

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

- `type` 是规范中稳定的 URI。v1 使用 `urn:dpe:error:<kebab-case>`；规范将来若发布到固定域名，MAY 改为该域名下的 URL（plan §16）。
- **`DPE-Error-Code` 响应头**：每个 DPE 错误响应 MUST 带 `DPE-Error-Code: <code>`，值等于 problem 体的 `code`。HEAD 响应不能带内容（RFC 9110 §9.3.2），HEAD 出错时只有这个头、没有 problem 体，客户端 MUST 按它分派；`retryable` 由 code 决定（core.md §6），不另设头。错误状态码的响应既无 problem 体、也无 `DPE-Error-Code` 头时不是 DPE 错误（例如 remote URL 配错或网关返回的 404），客户端 MUST NOT 猜测 code，按非协议错误上报。
- 扩展成员：`code`（core.md §6 的错误码）与 `retryable`（语义见 core.md §6：仅表示原样重试可能成功）。`DPE_MISSING_CONTENT` 另带 `missing: {pages: […], content_hashes: […], blobs: […]}` 与 `missing_truncated: true|false`（core.md §3.3）。
- HTTP 状态映射（每个 code 在给定端点上只有一个状态码；客户端按 `code` 分派，§3.2）：

| code | 状态码 |
| --- | --- |
| `DPE_VALIDATION`、`DPE_CONTRACT_UNSUPPORTED`、`DPE_CATEGORY_UNKNOWN`、`DPE_HASH_MISMATCH`、`DPE_MISSING_CONTENT` | 400 |
| `DPE_FORBIDDEN` | 403（写操作与 negotiate 都先判定授权，core.md §5 总则、§3.4） |
| `DPE_NOT_FOUND` | 404；带 `If-Match` 的 PUT / DELETE documents 为 412（§3.2） |
| `DPE_PRECONDITION_FAILED`、`DPE_ALREADY_EXISTS` | 412（条件头：PUT / DELETE documents）；409（请求体前置条件：move） |
| `DPE_SESSION_EXPIRED` | 410（会话过期、已消费、不存在、不属于调用者或不属于该 file_uri，一律如此） |
| `DPE_PAYLOAD_TOO_LARGE` | 413 |
| `DPE_PRECONDITION_REQUIRED` | 428 |
| `DPE_RATE_LIMITED` | 429 |
| `DPE_UNAVAILABLE` | 503 |

暂存分块的偏移不连续（`DPE_VALIDATION`，§4.7）带 `DPE-Upload-Offset` 头，客户端据此重新同步。

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
