# 官方 Git connector

把 Git 仓库的文件映射为 DPE 文档的 **dpe-run 插件**（[connector 契约](../../spec/connector-contract.md) §6）。
插件只产出源内容：不计算 hash、不接触 DPE remote 与 remote 凭证、不感知任何服务端实现的概念。

本包是 Git connector 的第一个交付（[Issue #65](https://github.com/A2C-SMCP/dpe-protocol/issues/65)）：
**骨架与文件映射、全量枚举**。增量游标与删除/移动意图、blob（图片字节）、端到端联调分别是后续拆分。

## 用法

```bash
# 开发环境（path 源指向同仓的 sdk/python 成员包，见 pyproject.toml）
uv sync
uv run pytest
uv run dpe-git-connector        # 不直接交互使用：由 dpe-run 以 stdio 启动

# 实例定义（connector 契约 §4.4）里指向本插件
{
  "definition_version": 1,
  "id": "docs-repo",
  "remote": { "url": "https://dpe.example.com/remotes/x" },
  "uri_prefix": "git://docs/",
  "plugin": { "manifest": "connectors/git/dpe-connector.json",
              "command": ["dpe-git-connector"] },
  "config": { "repo": "/srv/git/docs", "ref": "main" }
}
```

配置只有两项：`repo`（必填，**本地仓库路径**；本连接器用本机 git 读取，不代为访问远端）与
`ref`（缺省 `HEAD`）。清单 `dpe-connector.json` 与 `initialize` 自报的身份必须一致（契约 §4.1），
测试会断言。

## 映射规则

> 本节的规则就是本连接器的内容定义。**规则变更 = 全部文档的 hash 变化**（增量同步会当作内容
> 变化重新投递与重学）——发布时必须在 release notes 里标注为「全量内容变化」。

### 文件 → 文档

- 每个文件一篇文档，`file_uri` = 实例 URI 前缀 + 仓库内路径。路径按**原始字节**处理、按
  RFC 3986 百分号编码表达（非 UTF-8 的路径字节同样可表达），再调 `dpe_hash.normalize_file_uri`
  规范化。前缀必须以 URI 分隔字符（`/` `?` `#`）结尾——码点前缀匹配不识别段边界，`git://docs`
  这样的前缀拼上路径会改写 authority（`git://docs` + `a.md` → `git://docsa.md`），initialize
  时会被拒绝。
- `repo` 必须是**仓库根目录**（裸仓库则是裸仓库目录本身）：指向子目录时 git 仍能工作，但列出的
  路径是相对配置目录的（前缀丢失），同一文件会得到不同的身份，因此 initialize 直接拒绝。
- 只枚举**普通文件**：子模块（gitlink，内容不在本仓库）与符号链接（mode 120000，内容是链接
  目标而非源文件内容，不是源文件内容）都不产出。
- `file_type` 由扩展名或整名映射（下表），**源代码文件标 `txt`**：DPE 的 `file_type` 枚举里
  没有单个代码文件的取值（`*_repo` 是仓库级的），本连接器不发明取值。
- **不在映射表内的文件不产出**：既不产 `document` 也不产 `error`，也不影响游标。这意味着把一份
  被支持的文件改成不被支持的格式（改扩展名、改成子模块、删除）时，本包不会产出 `delete`——
  全量轮不表达删除（契约 §6.8），这一情形由后续的增量游标拆分处理。
- 文档 `title` 只取源里**显式声明**的标题（当前支持 markdown front matter 的 `title:`）；
  取不到就缺省。不从文件名推导——文件名是 `file_uri`（身份）的一部分，改名是移动而非内容变化。

| 扩展名 / 文件名 | file_type | 抽取方式 |
| --- | --- | --- |
| `.md` `.markdown` `.mdown` | `md` | CommonMark（+ GFM 表格）块结构 |
| `.txt` `.text` | `txt` | 按空行分块 |
| `.csv` / `.tsv` | `csv` / `tsv` | 整表一个元素（按行切分见下） |
| `.json` / `.ndjson` | `json` / `ndjson` | 规范化 JSON 文本 |
| `.py` `.pyi` `.java` `.js` `.mjs` `.cjs` `.jsx` `.ts` `.mts` `.cts` `.tsx` | `txt` | 按空行分块 |
| `Makefile` `Dockerfile` `LICENSE` `NOTICE` | `txt` | 按空行分块 |

暂不支持（不产出）：`rst`、`org`、`html`、`xml`、PDF、Office 文档、图片等一切二进制；这些格式
各自需要解析器，另立 extractor 任务。**当前没有任何源端天然分页的格式**：所有被支持的格式都是
单页文档（core §3.2：流式格式整篇只有一页），多页文档的映射属于分页格式 extractor 的范畴。

### 页与元素

- 每篇文档一页（`page_metadata` 为空对象）。页序与元素序即阅读顺序。
- markdown（CommonMark + GFM 表格，解析器 `markdown-it-py`，版本随本包钉死）：标题 → `Title`，
  段落与引用 → `NarrativeText`，围栏/缩进代码 → `CodeSnippet`，列表项 → `ListItem`（每个条目
  一个元素，项内的代码块并入该条目的文本、嵌套列表并入所属外层条目），块级原始 HTML →
  `UncategorizedText`（按源字面量保留，不解析 DOM 也不改写标记），水平线 → `PageBreak`
  （无 `text`），表格 → `Table`（`text` 行以换行分隔、单元格以制表符分隔；同时给出同构的
  `text_as_html`）。front matter（见下）是元数据头，不进元素。
  行内标记（`*强调*`、链接等）保留源字面量，只按块切分——不剥标记、不解析为纯文本。
- front matter 的认定：文件开头 `---` 与闭合 `---` 之间**每一行都是 YAML 顶层键值**（`key:`）
  才算；否则整段按 CommonMark 解析（开头两行 `---` 是水平线，不是元数据头），不静默丢内容。
  认定后该块进文档 `title`（仅取 `title:`），不进元素。
- txt 与源代码：按空行切成块，每块一个 `UncategorizedText` 元素——纯文本没有语义标记，
  类别不说假话。块内字节（含行尾）原样保留。
- csv / tsv：整表一个 `Table` 元素（`text` 与 markdown 表格同构，不带 HTML；`Table` 是内容
  类别，表格里的行列是它的文本表达）。
- json：`json.loads` 后按 `sort_keys=True, indent=2` 序列化（保证确定性），整段一个
  `NarrativeText`。ndjson：每个非空行一个 JSON 值，同样规范化后按行拼接。
- 不是合法 UTF-8 的文件、不是合法 JSON 的文件、单条目装不进 `max_message_bytes` 的文档：
  产出条目级 `error`（内容类为 `content_invalid`，插件内部异常为 `internal`），如实上报，
  不静默跳过；单行的解析异常不会带崩插件进程。

### metadata

**三层 metadata 一律为空对象**（元素 `metadata`、`page_metadata`、`doc_metadata` 都不写内容），
理由：

- 路径与文件名已经在 `file_uri`（身份）里，再进 metadata 会让一次 git 重命名变成
  「move + 内容变化」，破坏 move 保留学习产物的语义；
- `mtime`、commit id 是易变字段（core §2.4），进 hash 会让每轮同步都造出伪变更；
- 扩展名同理（它是 `file_uri` 的一部分），不从扩展名推导 `language` 之类的键；
- 文件大小是内容的函数，写进去不增加信息。

### 大元素的切分（确定性）

元素对象不可分块（core §3.2），因此超大的文本块按**固定预算**切成多个连续元素（类别不变，
csv 与 markdown 表格切出来的块是 `TableChunk`）：

- 预算是常量 `ELEMENT_BUDGET_BYTES`（1 MiB），按**元素对象 JCS 序列化后的 UTF-8 字节数**计；
- 切分在**行边界**进行，行尾字节留在前一块里；单个超长行独占一块（不再细分）；
- 预算**不读运行器的 `remote_limits`**：这是本连接器的策略选择（契约 §6.3 允许插件用协商
  限额做切分决策），目的是同一份源在任何远端、在任何远端调限额之后都得到相同的 hash；
- `remote_limits.max_payload_bytes` 只作**守卫**：按固定预算切分后仍超限的元素让该文档产出
  `error`（`content_invalid`）并如实上报——那是部署配置问题，不静默按远端值重新切分；
- 单条目编码后仍超过 `max_message_bytes` 时同样以条目级 `error` 上报（§6.5：MUST NOT 发送
  超限消息）。

### 确定性

同一提交上的两次全量枚举产出相同的条目序列与相同的 `doc_hash`（契约 §8.3 判据 1）。保证来自：
文件按路径字节序枚举、元素的定序即解析器给出的块序、JSON 规范化、固定切分预算、以及
**markdown 解析器版本随本包钉死**（`markdown-it-py`，锁文件固定）。解析器升级可能改变块结构
进而改变全部 markdown 文档的 hash，升级须按上面的规则声明为全量内容变化。

## 本包不做的事（后续拆分）

- 不产 `delete` / `move` / 游标（`capabilities.supports_cursor = false`，全量轮，契约 §6.8）；
- 不产 `blob`（图片等二进制不枚举）；
- 不接触 DPE remote、凭证与 hash（hash 由运行器计算，契约 §6.6）。

## 已知限制

- **请求的上界与 `cancel` 的定位**（依据 §6.1）：契约认可「读一行、处理、写一行」的循环——
  §6.7 的取消义务只约束**长请求**，没有长请求时插件无需在请求处理期间读取控制消息。本插件的
  每个请求都有**产出的上界**：`scan` 每批的条目数（`max_items`，缺省上限 1000）与编码字节数
  （单条消息 ≤ `max_message_bytes − 信封预留 − id 长度`，见 `server._next_batch`）都有上界，
  批次因此总是有界地结束。`cancel` 的实现与此一致：只记录最近一次被取消的请求 id，对重复到达
  的同 id 请求如实回 `-32001`，不改动任何在途状态。
  注意留一条前提：**输入侧没有字节上界**——单个超大文件的读取与映射会让一次 `scan` 的耗时随
  文件大小增长；将来若某个请求可能明显变长（例如 #67 的 `read_blob` 之外再引入逐文件解析的
  重工作），必须把读循环改成能并发处理控制消息的结构（select / 线程）。
- **枚举的磁盘访问是逐个 blob 一次 git 子进程**：整仓库全量枚举的成本与文件数线性，但常数不小
  （实测约每千文件数秒）；超大仓库的加速（`git cat-file --batch` 流式读取）留给后续拆分。
- 分页格式（PDF 按页、PPTX 按幻灯片）尚未支持，见「映射规则」；在此之前所有文档都是单页。
