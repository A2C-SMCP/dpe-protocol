# 示例插件：Jira Cloud（jira）

> **这是示例，不发布，也不是官方 connector**（官方的是 [`connectors/git`](../../connectors/git/)）。
> 它用来验证 Git connector 覆盖不到的 DPE 能力：大量小文档、数据源凭证、易变内容、限流、删除与
> 移动、网络来源的 blob。设计见 [Issue #223](https://github.com/A2C-SMCP/dpe-protocol/issues/223)。

把 Jira Cloud 的工单与项目映射为 DPE 文档的 **dpe-run 插件**（[connector 契约](../../spec/connector-contract.md) §6）。
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
| `include_project_docs` | 否 | 是否产出项目文档，缺省 `true`（见下文「项目文档」） |

凭证 `JIRA_EMAIL` / `JIRA_API_TOKEN`（Basic 认证）由运行器按清单注入，插件不回显它们。

### 对真实 Jira 手动运行

CI 只跑模拟 Jira。要对真实站点试一试（插件只读 Jira，不会写回）：

1. 在 <https://id.atlassian.com/manage-profile/security/api-tokens> 创建一个**不带权限范围**的 API
   token（带 scopes 的 token 要走 `api.atlassian.com` 网关地址，插件用的是站点地址）。想再加一道只读
   保险，就用一个在目标项目里只有浏览权限的账号来创建；
2. 把邮箱与 token 各写进一个只有自己可读的文件（例如 `~/.config/dpe-jira/email`、`token`，权限
   `600`），实例定义的 `secrets` 用 `{"file": "<绝对路径>"}` 引用——不要把 token 写进实例定义或命令行。
   相对路径以实例定义所在目录为基准，`~` 不会展开，所以 `secrets` 与 `plugin.manifest` 都写展开后的
   绝对路径；
3. 起一个本机内存版参考服务端当 remote，真实内容不离开本机：
   `uv run python -m dpe_sdk.testing --port 0`，把它输出的 JSON 里的 `url` 填进 `remote.url`；
4. 用一个小范围先跑，例如 `"jql": "project = \"EXAMPLE\" AND updated >= \"2026-01-01\""`；
   `uri_prefix` 必须是规范化不动点（scheme 与主机小写，如上面的 `jira://example.atlassian.net/`）；
5. `NO_PROXY=127.0.0.1,localhost uv run python -m dpe_sdk.run --state-dir <临时目录> <实例定义>`，
   标准输出是这一轮的报告。`NO_PROXY` 只影响运行器访问本机 remote；运行器不把 shell 里的代理变量
   传给插件，需要经代理访问 Jira 时，在实例定义的 `plugin.env` 里写 `HTTPS_PROXY`。
6. 查看推送上去的结果：DPE v1 读不回元素对象，但能列出文档、读出文档对象与页结构（每页的
   `page_metadata` 与元素 hash 列表），例如：

   ```python
   from dpe_sdk.transport import DpeClient

   with DpeClient("<参考服务端的 url>") as client:
       page = client.list_page("jira://example.atlassian.net/")  # next_cursor 不为 None 时继续翻
       for entry in page.documents:
           skeleton = client.get_skeleton(entry.file_uri)
           print(entry.file_uri, skeleton.document.title,
                 [(p.page_metadata, len(p.elements)) for p in skeleton.pages])
   ```

7. 跑完停掉参考服务端（数据随之消失），删掉临时目录，不再需要时撤销 token。

### 规模与开销

- **每轮固定开销**：`dpe-run` 每轮重启插件，握手时 2 个探测请求（`/myself` 与一次只取 1 条的搜索）；
  把增强搜索完整翻一遍（每页 100 个工单，约 ⌈工单数 / 100⌉ 个请求，只取 id、key、updated）；每个
  项目 3 个请求（项目、组件、版本，`include_project_docs: false` 时没有）；
- **变化开销**：每个新出现或 `updated` 变了的工单 2 个以上请求（全文、评论分页），图片附件另按
  `read_blob` 的块读取；
- **规模**：请求是串行的。在一次手动运行里，约 200 个工单的首轮全量约 3 分钟（约 1 秒一个工单），
  紧接着的无变化增量轮约 4 秒。按这个速度，几千个工单的首轮就要一小时左右，范围越大也越容易触发
  限流，所以这个示例适合几千个工单以内，更大的范围请拆成多个实例；
- **游标**随工单数线性增长（压缩后每个工单十几字节），在运行器缺省的单条消息上限下很难触及；万一
  放不进，运行器保留旧游标、下一轮从旧位置重扫，见「增量、删除与移动」。

### 手动端到端验证到了什么

对一个真实 Jira Cloud 项目做过只读验证（#223 §10）：

| 场景 | 结果 |
| --- | --- |
| 首轮全量 | 通过：全部条目 `created`，无失败 |
| 无变化 | 通过：增量轮没有任何写入 |
| 确定性 | 通过：同一参考服务端上两个互不重叠的空前缀各跑一次全量，`file_uri → doc_hash` 映射完全相同；增量实例在自己前缀下的映射也与之相同 |
| 项目文档 | 通过：三页结构正确 |
| 图片附件 | 通过：图片字节经 `read_blob` 读全并上传，长度与附件元数据一致；非图片附件只记元数据 |
| 凭证缺失 | 通过：`secret_missing`，退出码 3，插件不启动 |
| 凭证不外泄 | 通过：报告、日志与状态目录中都没有 token |

需要改动工单、只能在可随意修改的沙箱项目里做的场景**尚未对真实 Jira 验证**，目前只由模拟 Jira 的
测试覆盖：改描述、加评论、改字段、删除工单、换项目（移动）、Epic 与子任务、崩溃与恢复；429 限流
在真实运行中没有触发。

## 映射规则

- **文档 = 一个工单**：`file_uri` = 实例前缀 + `issue/<工单 key>`；`file_type` 为 `jira_issue`，
  `title` 为 summary，`doc_metadata` 只放不可变的 `{issue_id, created_at}`。
- **页**，`page_metadata.section` 依次为 `body`（描述）、`comments`（评论，按创建时间、再按评论
  id 在本地排序）、`fields`（固定字段，按固定顺序每个一个元素，值为空不产出），以及有附件时的
  `attachments`（见下文「附件」）。
- **ADF 转换**是只依赖节点结构的确定性规则（见 `src/dpe_jira/adf.py` 文档串），换一种语言照着
  实现能得到逐字节相同的元素。
- **名称会随语言变化**：状态、优先级、类型、解决结果的名称按凭证账号的语言设置返回，因此
  `metadata.value` 同时记 `{id, name}`。切换账号语言会让这些元素的内容变化——这是真实的内容变化，
  不是插件的不确定性。

## 项目文档

- **一个项目一篇**：`file_uri` = 实例前缀 + `project/<项目 key>`；`file_type` 为 `jira_project`，`title`
  为项目名，`doc_metadata` 为 `{project_id}`。三页：`description`（项目描述）、`components`（按组件
  id 升序，每个组件一个元素）、`versions`（按 Jira 里排定的版本顺序，每个版本一个元素，`metadata`
  记是否已发布、是否已归档与发布日期）。**不列工单清单**：清单由工单推导而来，不是项目自己的内容。
- **哪些项目**：配置 `projects` 时就是这些项目；配置 `jql` 时是本轮枚举到的工单所属的项目，所以一个
  项目的最后一个工单离开范围后，项目文档也会被删除。
- **增量**：项目没有更新时间，插件每轮重取项目、组件与版本（每个项目 3 个请求，即使什么都没变），
  按映射出的内容摘要判断是否变化；项目改 key 表达为移动。用 `jql` 覆盖大量项目时这部分开销与限流
  风险会明显增加，可以关掉 `include_project_docs`。项目在枚举时读不到，本轮整体失败、不当作删除：
  配置里写明的项目读不到标为不可重试（需要改配置），按工单推出的项目读不到视为暂时情况。
- 把 `include_project_docs` 从 `true` 改成 `false` 是配置变化，运行器会改跑全量轮，已有的项目文档
  **不会被删除**，需要时手动清理。

## 附件

- 附件单独成第 4 页 `attachments`，按附件 id 升序每个附件一个元素；没有附件不产生这一页。增删
  附件只改这一页，正文、评论、字段页不受影响。
- **图片**（扩展名 png、jpg / jpeg、gif、webp、bmp，按固定表定 `mime_type`，不取 Jira 给的类型）
  为 `Image`：`text` 是文件名，`blob` 是句柄（附件 id）加附件元数据里的大小，字节由运行器经
  `read_blob` 分块读取后上传。**其他附件**为 `NarrativeText`，只记文件名、类型与大小，不传字节。
- **不在引用位置关联**：描述或评论里内嵌的图片节点只产出不带字节的 `Image` 占位。Jira Cloud 没有
  公开这些节点与附件的对应关系（节点上的 id 是 Media Services 的 id，不是附件 id），按文件名猜或
  依赖渲染后的 HTML 都不确定，所以字节统一放在附件页。
- **读取**：用 HTTP Range 按偏移读；Jira 把下载重定向到媒体服务时手动跟随，只有与站点同源、且
  这条跳转链还没离开过站点的请求才带凭证。服务端不支持 Range 时整份下载到临时文件再按偏移取，句柄失效（下一轮开始或关闭）时删除。
  读到的总长必须等于附件元数据里的大小，对不上即判读取失败、不可重试。
- **已知限制**：附件在后台被清理而工单的 `updated` 不变时，增量轮看不到这次变化，要等工单下次
  变化或一次全量轮。读取是同步的：服务端不支持 Range 时，首次读取要在一个请求里整份下载，很大的
  附件可能超过运行器的 `read_blob` 超时，期间插件也响应不了取消；这种附件每一轮都会因同样的原因
  失败，不会自己恢复。

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
