# DPE Connector 契约

> 状态：**部分定稿**（M1 进行中）——§6「运行边界」是规范正文（Issue #34 定稿，经 Issue #39 补充 §6.3 / §6.5 的 uri_prefix 规范化不动点；其关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解）；§1–§5、§7–§8 仍为大纲，另行撰写。
> 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §11。
> 这是独立的中立规范，**不属于 Core**：核心投递协议完全不知道 connector 的存在（类比 git 与 remote-helper）。
> Issue #2 已关闭：平台不引入 connector 运行环境，connector 由用户自行开发、自行部署；托管运行若将来需要，另立独立产品。v1 官方 connector 只有 Git connector（本仓 `connectors/git/`）。

## 0. 定位

- **运行器负责推送，插件只负责产出**。connector（插件）产出变更、Document、删除意图、移动意图；协商、暂存、CAS、状态缓存与 DPE 凭证全部由运行器掌握，插件拿不到 remote 凭证。
- 同一个 connector 既可由用户用 SDK 自带的独立运行器（`dpe-run`）自行部署，也可托管在某个宿主中运行——宿主只是本契约的另一个运行器实现。
- v1 不开放租户自定义 connector 在托管环境运行，但契约设计必须保证将来开放时只需扩展、不需重构。

## 1. 术语与角色（大纲）

connector（插件）/ 运行器 / 宿主 / 内容源实例 / remote。

## 2. 插件产出模型（大纲）

- Document（复用 core.md §2 的数据模型：DPE 只表达内容面，插件产出的每个字段都是源内容、都进 hash；治理属性与抽取衍生物不属于 Document）；
- **blob 字节**：可携带 blob 的元素（契约 1 §4.1，目前为 Image），其字节由插件用自己持有的数据源凭证取得（运行器不持有数据源凭证，MUST NOT 自行解引用数据源 url，契约 1 §4.2）。小对象内联产出；大对象产出不透明句柄，运行器通过 `read_blob` 请求向插件分块拉取字节（线协议见 §6.7），自行计算 `sha256:` 引用并上传。字节确实无法取得时 `blob` 留空；图片 url 等源信息照常放在元素 metadata 中；
- **易变字段**：插件 SHOULD NOT 把不随内容变化的易变源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）放进 metadata，否则每轮同步都会制造内容变化（契约 1 §2）；同理 SHOULD NOT 在元素 metadata 中重复骨架已表达的位置（页码、页内序号），否则插入一页会让后续元素对象全部重传（core.md §2.4）；
- 删除意图 `{file_uri}` 与移动意图 `{from_uri, to_uri}`；
- 变更枚举方式：全量枚举 / 源端增量游标（fingerprint 只存运行器本地缓存，见 §6.8）。

## 3. URI 前缀绑定（大纲）

- 创建内容源实例时由宿主 / 运行器分配 URI 前缀；
- 运行器 MUST 拒绝越界的 Document、删除与移动意图（线协议上的强制与判定规则见 §6.5）；服务端前缀授权再兜底一次。

## 4. 配置与凭证（大纲）

- connector 用 JSON Schema 声明配置，宿主据此渲染配置表单；
- 数据源凭证由宿主密钥存储注入，MUST NOT 进入插件配置明文；
- DPE remote 凭证只属于运行器；数据源凭证只属于插件——需要数据源凭证才能取得的字节（如需鉴权的图片 url）一律由插件产出（见 2）。线协议上的凭证边界见 §6.1。

## 5. 生命周期与调度（大纲）

实例创建 / 校验 / 同步一轮（scan 轮，见 §6.4）/ 暂停 / 删除；调度策略属于运行器，不属于契约；进程边界与线协议见 §6。

## 6. 运行边界（规范正文）

> 本节由 Issue #34 定稿。它定义运行器与插件之间的进程边界与语言无关线协议：任何语言实现的插件，只要遵循本节，就能被任何遵循本契约的运行器驱动。插件只产出源内容（§2），线协议上不出现 DPE 的 hash、契约版本与 remote 细节；校验、hash、协商、暂存、CAS、重试与凭证全部由运行器负责。

### 6.1 进程边界与信任模型

- **插件 MUST 是独立进程**：由运行器启动，一个内容源实例对应一个插件进程；同一进程 MUST NOT 服务多个实例。语言无关性由此保证——插件不链接 SDK，也不要求与运行器同语言。
- **标准流分工**：插件从 stdin 读协议消息、向 stdout 写协议消息；stdout MUST 只包含协议消息（不得混入日志、调试输出或 BOM），诊断日志写 stderr。运行器 MUST 持续排空 stderr（或把它导向自身 stderr），否则插件可能因管道写满而阻塞。
- **请求方向**：v1 中所有请求都由运行器发起，插件只应答；插件 MUST NOT 主动发起请求或通知。单管道因此不会死锁；无长请求时，插件实现就是「读一行、处理、写一行」的循环，处理长请求期间仍 MUST 继续读取并处理控制消息（§6.7 的取消要求）。
- **信任模型**：插件产出是不可信输入。运行器 MUST 全量校验（§6.6）并自行计算一切 hash，MUST NOT 把产出字段直接当作已校验的 DPE 对象；MUST NOT 因任意插件产出而崩溃（协议级无法继续的情形除外，§6.2）。
- **凭证边界**：运行器 MUST NOT 经任何通道（命令行参数、环境变量、工作目录文件、协议消息）把 DPE remote 凭证交给插件；运行器 MUST NOT 自行解引用数据源 url（契约 1 §4.2）。数据源凭证由插件持有、只注入插件（§4）；无论采用何种注入机制，凭证 MUST NOT 经线协议传递，运行器 MUST NOT 使用或记录数据源凭证。
- **v1 隔离立场**：自部署场景下插件以用户自身权限运行于用户自己的环境；本契约**不要求**运行器提供沙箱、资源配额或网络出口限制。将来托管产品由宿主在进程之外施加这些（本契约的进程边界与凭证边界不变），因此只需扩展、不需重构。

### 6.2 传输与分帧

- 线协议 MUST 是 **JSON-RPC 2.0 over stdio**：传输为插件的 stdin/stdout 一对有序字节流。
- 分帧 MUST 是 **NDJSON**：一条消息 = 一行紧凑序列化的 JSON（UTF-8、LF 结尾）。JSON 字符串内的换行按 JSON 规则转义，因此一行必然是完整消息。MUST NOT 输出多行或缩进格式；每条消息 MUST 立即冲刷；MUST NOT 发送空行；MUST NOT 使用 JSON-RPC 批量数组——一条消息 MUST 是单个 JSON-RPC 对象。
- 报文 MUST 是 I-JSON（同 core.md §2.8 第 0 步：无重复键、无孤立代理项）。空行或无法解析的行是协议错误，运行器 MUST 终止实例并如实上报，MUST NOT 跳过该行继续。
- **消息上限 `max_message_bytes`**：由运行器在 initialize 中给出（单位 UTF-8 字节、含行尾 LF），进程存活期内不变。双方 MUST NOT 发送超过上限的消息；运行器收到超限消息 MUST 终止实例（无法安全解析，不得截断或跳过）；插件收到超限请求 MUST 以 `-32600`（invalid request）拒绝并尽力丢弃该行。运行器已知远端 capabilities 的 `max_payload_bytes` 时，上限 MUST NOT 小于它（使远端可接受的单个元素在 JSON 转义后仍能成行），并 SHOULD 留出充分余量。注意与 `max_payload_bytes` 的区分：后者是 DPE 服务端的请求体上限（http.md §4.1），两者独立。
- 协议版本为 **`dpe-connector/1`**，与 DPE 协议版本（`dpe/1`）、hash 契约版本（`dpe1`）相互独立。
- **报文字段**：方法参数与结果中，未标注「可选 / 可缺省」的字段 MUST 出现且不得为 `null`；`null` 仅在文中明确给出语义处有效（如 §6.4 的 `next`、`cursor`，§6.5 的 `file_uri`），其余情形按非法值拒绝。

### 6.3 初始化与进程生命周期

`initialize` MUST 是插件进程启动后运行器发出的第一条消息：

```json
{ "jsonrpc": "2.0", "id": 1, "method": "initialize",
  "params": {
    "protocol_versions": ["dpe-connector/1"],
    "max_message_bytes": 33554432,
    "instance": {
      "uri_prefix": "git://acme/repo/",
      "config": { "repo": "https://example.com/acme/repo.git" }
    },
    "runner": { "name": "dpe-run", "version": "0.1.0" },
    "remote_limits": { "max_payload_bytes": 8388608, "page_max_bytes": 268435456, "blob_max_bytes": 104857600 }
  } }
```

| params 字段 | 必填 | 说明 |
| --- | --- | --- |
| `protocol_versions` | 是 | 运行器支持的线协议版本，按优先级排列 |
| `max_message_bytes` | 是 | 单条消息上限（§6.2） |
| `instance.uri_prefix` | 是 | 实例 URI 前缀，MUST 是符合 core.md §1.1 文法的合法 URI，且已是规范化不动点（`normalize_file_uri(p) == p`）；运行器在启动实例前 MUST 校验，不满足即拒绝启动并如实上报。插件 SHOULD 据此构造 file_uri（§6.5） |
| `instance.config` | 是 | 实例配置（§4：插件以 JSON Schema 声明、宿主渲染的表单数据；MUST NOT 含凭证），可为 `{}` |
| `runner` | 否 | `{name, version}`，供插件日志 |
| `remote_limits` | 否 | 远端限额 `{max_payload_bytes, page_max_bytes, blob_max_bytes}`（http.md §4.1）；运行器获取过 capabilities 时 SHOULD 提供，供插件做源→文档映射的切分决策（core.md §3.2） |

响应：

```json
{ "jsonrpc": "2.0", "id": 1,
  "result": {
    "protocol_version": "dpe-connector/1",
    "plugin": { "name": "git-connector", "version": "0.1.0" },
    "capabilities": { "supports_cursor": true }
  } }
```

| result 字段 | 必填 | 说明 |
| --- | --- | --- |
| `protocol_version` | 是 | 从 `protocol_versions` 中选定的版本，原样返回 |
| `plugin` | 是 | `{name, version}`，插件自报（可观测性） |
| `capabilities` | 否 | v1 定义 `supports_cursor`（布尔，缺省 false，§6.8）；运行器 MUST 忽略不认识的键 |

- 插件 MUST 从 `protocol_versions` 中选择自己支持的一个；无一支持时 MUST 以 `-32004`（version_unsupported）失败。运行器收到不在自己列表中的版本时 MUST 终止实例，MUST NOT 静默降级。
- 配置不可用时插件 MUST 以 `-32005`（invalid_config）失败。
- **握手先行**：initialize 成功完成前，插件 MUST NOT 产出任何内容，MUST 以 `-32600` 拒绝其他请求。

进程生命周期：

- **启动**：运行器负责启动插件进程；可执行文件、参数与运行环境的取得方式属于宿主 / 运行器实现，不在本契约。插件 MUST NOT 依赖交互式终端（TTY）。
- **重启边界**：运行器 MAY 在轮边界重启插件（含空闲期）；MUST NOT 在一轮内重启，故障恢复除外（此时该轮作废，§6.4）。实例配置、插件版本或 uri_prefix 变化时，运行器 MUST 重启插件并丢弃本地游标缓存（§6.8）。
- **正常关闭**：运行器发送 `shutdown` 请求；插件 MUST 停止当前工作、以结果响应，并尽快以退出码 0 退出。运行器随后关闭 stdin；宽限期内未退出则终止进程（先 TERM、后 KILL，作用于进程组）。
- **stdin EOF**：插件读到 stdin EOF 时 MUST 尽快退出（不得悬挂），这是运行器崩溃后不残留孤儿进程的保证。运行器 MUST 在关闭 stdin 后同时监视进程退出，MUST NOT 只等流 EOF（插件可能留有子进程持有 stdout）。
- **崩溃**：插件异常退出（非零退出码或信号）时本轮作废，运行器 MAY 按自身策略重启并重试，并 MUST 如实上报。已提交的文档不受影响——投递是逐文档原子的（core.md §3.3），扫描轮本身幂等。
- **分发与签名（v1 最低要求）**：自部署场景下插件由用户自行选择与部署，本契约**不要求**运行器做来源校验或签名验证。插件的打包、审核与签名属分发方（以及将来宿主产品）的责任，在协议之外完成（校验发生在启动前），因此托管产品也不需要改动线协议。

### 6.4 扫描轮、批次与背压

- **轮（round）**：一次按批拉取的序列构成一轮——从运行器发出轮首 `scan` 开始，到插件在批次响应中给出新 `cursor`（不再有 `next`）结束。**一个实例同时至多一轮**；运行器 MUST NOT 在上一轮的产出全部落地（§6.8）之前开始下一轮。
- **轮首请求**：`{"cursor": "…"}`（省略或 `null` 表示全量枚举）；MUST NOT 带 `next`。
- **续批请求**：`{"next": "…"}`，取上一响应给出的 token；MUST NOT 带 `cursor`。
- `max_items`：运行器给出的建议批大小上限（可选整数），插件 SHOULD 遵守。无论批次大小，单条消息 MUST NOT 超过 `max_message_bytes`（§6.2）——批次内条目数由插件按序列化后的大小自行控制。

```json
// 轮首（"cursor" 为上一轮结束批给出的游标；省略或为 null 表示全量枚举）
{ "jsonrpc": "2.0", "id": 7, "method": "scan",
  "params": { "cursor": "c-42", "max_items": 100 } }
```

```json
// 续批（"next" 为上一条响应给出的 token）
{ "jsonrpc": "2.0", "id": 8, "method": "scan",
  "params": { "next": "n-2" } }
```

```json
// 中间批：还有后续
{ "jsonrpc": "2.0", "id": 7,
  "result": { "items": [ …产出项… ], "next": "n-2" } }
```

```json
// 结束批：本轮结束，给出下一轮游标；"incremental" 标明本轮是否为增量枚举（§6.4）
{ "jsonrpc": "2.0", "id": 8,
  "result": { "items": [ …产出项… ], "cursor": "c-43", "incremental": true } }
```

- **响应**：`{"items": [ …产出项… ], "next": "…"?, "cursor": …?, "incremental": true|false}`。
  - `next` 为字符串 = 还有后续批，运行器以下一个 `scan` 带该 token 继续；缺省或 `null` = 本轮结束（插件 SHOULD NOT 发送 `null`）。中间批 MUST NOT 带 `cursor` 与 `incremental`。
  - 结束批 MUST 带 `cursor`（字符串 = 新游标，§6.8；`null` = 本轮不提供游标）与 `incremental`（布尔）。空批合法（`items: []`）。
  - **`incremental`**：本轮是否**基于运行器给出的 cursor 增量枚举**——运行器给了 cursor 且插件实际基于它枚举 ⇒ true；运行器未给 cursor（首轮、缓存丢失）或插件无法使用而回退为全量枚举（§6.8）⇒ false。`incremental: false` 表示本轮只保证**现存内容**的完整枚举，此前的删除/移动不在表达范围内（插件 MAY 仍产出它能确定的删除/移动）。运行器收到 `incremental: false` MUST 如实上报并可观测，MUST NOT 视同删除/移动已同步。
  - 插件对未知或过期的 `next` MUST 以 `-32602`（invalid params）拒绝；运行器收到后 MUST 中止本轮，并以原 cursor 重开（MUST NOT 把 token 当作可忽略的提示）。
  - `scan` 收到 `error` 响应（如 `-32003`）时本轮失败：运行器 MUST 如实上报、MUST NOT 推进游标（§6.8），退避后重试。
  - 运行器 MAY 对进行中的 `scan` 设置超时并在超时后 `cancel`（策略属运行器实现，§7）；插件对 `cancel` 的义务见 §6.7。
- **背压**：插件只在收到 `scan` 时产出，运行器处理完一批再拉下一批；背压由拉模型天然给出，插件 MUST NOT 依赖任何自行推送的通道。
- **中止**：运行器可随时丢弃本轮（不再发 `scan`，或终止进程）；轮的产出在运行器提交之前没有任何副作用。进行中的请求可用 `cancel` 取消（§6.7）。
- **顺序**：产出项按其在流中的顺序生效；同一 file_uri 的多次出现按序后写覆盖（core.md §2.4 的同级写入）；跨 URI 可并发处理（运行器内）。判断 URI 同一性前 MUST 先按 core.md §1.1 规范化。

一轮的典型流程：初始化 → `scan`（轮首）→ 插件返回一批条目与 `next` → 运行器逐条处理（其间按需 `read_blob` 读取 blob，§6.7）→ 继续 `scan`（续批）→ … → 插件返回结束批与新 `cursor` → 运行器完成本轮全部落地后持久化游标（§6.8）。

### 6.5 产出项与文档分段

产出项是 `scan` 响应 `items` 数组的元素，四种 `kind`，均为封闭 schema——出现未定义的字段或未知 `kind` 时该条目失败（§6.5 末），运行器 MUST NOT 静默忽略。

**`document`**：一篇文档（core.md §2.1–§2.3 的内容模型），页以数组顺序内联给出。

```json
{ "kind": "document", "file_uri": "git://acme/repo/docs/a.md",
  "document": { "file_type": "md", "title": "A", "doc_metadata": { "author": "…" } },
  "pages": [
    { "page_metadata": {},
      "elements": [ { "category": "NarrativeText", "text": "…" } ] }
  ] }
```

- `file_uri`：文档身份（URI，文法与规范化见 core.md §1.1）。
- `document`：文档对象，字段与 core.md §2.1 一致，但**不含 `pages`**——页由本条目给出。首段 MUST 带 `document`，续段 MUST NOT 带；违例时该文档整篇失败。
- `pages`：页对象数组，字段与 core.md §2.2 一致；`elements` 为内联元素对象（线格式见 §6.6）。页数可为 0。
- 插件 MUST 保证每条消息（含内联元素与 base64 字节）不超过 `max_message_bytes`；为此 SHOULD 依据 `max_message_bytes` 与 `remote_limits` 决定页与元素的切分。确实无法在限制内表达的内容 MUST 以条目级 `error`（`content_invalid`）如实上报，MUST NOT 发送超限消息。
- `continued`：布尔，缺省 false；为 true 表示本段之后还有同一 `file_uri` 的段（非末段 MUST 为 true）。
- **分段规则**：只在页边界切分，页数组按流中顺序跨段拼接；同一 `file_uri` 的各段 MUST 连续出现（中间不得插入其他条目），一轮内同一时刻至多一个未闭合的分段文档；末段 MUST 省略 `continued` 或为 false。轮结束（或轮中止）时仍有未闭合的文档 → 该文档**整篇失败**（§6.8），MUST NOT 只提交已收到的页。
- 插件 SHOULD 保证同一源两次枚举的页序与元素序稳定（同一内容产出相同顺序）——这是收敛判据（§8）的前提。
- 同一轮内同一 `file_uri` 出现多次完整的 document / delete / move 时按顺序生效（后写覆盖）。

**`delete`**：`{"kind": "delete", "file_uri": "…"}`——按 URI 删除整篇文档（core.md §4）。

**`move`**：`{"kind": "move", "from_uri": "…", "to_uri": "…"}`——原子改名（core.md §4）。

- **删除与移动 MUST 是显式条目**：运行器 MUST NOT 用「未出现即删除」做集合差分——增量枚举下未出现不等于已删除，推断会造成静默误删。
- **前缀强制**：`file_uri` / `from_uri` / `to_uri` MUST 是符合 core.md §1.1 的 URI；运行器 MUST 拒绝并如实上报不在 `instance.uri_prefix` 内的条目，MUST NOT 投递。判定按 core.md §1.1 规范化后的条目与该前缀做前缀匹配（同 core.md §3 `list` 的规则：码点匹配，不识别 path 段边界；需要按段匹配时前缀末尾自带分隔符）。前缀本身是规范化不动点（§6.3），条目规范化后仍以它开头，判定不因规范化而失效。

**`error`**：插件对无法产出的条目如实上报，MUST NOT 静默跳过。

```json
{ "kind": "error", "file_uri": "git://acme/repo/docs/broken.md",
  "code": "source_unavailable", "message": "…", "retryable": true }
```

- `code`、`message`、`retryable` 必填；`file_uri` 可选（为 `null` 或缺省，错误不针对单篇文档时）。
- `code` 为封闭枚举：`source_unavailable`（源端暂时不可达或读取失败）、`content_invalid`（源内容无法映射为合法 DPE 内容，或无法在 `max_message_bytes` 限制内表达）、`internal`（插件内部错误）。
- `retryable` 表示「同样的扫描重跑可能成功」，语义同 core.md §6 的 `retryable`。
- 条目级错误不终止本轮，但阻止游标推进（§6.8），运行器 MUST 如实上报。

条目的失败归属：

- **条目级失败**（封闭 schema 违例、未知 `kind`、内容校验失败、blob 读取失败、越界）：该条目失败，本轮继续处理其余条目，游标不得推进（§6.8），运行器 MUST 如实上报并在报告中区分来源。
- **协议级失败**（分帧 / JSON-RPC 违例、消息超限、未知 method、initialize 失败）：按 §6.2、§6.3 处理，运行器终止实例。

请求级错误用 JSON-RPC `error` 响应；错误码是闭集：

```json
{ "jsonrpc": "2.0", "id": 21,
  "error": { "code": -32003, "message": "source read failed",
             "data": { "retryable": true } } }
```

| code | 名称 | 语义 |
| --- | --- | --- |
| `-32700` / `-32600` / `-32601` / `-32602` / `-32603` | JSON-RPC 标准码 | 解析错误 / 非法请求 / 未知 method / 参数非法 / 内部错误 |
| `-32001` | `cancelled` | 请求被 `cancel` 取消（§6.7） |
| `-32002` | `unknown_handle` | 句柄未知或已失效（§6.7） |
| `-32003` | `source_failed` | 源端失败（`scan` 或 `read_blob`）；`data.retryable` 标注可否原样重试 |
| `-32004` | `version_unsupported` | initialize 无共同版本（§6.3） |
| `-32005` | `invalid_config` | initialize 的实例配置不被插件接受（§6.3） |

`data` 为扩展位（如 `retryable`）。运行器遇到未知的错误码 MUST 按不可重试处理并如实上报，MUST NOT 崩溃。

### 6.6 元素线格式与校验流水线

线元素与 core.md §2.3 的元素对象一致，唯一区别是 `blob` 字段的取值：线协议上给出字节的来源，运行器取得字节后把它替换为 `sha256:` 引用。

- 线协议上的 `blob` MUST 是下面的字节来源对象，MUST NOT 是 `sha256:` 引用字符串——插件不计算、也不得伪造引用；运行器取得字节后自行写入引用并上传。
- 字节来源互斥二选一：
  - `{"inline": "<base64>"}`：内联字节（RFC 4648 §4 标准字母表，带填充）。只适合小对象，受 `max_message_bytes` 约束（§6.2）。
  - `{"handle": "…", "size": 12345}`：不透明句柄（§6.7）；`size` 为字节数（整数，≤ 2^53−1），可选，插件 SHOULD 提供已知的大小。
- `blob` 缺省或为 `null` 表示源确实没有字节（合法；运行器最终不产出 `blob`）。`blob` 对象中 `inline` 与 `handle` 同时出现或同时缺失（但 `blob` 对象存在）→ 条目失败。
- category 不允许携带 blob 的元素 MUST NOT 出现 `blob` 与 `mime_type`（与 core.md §2.3 同样的封闭规则，契约 1 §4.1）。

```json
{ "category": "Image", "text": "架构图", "mime_type": "image/png",
  "blob": { "handle": "blob-1", "size": 20480 } }
```

运行器对每个文件条目按以下流水线校验，遇错即该条目失败（MUST NOT 跳过出错的元素或只提交其余部分）：

1. 条目封闭 schema（§6.5）；
2. 页对象与元素对象按 core.md §2 校验，顺序同 core.md §2.8：形状 → category 封闭枚举 → 封闭 schema（含 category 允许的字段）→ 逐字段（类型、数值界限 §2.6、metadata 全量、base64 与 `size` 合法性）。其中 `blob` 按本节的字节来源对象校验，替代 core.md §2.8 对 blob 引用格式的校验；第 3 步替换为引用之后，元素才是 core.md §2.3 的元素对象；
3. blob 解析（§6.7）：内联解码或句柄读取，取得字节、算出 `sha256:` 引用；
4. 分段合并为完整文档后，整体再按 core.md §2 的文档层规则校验（含 `file_type` 枚举），并由运行器重算三层 hash。

- 本地校验的错误码沿用 core.md §2.8 的口径（`DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN`），但这是**运行器对插件产出的本地校验**，与远端返回的 DPE 错误是两个来源，报告时 MUST 区分。
- hash 一律由运行器用规范的 hash 契约计算；插件产出中没有、也不需要 hash。

### 6.7 blob 读取与句柄生命周期

**`read_blob`**（运行器 → 插件）：

```json
{ "jsonrpc": "2.0", "id": 21, "method": "read_blob",
  "params": { "handle": "blob-1", "offset": 0, "max_bytes": 1048576 } }
```

```json
{ "jsonrpc": "2.0", "id": 21,
  "result": { "data": "iVBORw0KGgo…", "eof": false } }
```

- `offset`：本次读取的起始字节偏移（≥ 0，缺省 0）；`max_bytes`：本次返回的最大原始字节数（> 0）。插件 MUST 支持句柄范围内的任意偏移读取（内部可重开 / 重取数据源，方式不限），使运行器能从断点重试。
- `data`（必填）为原始字节的 base64（RFC 4648 §4，带填充），原始长度 MUST NOT 超过 `max_bytes`；`eof`（必填，布尔）为 true 表示本次返回的块到达字节流末尾，false 表示之后还有字节。返回空 `data` 时 MUST 带 `eof: true`（防止运行器空转）；offset 等于或超过实际长度时返回空块 + `eof: true`。
- 插件对未知或已失效的句柄 MUST 以 `-32002`（unknown_handle）拒绝；源端读取失败 MUST 以 `-32003`（source_failed）拒绝，并在 `data.retryable` 中标注可否原样重试。

句柄：

- 句柄是不透明字符串，含义由插件定义；运行器 MUST NOT 解析。
- **快照一致性**：插件 MUST 保证句柄指向产出它时那份字节的确定快照——轮内任意时刻、任意偏移读到的字节都来自同一份字节；同一偏移重复读取结果一致。
- **生命周期**：句柄自产出起、至运行器发出下一轮的轮首 `scan`（或 `shutdown`）为止有效；此后插件 MAY 失效。运行器 MUST 在下一轮开始前完成上一轮全部句柄的读取（§6.4 的轮约束）；同一句柄 MAY 多次读取（重试）。
- **`size` 一致性**：`size` 已声明时，读取全部完成后的实际总长 MUST 等于它；不符（或提前 `eof`）时运行器 MUST 判该条目失败。

运行器的读取职责：

- MUST 以分块方式流式读取（块大小由运行器决定、受 `max_message_bytes` 约束），MUST NOT 要求单条消息承载完整 blob。
- 读取完成后运行器才得到 `sha256:` 与总长；blob 的分块上传在**读取完成之后**进行——上传路径含 `sha256`、`Content-Range` 含 total（http.md §4.7），两者都必须先确定。运行器 MAY 在本地暂存字节（内存或临时文件）以衔接读取与上传，MUST NOT 在摘要与总长确定前开始上传。
- 声明 `size` 超过远端 `blob_max_bytes` 时 SHOULD 立即判该条目失败，不必读取。
- **失败语义**：读取失败（含取消、超时、`size` 不符、句柄失效）使该条目**整篇失败**：运行器 MUST NOT 提交该文档（MUST NOT 只提交其余元素，MUST NOT 用空 `blob` 顶替——那会写出与源不等价的内容），MUST 如实上报；本轮游标不得推进（§6.8）。运行器 MAY 在本轮内重试读取。
- 同一轮内相同内容的 blob SHOULD 只读取与上传一次（去重属运行器实现）。

取消与超时：

- 运行器 MUST 为每个 `read_blob` 设置超时（有界、可配置）。超时或轮中止时，运行器 MAY 发送取消通知：

  ```json
  { "jsonrpc": "2.0", "method": "cancel", "params": { "id": 21 } }
  ```

- `cancel` 是通知（无响应）；它适用于任何进行中的请求（`scan` 与 `read_blob` 均可取消）。插件 MUST 尽快中止对应请求，并以 `-32001`（cancelled）错误响应；为此插件 MUST 在处理长请求期间继续读取并处理控制消息。
- 取消或超时后该次读取视为失败，但句柄不因此失效（可从任意偏移重试）。运行器 MUST 忽略已放弃请求迟到的响应。
- 运行器对不响应取消的插件 MAY 在超时后终止插件进程（该轮作废，§6.3）。

### 6.8 游标与增量

- **游标**：插件在轮结束时给出的不透明字符串（§6.4），代表「本轮枚举到的源状态位置」。运行器在下一轮把它作为轮首 `scan` 的 `cursor` 传回；插件据此只产出该位置之后的变更。
- 性质：MUST 不透明（运行器不得解析）；MUST 自包含——插件进程重启后仍可解释，解释不了时插件 MUST 回退为全量枚举（结束批标记 `incremental: false`，§6.4），MUST NOT 因此报错；运行器 MUST 只在本地缓存、按实例隔离（§2「fingerprint 只存运行器本地缓存」），MUST NOT 把它写进任何 DPE 字段或发往服务端；缓存丢失或首轮没有游标 = 全量枚举。
- **覆盖范围（增量轮）**：`incremental: true` 的轮 MUST 产出自所给游标以来的**一切**变化，包括删除与移动（显式条目）与内容变更；插件 MUST NOT 让新游标越过本次未能成功产出的变更。
- **全量轮**：`incremental: false` 的轮只保证**现存内容**的完整枚举——此前的删除与移动无法由协议表达：它们是相对上次枚举的差，而跨轮状态只有游标本身（§2「fingerprint 只存运行器本地缓存」；游标不可得时这个差不存在）。插件 MAY 产出它自己能确定的删除与移动（如源端提供删除事件）。运行器 MUST 按 §6.4 如实上报这类轮，MUST NOT 视同删除与移动已同步；全量轮的新游标同样受 fail-closed 约束——整轮成功落地后持久化，此后恢复为增量轮。
- **不支持游标**：`capabilities.supports_cursor` 为 false 时插件 MUST NOT 给出游标（结束批的 `cursor` 为 `null`、`incremental` 恒为 false），运行器 MUST NOT 缓存游标——此后每轮都是全量轮（推送侧的增量由 doc_hash 筛选承担，core.md §3.2），删除与移动只能靠插件自己确定并显式产出。
- **持久化（fail-closed）**：运行器 MUST 仅在整轮**全部条目成功落地**之后才持久化新游标——即无条目错误、无校验与越界失败、全部 blob 读取成功、全部文档以 `created` / `updated` / `unchanged` 提交成功、全部删除与移动成功或已达成目标状态（core.md §5.2）。任何未解决失败 → MUST 保留旧游标、MUST 如实上报、退避后重试。
- 重扫是安全的：commit 由内容幂等（core.md §5.2），delete 重试得到 `DPE_NOT_FOUND` 视为成功（core.md §5.2）。恢复路径就是稳定重跑整轮，MUST NOT 尝试只重放失败项。
- **永久失败不得空转**：越界产出、schema 违例、超出远端限额等插件缺陷会让游标被持续扣住。运行器 MUST 如实上报（含 file_uri 与条目定位）并可观测，MUST NOT 静默跳过，也 MUST NOT 无退避地空转；`retryable: false` 的条目同样不得被当作「跳过」。重试节奏与上限属运行器策略（§7）。

## 7. 错误与可观测（大纲）

- 插件错误分类（源端不可达 / 内容不合法 / 越界）、运行器重试责任边界；线协议上的错误承载与游标推进规则见 §6.5、§6.8。

## 8. 一致性要求（大纲）

同一源的增量产出与全量枚举收敛后 MUST 一致（对应 M4 验收）；删除与移动的表达以游标为前提（§6.8），M4 的删除与移动验收走游标路径。

## 9. 非目标

- 不定义任何具体数据源（官方 Git connector 以插件形式另行交付）；
- 不定义宿主的多租户、计费、权限模型；
- 不复制 Core 的投递语义——运行器直接遵循 core.md 与 HTTP 绑定。
