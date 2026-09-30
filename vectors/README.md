# 一致性向量

向量是 [hash 契约 1](../spec/hash-contract-1.md) 的组成部分，由规范参考实现 `scripts/gen_vectors.py` 生成；SDK 与任何服务端实现只**消费**向量（在各自 CI 中逐字节校验），不得生成或手改。

```bash
make vectors          # 重新生成（仅在规范变更时）
make check-vectors    # CI：校验已提交向量与生成器一致
```

## 文件格式

每个 `*.json` 是一个向量，`kind` 二选一：

- **`document`**：`documents` 是一到多篇文档的内容身份视图（`title` + `pages[{number,title,elements[]}]`，元素只含内容字段）；`expected` 给出每篇文档在各契约下的 `doc_hash`、逐页 `page_hash` 与逐元素 `content_hash`。多篇文档用于表达等值/变化断言（如 `null_vs_empty_title` 两篇的 doc_hash 相同）。
- **`jcs`**：`cases[]` 每条给出 JSON 输入、RFC 8785 规范化字符串 `canonical` 及其 UTF-8 字节的 `sha256`。

`manifest.json` 记录契约版本、生成器与各文件 sha256（provenance）。

## 假想契约 `dpe2`

`upgrade_drill.json` 用于契约升级演练（同一份内容在两个契约下的期望值）。`dpe2` **不是真实契约，仅用于测试**，定义：与 `dpe1` 完全相同，但每次摘要在长度前缀拼接时额外前置一个内容为 ASCII `dpe2` 的段，值前缀为 `dpe2:`。实现的多契约管线应能同时算出两者。
