# 一致性向量

向量是规范的组成部分：绝大多数是 [hash 契约 1](../spec/hash-contract-1.md) 的向量，`config_schema_patterns.json` 是 [connector 契约](../spec/connector-contract.md) §4.1.1（`config_schema` 的正则子集）的向量，`nesting_depth.json` 是 [core.md](../spec/core.md) §2.8（对象嵌套深度上界）的向量，两者与 hash 契约无关。全部由规范参考实现 `scripts/gen_vectors.py`（辅以 `scripts/pattern_subset.py`）生成；SDK 与任何服务端实现只**消费**向量（在各自 CI 中逐字节校验），不得生成或手改。

```bash
make vectors          # 重新生成（仅在规范变更时）
make check-vectors    # CI：校验已提交向量与生成器一致
```

## 文件格式

每个 `*.json` 是一个向量，`kind` 六类：

- **`document`**：
  - `documents` 是一到多篇文档的 hash 输入：
    - 文档：`file_type`、可选 `title`、可选 `doc_metadata`、`pages[{title, page_metadata, elements[]}]`；页没有页号，数组顺序即阅读顺序。
    - 元素：元素对象，即 category 允许的字段加 `metadata`。
    - 这是三层对象的**展开视图**：线上的文档对象 `pages` 与页对象 `elements` 是子对象 hash 列表，子对象单独投递（契约 1 §5）。
  - `expected` 给出每篇文档在各契约下的 `doc_hash`、逐页的 `page_hash`，以及逐元素的 `content_hash`（`pages[i].elements[j]`，i 为输入数组下标）。`preimage_basic` 另给出 `preimages`：文档对象（`document`）、各页对象（`pages`）、各元素对象（`elements`）规范化后的 JCS 原像字符串，hash 即其 UTF-8 字节的 SHA-256。
  - `relations`（可选）是本向量要证明的**规范性质**，生成器在生成时断言其成立，消费方 SHOULD 一并断言：
    - `{"equal": [ref…]}`：所列值两两相等；
    - `{"distinct": [ref…]}`：所列值两两不同。
    - `ref` 形如 `<文档键>.<路径>`，例如 `base.doc_hash`、`base.pages.0.page_hash`、`base.pages.0.elements.1`；有多个契约时，在每个契约下分别成立。
- **`jcs`**：`cases[]` 每条给出 JSON 输入、RFC 8785 规范化字符串 `canonical` 及其 UTF-8 字节的 `sha256`。
- **`uri`**：file_uri 的语法规范化（[core.md §1.1](../spec/core.md)）。`cases[]` 每条给出合法输入 `input` 与其规范化输出 `normalized`（规范化幂等，`normalized` 是不动点）；`invalid_cases[]` 每条给出必须被拒绝的输入 `input` 与期望错误码 `code`（恒为 `DPE_VALIDATION`）。消费方 MUST 逐例通过。
- **`invalid`**：拒绝类用例，固定 [core.md §2.8](../spec/core.md) 的校验顺序。`cases[]` 每条包含：
  - `object_kind`：`element` / `page` / `document`（线上原像），或 `expanded_document`（上文的展开视图，按与线上请求相同的顺序校验：文档自身字段 → 各页自身字段 → 各页元素）；
  - `contract`：本次选择的契约；
  - `input`：被校验的 JSON 值；或改为 `input_json`，以原始 JSON 文本给出，用于 I-JSON 违例这类无法以解析后的值表达的输入。消费方 MUST 用拒绝重复键的严格解析器读取，解析阶段的拒绝同样视为 `DPE_VALIDATION`，位置为 `""`（core §2.8 第 0 步）；
  - `code`：期望的错误码；
  - `path`（可选）：期望的违例位置，RFC 6901 JSON Pointer。

  消费方 MUST 拒绝每条输入，且错误码一致。用例带 `path` 时（只有一处违例的输入），违例位置也 MUST 一致。不带 `path` 的用例含多处违例，只断言错误码，以此检验校验顺序。生成器在生成时用参考校验器断言每条用例。

  「受支持的契约」按 manifest 的 `contract` 加上用例的 `contract` 计算，不按消费方自己支持的契约集合计算。例如，同时支持 dpe1 与 dpe2 的过渡期服务端仍应按此口径运行用例。（运行时校验按服务端声明的契约集合判定，core.md §3.1；此处口径只用于运行向量。）

- **`nesting_depth`**（`nesting_depth.json`，规范依据 [core.md](../spec/core.md) §2.8 第 0 步第三项）：
  - `limit`：对象的嵌套深度上界（MUST ≤ `limit`，超出为 `DPE_VALIDATION`）；实现导出的常量 MUST 与之相等；
  - `cases[]` 每条包含 `object_kind`、`contract`、`input`（被校验的 JSON 值）与 `input_json`（同一输入的 JSON 文本）。`accept: true` 的用例 MUST 被接受；其余给出 `code`（单处违例的用例还给出 `path`）MUST 被拒绝且错误码一致。消费方 MUST 对每条用例走两条路径并得到同一结论：对象级入口（直接校验 `input`）与文本入口（严格解析 `input_json` 后校验）。边界用例的最深一层是空容器（`{}` 或 `[]`）；
  - 深度按**每个对象自身**计：标量记 0，对象或数组记 1 + 其子值的最大深度，空容器记 1（`{}` 记 1、`{"a": {"b": [1]}}` 记 3）；页的 `elements` 与文档的 `pages` 按子 hash 列表计（列表自身记 1、每一项不计入），深度与对象在报文中的位置无关（绑定信封的层数不计入）；
  - `expanded_document` 用例的违例位置同样为对象自身（`""`），不换算为相对报文根的路径（core §2.8 违例位置）。
- **`pattern`**（`config_schema_patterns.json`，规范依据 [connector 契约](../spec/connector-contract.md) §4.1.1，与 hash 契约无关）：
  - `valid_patterns[]`：MUST 被接受且可转译（子集外的 pattern 使清单整体 `manifest_invalid`）；匹配语义由 `match_cases` 逐条钉住；含结构上界边界与最坏情况形状；
  - `invalid_patterns[]`：MUST 被拒绝；`reason` 是参考实现给出的原因标记，供人阅读，不作跨实现的断言。无法以普通字符串表达的输入（孤立代理项等，同 hash 向量的 `input_json` 约定）以 `pattern_json` 给出原始 JSON 文本，消费方用严格解析器读取；
  - `match_cases[]`：每条给出 `pattern`、`value` 与是否匹配的 `match`；无法以普通字符串表达的输入（孤立代理项等）以 `value_json` 给出原始 JSON 文本（同 `pattern_json` 的约定）。期望值是**手写**的规范语义，生成器只用参考匹配器交叉核对（不一致即生成失败），避免「翻译器自己验证自己」；消费方 MUST 逐条通过；
  - `schema_cases[]`：每条给出 `schema`（必填）与 `manifest_valid`（必填）；可另给 `config` 与 `config_valid`（两者必须成对出现，生成器断言其配对）。`manifest_valid` 由 §4.1.1 的**封闭关键字子集检查器**交叉核对（未列出的关键字、`$defs` 非根、`$ref` 非唯一形式（含前缀/解码边界）/目标不存在/`$defs` 引用成环、`items` 数组形式、schema 的展开深度（≤64：根计 1，schema 位置与 `$ref` 跳转各 +1、沿引用继续计）、清单文件的 JSON 嵌套深度（≤64；向量里的 `schema` 被包进清单对象，计数含包装层，含数据位置上的深度）、容器形状与 `type` 取值，以及 pattern 子集越界都判清单不合法；`const` / `default` / `examples` 内的同名键是数据）；`config_valid` 是手写期望，生成器不做 JSON Schema 求值。

`manifest.json` 记录：
- 契约版本；
- 契约常量，与 SDK 导出的常量必须一致：file_type 推荐登记表 `file_types`（按 core.md §2.5 表中顺序），以及 category 封闭枚举与各自允许的内容字段 `category_content_fields`（契约 1 §4.1；键按名排序，字段按表中顺序）；
- 生成器与各文件的 sha256（provenance）。

## 假想契约 `dpe2`

`upgrade_drill.json` 用于契约升级演练（同一份内容在两个契约下的期望值）。`dpe2` **不是真实契约，仅用于测试**，定义：与 `dpe1` 完全相同，但每个对象的摘要输入是 ASCII `dpe2` 后接 JCS 原像字节，即 `sha256(utf8("dpe2") ‖ utf8(JCS(norm(obj))))`，值前缀为 `dpe2:`。实现的多契约管线应能同时算出两者；两契约 `expected` 中逐页、逐元素结果按位置一一对应，即契约升级"原位重算、不重新配对"的对应关系断言（契约 1 §6）。
