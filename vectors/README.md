# 一致性向量

向量是 [hash 契约 1](../spec/hash-contract-1.md) 的组成部分，由规范参考实现 `scripts/gen_vectors.py` 生成；SDK 与任何服务端实现只**消费**向量（在各自 CI 中逐字节校验），不得生成或手改。

```bash
make vectors          # 重新生成（仅在规范变更时）
make check-vectors    # CI：校验已提交向量与生成器一致
```

## 文件格式

每个 `*.json` 是一个向量，`kind` 二选一：

- **`document`**：
  - `documents` 是一到多篇文档的 **hash 核心输入视图**：
    - 文档：`file_type`、可选 `doc_metadata`、可选 `attributes`、`pages[{number,title,page_metadata,elements[]}]`；契约 1 没有 doc title。
    - 元素：一个内容对象（category 规定的字段 + `metadata`），外加可选的 `occurrence`（线上属于该位置骨架 entry 的随位字段，core.md §2.3）。
    - 这一视图不是线上报文，差别有两处：hash 核心对 pages 数组顺序宽容，线上 MUST 按 number 升序；hash 核心按保留键全集过滤 metadata（契约 1 §2.1），线上 metadata 出现保留键即拒绝（见 `metadata_reserved_and_null`）。
  - `expected` 给出每篇文档在各契约下的 `doc_hash`、`state_hash`，以及逐页的 `page_hash` 和逐元素的 `content_hash`（`pages[i].elements[j]`，i 为输入数组下标）。
  - `relations`（可选）是本向量要证明的**规范性质**，生成器在生成时断言其成立，消费方 SHOULD 一并断言：
    - `{"equal": [ref…]}`：所列值两两相等；
    - `{"distinct": [ref…]}`：所列值两两不同。
    - `ref` 形如 `<文档键>.<路径>`，例如 `base.doc_hash`、`base.state_hash`、`base.pages.0.page_hash`、`base.pages.0.elements.1`；有多个契约时，在每个契约下分别成立。
- **`jcs`**：`cases[]` 每条给出 JSON 输入、RFC 8785 规范化字符串 `canonical` 及其 UTF-8 字节的 `sha256`。

`manifest.json` 记录以下内容：
- 契约版本；
- 契约常量，与 SDK 导出的常量必须一致：保留键全集 `reserved_metadata_keys` 及其两组 `occurrence_keys` / `undelivered_keys`，file_type 封闭枚举 `file_types`；
- 生成器与各文件的 sha256（provenance）。

## 假想契约 `dpe2`

`upgrade_drill.json` 用于契约升级演练（同一份内容在两个契约下的期望值）。`dpe2` **不是真实契约，仅用于测试**，定义：与 `dpe1` 完全相同，但每次摘要在长度前缀拼接时额外前置一个内容为 ASCII `dpe2` 的段，值前缀为 `dpe2:`。实现的多契约管线应能同时算出两者；两契约 `expected` 中逐页、逐元素结果按位置一一对应，即契约升级"原位重算、不重新配对"的对应关系断言（契约 1 §7）。
