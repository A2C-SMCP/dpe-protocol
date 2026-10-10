# DPE Connector 契约

> 状态：**定稿**——§6「运行边界」由 Issue #34 定稿（经 Issue #39 补充 §6.3 / §6.5 的 uri_prefix 规范化不动点），§1–§5、§7–§8 由 Issue #41 定稿（经 Issue #80 补充 §4.1.1 的 config_schema 正则子集，经 Issue #95 同步 §6.6 的 `file_type` 语法措辞，经 Issue #94 将 §4.1 / §4.4 的 JSON 嵌套深度统一为 core.md §2.8 的计数口径）；全文关键词 MUST / MUST NOT / SHOULD / SHOULD NOT / MAY 按 RFC 2119 理解。
> 依据：[docs/plan/v1-plan.md](../docs/plan/v1-plan.md) §11。
> 这是独立的中立规范，**不属于 Core**：核心投递协议完全不知道 connector 的存在（类比 git 与 remote-helper）。
> Issue #2 已关闭：平台不引入 connector 运行环境，connector 由用户自行开发、自行部署；托管运行若将来需要，另立独立产品。v1 官方 connector 只有 Git connector（本仓 `connectors/git/`）。

## 0. 定位

- **运行器负责推送，插件只负责产出**。connector（插件）产出变更、Document、删除意图、移动意图；协商、暂存、CAS、状态缓存与 DPE 凭证全部由运行器掌握，插件拿不到 remote 凭证。
- 同一个 connector 既可由用户用 SDK 自带的独立运行器（`dpe-run`）自行部署，也可托管在某个宿主中运行——宿主只是本契约的另一个运行器实现。
- v1 不开放租户自定义 connector 在托管环境运行，但契约设计必须保证将来开放时只需扩展、不需重构。

## 1. 术语与角色

- **插件（connector）**：产出源内容的独立进程（§6.1）。它持有数据源凭证，按 §2 产出条目，不接触 DPE remote。
- **运行器（runner）**：启动并驱动插件、校验其产出、计算 hash，并经 DPE 协议（core.md、http.md）向 remote 投递的一方。它持有 remote 凭证、实例定义与本地游标缓存。
- **宿主（host）**：以服务形态管理多个实例的运行器（如将来的托管产品）。对本契约而言宿主就是运行器；它的多租户、计费与权限模型不在本契约内（§9）。
- **独立运行器（standalone runner）**：以命令行进程形态运行、每次调用至多执行一轮的运行器，如 SDK 自带的 `dpe-run`。独立运行器除满足适用于全部运行器的条文外，还 MUST 满足标注「独立运行器规范档」的条文（§4.4、§5.2、§7.4–§7.6），使同一份实例定义能被不同语言的独立运行器执行，其输出能被同一套工具消费。
- **内容源实例（实例）**：一个插件、一份配置、一组凭证引用、一个 URI 前缀与一个 remote 的绑定（§4.4）。一个实例对应一个插件进程（§6.1），以及一份本地游标缓存（§6.8）与同步状态（§7.2）。实例的**内容身份**由 remote URL、URI 前缀、实例配置（按 RFC 8785 JCS 比较）与插件的 `name` / `version`（§4.1）组成；任何一项变化，运行器 MUST 重启插件并丢弃该实例的本地游标缓存（§6.3）。凭证（引用与取值）、启动命令、冲突策略与运行器调优参数不属于内容身份。
- **remote**：实例投递的 DPE 服务端，以基 URL 标识（http.md §1）。
- **轮（round）**：一次完整的按批枚举与投递（§6.4）。
- **清单（manifest）**：插件随分发附带的静态声明文件（§4.1）。

## 2. 插件产出模型

> 本节规定产出的语义；线格式见 §6.5–§6.7，游标见 §6.8。

- 插件产出四类条目：文档、删除意图、移动意图与错误（§6.5）。
- **文档**复用 core.md §2 的内容模型。DPE 只表达内容面：插件产出的每个字段都是源内容，都进 hash（契约 1 §2）；治理属性（如访问控制）与服务端衍生物（如抽取产物、关键词）MUST NOT 出现在任何字段中。
- **易变字段**：插件 SHOULD NOT 把不随内容变化的易变源字段（浏览计数、最近访问时间、在线状态、同步时间戳等）放进 metadata，否则每轮同步都会制造内容变化（契约 1 §2）；同理 SHOULD NOT 在元素 metadata 中重复骨架已表达的位置（页码、页内序号），否则插入一页会让后续元素对象全部重传（core.md §2.4）。
- **blob 字节**：可携带 blob 的元素（契约 1 §4.1）的字节由插件用自己持有的数据源凭证取得，以内联字节或句柄产出（§6.6、§6.7），运行器分块读取后自行计算 `sha256:` 引用并上传。运行器 MUST NOT 自行解引用数据源 url（契约 1 §4.2）。字节确实无法取得时 `blob` 留空，url 等源信息照常放在元素 metadata 中。
- **删除与移动** MUST 以显式的 `delete` / `move` 条目产出；运行器 MUST NOT 以「未出现即删除」推断（§6.5）。
- **枚举方式**：全量枚举，或基于源端增量游标的增量枚举（§6.8）。跨轮的差只经游标表达，游标只存于运行器本地缓存；插件 MAY 持有加速用的本地缓存（如源数据的本地副本），但该缓存丢失时 MUST 仍产出正确结果，MUST NOT 依赖进程外的自有持久状态来表达删除与移动。
- **确定性**：插件 SHOULD 保证同一源状态的两次枚举产出相同的内容与顺序（§6.5）；这是收敛判据（§8.3）的前提。

## 3. URI 前缀绑定

- 每个实例 MUST 绑定一个 URI 前缀，在创建实例时由宿主分配或由实例定义（§4.4）给出。前缀 MUST 是符合 core.md §1.1 文法的 URI，且是规范化不动点（§6.3）。
- 运行器 MUST 拒绝越界的文档、删除与移动条目并如实上报（判定规则见 §6.5）。服务端的前缀授权（core.md §5.1）是独立的第二道防线，运行器 MUST NOT 以它代替本地判定。
- **前缀不重叠**：同一运行器管理的多个实例（独立运行器：同一状态目录中登记的实例，§4.4）投递到同一 remote（实例定义中 `remote.url` 相同）时，任意两个实例的前缀 MUST NOT 互为前缀（含相等）；运行器 MUST 在创建或启动实例时检查并拒绝（§7.1 实例级）。否则一个实例的删除或移动会落入另一个实例的范围。跨运行器的重叠无法由运行器察觉，由服务端前缀授权兜底。
- **前缀变更**：前缀属于实例的内容身份（§1）。前缀变化后，原前缀下已投递的文档 MUST NOT 被隐式删除或迁移；需要清理或迁移时，由使用者显式执行（core.md §4 的 delete / move）。

## 4. 配置与凭证

### 4.1 清单

插件 MUST 随分发附带清单：名为 `dpe-connector.json` 的 UTF-8 JSON 文件（I-JSON，同 core.md §2.8 第 0 步）。清单是静态声明，宿主与运行器 MUST 能在不启动插件的前提下读取它（渲染配置表单、校验配置、确定所需凭证）；取得清单 MUST NOT 依赖执行插件。

```json
{ "manifest_version": 1,
  "name": "git-connector",
  "version": "0.1.0",
  "description": "以 Git 仓库为数据源",
  "protocol_versions": ["dpe-connector/1"],
  "config_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "repo": { "type": "string" },
      "ref": { "type": "string", "default": "main" } },
    "required": ["repo"],
    "additionalProperties": false },
  "secrets": [
    { "name": "GIT_TOKEN", "description": "读取私有仓库的访问令牌", "required": false } ] }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `manifest_version` | 是 | 整数；本规范定义 `1`。运行器遇到不认识的版本 MUST 拒绝 |
| `name` | 是 | 非空字符串；MUST 与 initialize 响应的 `plugin.name` 相同 |
| `version` | 是 | 非空字符串；MUST 与 initialize 响应的 `plugin.version` 相同 |
| `description` | 否 | 字符串，供展示 |
| `protocol_versions` | 是 | 非空字符串数组：插件支持的线协议版本（§6.2） |
| `config_schema` | 是 | JSON Schema（draft 2020-12）对象，根 MUST 为 `"type": "object"`；插件无配置时为 `{"type": "object", "additionalProperties": false}` |
| `secrets` | 否 | 数据源凭证声明数组，缺省为 `[]`；每项为 `{name, description?, required?}`，见下 |

- 清单文件的 JSON 嵌套深度（计数口径见 core.md §2.8 第 0 步，全协议只有这一处定义）MUST ≤ 64；超出即拒绝（`manifest_invalid`，§7.4），在读取阶段判定、不进入任何语义校验。上界使合法文件在各实现的默认 JSON 解析限额（如 serde_json 的递归限额 128）内也能被读取。
- 清单是封闭 schema：出现未定义的成员（含 `secrets` 项内）或成员类型不符时，运行器 MUST 拒绝该清单（`manifest_invalid`，§7.4）。
- `config_schema` MUST 自包含：`$ref` 只能引用本文档内的位置，运行器 MUST NOT 解析外部引用（不得因校验配置而访问网络或文件）；`format` 只作注解，运行器 MUST NOT 据此判定校验失败——使不同实现对同一配置得出相同的校验结论。插件 SHOULD 在根上设 `"additionalProperties": false`。它 MUST NOT 声明凭证字段：实例配置经线协议原样交给插件（§6.3），不是凭证通道；凭证一律经 `secrets` 声明、按 §4.3 注入。**`config_schema` 是封闭 schema**（与 DPE 的三层对象同理）：只允许 §4.1.1 列出的关键字，`$ref` 只允许 §4.1.1 规定的唯一形式；`pattern` 的值与 `patternProperties` 的每个键 MUST 属于 §4.1.1 的正则子集。content 词（`contentEncoding`、`contentMediaType`）与 `format` 一样只作注解：运行器 MUST NOT 据此判定校验失败；`contentSchema` 不允许出现（见 §4.1.1）。
- `secrets` 每项：
  - `name`（必填）：注入插件进程的环境变量名，MUST 匹配 `^[A-Z][A-Z0-9_]*$`，在清单内唯一；MUST NOT 以 `DPE_` 开头（该前缀保留给运行器）；MUST NOT 与 §4.3 基础环境中的变量同名。
  - `description`（可选）：字符串，供宿主渲染表单。
  - `required`（可选）：布尔，缺省 true。
- **身份核对**：initialize 成功后，运行器 MUST 核对 `plugin.name` / `plugin.version` 与清单相同、选定的 `protocol_version` 在清单的 `protocol_versions` 中；不符即终止实例（`plugin_mismatch`）。清单与实际可执行文件漂移时，依据清单完成的配置校验与凭证注入都不再可信。

#### 4.1.1 `config_schema` 的正则子集

`pattern` 的值与 `patternProperties` 的每个键 MUST 属于下述**可移植子集**——判定只依据本节的文法与结构上界，与本地正则引擎能否编译无关；不属于（含语法错误、超出上界）时运行器 MUST 拒绝该清单（`manifest_invalid`，§7.4）。子集以 [RFC 9485](https://www.rfc-editor.org/rfc/rfc9485)（I-Regexp，为跨引擎互操作定义的正则子集，JSONPath（RFC 9535）即采用它）为底稿，与 §4.1 其余部分同理，目的是使不同实现对同一配置得出相同的校验结论。

与 RFC 9485 的差异：

| 项 | RFC 9485 | 本子集 |
| --- | --- | --- |
| 锚点 | 无（`^`、`$` 是普通字符） | `^` 只允许在顶层每个分支的开头、`$` 只允许在顶层每个分支的结尾；其他位置 MUST 用 `\^`、`\$` 转义 |
| 多字符转义 | 无 | 支持 `\d \D \w \W \s \S`（ASCII 语义，见语义表） |
| `\xHH` | 无 | 支持；H 为十六进制（大小写均可），值为 U+0000–U+00FF |
| `(?:…)` | 不属于文法 | 允许，与 `(…)` 同义 |
| `\p{…}`、`\P{…}` | 支持 | MUST NOT 使用：各实现内嵌的 Unicode 版本不同步，展开为码点集合也会随版本漂移 |
| 类内集合序列 | `[&&]`、`[--]`、`[~~]` 等均为合法类 | MUST NOT 出现未被转义的 `&&`、`--`、`~~`：部分引擎把它们当类集合运算，需保证同一文本在任何实现下同义 |
| 匹配语义 | 整串匹配（XSD 语义） | 未锚定（search）：串中存在一处匹配即通过；`^`、`$` 是作者获得整串约束的手段 |
| `.` 的语义 | 由 §5.3 映射为 `[^\n\r]` | `[^\n\r]`（与该映射一致） |

文法（ABNF；其中的「字符」是 Unicode 标量值）：

```abnf
pattern    = top-branch *("|" top-branch)
top-branch = ["^"] *piece ["$"]        ; 锚点只允许出现在顶层分支的首/尾
branch     = *piece                    ; 分组内
piece      = atom [quantifier]
atom       = "." / literal / escape / shorthand / class / "(" branch *("|" branch) ")"
           / "(?:" branch *("|" branch) ")"
quantifier = "*" / "+" / "?" / "{" 1*DIGIT ["," [1*DIGIT]] "}"
shorthand  = "\" ( "d" / "D" / "w" / "W" / "s" / "S" )
escape     = "\" ( "(" / ")" / "*" / "+" / "-" / "." / "?" / "[" / "\" / "]" / "^"
                  / "{" / "|" / "}" / "$" / "n" / "r" / "t" ) / "\x" HEXDIG HEXDIG
class      = "[" ["^"] ("-" / class-item) *class-item ["-"] "]"
class-item = class-char ["-" class-char] / shorthand
class-char = ( %x00-2C / %x2E-5A / %x5E-D7FF / %xE000-10FFFF ) / escape   ; 除 -、[、\、] 外的标量值，或转义
```

其中 `literal`（用于原子位置）是除元字符（`\`、`.`、`^`、`$`、`|`、`?`、`*`、`+`、`(`、`)`、`[`、`]`、`{`、`}`）外的任意 Unicode 标量值；`class-char`（用于类内）是除 `[`、`]`、`-`、`\` 外的任意 Unicode 标量值——类内元字符（`.`、`^`、`$`、`|`、`?`、`*`、`+`、`(`、`)`、`{`、`}`）没有特殊含义，照字面书写即可（`[.]`、`[a*b]`、`[^a$]` 都合法）。两者都不可以是孤立代理项 U+D800–U+DFFF（不是标量值，出现即越界——清单文件经 I-JSON 本已不允许，此处对在内存中构造的清单同样成立）；类内另加：

- 字面 `[`、`]` MUST 转义（`\[`、`\]`），`\` MUST 转义（`\\`）；
- MUST NOT 出现**相邻**的 `&&`、`--`、`~~` 三组字符（按字面文本判定：紧邻的两个同字符一律违例，无论前一个是否被转义——`[a\--b]` 也违例）；写出相邻字符时用 `\xHH`（如 `[a\x26\x26b]`、`[\x2D\x2D]` 合法；`&`、`~` 不在转义白名单里，不能写 `\&`）；`-` 只能是类首/类尾的字面字符（或 `\-`、`\x2D`）或区间分隔符；
- 区间的两个端点 MUST 是单字符（字面或转义，含 `\xHH`），MUST NOT 是简写类；端点 MUST 满足 c1 ≤ c2（按码点序）；
- `^` 仅在类内首位表示否定，其他位置是字面字符。

**封闭关键字子集**：`config_schema`（含 `$defs` 中的子 schema 与所有嵌套位置）MUST 只使用下表列出的关键字；出现未列出的关键字（`$id`、`$anchor`、`$dynamicAnchor`、`$dynamicRef`、`definitions`、`dependencies`、`additionalItems`、`prefixItems`、`contains`、`minContains`、`maxContains`、`dependentSchemas`、`unevaluatedItems`、`unevaluatedProperties`、`contentSchema`、`multipleOf` 等，无论出现在哪个位置）一律拒绝该清单（`manifest_invalid`，§7.4）。与 DPE 的三层对象一样，`config_schema` 是封闭 schema：只用表达力够用的一小组关键字，未定义的关键字不猜语义——这正是「不同实现对同一配置得出相同结论」的前提。允许 schema 的位置一律允许布尔 schema（`true` / `false`，如 `"additionalProperties": false`）。

| 组 | 允许的关键字 |
| --- | --- |
| 结构与组合 | `type`、`properties`、`patternProperties`、`additionalProperties`、`required`、`propertyNames`、`items`（仅单个 schema，数组形式拒绝）、`allOf`、`anyOf`、`oneOf`、`not`、`if`、`then`、`else`、`$defs`（仅根，见下） |
| 引用 | `$ref`（唯一形式见下） |
| 方言 | `$schema`（仅根；MUST 缺省或为 draft 2020-12，`…/2020-12/schema` 与带空片段 `…/2020-12/schema#` 等价） |
| 通用 | `enum`、`const` |
| 字符串 | `pattern`（属于本节的正则子集）、`minLength`、`maxLength` |
| 数值 | `minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum` |
| 数组 | `minItems`、`maxItems`、`uniqueItems` |
| 对象 | `minProperties`、`maxProperties`、`dependentRequired` |
| 注解 | `title`、`description`、`default`、`examples`、`deprecated`、`readOnly`、`writeOnly`、`format`、`contentEncoding`、`contentMediaType`、`$comment` |

- `$defs` MUST 只出现在根上；其值 MUST 是 schema，其中的关键字同样只允许本表。
- `$ref` MUST 恰为 `"#/$defs/<名字>"` 一层：前缀 MUST 是字面的 `#/`（不得写成 `%2F` 等编码形式），其后按 RFC 6901 §6 处理（先百分号解码、再按 `/` 拆分、最后处理 `~1`、`~0`，顺序不可换）；含非法百分号序列（`%` 后不是两位十六进制）或 `~` 后不是 `0`/`1` 的片段 MUST 拒绝；解码后的指针 MUST 恰为 `$defs` 与 `<名字>` 两段（名字非空）；`#`、锚点（`#名字`）、深于一层（`#/$defs/a/b`）、其他位置（`#/properties/x`）与外部 URI 一律拒绝；名字 MUST 在根的 `$defs` 中存在。`$defs` 各条之间的引用关系 MUST 构成有向无环图（对 `$defs` 做一次拓扑检查即可）——环会在求值时无限递归，行为随实现而异；这同时排除了递归 schema（含经 `properties` 等位置回到自身的写法），这是有意的取舍：配置 schema 不需要递归。**展开深度** MUST ≤ 64：根计 1，每进入一个 schema 位置再 +1，每经过一次 `$ref` 跳转再 +1 并沿引用进入目标继续计，取所有路径的最大值（`$defs` 已是 DAG，记忆化后线性可算）。未被引用的 `$defs` 条目不参与展开深度（它们从不被求值），深度一律从根计起。上界保证合法清单在任何实现的默认递归限额下都能完成求值：同时守住 JSON 深度与展开深度的最紧形状（63 层 `additionalProperties` 内联链、布尔叶子——两个上界恰好同时顶格）在解析期的元模式校验约需 500–525 帧（Python 默认限额 1000，留约两倍余量；求值阶段本身约 250–300 帧）。
- `multipleOf` 与 `contentSchema` MUST NOT 使用：前者的整除判定在浮点与大整数上跨实现不一致（实测 `0.3` 对 `multipleOf: 0.1` 被判非整数倍、大整数受浮点舍入误判），后者在 JSON Schema 里是 schema 位置、各库会按 schema crawl，留作字段即是分歧面；需要倍率表达时用 `enum` / `const` 或上下界。
- 注解词只作注解：运行器 MUST NOT 据此判定校验失败；`default`、`examples`、`enum`、`const` 等处的同名键是数据，MUST NOT 按关键字解析。

文法之外另有约束：

- 量词 MUST 紧跟一个原子，且每个原子至多一个量词；`*a`、`a**`、`a{2}{3}`、`{,3}` 都不合法（`{`、`}` 作字面 MUST 转义）；`{m,n}` 形式 MUST 满足 m ≤ n。
- `^`、`$` 只允许出现在**顶层**分支的首/尾；`a^b`、`(^a)`、`(a$)`、`(?:^a)` 都不合法，其他位置作字面 MUST 转义。
- schema 自身 MUST 通过 draft 2020-12 元模式校验（本表之外的元层违例——如 `type` 取值不认识、`properties` 不是对象——同样拒绝该清单）；
- 结构上界：pattern 的长度 MUST ≤ 1024 个 Unicode 标量值；**分组嵌套深度** MUST ≤ 64；**展开规模** MUST ≤ 4096。分组嵌套深度按括号层数计：最外层分组计 1，不含分组的 pattern 计 0（量词与字符类不计入；但下述上界对它们另有约束）。展开规模按书写形式逐项、递归地计数（不去重、不做集合归并——归并结果取决于各实现的集合算法，会在边界上分歧）：字面按码点计 1（`é` 计 1，不论其 UTF-8 字节数）；转义、`.`、简写各计 1；字符类的取反 `^` 不计，其余按写出的每一项计 1（`[aaaa]` 计 4、`[a-cb]` 计 2，类内简写各计 1）；锚点计 0、不参与量词；**分组至少计 1**（空组计 1——否则「零尺寸原子 × 大计数」会绕过本上界）；分组为组内之和与 1 的较大者；连接、选择为各子项之和；量词为「因子 × 被量化原子的展开规模」，因子为：`{m}` 记 m、`{m,n}` 记 n、`{m,}` 记 m、`*`、`+`、`?` 记 1；嵌套时外层因子乘内层规模，如 `(a{2}){3}` 为 6。深度取 64 是因为各引擎的解析器嵌套限额（如 RE2 系的 250）除分组外还计入量词与字符类：最坏形状（每层分组带量词）的解析嵌套约为分组层数的两倍、实测在 125 层即触及默认限额，64 层取其约一半。

语义：

| 构造 | 语义 |
| --- | --- |
| `.` | 除 U+000A、U+000D 外的任意单个 Unicode 标量值 |
| `\d` / `\D` | `[0-9]` / 其补（在全体 Unicode 标量值上取补，下同） |
| `\w` / `\W` | `[0-9A-Za-z_]` / 其补 |
| `\s` / `\S` | `[\t\n\v\f\r ]`（U+0009–U+000D 与 U+0020）/ 其补 |
| `^` / `$` | 文本的开头 / 结尾；无多行语义，`$` MUST NOT 匹配末尾换行之前 |
| `\xHH` | 值为 U+00HH 的单字符 |

匹配方式：未锚定（search）——在串的任意位置找到一处匹配即通过；`patternProperties` 的键以同一方式匹配属性名。区分大小写、按码点（不是字节或 UTF-16 码元）、不使用任何标志；匹配的字符宇宙是 Unicode 标量值（类区间跨越代理区时不含代理码位）。

结构上界使编译规模有确定上界：**子集内的任何 pattern MUST 被接受并正确匹配**，实现 MUST NOT 以本地引擎的资源上限为由拒绝（实现方式不限，例如先转译为显式字符类/码点区间再交给本地引擎）。这是可用性约束，不是安全边界——清单来自插件，而插件本来就能执行任意代码；配置来自使用者。

### 4.2 实例配置

- 实例配置是一个 JSON 对象，经 initialize 的 `instance.config` 原样交给插件（§6.3）。
- 启动插件前，运行器 MUST 按清单的 `config_schema`（draft 2020-12 语义）校验实例配置；不通过时 MUST NOT 启动插件，按实例级失败上报（`config_schema_violation`，§7.1）。
- 运行器 MUST 原样传递配置，MUST NOT 填充 `default` 或做任何改写——`default` 等注解只供宿主渲染表单，缺省值由插件自行解释。这样配置的比较（§1 内容身份）与插件看到的值始终一致。
- schema 无法表达的语义约束（如仓库不存在、ref 无效）由插件在 initialize 时以 `-32005`（invalid_config）拒绝（§6.3）。

### 4.3 凭证

两类凭证严格分属：**remote 凭证**只属于运行器，用于向 remote 鉴权（http.md §6）；**数据源凭证**只属于插件，用于读取内容源与 blob 字节。

**来源引用**：实例定义、宿主存储等配置载体中，凭证 MUST 以来源引用表示，MUST NOT 含凭证值本身——声明可以提交进版本库，凭证值不可以。独立运行器 MUST 支持以下两种引用（宿主 MAY 以自己的密钥存储替代）：

- `{"env": "NAME"}`：取运行器进程环境变量 `NAME` 的值；
- `{"file": "path"}`：取文件的全部内容（UTF-8），去掉末尾的一个 LF（或 CRLF）；相对路径的基准见 §4.4。

引用对象是封闭 schema，恰含上述成员之一。解析结果为空字符串视同无法解析。

**解析时机**：运行器 MUST 在每次启动插件进程时重新解析数据源凭证（remote 凭证同理，在每次需要时从引用解析），MUST NOT 持久化解析值，MUST NOT 跨进程启动缓存。凭证轮换因此只需在轮边界重启插件（§6.3）即生效；凭证不属于实例的内容身份（§1），轮换 MUST NOT 导致丢弃游标。

**缺失**：`required` 为 true 的凭证无法解析（引用缺失、变量未设、文件不可读、值为空）时，运行器 MUST NOT 启动插件，按实例级失败上报（`secret_missing`），报告只含凭证名。`required` 为 false 的凭证无法解析时，运行器 MUST NOT 注入该变量（MUST NOT 以空字符串顶替），由插件按缺失处理。

**注入**：数据源凭证 MUST 且只能以环境变量注入插件进程，变量名为清单声明的 `name`；运行器 MUST NOT 经命令行参数、实例配置、协议消息或工作目录文件传递数据源凭证。插件进程的环境 MUST 恰由以下三部分组成，运行器 MUST NOT 让插件继承自身的其余环境变量（运行器环境中可能有 remote 凭证，§6.1）：

1. **基础环境**：运行器自身环境中存在的 `PATH`、`HOME`、`LANG`、`LC_*`、`TZ`、`TMPDIR`；Windows 上另加 `SYSTEMROOT`、`TEMP`、`TMP`、`USERPROFILE`、`PATHEXT`、`COMSPEC`；
2. 已解析的数据源凭证；
3. 实例定义的 `plugin.env`（非秘密的字面量，§4.4）。

**remote 凭证不可达**：数据源凭证的引用 MUST NOT 与 remote 凭证的引用指向同一来源（同名 `env`，或解析为同一路径的 `file`）；运行器 MUST 检测并拒绝（`definition_invalid`）。连同上面的环境构造，运行器不经任何通道把 remote 凭证交给插件。v1 不要求进程隔离（§6.1）：插件与运行器以同一用户运行时，`file` 型 remote 凭证能否被插件读取取决于文件权限；需要更强的隔离时，由部署方或宿主在进程之外提供（不同用户、容器等）。

**不外泄**：凭证值 MUST NOT 出现在运行器的日志、轮报告、错误消息与诊断中（§7）。运行器无法识别插件自行写出的凭证，因此插件 MUST NOT 把凭证值写入 stdout、stderr 或任何产出；运行器转发插件 stderr 前 SHOULD 把已知的凭证值替换为固定标记。

### 4.4 实例定义（独立运行器规范档）

独立运行器 MUST 接受以下格式的实例定义：一个 UTF-8 JSON 文件（I-JSON），描述一个实例。定义文件（含 `config`）的 JSON 嵌套深度（计数口径见 core.md §2.8 第 0 步，全协议只有这一处定义）MUST ≤ 64；超出时按 `definition_invalid` 拒绝（§7.4），在读取阶段判定。宿主 MAY 以自己的存储表达同样的信息，但 MUST 满足 §3–§4.3 的要求。

```json
{ "definition_version": 1,
  "id": "acme-repo",
  "remote": { "url": "https://dpe.example.com/remotes/acme",
              "credential": { "env": "ACME_DPE_AUTHORIZATION" } },
  "uri_prefix": "git://acme/repo/",
  "plugin": { "manifest": "plugins/git/dpe-connector.json",
              "command": ["dpe-git-connector"],
              "env": { "GIT_SSL_CAINFO": "/etc/ssl/certs/ca.pem" } },
  "config": { "repo": "https://example.com/acme/repo.git" },
  "secrets": { "GIT_TOKEN": { "file": "/run/secrets/git_token" } } }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `definition_version` | 是 | 整数；本规范定义 `1`。不认识的版本 MUST 拒绝 |
| `id` | 是 | 实例的本地标识，匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，在同一状态目录内唯一；是本地状态（游标缓存、同步状态、内容身份指纹、排他锁）与日志（§7.5）的隔离键 |
| `remote.url` | 是 | remote 的基 URL（http.md §1）；MUST NOT 含 userinfo |
| `remote.credential` | 否 | 来源引用（§4.3）。解析值是完整的 `Authorization` 请求头值（如 `Bearer …`），运行器原样发送、不解释方案（http.md §6）；缺省表示不发送 `Authorization`。`Authorization` 头无法承载的方案（如 mTLS）由运行器实现经 `runner` 扩展 |
| `uri_prefix` | 是 | 实例 URI 前缀（§3） |
| `plugin.manifest` | 是 | 清单路径（§4.1） |
| `plugin.command` | 是 | 非空字符串数组，启动插件的 argv。`argv[0]` 含路径分隔符时按路径解析，否则在 `PATH` 中查找；运行器 MUST 直接执行，MUST NOT 经 shell 解释 |
| `plugin.env` | 否 | 对象，字符串到字符串：注入插件的非秘密环境变量（§4.3）。变量名 MUST NOT 以 `DPE_` 开头，MUST NOT 与基础环境或已声明的凭证同名 |
| `config` | 是 | 实例配置（§4.2），可为 `{}` |
| `secrets` | 否 | 对象：键为清单声明的凭证名，值为来源引用（§4.3）。出现清单未声明的名字 MUST 拒绝 |
| `conflict_policy` | 否 | 对象 `{commit?, delete?}`：写入冲突的策略（§7.2）。`commit` 取 `"source_wins"`（缺省）或 `"report"`；`delete` 取 `"report"`（缺省）或 `"source_wins"` |
| `runner` | 否 | 对象，留给运行器实现的调优参数（超时、批大小、重试上限等）；运行器 MUST 忽略不认识的键，其内容 MUST NOT 改变本契约规定的行为 |

- 实例定义是封闭 schema（`runner` 内部除外）：出现未定义的成员或成员类型不符时，运行器 MUST NOT 启动插件，按实例级失败上报（`definition_invalid`）。
- 相对路径（`plugin.manifest`、`file` 引用，以及含路径分隔符的 `argv[0]`）以实例定义文件所在目录为基准。
- 实例定义 MUST NOT 含凭证值（§4.3）；`remote.url` 中的 userinfo、`plugin.env` 与 `config` 都不是凭证通道。
- **状态目录**：独立运行器 MUST 把每个实例的本地状态（游标缓存、同步状态与意图日志（§7.2）、内容身份指纹与排他锁）按 `id` 存放在一个状态目录中；状态目录的位置由运行器实现决定，SHOULD 可由调用方指定。同一状态目录中登记过的实例就是该独立运行器「管理的实例」：`id` 在其中唯一，§3 的前缀不重叠也在其中检查——启动实例时，运行器 MUST 把它的 `remote.url` 与 `uri_prefix` 同状态目录中其余实例的登记比较，重叠即拒绝（`definition_invalid`）。登记与重叠检查 MUST 原子完成（如在状态目录级的锁内进行），以免两个前缀重叠的新实例同时首次启动时都通过检查。
- **内容身份指纹**：运行器 MUST 把游标与产生它的实例内容身份（§1）一并持久化。启动时内容身份与已存的不同（如沿用同一 `id` 改了 `config` 或 `uri_prefix`），MUST 视为缓存丢失：丢弃游标与同步状态，以全量轮开始（§6.8），并更新登记。

## 5. 生命周期与调度

### 5.1 实例操作

| 操作 | 语义 |
| --- | --- |
| 创建 | 绑定前缀（§3），校验清单（§4.1）、实例配置（§4.2）与凭证引用（§4.3）。不开始轮 |
| 校验 | 启动插件、完成 initialize 与身份核对（§4.1），获取 remote 的 capabilities（http.md §4.1），随后 `shutdown`（§6.3）。MUST NOT 发出 `scan`，不产生任何写入 |
| 同步一轮 | §6.4–§6.8 |
| 暂停 | 不再开始新的一轮；进行中的轮 MAY 完成或中止（中止在提交前无副作用，§6.4） |
| 删除 | 丢弃本地游标缓存（独立运行器：从状态目录中移除该实例的状态与登记，§4.4）。MUST NOT 隐式删除或迁移 remote 上已投递的文档——清理是使用者的显式操作（core.md §4） |

### 5.2 调度、并发与重试

- **调度不属于本契约**：何时开始一轮由运行器（宿主）决定。独立运行器每次调用至多执行一轮后退出；周期性或事件驱动的触发（CI、版本库钩子、webhook 处理器、外部定时器）由调用方承担。
- **单轮约束跨进程成立**（§6.4）：独立运行器（独立运行器规范档）解析实例定义得到 `id` 后，MUST 先取得该实例的排他锁，再做其余实例级校验（清单、配置、凭证、前缀登记）与启动插件，并持有至本轮结束（含游标持久化）；取不到锁时 MUST NOT 启动插件，以 `instance_busy` 结束（§7.6）。这是暂时状态而非实例级失败，调用方稍后重试即可。锁 MUST 在持有进程异常退出时自动释放（如操作系统提供的文件锁），以免残留锁永久阻塞实例。
- **重试**：轮失败后的重试 SHOULD 采用有上限、带抖动的指数退避；remote 返回 `Retry-After` 时 MUST 遵守（core.md §6）。实例级失败（§7.1）不改动实例定义、清单或凭证就无法恢复，MUST NOT 自动重试。独立运行器 MAY 在单次调用内有限次重试，也 MAY 直接以非零退出码返回、交给调用方重试。永久失败不得空转（§6.8）。

## 6. 运行边界

> 本节由 Issue #34 定稿。它定义运行器与插件之间的进程边界与语言无关线协议：任何语言实现的插件，只要遵循本节，就能被任何遵循本契约的运行器驱动。插件只产出源内容（§2），线协议上不出现 DPE 的 hash、契约版本与 remote 细节；校验、hash、协商、暂存、CAS、重试与凭证全部由运行器负责。

### 6.1 进程边界与信任模型

- **插件 MUST 是独立进程**：由运行器启动，一个内容源实例对应一个插件进程；同一进程 MUST NOT 服务多个实例。语言无关性由此保证——插件不链接 SDK，也不要求与运行器同语言。
- **标准流分工**：插件从 stdin 读协议消息、向 stdout 写协议消息；stdout MUST 只包含协议消息（不得混入日志、调试输出或 BOM），诊断日志写 stderr。运行器 MUST 持续排空 stderr（或把它导向自身 stderr），否则插件可能因管道写满而阻塞。
- **请求方向**：v1 中所有请求都由运行器发起，插件只应答；插件 MUST NOT 主动发起请求或通知。单管道因此不会死锁；无长请求时，插件实现就是「读一行、处理、写一行」的循环，处理长请求期间仍 MUST 继续读取并处理控制消息（§6.7 的取消要求）。
- **信任模型**：插件产出是不可信输入。运行器 MUST 全量校验（§6.6）并自行计算一切 hash，MUST NOT 把产出字段直接当作已校验的 DPE 对象；MUST NOT 因任意插件产出而崩溃（协议级无法继续的情形除外，§6.2）。
- **凭证边界**：运行器 MUST NOT 经任何通道（命令行参数、环境变量、工作目录文件、协议消息）把 DPE remote 凭证交给插件；运行器 MUST NOT 自行解引用数据源 url（契约 1 §4.2）。数据源凭证由插件持有、只以环境变量注入插件（§4.3）；凭证 MUST NOT 经线协议传递，运行器除按 §4.3 解析与注入外 MUST NOT 使用或记录数据源凭证。
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
| `instance.config` | 是 | 实例配置（§4.2：按清单的 `config_schema` 校验、原样传递；MUST NOT 含凭证），可为 `{}` |
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

- **启动**：运行器负责启动插件进程；插件进程的环境 MUST 按 §4.3 构造，独立运行器的启动命令见 §4.4；宿主取得可执行文件的方式属于宿主实现。插件 MUST NOT 依赖交互式终端（TTY）。
- **重启边界**：运行器 MAY 在轮边界重启插件（含空闲期）；MUST NOT 在一轮内重启，故障恢复除外（此时该轮作废，§6.4）。实例的内容身份（§1：remote、uri_prefix、实例配置、插件 name / version）变化时，运行器 MUST 重启插件并丢弃本地游标缓存（§6.8）。
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
  - 运行器 MAY 对进行中的 `scan` 设置超时并在超时后 `cancel`（策略属运行器实现，§5.2）；插件对 `cancel` 的义务见 §6.7。
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
- 插件 SHOULD 保证同一源两次枚举的页序与元素序稳定（同一内容产出相同顺序）——这是收敛判据（§8.3）的前提。
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
- **协议级失败**（分帧 / JSON-RPC 违例、消息超限、未知 method）：按 §6.2、§6.3 处理，运行器终止实例。initialize 的失败（无共同版本、插件拒绝配置、选定版本不在运行器列表中）同样终止实例，按 §7.1 归为实例级——不改动实例定义或插件就无法恢复。

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
4. 分段合并为完整文档后，整体再按 core.md §2 的文档层规则校验（含 `file_type` 语法），并由运行器重算三层 hash。

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
- 性质：MUST 不透明（运行器不得解析）；MUST 自包含——插件进程重启后仍可解释，解释不了时插件 MUST 回退为全量枚举（结束批标记 `incremental: false`，§6.4），MUST NOT 因此报错；运行器 MUST 只在本地缓存、按实例隔离（§2「游标只存于运行器本地缓存」），MUST NOT 把它写进任何 DPE 字段或发往服务端；缓存丢失或首轮没有游标 = 全量枚举。
- **覆盖范围（增量轮）**：`incremental: true` 的轮 MUST 产出自所给游标以来的**一切**变化，包括删除与移动（显式条目）与内容变更；插件 MUST NOT 让新游标越过本次未能成功产出的变更。
- **全量轮**：`incremental: false` 的轮只保证**现存内容**的完整枚举——此前的删除与移动无法由协议表达：它们是相对上次枚举的差，而跨轮状态只有游标本身（§2「游标只存于运行器本地缓存」；游标不可得时这个差不存在）。插件 MAY 产出它自己能确定的删除与移动（如源端提供删除事件）。运行器 MUST 按 §6.4 如实上报这类轮，MUST NOT 视同删除与移动已同步；全量轮的新游标同样受 fail-closed 约束——整轮成功落地后持久化，此后恢复为增量轮。
- **不支持游标**：`capabilities.supports_cursor` 为 false 时插件 MUST NOT 给出游标（结束批的 `cursor` 为 `null`、`incremental` 恒为 false），运行器 MUST NOT 缓存游标——此后每轮都是全量轮（推送侧的增量由 doc_hash 筛选承担，core.md §3.2），删除与移动只能靠插件自己确定并显式产出。
- **持久化（fail-closed）**：运行器 MUST 仅在整轮**全部条目成功落地**之后才持久化新游标——即无条目错误、无校验与越界失败、全部 blob 读取成功、全部文档以 `created` / `updated` / `unchanged` 提交成功、全部删除与移动成功或已达成目标状态（core.md §5.2）。任何未解决失败 → MUST 保留旧游标、MUST 如实上报、退避后重试。
- 重扫是安全的：commit 由内容幂等（core.md §5.2），delete 重试得到 `DPE_NOT_FOUND` 视为成功（core.md §5.2）。恢复路径就是稳定重跑整轮，MUST NOT 尝试只重放失败项（此禁令针对跨轮恢复；§7.2 的轮内冲突重读不在此列）。
- **永久失败不得空转**：越界产出、schema 违例、超出远端限额等插件缺陷会让游标被持续扣住。运行器 MUST 如实上报（含 file_uri 与条目定位）并可观测，MUST NOT 静默跳过，也 MUST NOT 无退避地空转；`retryable: false` 的条目同样不得被当作「跳过」。重试节奏与上限属运行器策略（§5.2），上报形式见 §7。

## 7. 错误与可观测

> 本节把 §6 分散规定的失败处置归为统一分层，并为独立运行器规定机器可读的输出。线协议上的错误承载见 §6.5，游标推进规则见 §6.8；本节细化失败的归属与上报，不改变它们。

### 7.1 失败分层

| 层级 | 情形 | 运行器处置 | 游标 |
| --- | --- | --- | --- |
| 协议级 | 分帧或 JSON-RPC 违例、消息超限、未知 method、initialize 错误响应（实例级，见下行）之外的握手违例（如 initialize 结果缺少必填字段、握手完成前发来响应以外的消息）（§6.2、§6.3、§6.5） | 终止实例，本轮作废 | 不推进 |
| 实例级 | 清单不合法、实例定义不合法（含前缀非法或重叠）、配置未通过 schema、必填凭证缺失、无共同协议版本（`-32004`）、插件拒绝配置（`-32005`）、选定版本不在运行器列表中、插件身份与清单不符（§3、§4、§6.3）；remote 拒绝凭证（HTTP 401，http.md §6）、remote 不支持运行器可用的任何 hash 契约（capabilities，http.md §4.1） | 不启动插件或终止插件，不开始轮；MUST NOT 自动重试（§5.2） | 不推进 |
| 轮级 | 实例已有轮在进行（`instance_busy`，未启动插件，§5.2）；`scan` 的 `error` 响应、未知或过期的 `next`（§6.4）、插件异常退出（§6.3）；`scan` 超时，或任何请求超时后插件不响应 `cancel`（§6.7）；remote 不可用：网络失败，或可重试的 DPE 错误（`DPE_UNAVAILABLE` / `DPE_RATE_LIMITED`）按 `Retry-After` 重试耗尽 | 本轮作废并上报，退避后重试（§5.2） | 不推进 |
| 条目级 | 见下表 | 该条目失败，本轮继续处理其余条目（§6.5） | 不推进（§6.8） |

条目级失败按来源区分（§6.5 要求报告区分来源）：

| source | 情形 | code |
| --- | --- | --- |
| `plugin` | 插件产出的 `error` 条目（§6.5） | 插件给出的 `source_unavailable` / `content_invalid` / `internal` |
| `validation` | 运行器对插件产出的本地校验失败（§6.6），含分段在轮结束时仍未闭合（§6.5） | `DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` / `segment_unclosed` |
| `prefix` | 条目越出实例前缀（§6.5） | `out_of_prefix` |
| `blob` | blob 读取失败（含取消、超时、句柄失效）、`size` 不符、超过远端限额（§6.7） | `blob_read_failed` / `blob_size_mismatch` / `blob_too_large` |
| `remote` | remote 对该条目的写入返回不可重试的 DPE 错误（core.md §6）；可重试的错误不落到条目级，重试耗尽即按轮级中止本轮 | DPE 错误码原样 |

- `retryable` 的含义同 §6.5：同样的轮重跑可能成功。`plugin` 来源取插件给出的值；`blob_read_failed` 取插件 `-32003` 的 `data.retryable`（取消、超时视为 true，句柄失效视为 false）；`remote` 来源取 core.md §6 的值，唯一的例外是：按 §7.2 本可自动重新判定的冲突（`source_wins` 下的 commit / delete、源被改写的 move）因超过重试上限而失败时取 true；其余运行器判定的条目级失败均为 false。
- 运行器对条目的处理 MUST NOT 因其中某个条目失败而崩溃或放弃本轮其余条目（§6.1）。

### 7.2 写入冲突

**同步状态**：运行器 MUST 为每个实例维护本地同步状态——每个文档最近一次**确认**的 remote doc_hash（类比 Git 的 remote-tracking ref），键为 core.md §1.1 规范化后的 file_uri（独立运行器存放于状态目录，§4.4）。它与游标一样只作缓存：丢失时结果仍然正确，只是冲突保护退化（见下）。

- 以下结果确认并更新同步状态：`created` / `updated` / `unchanged`、已达成（remote 已等于本地内容），以及按 `source_wins` 重新提交成功；delete 成功或已达成时删除该条目；move 成功或已达成时把条目从 `from_uri` 迁到 `to_uri`。冲突上报时不更新。
- **意图日志**：写入请求发出前，运行器 MUST 先持久化本次写入预期的 remote 状态（意图），确认结果后再并入同步状态。意图的取值是 doc_hash 或「不存在」：commit 记 `file_uri → N`，delete 记 `file_uri → 不存在`，move 同时记两侧 `from_uri → 不存在`、`to_uri → 本次 move 的 base_hash`。判定时，R 等于该 URI 的待定意图值（含「不存在」）即视为本实例自己已生效的写入：按意图更新同步状态（「不存在」即删除条目），再按判定表继续。否则进程在写入生效、记录确认之前崩溃，下一轮会把自己的写入误报为他人改写或删除。写入结果一旦确定（成功或失败，含冲突），对应 URI 的待定意图 MUST 清除，只有结果未知（崩溃、响应丢失）时才保留——否则残留的意图会把之后他人的同值写入误当作自己的。意图 MAY 整批持久化后再并发发出写入；持久化只要求对进程崩溃持久（不要求对掉电持久）——意图日志丢失时退化为误报冲突并如实上报，不会造成错误写入。
- **契约**：比较 MUST 在同一 hash 契约下进行。同步状态中的值带契约前缀；服务端仍接受该契约时可直接作前置条件（契约 1 §6、core.md §5.1），不再接受或运行器改用其他契约时，这些条目按缺失处理。

**前置条件与冲突判定**：写入前运行器以 `batch_head` / `head` 读取 remote 值 R，同步状态中的值为 L，本地新内容的 doc_hash 为 N：

| 情形 | 判定 |
| --- | --- |
| R == N（commit），或目标状态已达成（delete / move，core.md §5.2） | 已达成，跳过并确认 |
| 无 L，R 不存在 | commit 以 `if_absent` 新建 |
| 无 L，R 存在 | 以 R 为 `base_hash`。这是同步状态缺失时的退化：两轮之间他人的写入无法识别，冲突保护只覆盖读写之间的竞态窗口 |
| 有 L，R == L | 以 L 为 `base_hash` 正常写入 |
| 有 L，R 存在且 ≠ L | 他人改写：冲突 |
| 有 L，R 不存在 | 他人删除（或移走）：冲突 |

delete 与 move 用同一张表（move 取 `from_uri` 的 L 与 R）。此外，以前置条件发出的写入仍可能因读写之间的竞态得到 `DPE_PRECONDITION_FAILED`、`DPE_ALREADY_EXISTS` 或不属于「已达成」的 `DPE_NOT_FOUND`（core.md §5.2），同样是冲突。

**冲突处置**：运行器就是 core.md §5.2 所说的「上层」。它 MUST NOT 改用 force（move 本就不支持 force，core.md §4），MUST 重新读取冲突 URI 的当前状态后按下表决定，运行器实现的其他按 URI 缓存（若有）MUST NOT 影响本节的判定。重新提交以重读到的 doc_hash 为前置条件，仍经 CAS：同级写入之间经 CAS 的后写覆盖不属于静默覆盖（core.md §7 第 4 条）。

策略由实例的 `conflict_policy` 给出（§4.4；宿主以自己的方式配置，缺省值相同）：`commit` 取 `source_wins`（缺省）或 `report`，`delete` 取 `report`（缺省）或 `source_wins`。delete 不可逆，会连带丢掉他人的新内容与学习产物，因此缺省不以源为准。

| 意图 | 重读结果 | 处置 |
| --- | --- | --- |
| commit | remote 的 doc_hash 等于本次提交内容 | 已达成，计 `unchanged` |
| commit | 文档存在、内容不同 | `source_wins`：以重读到的 doc_hash 为 `base_hash` 重新提交；`report`：条目失败 |
| commit | 文档不存在（他人已删除或移走） | `source_wins`：以 `if_absent` 重建（复活，core.md §4）；`report`：条目失败 |
| delete | 文档不存在 | 已达成，计 `deleted` |
| delete | 文档存在、被他人改写 | `report`：条目失败；`source_wins`：以重读到的 doc_hash 为 `base_hash` 重新删除 |
| move | 源存在、被他人改写 | 受 `commit` 策略约束——`source_wins`：以重读到的 doc_hash 为 `base_hash` 重试 move（move 保留内容与学习产物，他人的写入随之移到目标）；`report`：条目失败 |
| move | 目标已被占用（`DPE_ALREADY_EXISTS`） | 条目失败 |
| move | 源已不存在，目标未达成（`DPE_NOT_FOUND`） | 条目失败 |

- **重试上限**：本轮内对同一条目的重读与重新提交 MUST 有次数上限（由运行器决定，SHOULD 不超过 2 次），以免与并发写入者互相饿死；超过即该条目失败（`retryable: true`，§7.1），游标不推进（§6.8）；`report` 策略下的冲突与 move 的两类失败重跑不会自行消失，取 false。
- **`source_wins` 的副作用**：它会覆盖他人在实例前缀内的同级写入，并复活他人删除的文档，两者都会在 remote 侧引起重新学习（plan §0.1 P3）；两个写入者都以自己为准时，同一文档会被反复改写。因此服务端与其他写入者 SHOULD NOT 写入 connector 实例的前缀；需要在 remote 上保持删除的文档，应先从源中删除，或对该实例使用 `report`。
- **永久冲突**：move 的后两类失败与 `report` 策略下的冲突，重跑不会自行消失，按 §6.8「永久失败不得空转」处理。恢复由使用者完成：在 remote 上消除冲突后重跑；或丢弃该实例的本地游标，以全量轮恢复现存内容（全量轮不表达此前的删除与移动，§6.8）。运行器 MUST NOT 把失败的 move 自动退化为「在目标上 commit、再删除源」——那会丢失学习产物并覆盖占用目标的文档。

### 7.3 轮结果

运行器 MUST 以下列三态之一上报每一轮，并附 `incremental`：

| outcome | 含义 |
| --- | --- |
| `succeeded` | 枚举完整结束，全部条目成功落地（§6.8 的持久化条件）；新游标已持久化，不支持游标时同样以此表示整轮成功 |
| `partial` | 枚举完整结束（收到结束批），但存在条目级失败；游标不推进 |
| `failed` | 实例级、轮级（含 `instance_busy`）或协议级失败，轮未完整结束或未开始；游标不推进 |

- `incremental` 取自结束批（§6.4）；轮未收到结束批时为 `null`。
- `incremental: false` 的 `succeeded` 轮表示此前的删除与移动未被表达（§6.8），运行器 MUST 让它可观测（§7.5），MUST NOT 把它与增量轮混同。

### 7.4 轮报告（独立运行器规范档）

每次调用结束时，独立运行器 MUST 向 stdout 写出恰好一个轮报告——单行紧凑 JSON 对象（UTF-8，LF 结尾）——stdout 不输出其他内容。未能开始轮（实例级失败、`instance_busy`）时同样输出报告，`outcome` 为 `failed`、`incremental` 为 `null`、`counts` 各项为 0。唯一的例外是命令行用法错误：此时以退出码 64 结束、不输出报告（§7.6）。

```json
{ "report_version": 1,
  "instance": { "id": "acme-repo", "uri_prefix": "git://acme/repo/" },
  "remote_url": "https://dpe.example.com/remotes/acme",
  "plugin": { "name": "git-connector", "version": "0.1.0" },
  "protocol_version": "dpe-connector/1",
  "started_at": "2026-10-09T08:00:00Z", "ended_at": "2026-10-09T08:00:42Z",
  "outcome": "partial", "incremental": true, "cursor_persisted": false,
  "cursor_held_since": "2026-10-09T08:00:00Z",
  "counts": { "created": 3, "updated": 1, "unchanged": 0, "deleted": 1, "moved": 0, "failed": 1 },
  "failures": [
    { "level": "item", "source": "prefix", "code": "out_of_prefix",
      "message": "file_uri outside instance prefix",
      "file_uri": "git://other/x.md", "retryable": false } ] }
```

| 字段 | 说明 |
| --- | --- |
| `report_version` | 整数；本规范定义 `1` |
| `instance` | `{id, uri_prefix}`，取自实例定义；实例定义无法解析或缺少该成员时，对应成员为 `null` |
| `remote_url` | 实例定义的 `remote.url`；无法取得时为 `null` |
| `plugin` | `{name, version}`；插件未完成 initialize 时为 `null` |
| `protocol_version` | 选定的线协议版本；未完成 initialize 时为 `null` |
| `started_at` / `ended_at` | RFC 3339 UTC 时间戳 |
| `outcome` / `incremental` | §7.3 |
| `cursor_persisted` | 布尔：本轮是否持久化了新游标 |
| `cursor_held_since` | 游标被扣住的起始时刻：自该实例上一次 `succeeded` 的轮以来，第一个已取得排他锁（§5.2）且结果不是 `succeeded` 的轮的 `started_at`（RFC 3339 UTC，记录于状态目录，§4.4）。本轮为 `succeeded`、此前没有这样的轮，或状态目录中尚无该实例时为 `null`；未取得锁的调用（`instance_busy`）不读写该记录，报告中取 `null`。`supports_cursor: false` 的实例以同一定义计算（只看 outcome，不看是否有游标）。供调用方对长期卡住的实例告警 |
| `counts` | 各项计数（整数，均必填）：`created` / `updated` / `unchanged` 为 commit 结果（core.md §3.3）；`deleted` / `moved` 含按 core.md §5.2 视为已达成的情形；`failed` 为条目级失败的条目数 |
| `failures` | 失败数组，元素见下；无失败时为 `[]` |
| `failures_truncated` | 可缺省：失败过多时运行器 MAY 截断 `failures`，此字段为被省略的条数 |

`failures` 的元素：`{level, source, code, message, retryable, file_uri?, from_uri?, to_uri?}`。

- `level`：`protocol` / `instance` / `round` / `item`（§7.1）。
- `source`：`plugin`（插件报告的错误，含 JSON-RPC `error` 响应）/ `validation` / `prefix` / `blob` / `remote` / `runner`（运行器自身判定的非条目级失败）。
- `code`：字符串。插件 `error` 条目原样取其 `code`；插件的 JSON-RPC 错误取 §6.5 错误码表的名称（`cancelled`、`unknown_handle`、`source_failed`、`version_unsupported`、`invalid_config`），JSON-RPC 标准码依次记为 `parse_error`、`invalid_request`、`method_not_found`、`invalid_params`、`internal_error`，未知错误码记为 `unknown_error`（按不可重试处理，§6.5）；remote 错误取 DPE 错误码原样；运行器自身的判定取下表。
- `message`：人类可读的说明，MUST NOT 含凭证值（§4.3）。
- `file_uri` / `from_uri` / `to_uri`：条目级失败 MUST 给出所涉 URI（规范化后，core.md §1.1）。

运行器自有错误码（闭集）：

| code | 层级 | 含义 |
| --- | --- | --- |
| `manifest_invalid` | instance | 清单不可读或不合法（§4.1） |
| `definition_invalid` | instance | 实例定义不合法，含前缀非法或重叠、凭证引用指向 remote 凭证（§3、§4.3、§4.4） |
| `config_schema_violation` | instance | 实例配置未通过 `config_schema`（§4.2） |
| `secret_missing` | instance | 必填凭证无法解析（§4.3） |
| `version_unsupported` | instance | 插件选定的版本不在运行器的列表中（§6.3） |
| `plugin_mismatch` | instance | 插件自报身份与清单不符（§4.1） |
| `remote_unauthorized` | instance | remote 拒绝凭证（HTTP 401，http.md §6） |
| `remote_incompatible` | instance | remote 不支持运行器可用的任何 hash 契约（http.md §4.1） |
| `instance_busy` | round | 同一实例已有轮在进行（§5.2）；`retryable: true` |
| `protocol_error` | protocol | 分帧、JSON-RPC 或握手违例，消息超限（§6.2、§6.3） |
| `plugin_crashed` | round | 插件异常退出（§6.3） |
| `timeout` | round | `scan` 超时，或请求超时后插件不响应 `cancel`（§6.7） |
| `remote_unavailable` | round | remote 整体不可用（§7.1） |
| `out_of_prefix` | item | 越界条目（§6.5） |
| `segment_unclosed` | item | 分段文档在轮结束时未闭合（§6.5） |
| `blob_read_failed` | item | blob 读取失败（§6.7） |
| `blob_size_mismatch` | item | 读取总长与声明的 `size` 不符（§6.7） |
| `blob_too_large` | item | blob 超过 remote 的 `blob_max_bytes`（§6.7） |

### 7.5 结构化日志（独立运行器规范档）

- 独立运行器 MUST 把日志写到 stderr，格式为 NDJSON：每行一个紧凑 JSON 对象。每条日志 MUST 含 `ts`（RFC 3339 UTC）、`level`（`debug` / `info` / `warn` / `error`）、`event`、`instance`（实例 `id`）与 `round`（运行器为每轮生成的不透明标识，轮外事件为 `null`）。
- 运行器 MUST 至少输出以下事件：

| event | 附加字段 | 时机 |
| --- | --- | --- |
| `plugin.started` | `pid` | 插件进程启动后 |
| `plugin.exited` | `exit_code`、`signal`（可为 `null`） | 插件进程退出后 |
| `round.started` | `cursor_given`（布尔：轮首 `scan` 是否带了游标） | 发出轮首 `scan` 前 |
| `item.failed` | 同 §7.4 `failures` 元素的字段 | 每个失败发生时（任何层级） |
| `round.finished` | `outcome`、`incremental`、`cursor_persisted`、`cursor_held_since`、`counts` | 轮结束时 |
| `plugin.stderr` | `line`、`truncated`（布尔） | 插件 stderr 每输出一行 |

- 运行器给出了游标而插件回退为全量轮（`incremental: false`，§6.8）时，`round.finished` MUST 为 `warn` 级；`outcome` 为 `partial` 或 `failed` 时 MUST 为 `warn` 或 `error` 级。
- `plugin.stderr`：插件 stderr 按行转发；单行超过运行器的长度上限（SHOULD 不小于 8 KiB）时截断并标 `truncated: true`；非 UTF-8 字节以 U+FFFD 替换。转发前按 §4.3 脱敏。
- 日志 MUST NOT 含凭证值与 `Authorization` 头（§4.3），SHOULD NOT 含 blob 字节与完整的元素内容。
- 实现 MAY 增加事件与字段；日志的消费者 MUST 忽略不认识的事件与字段。

### 7.6 退出码（独立运行器规范档）

| 退出码 | 含义 |
| --- | --- |
| 0 | `succeeded`（含全量轮；需要区分时读报告的 `incremental`） |
| 1 | `partial` |
| 2 | `failed`：轮级（`instance_busy` 除外）或协议级失败，重跑可能成功 |
| 3 | `failed`：实例级失败，不修改实例定义、清单或凭证就无法恢复 |
| 4 | `failed`：`instance_busy`，稍后重试即可 |

- 命令行用法错误（如未给出实例定义）MUST 返回 64（同 BSD `EX_USAGE`），且不输出轮报告（§7.4）；其余退出码都伴随一个轮报告。其他非零退出码表示运行器自身异常（如被信号终止），调用方 SHOULD 按 2 处理。

## 8. 一致性要求

### 8.1 插件

插件 MUST：

1. 附带合法的清单，initialize 自报的 `name` / `version` 与清单相同（§4.1）；
2. 作为独立进程，经 stdio 以 NDJSON 分帧的 JSON-RPC 2.0 通信；stdout 只含协议消息，不主动发起请求（§6.1、§6.2）；
3. 先握手后产出；版本协商不静默降级，配置不可用时以 `-32005` 拒绝（§6.3）；
4. 响应 `shutdown` 后以退出码 0 退出，读到 stdin EOF 时尽快退出（§6.3）；
5. 产出封闭 schema 的条目，删除与移动以显式条目表达，无法产出的内容以 `error` 条目如实上报（§2、§6.5）；
6. 不计算、不伪造 `sha256:` 引用；句柄快照一致，支持句柄范围内任意偏移的读取（§6.6、§6.7）；
7. 处理长请求期间继续处理 `cancel`（§6.7）；
8. 游标不透明且自包含；增量轮覆盖自游标以来的一切变化；解释不了游标时回退全量并标记 `incremental: false`（§6.8）；
9. 只从环境变量取得凭证，不把凭证值写入任何输出（§4.3）。

插件 SHOULD 保证枚举确定（§2），不产出易变字段与位置序号（§2）。

### 8.2 运行器

运行器 MUST：

1. 不经任何通道把 remote 凭证交给插件，按 §4.3 构造插件环境（§4.3、§6.1）；
2. 启动插件前校验清单、实例配置与凭证，initialize 后核对插件身份（§4.1–§4.3）；
3. 全量校验插件产出，自行计算全部 hash（§6.1、§6.6）；
4. 强制 URI 前缀，拒绝越界条目，拒绝重叠的前缀（§3、§6.5）；
5. 保证每个实例同时至多一轮（§5.2、§6.4）；
6. blob 读全后再上传，读取失败时该文档整篇失败（§6.7）；
7. 游标 fail-closed：只在整轮全部落地后持久化（§6.8）；
8. 绝不自动 force，按 §7.2 处理写入冲突；
9. 以 §7.3 的三态上报每一轮，全量轮可观测；
10. 不在日志、报告与错误中写出凭证值（§4.3、§7）。

独立运行器另 MUST 满足 §4.4、§5.2、§7.4–§7.6。

### 8.3 收敛判据

记号：对源状态 S，F(S) 为一个全量轮在 S 上的产出经运行器计算后的映射 `file_uri → doc_hash`（file_uri 按 core.md §1.1 规范化）；R 为 remote 上实例前缀内的文档映射（core.md §3 的 `list`）。以下判据以「前缀内没有其他写入者、所述各轮均为 `succeeded`」为前提；声明符合本契约的插件与运行器组合 MUST 满足：

1. **确定性**：同一 S 上的两个全量轮得到相同的 F(S)。
2. **全量轮**：全量轮落地后，F(S) 中每个 URI 在 R 中存在且 doc_hash 相同；此前已删除或移出的文档可能残留在 R 中（§6.8）。
3. **增量收敛**：从空前缀开始，一个全量轮在 S0 上落地（R = F(S0)）；此后源依次变为 S1, …, Sn，每次变化后运行一个 `incremental: true` 的轮。最终 R = F(Sn)——包括删除与移动的结果。
4. **失败不破坏收敛**：判据 3 的序列中任一轮为 `partial` 或 `failed` 时，原样重跑直至 `succeeded`，结论不变（游标 fail-closed，§6.8）。

判据 3 即 plan §14 M4 的验收口径（增量推送、删除与移动的结果与全量推送收敛后一致）。

### 8.4 验证方法

本节说明如何验证 §8.1–§8.3，不规定测试代码的组织。

**线协议夹具插件**（验证运行器）：一个脚本化的插件，由实例配置选择场景（如 `{"scenario": "multi_batch"}`），覆盖全部线协议消息与失败路径。由于插件是独立进程，同一个夹具可供任何语言的运行器使用。运行器针对每个场景 MUST 得到下表的结果：

| 场景 | 插件行为 | 运行器预期 |
| --- | --- | --- |
| `empty` | 正常握手；一批空结束批，`cursor: null`、`incremental: false` | `succeeded`，退出码 0，全量轮可观测 |
| `version_mismatch` | initialize 以 `-32004` 拒绝 | 实例级 `version_unsupported`，退出码 3 |
| `invalid_config` | initialize 以 `-32005` 拒绝 | 实例级，source `plugin`、code `invalid_config`，退出码 3 |
| `identity_mismatch` | 自报的 `version` 与清单不符 | `plugin_mismatch`，退出码 3 |
| `multi_batch` | 三批（`next` 续批），含 document、delete、move | `succeeded`，remote 状态符合产出，游标已持久化 |
| `segmented` | 一篇文档分三段（`continued`） | 合并为一篇提交 |
| `unclosed_segment` | 轮结束时分段未闭合 | `partial`，`segment_unclosed`，该文档未提交，游标未推进，退出码 1 |
| `error_item` | `error` 条目（`source_unavailable`，`retryable: true`） | `partial`，source `plugin` |
| `out_of_prefix` | 越界的 document，以及 `to_uri` 越界的 move | `partial`，`out_of_prefix`，均未投递 |
| `invalid_item` | 未知 `kind`、未定义字段、未知 category | `partial`，`DPE_VALIDATION` / `DPE_CATEGORY_UNKNOWN` |
| `blobs` | 内联 blob 与需多块读取的句柄 blob | `succeeded`，blob 已上传，引用等于字节的 sha256 |
| `blob_size_mismatch` | 句柄实际长度与 `size` 不符 | `partial`，`blob_size_mismatch`，文档未提交 |
| `unknown_handle` | `read_blob` 返回 `-32002` | `partial`，`blob_read_failed` |
| `cancel` | `read_blob` 挂起，收到 `cancel` 后以 `-32001` 响应 | 运行器超时后发出 `cancel`；`partial`，`blob_read_failed` |
| `crash` | 第二批之前以非零码退出 | `failed`，`plugin_crashed`，游标未推进，退出码 2 |
| `oversize` | 输出超过 `max_message_bytes` 的行 | `failed`，`protocol_error`，退出码 2 |
| `bad_framing` | 输出空行或非 JSON 行 | `failed`，`protocol_error`，退出码 2 |
| `scan_error` | `scan` 以 `-32003` 失败 | `failed`，`source_failed`，退出码 2 |
| `fallback_full` | 收到游标但回退全量（`incremental: false`） | `succeeded`，`round.finished` 为 `warn` 级 |
| `no_cursor` | `supports_cursor: false` | 不缓存游标，下一轮不带游标 |
| `env_probe` | 声明一个必填凭证；把自身环境变量的**名字**（不含值）作为文档内容产出 | 环境恰为 §4.3 的三部分；凭证缺失时 `secret_missing`，退出码 3 |

另有不依赖插件行为的运行器用例：同一实例并发运行两次，其一以 `instance_busy`（退出码 4）结束；清单或实例定义违例得到 `manifest_invalid` / `definition_invalid`，配置未通过 schema 得到 `config_schema_violation`；清单的 `pattern` / `patternProperties` 键超出 §4.1.1 的子集或结构上界（如 lookaround、反向引用、`\p{…}`、超展开规模）得到 `manifest_invalid`；同一状态目录中前缀重叠的第二个实例被拒绝（`definition_invalid`）；沿用同一 `id` 修改 `config` 后，下一轮不带游标（全量轮）；remote 上他人改写某文档、源端随后也修改该文档（使插件再次产出它）后，commit 冲突按 `source_wins` 重新提交、`report` 下判条目失败（`retryable: false`，`cursor_held_since` 非 `null`）；同步状态与意图日志：写入生效后、确认前终止运行器进程，重跑不得报告冲突（含 delete 后源端重建同一 URI、move 后向原 URI 写入）；move 目标被占用时为 `partial`，`cursor_held_since` 保持为首次失败轮的开始时刻。

**插件测试驱动**（验证插件）：驱动插件进程并检查 §8.1——分帧与握手、条目的封闭 schema、句柄的快照一致性（同一句柄在不同偏移重复读取）、`shutdown` / stdin EOF 后按时退出、`cancel` 的响应，以及确定性（判据 1）。

**收敛测试**（验证判据 2–4）：以可脚本化变更的源夹具（新增、修改、文档中间插入一页、删除、改名、二进制内容）驱动插件与运行器，对参考服务端核对 §8.3；官方 connector 另以真实服务端完成 plan §14 M4 的端到端验收。

## 9. 非目标

- 不定义任何具体数据源（官方 Git connector 以插件形式另行交付）；
- 不定义宿主的多租户、计费、权限模型；
- 不定义调度策略（§5.2）；
- 不复制 Core 的投递语义——运行器直接遵循 core.md 与 HTTP 绑定。
