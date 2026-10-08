# DPE Core v1（抽象模型与操作语义）

> 状态：**定稿**（M1，2026-10-06；此后的变更经 Issue 修订并发布新文档版本）｜ 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md)（尤其 §0.1 北极星原则），经 Issue #3、#4、#6、#30、#31、#39、#60、#63、#73 修订
> 本文关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。

DPE（Document / Page / Element）是把任意格式文档的**内容面**——面向 LLM 阅读的内容——**正确、增量、可靠**地投递到一个远端的标准协议。本文定义与传输无关的核心语义；v1 唯一的规范性传输绑定是 HTTP（[bindings/http.md](bindings/http.md)）；内容身份的计算见 [hash-contract-1.md](hash-contract-1.md)。

DPE 只表达内容（plan §0.1 P1）：不承载编辑、治理（鉴权、ACL、租户）、学习与召回的状态。这些属于服务端实现或 connector 的职责，本文 MUST NOT 出现任何服务端实现的私有概念。

## 1. 概念模型

一篇文档是一棵三层同构的 tree（契约 1），与 Git 的对象模型一一对应：

| 概念 | 定义 |
| --- | --- |
| **Remote** | 一个文档空间的地址，由服务端定义，协议不关心其内部结构。所有操作都相对于一个 remote。 |
| **file_uri** | 文档身份。RFC 3986 的 URI（scheme 必需，可带 query 与 fragment），在同一 remote 内唯一。scheme 由上游决定，协议不规定格式，不引入租户概念。比较前按 §1.1 做语法规范化；除此之外 MUST NOT 做任何语义规范化。 |
| **Document object** | 文档对象，三层 tree 的根：`{file_type, title?, doc_metadata, pages: [page_hash…]}`（§2.1）。对应 Git 的根 tree。 |
| **Page object** | 一页：`{title?, page_metadata, elements: [content_hash…]}`（§2.2），按 `page_hash` 寻址。对应 Git 的子 tree。 |
| **Element object** | 一个元素：`{category, 内容字段…, metadata}`（§2.3），按 `content_hash` 寻址。对应 Git 的 blob。 |
| **Blob** | 二进制内容（图片等），按 `sha256:<64hex>` 寻址，由元素对象的 `blob` 字段引用；哪些 category 可以携带 blob 由契约 1 §4.1 的表决定，blob 本身与内容类型无关（类比 Git 的 tree 不关心 blob 是什么）。blob 的存储方式由服务端实现决定。 |
| **doc_hash** | 文档对象的 hash（契约 1 §5）。DPE 中文档的全部状态都进 doc_hash，因此它同时是文档的**版本令牌**：判断是否变化、CAS 前置条件（§5）都只用它（plan §0.1 P4）。作用域是单个文档，remote 不存在全局版本。 |
| **Staging session** | 暂存会话。由 negotiate 开启，绑定 `(file_uri, 调用者身份)`，有过期时间；暂存的页对象、元素对象与 blob 对读接口和召回**不可见**（§3.4）。 |

每个对象的 hash 只由它自身内容和子对象 hash 的有序列表决定，不含它在上层中的位置：顺序由上层对象的数组表达。同一内容的对象（元素或页）在文档内只存一份、可被多处引用。

### 1.1 file_uri 的语法规范化（RFC 3986 §6.2.2）

`file_uri` 的文法是 RFC 3986 的 `URI` 产生式：scheme 必需，query 与 fragment 可选。规范化只在**语法层**进行，其结果即身份的比较形式。

**合法性判定**：下列三条构成封闭清单，不满足任一条的输入即 `DPE_VALIDATION`。实现 MUST NOT 在此清单之外加严或放宽：

1. 串以 scheme 开头：`ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )` 后接 `:`（RFC 3986 §3.1）；
2. 全串字符集：每个字符 MUST 属于 `pct-encoded`（`%` 加两位 HEXDIG）或 ASCII 的 `unreserved` / `reserved` 字符。非 ASCII 字符、空格、控制字符及其他不在字符集中的字符一律非法——上游 MUST 先按 UTF-8 做百分号编码再构造 `file_uri`；
3. 本规范不校验更深的成分文法（host 是否为合法 IP、port 是否为数字等）：这些按字面处理，不属于本节的拒绝面。

**变换**（三步，依次施加）：

1. 解码表示 `unreserved` 字符（`A-Z` / `a-z` / `0-9` / `-` / `.` / `_` / `~`）的百分号三元组，hex 位大小写不敏感（`%7e` → `~`）；
2. scheme，以及有 authority 时 host 成分（userinfo 之后、port 之前）中的 ASCII 大写字母改为小写：第 1 步解码出的字符参与本步（host 中的 `%45` 最终为 `e`）；仍以百分号三元组保留的编码不受本步影响（`%C3` 保留为 `%C3`，不得被本步小写成 `%c3`；三元组的 hex 由第 3 步统一为大写）。userinfo、port、path、query、fragment 保持原样。没有 authority 的 URI（如 `feishu:xxx`、`urn:…`）只做 scheme 小写，其余部分按其 scheme 语义是不透明内容，MUST NOT 另行解释；
3. 其余百分号三元组的 hex 位统一为大写（`%2f` → `%2F`）；`reserved` 字符的三元组 MUST NOT 解码（`%2F` 保持编码形式，不等于 `/`）。

**明确不做**（MUST NOT）：RFC 3986 §6.2.2.3 点段移除（`.` 与 `..` 是字面内容，`%2E` 解码为 `.` 后同样保留）、§6.2.3 基于 scheme 的规范化（不补空 path 的 `/`、不去默认端口、不增删尾斜杠）、§6.2.4 基于协议的规范化；不做 IDNA / punycode 转换；除上述解码外不新增或删除任何百分号编码。

规范化是**幂等**的：`normalize(normalize(x)) == normalize(x)`，所有实现 MUST 满足。

**应用**：

- 服务端 MUST 以规范化形式作为 `file_uri` 的身份：同一 remote 内的唯一性、暂存会话绑定（§3.4）、§3 `list` 的前缀匹配都基于它；`get_skeleton` 与 `list` 返回的 `file_uri`、`move` 成功响应的 `Content-Location` MUST 为规范化形式；
- 所有接受 `file_uri` 的入口——commit / delete / head / get_skeleton 的 `uri`、batch_head 的 `uris`、negotiate 的 `file_uri`、move 的 `from_uri` 与 `to_uri`——都按本节校验与规范化；`list` 的 `prefix` 是例外（不规范化、不按 URI 文法校验，§3）；connector 的实例 URI 前缀须是规范化不动点（connector 契约 §6.3）；
- 一致性向量以 `kind: "uri"` 提供合法（输入 → 规范化输出）与非法（输入 → `DPE_VALIDATION`）两组用例，各实现 MUST 逐例通过。

## 2. 数据模型

三层对象都是**封闭 schema**：每个字段 MUST 显式属于下文定义，未定义的字段 MUST 拒绝（`DPE_VALIDATION`）。可选字段缺省与 null 等价（契约 1 §3.2）。校验顺序见 §2.8。

### 2.1 Document object（文档）

| 字段 | 说明 |
| --- | --- |
| `file_type` | 必需。封闭枚举（§2.5） |
| `title` | 字符串，可缺省。源给出的文档标题（文档标题、网页 `<title>`、issue summary 等） |
| `doc_metadata` | JSON 对象，缺省视同 `{}` |
| `pages` | 必需。页对象 `page_hash` 的数组；**数组顺序即页的阅读顺序**，可为空数组 |

`file_uri` 是文档身份，不属于文档对象，不进 hash。`title` 与页 title 对称；源文件名（如 `filename`）是另一项源属性，放在 `doc_metadata` 中，不能代替 title。展示名如何选取由实现决定，协议不规定。

### 2.2 Page object（页）

| 字段 | 说明 |
| --- | --- |
| `title` | 字符串，可缺省 |
| `page_metadata` | JSON 对象，缺省视同 `{}` |
| `elements` | 必需。元素对象 `content_hash` 的数组；数组顺序即页内阅读顺序，可为空数组 |

页没有页号字段：页的位置就是它在文档对象 `pages` 中的下标。源文件自带的页码标签（印刷页码、PDF PageLabels，如 `iv`）是源内容，放进 `page_metadata`。

### 2.3 Element object（元素）

| 字段 | 说明 |
| --- | --- |
| `category` | 必需。封闭枚举，见契约 1 §4.1 |
| `text` | 字符串，可缺省 |
| `text_as_html` | 仅 `Table` / `Formula` |
| `blob` / `mime_type` | 仅契约 1 §4.1 允许携带 blob 的 category（目前为 `Image`）；`blob` 为 blob 引用 `"sha256:…"`，`mime_type` 为字节的媒体类型（契约 1 §4.2） |
| `metadata` | JSON 对象，缺省视同 `{}`。版面坐标、图片 url 等源提供的信息都放在这里 |

- 元素对象中出现其 category 未允许的字段（如 NarrativeText 带 `text_as_html`）MUST 拒绝（`DPE_VALIDATION`）——否则同一 content_hash 会对应不同字节，服务端去重时静默丢掉其一。

### 2.4 字段原则

**源即内容**（plan §0.1 P2）：DPE 的每个字段都由源提供，除身份 `file_uri` 外全部进 hash，没有例外、没有过滤。

- **服务端衍生物不进 DPE**：keywords、各类服务端 id、抽取过程写回的字段（如全局指代字典）等由服务端计算的数据，MUST NOT 写入任何 DPE 字段（含 metadata）。它们属于服务端自己的存储，读接口也 MUST NOT 把它们作为 DPE 字段返回。
- **源字段不得带会自行变化的默认值**（如 `created_at` 缺省取当前时间）：值由源提供，源给不出就留空。
- **易变字段不应进入 metadata**：不随内容变化、却会频繁变化的源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）SHOULD NOT 放进任何 metadata——它们一旦进 hash，每次同步都会制造内容变化。
- **不写入由位置算出的序号**（非规范性指引）：位置只由数组表达一次。元素 metadata SHOULD NOT 写入页码、页内序号，page_metadata SHOULD NOT 写入由位置算出的页序号。这不改变"进 hash"的规则，只是代价说明：这类键会让插入一页时后续的对象连锁变化、全部重传，退化为全量传输。源文件自带的页码标签不在此列（§2.2）。
- **治理不属于 DPE**：访问控制等治理属性不是内容，协议不承载；服务端依据调用者身份或自身配置决定（plan §0.1 P1）。将来若需要把数据源的文档权限同步到远端，以不进内容核心的扩展方式引入。
- **所有写入同级，只经 commit**（plan §0.1 P5）：DPE 字段只能经 commit 写入；服务端实现 MUST NOT 提供绕过 commit 修改 DPE 字段的途径。服务端自己修改内容（包括作为某个 URI 的来源，如用户直接上传的文件）同样经 commit、同样受 CAS 约束。来源与服务端的写入同权同级，后写覆盖前写。

### 2.5 file_type

封闭枚举，由源提供，属于文档对象，进 doc_hash。取值：

`bmp` `csv` `doc` `docx` `eml` `epub` `heic` `html` `jpg` `json` `md` `msg` `ndjson` `odt` `org` `pdf` `png` `ppt` `pptx` `rst` `rtf` `tiff` `tsv` `txt` `wav` `xls` `xlsx` `xml` `zip` `java_repo` `python_repo` `javascript_repo` `typescript_repo` `unk` `empty` `tfchat` `jira_project` `jira_issue`

注意是 `md` 而不是 `markdown`。`tfchat`、`jira_project`、`jira_issue` 是开放格式名，属 plan §1 命名规则的登记例外；新增取值 MUST NOT 带产品或品牌名。未知取值 MUST 拒绝（`DPE_VALIDATION`）。SDK MUST 以常量导出本枚举（同 `vectors/manifest.json` 的 `file_types`）。

### 2.6 数值

对象中任何位置的整数字面量（不含小数点与指数）的绝对值 MUST NOT 超过 2^53−1（与 JCS 一致，契约 1 §3.3；超大 id 请用字符串），否则 `DPE_VALIDATION`；带小数点或指数的数字按 IEEE-754 double 处理，超出 double 范围的（如 `1e400`）同样 `DPE_VALIDATION`。数值检查属于 §2.8 第 4 步。

### 2.7 内容等价

hash 定义了"同一内容"：两份输入的 doc_hash 相等，即为同一内容。契约 1 下的等价关系恰好包括：值为 null 的键与缺省等价；metadata 字段缺省与 `{}` 等价；JSON 数字按 JCS 规范化（如 `1.0` 与 `1`）。等价关系之外的任何差异都是内容变化——包括空串 `""` 与 null、Unicode 与 URL 的不同写法。服务端读回时 MAY 返回等价类中的任一表示，SHOULD 返回最近一次写入的原样表示。

### 2.8 校验顺序

一个对象可能同时有多处违例。为使任何实现对同一输入给出**相同的错误码**，校验 MUST 按以下顺序进行，遇到第一处违例即拒绝：

0. **I-JSON**：报文 MUST 是 [I-JSON](https://www.rfc-editor.org/rfc/rfc7493)，否则 `DPE_VALIDATION`。本步对整个报文做检查，先于下列所有步骤，只检查两件事：
   - 字符串与对象键都是合法的 Unicode 字符序列，不含孤立代理项；
   - 同一对象内没有重复的键。

   本步对应解析阶段。常见解析器默认放行重复键，有的也放行孤立代理项，实现 MUST 选用或配置能拒绝这两类输入的解析器（如 Python `json` 的 `object_pairs_hook`、serde 的自定义 `Visitor`）。

   数值范围不在本步检查，按第 4 步执行（§2.6）。解析器 MUST NOT 在解析阶段因数值越界而拒绝报文（如 serde_json 需启用 `arbitrary_precision`），以便按顺序在出错的值上报告。
1. **形状**：值必须是 JSON 对象，必有字段（元素的 `category`，页对象的 `elements`，文档对象的 `file_type` 与 `pages`）必须出现且不为 null（null 视同缺省，契约 1 §3.2），否则 `DPE_VALIDATION`。
2. **category**（仅元素对象）：不是字符串 → `DPE_VALIDATION`；不在契约 1 §4.1 的封闭枚举内 → `DPE_CATEGORY_UNKNOWN`。category 先于封闭 schema 判定，因为允许哪些字段由 category 决定。
3. **封闭 schema**：出现未定义的字段 → `DPE_VALIDATION`。其 category 未允许的内容字段同样算未定义字段；字段值为 null 也同样拒绝。
4. **逐字段**：按 §2.1–§2.3 表中的字段顺序逐个校验，一个字段完整校验后才校验下一个字段。完整校验包括：
   - 值的类型；
   - file_type 枚举（§2.5）；
   - `blob` 的引用格式（契约 1 §1）；
   - metadata 中的全部值（§2.6）；
   - 子对象 hash 列表的每一项，按数组顺序。每一项依次判定：
     1. 是字符串，否则 `DPE_VALIDATION`；
     2. 带 `:` 分隔的前缀，且前缀是受支持的契约，否则 `DPE_CONTRACT_UNSUPPORTED`（契约 1 §1）；
     3. 前缀之后是 64 位小写 hex，否则 `DPE_VALIDATION`；
     4. 前缀与本对象的契约相同，否则 `DPE_VALIDATION`（契约混用，契约 1 §5）。

一次请求携带多个对象时，按「文档对象 → 页对象（按数组顺序）→ 元素对象（按数组顺序）」逐个校验（commit 见 §3.3 第 1 步）。

**违例位置**（RFC 6901 JSON Pointer，相对于被校验的对象）：
- 第 0、1 步：对象自身（`""`）；
- 第 2 步：`/category`；
- 第 3 步：未定义字段的键。有多个未定义字段时，取 UTF-16 码元序最小的键；
- 第 4 步：出错的字段。metadata 中的值出错时，指向该值本身（如 `/metadata/a/0`）；子 hash 列表出错时，指向出错的那一项（如 `/elements/2`）；该字段本身类型不对时，指向字段（如 `/elements`）。

违例位置是 SDK 与诊断层面的要求，用于开发者定位问题。HTTP problem 体不携带违例位置（HTTP 绑定 §5）。

按此顺序，错误码在各实现间唯一确定。只有一处违例时，违例位置也唯一确定。多处违例且错误码相同时，报告哪一处由实现决定。一致性向量 `vectors/invalid_objects.json` 给出规范性用例：错误码必须一致；用例声明了位置时，位置也必须一致。

## 3. 操作

只读操作无前置条件；写操作（commit / delete / move）受 §5 CAS 约束，且 MUST 是**单文档原子**的。协议不提供批量 commit，也不提供集合级清单同步。

| 操作 | 语义 | 写？ |
| --- | --- | --- |
| `capabilities` | 返回协议版本、接受的 hash 契约版本、限额（`max_payload_bytes`、`page_max_bytes`、`staging_ttl`、blob 上限与分块参数）、`content_encodings`、可选能力 | 否 |
| `head` / `batch_head` | 按 URI 返回 `{doc_hash}`；不存在返回 null。批量版只读，不涉及原子性 | 否 |
| `get_skeleton` | 返回 `doc_hash`、文档对象与全部页对象（按文档对象顺序），不含元素对象。返回值 MUST 与最近一次写入的值**内容等价**（§2.7）——据此重算的 doc_hash 必须等于返回值 | 否 |
| `list` | 按 file_uri 前缀分页返回 `{file_uri, doc_hash}`。前缀按原样（不做 §1.1 的规范化，它不是完整的 URI）与规范化后的 file_uri 做码点前缀匹配，不识别 path 段边界；需要按段匹配时，调用方在前缀末尾自带分隔符（如 `/`） | 否 |
| `negotiate` | 提交文档对象（可附带部分页对象），返回缺失的页与元素对象，并开启一个 `staging_session` | 否（只开会话；开会话前须写授权，§3.4） |
| `upload` | 向暂存会话上传页对象、元素对象或 blob。幂等；服务端 MUST 校验字段（§2）并重算 hash，不符返回 `DPE_HASH_MISMATCH`；响应给出该对象引用的下一层中缺失的部分；页对象与 blob 支持分块与断点续传 | 暂存 |
| `commit` | 提交文档对象（页对象与元素对象可内联或引用暂存会话）。**原子切换**：要么完整生效，要么没有任何变化 | 是 |
| `delete` | 按 URI 删除整篇文档 | 是 |
| `move` | `from_uri → to_uri` 原子改名，学习产物原样保留。前置：源满足 CAS，目标不存在（否则 `DPE_ALREADY_EXISTS`） | 是 |

协议不提供按 hash 读取单个页对象或元素对象的接口：投递只依赖缺失清单，不需要读骨架；`get_skeleton` 的响应没有请求那样的上限，可流式返回。将来若增加按对象读取（对应 `git cat-file`），属于增量扩展，且 MUST 限定在调用者有读权限的文档内，否则会成为跨文档探测口（§8）。

### 3.1 能力协商

- 客户端 MUST 使用服务端声明接受的 hash 契约版本计算 hash；不支持时 MUST 失败（`DPE_CONTRACT_UNSUPPORTED`），MUST NOT 静默降级。
- 服务端收到未声明支持的契约版本的 hash 值 MUST 拒绝。
- **每个请求声明所用契约**（HTTP 绑定 §3.1）。读接口（head / batch_head / get_skeleton / list）MUST 按请求声明的契约返回 hash 值；服务端同时支持多个契约时，MUST 能对每个受支持契约给出已存内容的 hash（契约 1 §6 原位重算）。这样契约升级期间，仍用旧契约的客户端比对的是同一契约下的值，不会把未变内容误判为变化。

### 3.2 投递路径

- **快路径**：小文档 MAY 跳过 negotiate，在一次 commit 中内联文档对象、全部页对象与元素对象，一次往返完成。
- **大量小文档**：用 `batch_head` 比对 doc_hash 筛选需要推送的文档（本地算出的 doc_hash 与服务端返回值不同即推送），再并发快路径提交。筛选不依赖任何本地缓存，缓存丢失时结果不变。DPE 的全部字段都进 doc_hash，因此任何字段的变化都会被这一步发现。
- **大文档：逐层协商**，与 Git 推送 tree 的方式相同，每一层只补传缺失的部分：
  1. `negotiate` 提交文档对象（可附带部分或全部页对象），得到 `missing_pages`，以及已附带的页中缺失的元素对象；
  2. 上传缺失的页对象，每次响应给出该页缺失的元素对象；
  3. 上传缺失的元素对象，每次响应给出该对象缺失的 blob；
  4. 上传缺失的 blob；
  5. `commit` 提交文档对象并引用会话，原子切换。中途失败不影响召回，暂存内容过期回收。
- **规模边界**（如实写明，v1 的全部上限）：
  - **文档对象**必须整体出现在 negotiate 与 commit 的请求中，不可分块。文档对象约为 72 字节 × 页数，超过 `max_payload_bytes`（如 8 MiB 下约 11.6 万页）时返回 `DPE_PAYLOAD_TOO_LARGE`。这样规模的"文档"通常应由上游拆为多篇文档（各有自己的 file_uri）。
  - **页对象**可分块上传（§3.4），总大小上限为 capabilities 的 `page_max_bytes`，超过返回 `DPE_PAYLOAD_TOO_LARGE`。
  - **元素对象**不分块，超过 `max_payload_bytes` 时（只可能是超大文本；二进制走 blob）返回 `DPE_PAYLOAD_TOO_LARGE`，协议不提供文本切分，切分是上游的内容决策。
  - **blob** 可分块，总大小上限为 `blob_max_bytes`。
  - 在这些上限之内，任何文档都能提交。骨架增量的粒度是页：插入、删除、移动一页只需传文档对象与新页，其余页对象不重传。流式格式（markdown、docx、在线文档等）通常整篇只有一页，不享受骨架增量，但同样能提交。按内容分块的更细粒度树（如 prolly tree）不在 v1 范围。

### 3.3 commit 语义

请求：CAS 前置条件（§5）与请求体。请求体的成员是文档对象 `document`（必有）、可选的内联页对象 `pages` 与内联元素对象 `objects`、可选的 `staging_session` 引用、可选的 `force`。前置条件 `base_hash` / `if_absent` 由绑定在请求体之外承载（HTTP 为条件头，HTTP 绑定 §3.2），不是请求体成员。

服务端 MUST 按以下顺序求值，前一步失败即返回，后续步骤不执行；每个错误码都只出自其中唯一的一步：

0. **传输层**（与文档状态无关）：请求体超过 `max_payload_bytes` → `DPE_PAYLOAD_TOO_LARGE`。认证（401）、限流（`DPE_RATE_LIMITED`）、不可用（`DPE_UNAVAILABLE`）同属传输层，可在任何时刻发生，不在求值顺序之内。
1. **报文校验**（§2）：只看请求本身，与文档状态无关。依次为：
   1. 整个请求体是 I-JSON（§2.8 第 0 步，`DPE_VALIDATION`）；
   2. 契约声明（`DPE_CONTRACT_UNSUPPORTED`）；
   3. 请求信封：请求体只含上面列出的五个成员（出现其他成员，含 `base_hash` / `if_absent`，即未定义成员），`document` 必须出现，各成员的类型正确（`pages` / `objects` 为数组，`staging_session` 为字符串，`force` 为布尔），否则 `DPE_VALIDATION`；可选成员为 null 视同缺省（HTTP 绑定 §3.1）；
   4. 按 §2.8 校验文档对象，然后是内联页对象（按数组顺序），然后是内联元素对象（按数组顺序），错误码为 `DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` / `DPE_CONTRACT_UNSUPPORTED`；
   5. force 与 `base_hash` / `if_absent` 不得并存（`DPE_VALIDATION`）。
2. **授权**（§5 总则）：调用者对该 URI 的写授权；`force` 为 true 时还包括 force 权限 → `DPE_FORBIDDEN`。授权所需的输入（`force`）在请求体中，因此授权排在报文校验之后；报文校验与文档状态无关，授权仍先于一切依赖文档状态的判定（§8）。
3. **前置条件存在性**：既无 `base_hash`、`if_absent` 也无 `force` → `DPE_PRECONDITION_REQUIRED`（即使内容未变也拒绝，保持"写操作必须带前置条件"的形式要求）。
4. **unchanged**：对文档对象算出本次提交的 doc_hash（只需文档对象）；当前 doc_hash 已等于它时返回 `unchanged`，**不论前置条件是否满足**（§5.2），MUST NOT 产生任何写入或学习动作，也不处理内联对象、不检查也不消费所引用的暂存会话。
5. **前置条件求值**（§5.1）→ `DPE_PRECONDITION_FAILED` / `DPE_ALREADY_EXISTS` / `DPE_NOT_FOUND`。
6. **会话与可得性**：引用的暂存会话须可用于本文档（§3.4，否则 `DPE_SESSION_EXPIRED`）。服务端对每个内联对象计算 hash（Rule 0：不信任客户端，hash 一律由服务端算出；内联对象没有声明的 hash，因此不会出现 `DPE_HASH_MISMATCH`），然后检查文档对象引用的每个 page_hash、这些页对象引用的每个 content_hash、这些元素对象引用的每个 blob，都能在「去重范围内的已存内容 ∪ 暂存会话 ∪ 内联」中找到，否则 `DPE_MISSING_CONTENT`，本次 commit 无任何效果。
   - 缺失时，problem 体 MUST 带上缺失的 hash 清单（按层分为 pages / content_hashes / blobs，MAY 截断并注明），客户端上传到同一会话后重新 commit 即可。
   - **闭包不变式**：去重范围内已存的页对象，其引用的全部元素对象与 blob 也都在去重范围内（服务端只以完整的子树形式存储对象）。正因如此，缺失清单可以逐层给出：一个页不缺失，它的整棵子树就不缺失。
   - **去重范围**由服务端自行决定，协议只约束上下界：MUST 至少包含该文档当前状态引用的全部对象，MUST NOT 超出调用者有写授权的文档；negotiate、upload 响应中的缺失清单与 commit 的可得性判定 MUST 使用同一范围（§8）。范围不在 capabilities 中声明：客户端只按缺失清单行事，不需要知道范围（类比 Git 不暴露服务端的对象存储策略）。

服务端 MUST 按 category 实例化元素，MUST NOT 全部按纯文本处理。

响应：

```json
{ "status": "created|updated|unchanged",
  "doc_hash": "dpe1:…",
  "delta": { "added": 3, "removed": 1, "retained": 120 } }
```

**delta 是规范定义的量**：把新旧文档的页对象展开为元素 `content_hash` 序列，取两者**多重集**之差——`added` = 新∖旧，`removed` = 旧∖新，`retained` = 交集大小（均按多重集计）。服务端 MUST 如实回报；`created` 时旧文档为空集，`unchanged` 时 `added = removed = 0`。一致性跑分器校验 delta 与 `get_skeleton` 读回的结果。

**变化即重学**（plan §0.1 P3）：

- 任何层级的 hash 变化都是该层内容的变化：content_hash 变化是元素内容变了，page_hash 变化是页的内容（title、page_metadata 或元素序列）变了，doc_hash 变化是文档的内容（file_type、title、doc_metadata 或页序列）变了。服务端 SHOULD 刷新依赖变化层级的衍生物；刷新的效率由服务端自己解决，协议不为规避重学做任何设计。
- 顺序属于内容：页序列、页内元素序列的变化会改变 doc_hash / page_hash，即使 delta 全为 retained。例：对调"他们离婚了"与"A 与 C 再婚了"两句，delta 为 0，但页的内容已变，基于上下文的衍生物需要刷新；在中间插入一页，其余页对象不变，但文档对象已变，依赖页序的衍生物需要刷新。
- **delta 衡量的是传输量**：`retained` 只代表该元素对象不需要重传，不代表依赖它的衍生物仍然有效。

协议只保证**不制造伪变更**（内容未变的元素不会出现在 added / removed 中）；服务端是否高效地完成了重学，由其自身测试保证。

### 3.4 暂存会话

```
negotiate ──▶ open ──upload*──▶ open ──commit 成功──▶ 已消费
                │                    │  commit 失败 → 仍为 open
                └──── TTL 过期 ──────┴──▶ 过期回收（对外从不可见）
```

- 会话绑定 `(file_uri, 调用者身份)`，按文档隔离；**不绑定任何前置条件**。CAS 只在 commit 时按该次 commit 的前置条件裁决（§5），因此各种前置条件下的会话校验完全相同：会话存在、未过期、未消费，且 file_uri 与调用者身份相符。
- **negotiate 的求值顺序**：报文校验（只看请求本身）→ 对 file_uri 的写授权（§5 总则，`DPE_FORBIDDEN`）→ 开会话并计算缺失清单。授权先于开会话与一切依赖文档状态的判定；未通过授权的调用者 MUST NOT 获得会话或缺失清单（理由：§3.3 的去重范围上下界、§8 的探测面）。
- 会话中可暂存页对象、元素对象与 blob。negotiate 附带的页对象同样存入会话。
- **逐层缺失清单**：上传页对象的响应给出它引用、而在去重范围与本会话中都不存在的 content_hash；上传元素对象的响应给出它引用的缺失 blob。
- **分块**：页对象与 blob 支持分块与断点续传；全部字节到齐后服务端才校验 hash。元素对象不分块（§3.2）。
- **upload 的求值顺序约束**（各端点完整阶梯见 HTTP 绑定 §4.6、§4.7）：会话可用性判定先于「本会话中该 hash 是否已完成」，「已完成」判定先于分块参数与分块请求体的校验——分块请求的字节在到齐前不可校验，本会话中已完成的 hash 重复上传 MUST 成功（幂等见下），不得因分块参数或请求体不合法而失败；其他情形下（含非分块对象）请求本身的校验先于会话判定。到齐后的校验（blob 的 sha256、对象的解析与重算）失败 MUST 丢弃该对象已收的全部内容，重传从零开始。
- **分块续传的客户端义务**：任何一块的响应丢失或不确定时，客户端 MUST NOT 盲目重发，MUST 先断点查询（只读、不续期）并按其给出的已收偏移续传；盲目重发（from 小于已收偏移）得到 `DPE_VALIDATION`，该错误响应带已收偏移供重新同步（HTTP 绑定 §4.7）。断点查询返回本会话已收字节数（无进度为 0，已完成为该对象的总字节数，HTTP 绑定 §4.7）；偏移等于自己声明的总量即上传已完成，重发任意一块即可获得 `200` 与缺失清单。
- 引用的会话已过期、已消费、不存在、不属于该调用者或不属于该 file_uri 时，upload / commit 一律返回 `DPE_SESSION_EXPIRED`（不区分原因，避免泄露他人会话是否存在）。
- **失败的 commit（任何错误）MUST NOT 消费会话**。并发写入导致 `DPE_PRECONDITION_FAILED` 时，客户端重新读取后，以新的前置条件引用**同一会话**重新 commit，已上传到会话的内容无需重传。若他人的写入移除了本次提交依赖、但未上传到会话的对象（最小去重范围下它们原本来自文档的当前状态），commit 得到 `DPE_MISSING_CONTENT` 及缺失清单，补传到同一会话后重新 commit 即可。
- **过期**：会话的过期时间从最近一次成功的 negotiate 或 upload（含分块上传的中间块）起算，每次成功操作都会续期；只读的断点查询不续期（避免靠反复查询保活）；capabilities 声明的 `staging_ttl` MUST 不少于 1 小时。持续上传的大文档因此不会中途过期，闲置的会话尽快回收。
- 会话过期后重开 negotiate，新会话从空开始：过期会话中的内容不再可得，缺失清单与可得性判定都只看去重范围与新会话。
- upload 幂等：同一 hash 重复上传 MUST 成功且无副作用。upload 的一切可观察结果（"新写入 / 重复"的区分、断点续传的已收字节数）MUST 只按**本会话内**已收到的内容判定，与服务端其他位置是否已存该对象无关（§8）；缺失清单按去重范围计算（§3.3）。
- commit 以 `created` / `updated` 成功后会话即消费完毕；`unchanged` 不消费会话（会话随 TTL 回收）。未引用的暂存对象随会话回收。

## 4. 删除与移动

- `delete` 受 CAS 约束（§5），成功后该 URI 上的 `head` 返回 null。
- **复活防护**：基于旧 doc_hash 的 commit 在文档被删后只会得到 `DPE_NOT_FOUND`，由上层决定是否以 `if_absent` 重建。服务端不需要保存墓碑。
- `move` 成功后目标 URI 的 doc_hash 等于移动前源文档的 doc_hash（file_uri 不进 hash），源 URI 视同被删，不留任何痕迹（`list` / `head` 中与 delete 后完全一致）。学习产物 MUST 原样保留（不重新学习）。move MUST 带 `base_hash`，不支持 force。
- 文档内部删页、删元素、移动页，都通过提交新的文档对象 / 页对象表达。

## 5. 写入冲突（CAS）与重试

**授权总则**：所有写操作（commit / delete / move）MUST 先完成授权判定，再做任何依赖文档状态的判定（是否存在、doc_hash、unchanged 等）；move 对源与目标两侧都要判定。无写授权时一律返回 `DPE_FORBIDDEN`，与文档是否存在、内容为何无关（§8）。**negotiate 是写路径的第一步**：它不写入内容，但开会话同样 MUST 先完成对 `file_uri` 的写授权判定（§3.4）；upload 不重复判定——会话只属于与该 file_uri 绑定的调用者，commit 照常重新授权。

### 5.1 前置条件

| 场景 | 前置条件 | 结果 |
| --- | --- | --- |
| 新建 | `if_absent` | 已存在 → `DPE_ALREADY_EXISTS` |
| 更新 / 删除 / 移动 | `base_hash = H`（读到的 doc_hash） | 当前 doc_hash 不等于 H → `DPE_PRECONDITION_FAILED`；文档已删 → `DPE_NOT_FOUND` |
| 无前置条件 | — | **默认拒绝**：`DPE_PRECONDITION_REQUIRED` |
| 强制覆盖 | `force: true`（仅 commit） | 无条件覆盖。服务端 MAY 对 force 要求额外权限（`DPE_FORBIDDEN`） |

- **delete 的求值顺序**：授权 → 前置条件存在性（`DPE_PRECONDITION_REQUIRED`）→ 文档存在性（`DPE_NOT_FOUND`）→ `base_hash` 比较（`DPE_PRECONDITION_FAILED`）→ 删除。
- **ABA 无害**：文档从 A 变为 B 又变回 A 后，基于 A 的写入成立。协议不承载历史，当前状态就是写入者读到的状态，这次写入不会丢失任何人的内容。删除后以相同内容重建的文档同理：基于旧 A 的写入会作用于新建的文档，协议对学习产物的延续不作承诺。
- **契约升级期**：服务端同时接受多个契约时，MUST 把当前内容在任一受支持契约下的 doc_hash 都视为与 `base_hash` 匹配（契约 1 §6）。
- 前缀授权（哪个调用者能写哪些 URI）由服务端实现决定，协议只定义错误语义（`DPE_FORBIDDEN`）。

### 5.2 幂等由内容保证

写入的结果状态就是它提交的内容，因此协议不需要幂等键：

- **commit**：当前 doc_hash 已等于提交内容时一律返回 `unchanged`（§3.3）。响应丢失后原样重试，首次已生效则得到 `unchanged`，未生效则正常执行。
- **move**：请求为 `from_uri`、`to_uri` 与 `base_hash`。求值顺序为：
  0. 传输层：同 commit（§3.3 第 0 步）。
  1. 报文校验：请求体是 I-JSON（`DPE_VALIDATION`）→ 契约声明（`DPE_CONTRACT_UNSUPPORTED`）→ 请求信封：只含上述三个成员，`from_uri` / `to_uri` 必须出现且为字符串，`base_hash` 可缺省、出现时为字符串，否则 `DPE_VALIDATION`（可选成员为 null 视同缺省；move 不支持 force，带 `force` 即未定义成员）。
  2. 授权：源与目标两侧的写授权 → `DPE_FORBIDDEN`。与 commit 相同，授权排在只看请求本身的报文校验之后、一切依赖文档状态的判定之前。
  3. 前置条件存在性：缺少 `base_hash` → `DPE_PRECONDITION_REQUIRED`。
  4. 源不存在时：若目标存在且其 doc_hash 等于 `base_hash`，返回成功（与首次成功的响应相同）；否则 `DPE_NOT_FOUND`。
  5. 源存在时：doc_hash 不等于 `base_hash` → `DPE_PRECONDITION_FAILED`；目标已存在 → `DPE_ALREADY_EXISTS`；否则执行移动。

  `base_hash` 按值比较，与 commit 的 `base_hash` 相同：等于当前内容在任一受支持契约下的 doc_hash 即匹配（§5.1），不是这样的值（含前缀未知、格式不对）一律视为不匹配，不另做格式校验。

  第 4 步是"目标状态已达成即成功"：服务端不区分重试与首次请求，因此若源恰好已被他人删除、而目标恰好是另一份内容相同的文档，也返回成功——此时协议只保证状态（源不存在、目标内容为 H），不保证目标的学习产物来自源。首次 move 成功后目标又被他人改写或删除的，重试得到 `DPE_NOT_FOUND`，SDK 如实上报，由上层重新读取后决定。

  `from_uri` 与 `to_uri` 规范化后（§1.1）相等时：源存在即「目标已存在」，按第 5 步返回 `DPE_ALREADY_EXISTS`（doc_hash 与 `base_hash` 不符时 `DPE_PRECONDITION_FAILED` 仍优先）；MUST NOT 视为「目标状态已达成」而返回成功——第 4 步的已达成语义只覆盖源已被删除的情形。
- **delete**：响应丢失后重试得到 `DPE_NOT_FOUND` 时，SDK MUST 视为成功（目标状态"不存在"已达成）。
- **force commit**：重放 force 会覆盖首次提交之后他人的写入。响应丢失后 SDK MUST NOT 自动重放：先 `head`，doc_hash 等于提交内容即成功，否则上报，由上层决定是否重新发起。

重试后得到 `DPE_PRECONDITION_FAILED`，说明首次写入之后（或之前）有他人写入。按同级写入规则（§2.4），SDK 如实上报，由上层重新读取后决定；SDK 绝不自动改用 force。commit 恢复为 `unchanged` 时，首次写入的 delta 不可得，这是预期行为。

## 6. 错误码

`retryable` 只表示"**原样重试**同一请求可能成功"。需要改变请求才能恢复的错误一律为 `否`，恢复动作见"恢复"列；SDK 的通用重试逻辑只依据 `retryable`。

| code | retryable | 语义 | 恢复 |
| --- | --- | --- | --- |
| `DPE_VALIDATION` | 否 | 报文不合法：任一层对象出现未定义字段、元素对象多余字段、未知 file_type、整数越界、force 与其他前置条件并存、暂存分块的 Content-Range 非法、块长不符、总量与先前声明不一致、偏移不连续等 | 修正报文；暂存分块的偏移不连续时按错误响应带的 `DPE-Upload-Offset` 重新同步（HTTP 绑定 §4.7） |
| `DPE_CONTRACT_UNSUPPORTED` | 否 | hash 契约版本不被支持 | 按 capabilities 换契约，或失败 |
| `DPE_CATEGORY_UNKNOWN` | 否 | category 不在契约的封闭枚举内 | 修正报文 |
| `DPE_PRECONDITION_REQUIRED` | 否 | 写操作缺少 CAS 前置条件 | 补前置条件 |
| `DPE_PRECONDITION_FAILED` | 否 | 当前 doc_hash 与 `base_hash` 不符 | 重新读取后由上层决定（§5.2） |
| `DPE_ALREADY_EXISTS` | 否 | `if_absent` 冲突 / move 目标已存在 | 交上层决定 |
| `DPE_NOT_FOUND` | 否 | 文档不存在（含已删除） | delete 重试视为成功（§5.2）；commit 时交上层决定是否以 `if_absent` 重建（§4） |
| `DPE_SESSION_EXPIRED` | 否 | 暂存会话不可用：过期、已消费、不存在、不属于调用者或不属于该 file_uri（§3.4） | 重新 negotiate，上传缺失对象后重新 commit |
| `DPE_HASH_MISMATCH` | 否 | 暂存上传的对象与路径中声明的 hash 不符（仅 upload；commit 的内联对象没有声明的 hash）；到齐校验失败时丢弃该对象已收的全部内容 | 修正对象或 hash，从零重传 |
| `DPE_MISSING_CONTENT` | 否 | commit 引用了去重范围内不可得的页对象 / 元素对象 / blob；problem 体带缺失清单 | 引用了会话：把缺失对象上传到同一会话后重新 commit。未引用会话（快路径）：缺页对象或元素对象时可改为内联后重新 commit；缺 blob 时（blob 不能内联）先 negotiate 开会话，上传后再 commit |
| `DPE_PAYLOAD_TOO_LARGE` | 否 | 单个非分块请求或单个元素对象超过 `max_payload_bytes`；暂存分块的单块超过 `blob_chunk_bytes`（页对象分块同此上限）；页对象与 blob 的总大小超过 `page_max_bytes` / `blob_max_bytes`（分块时按首块声明的总量即判）（均按压缩前计算） | 改走暂存与分块；文档对象或元素对象超限由上游拆分文档或切分文本（§3.2） |
| `DPE_FORBIDDEN` | 否 | 无权限（含前缀授权、force 权限） | — |
| `DPE_RATE_LIMITED` | 是 | 限流（配合 Retry-After） | 按 Retry-After 原样重试 |
| `DPE_UNAVAILABLE` | 是 | 服务端暂时不可用（配合 Retry-After） | 按 Retry-After 原样重试 |

## 7. 一致性要求（判据）

任何实现 MUST 满足（plan §13 摘录，作为规范性验收条款）：

1. 增量推送、删除、move 收敛后的结果与全量推送完全一致；
2. 不制造伪变更：内容未变的元素 MUST NOT 出现在 delta 的 added/removed 中；
3. 分批投递期间，读接口与召回 MUST NOT 看到中间态；
   （说明，非规范性）这里的中间态指暂存中、尚未 commit 的内容；原子性约束的是 DPE 内容的读取。commit 生效后，服务端衍生物（索引、抽取等）何时追上新内容由服务端决定，不属于 DPE（本文开头的定位，plan §0.1 P1），例如衍生物可以最终一致地更新。
4. 不静默覆盖他人写入：默认拒绝无前置条件的写；同级写入之间的后写覆盖经 CAS 发生，不属于静默覆盖；
5. 字段变更不被静默丢弃：DPE 的全部字段都进 doc_hash，内容等价（§2.7）之外的任何变化都会被发现并落库；
6. 契约升级不引起未变内容的重推或重学。

## 8. 安全考虑

- **跨文档探测**：按内容寻址的去重天然是一个存在性预言机。若缺失清单（negotiate、upload 响应）或 commit 的可得性判定覆盖整个 remote，一个只被授权写某个 URI 前缀的调用者，可以通过提交任意 hash 探测其他文档里是否存在某段内容、某个页或某个 blob，绕过前缀授权；更进一步，commit 引用一个自己从未上传的 hash 若能成功，就等于凭 hash "认领"了别人的内容。因此去重范围 MUST NOT 超出调用者的写授权范围，各处缺失清单与 commit MUST 使用同一范围（§3.3），范围外的对象一律视为缺失。
- **上传侧信道**：upload 的"新写入 / 重复"状态与断点续传偏移只按本会话判定（§3.4），否则上传一个猜测的对象即可从响应得知它是否存在于别处。
- **会话隔离**：会话绑定调用者身份；引用他人会话与引用不存在的会话返回同一错误（§3.4），不泄露会话是否存在。
- **写操作不是探测口**：所有写操作先判定授权（§5 总则）。对无写授权的 URI，commit 的 unchanged、delete 的 `DPE_NOT_FOUND`、move 的"目标状态已达成即成功"与 `DPE_ALREADY_EXISTS` 都不会出现——否则调用者可借此得知文档是否存在、内容是否为某个 H。move 对源与目标两侧都先判定授权。
- **凭证边界**：数据源凭证与 remote 凭证分属 connector 与运行器（connector 契约 §0），协议不提供由服务端代为抓取 url 的通道（契约 1 §4.2）。
