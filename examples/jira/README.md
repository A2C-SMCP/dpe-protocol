# 示例插件：Jira Cloud（jira）

> **这是示例，不发布，也不是官方 connector**（官方的是 [`connectors/git`](../../connectors/git/)）。
> 它用来验证 Git connector 覆盖不到的 DPE 能力：大量小文档、数据源凭证、易变内容、限流，后续还有
> 删除与移动、网络来源的 blob。设计见 [Issue #223](https://github.com/A2C-SMCP/dpe-protocol/issues/223)，
> 按其中的拆分逐步实现；**当前只有全量枚举**（`supports_cursor: false`），增量、附件字节与项目文档
> 尚未实现，`include_project_docs` 目前只接受、不生效。

把 Jira Cloud 的工单映射为 DPE 文档的 **dpe-run 插件**（[connector 契约](../../spec/connector-contract.md) §6）。
插件只产出源内容：不计算 hash、不接触 DPE remote。运行时只依赖 `dpe-hash`（Retry-After 解析与
站点地址校验）。

## 用法

```bash
uv sync
uv run pytest            # 含模拟 Jira + 真实 dpe-run + 参考服务端的端到端用例

# 实例定义（connector 契约 §4.4）
{
  "definition_version": 1,
  "id": "jira-abc",
  "remote": { "url": "https://dpe.example.com/remotes/x" },
  "uri_prefix": "jira://example.atlassian.net/",
  "plugin": { "manifest": "examples/jira/dpe-connector.json", "command": ["dpe-jira"] },
  "config": { "site": "https://example.atlassian.net", "projects": ["ABC"] },
  "secrets": {
    "JIRA_EMAIL": { "env": "JIRA_EMAIL" },
    "JIRA_API_TOKEN": { "file": "jira-token.txt" }
  }
}
```

| 配置 | 必填 | 说明 |
| --- | --- | --- |
| `site` | 是 | 站点地址，与 remote URL 同一套文法（HTTP 绑定 §1）：小写、无尾部 `/`、无查询串 |
| `projects` | 二选一 | 项目 key 列表 |
| `jql` | 二选一 | 自定义范围；不得含 `ORDER BY`（插件统一按 id 升序；检测是简单的文本匹配，字符串字面量里出现 `order by` 也会被拒） |

凭证 `JIRA_EMAIL` / `JIRA_API_TOKEN`（Basic 认证）由运行器按清单注入，插件不回显它们。

## 映射规则

- **文档 = 一个工单**：`file_uri` = 实例前缀 + `issue/<工单 key>`；`file_type` 为 `jira_issue`，
  `title` 为 summary，`doc_metadata` 只放不可变的 `{issue_id, created_at}`。
- **三页**，`page_metadata.section` 依次为 `body`（描述）、`comments`（评论，按创建时间、再按评论
  id 在本地排序）、`fields`（固定字段，按固定顺序每个一个元素，值为空不产出）。
- **ADF 转换**是只依赖节点结构的确定性规则（见 `src/dpe_jira/adf.py` 文档串），换一种语言照着
  实现能得到逐字节相同的元素。
- **名称会随语言变化**：状态、优先级、类型、解决结果的名称按凭证账号的语言设置返回，因此
  `metadata.value` 同时记 `{id, name}`。切换账号语言会让这些元素的内容变化——这是真实的内容变化，
  不是插件的不确定性。

## 错误处理

- 握手时探测凭证并用一次搜索检查范围：凭证被拒、JQL 不合法或项目不存在、站点不是 Jira API
  都以 `-32005` 拒绝（需要改配置）；探测时的网络失败与限流不算配置问题，留给 scan 报告——
  域名解析失败、TLS 证书错误也归为网络失败，所以站点写错域名不会在握手时以 `-32005` 暴露；
- 429 / 503 按 `Retry-After` 退避重试（最多 4 次，单次最长等 60 秒）；耗尽、网络失败或 5xx 时
  本轮以可重试的 `-32003` 中止，由运行器退避后重来；凭证失效、请求被拒或响应不符协议时同样
  中止，但标为不可重试；
- 枚举之后被删的工单产出条目级 `source_unavailable`；内容无法映射产出 `content_invalid`。
