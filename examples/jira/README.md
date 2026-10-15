# 示例插件：Jira Cloud（jira）

> **这是示例，不发布，也不是官方 connector**（官方的是 [`connectors/git`](../../connectors/git/)）。
> 它用来验证 Git connector 覆盖不到的 DPE 能力：大量小文档、数据源凭证、易变内容、限流、删除与
> 移动，后续还有网络来源的 blob。设计见 [Issue #223](https://github.com/A2C-SMCP/dpe-protocol/issues/223)，
> 按其中的拆分逐步实现；**附件字节与项目文档尚未实现**，`include_project_docs` 目前只接受、不生效。

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

## 增量、删除与移动

插件支持游标（`supports_cursor: true`）。游标记下上一轮范围内每个工单的 id、key 与 `updated`，
下一轮先列出范围内全部工单的这三项再比较：

- 新出现的工单、`updated` 变了的工单 → 取全文，重新产出文档；没变的跳过；
- 上一轮有、这一轮没有的工单 → **删除**对应文档；
- 同一个 id 的 key 变了（工单换了项目）→ **移动**：旧地址改名为新地址，紧跟新地址的完整内容。

需要知道的几点：

- **变化靠 `updated` 判断**。Jira 在改字段、加评论或改评论时都会更新它。但文档里有些文字来自
  别的实体：人员显示名、组件与版本名称、父工单、凭证账号语言决定的状态等名称。这些在别处改了
  不会更新本工单的 `updated`，增量轮会保留旧文字，直到工单下次变化或一次全量轮；需要立刻刷新时
  清除该实例的游标即可。
- **移出范围等同于删除**。工单在 Jira 里被改到 JQL 范围之外（换了项目、改了标签等），插件看到
  的就是它消失了，会删掉对应文档。这是「范围」本身的含义。改实例配置里的 `projects` / `jql`
  属于配置变化，运行器会丢弃游标、改跑全量轮，插件不需要额外处理。**换凭证账号**时游标随之作废
  （游标绑定了账号），换账号后的那一轮是全量轮、不删任何文档——代价是新账号看不到的旧文档会一直
  留在 remote 上，需要时手动清理。**同一账号的权限被收窄**插件区分不了，它看不到的工单会被当作
  删除；收权限前请先清除该实例的游标（同样会留下看不到的旧文档）。
- **列表不完整时一律不删**。删除是从「这一轮没看到」推断出来的，所以每轮先把范围内的工单完整
  列完再产出任何东西；任何一页失败、分页字段矛盾或重复，本轮直接以可重试错误结束，不产出删除
  和移动，游标也不推进，下一轮从原位置重来。
- **取全文失败的工单下一轮重试**：本轮报条目级错误，新游标里保留它上一轮的记录；移动的目标
  取不到或装不进单条消息时连移动也不发。
- **游标装不进单条消息**时插件不给游标，运行器保留旧游标、下一轮从旧位置重扫；范围不缩小就会
  一直如此，需要调大运行器的单条消息上限。

## 错误处理

- 握手时探测凭证并用一次搜索检查范围：凭证被拒、JQL 不合法或项目不存在、站点不是 Jira API
  都以 `-32005` 拒绝（需要改配置）；探测时的网络失败与限流不算配置问题，留给 scan 报告——
  域名解析失败、TLS 证书错误也归为网络失败，所以站点写错域名不会在握手时以 `-32005` 暴露；
- 429 / 503 按 `Retry-After` 退避重试（最多 4 次，单次最长等 60 秒）；耗尽、网络失败或 5xx 时
  本轮以可重试的 `-32003` 中止，由运行器退避后重来；凭证失效、请求被拒或响应不符协议时同样
  中止，但标为不可重试；
- 枚举之后被删的工单产出条目级 `source_unavailable`；内容无法映射产出 `content_invalid`。
