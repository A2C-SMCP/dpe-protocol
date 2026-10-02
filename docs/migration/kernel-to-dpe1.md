# 内核现状（TFRobotV2）迁移到 DPE / hash 契约 1 的对照与待办

> 非规范文档。原为 `spec/hash-contract-1.md` §7，按 Issue #4 C2 移出规范正文，并按 plan §0.1 北极星原则（#4 评审中确定）改写。
> 内核现状基于 TFRobotV2 `a130da93`（见 Issue #3「内核现状速查」）。

## hash 规则对照

| 项 | 内核现状 | 契约 1 |
| --- | --- | --- |
| 值格式 | 截断 32 hex、版本记在旁路字段 | `dpe1:` + 完整 64 hex |
| doc_hash 范围 | 含 `file_uri`、`file_type`、title（恒空）、`json.dumps(doc_metadata)` | 含 `file_type`、可选 `title` 与 doc_metadata（null 键删除 + JCS）；无 file_uri |
| category | 不进 hash（基类只有 text） | 进 hash，封闭枚举，未知拒绝 |
| metadata | 不进 element / page hash | 三层 metadata 的**全部键**都进 hash，不设保留键、不做过滤 |
| 页号 | 只用于排序 | 不是 DPE 字段：页序即文档对象 `pages` 的数组顺序；内核的页号退化为存储层的位置序号（与 seq_in_page 地位相同），不进 hash |
| hash 结构 | 长度前缀拼接的扁平摘要 | 三层同构 tree：元素 / 页 / 文档对象各自 `sha256(JCS(对象))`（#6） |
| 图片 | url / base64 / path 取首个非空 | 字节以 `image_blob`（`sha256:` 引用）作为元素对象字段进 hash；url 等作为元素 metadata 进 hash |
| hash 策略 | 按文件类型选择（default / image-source） | 不存在策略，只由契约版本 + category 决定 |
| 结构化值 | Python `json.dumps` | RFC 8785 JCS |
| 升级行为 | 策略 URI 不同即全删全建 | 原位重算，身份不变，禁止重新配对 |
| 并发控制 | 按 file_uri 的 advisory lock，无版本 | doc_hash 即版本令牌（`If-Match: doc_hash`），不另设 revision 列 |

## 内核待办（北极星原则带来的变化）

- **衍生物移出 metadata**：全局指代字典、keywords、学习状态、会话键等服务端衍生数据，从 doc / page / element metadata 中移到独立的衍生物存储。DPE 字段中只留源内容，读回时除 core.md §2.7 的内容等价外一字不差。不再需要"SDK 保留键 ∪ 私有衍生键"的过滤集合。
- **元素 metadata 去掉位置冗余**：`page_number`、`seq_in_page` 等由骨架表达的位置，不再写入元素 metadata；否则它们会进 hash，插入一页时后续元素对象全部重传。版面坐标、图片 url 作为源内容保留在 metadata 中并进 hash。
- **补全页级 / 文档级刷新**：OntoIndex 的 `update_doc` / `update_page` 目前是空操作，这是缺陷。page_hash 或 doc_hash 变化（title、metadata、页序、元素序）都是内容变化，应刷新依赖该层的衍生物，重学效率由内核优化（plan §0.1 P3）。
- **页的配对改为按 page_hash 对齐**：页不再有页号身份。两阶段 LCS 中按页号匹配稳定页的逻辑（`compute_stable_page_numbers`，以及按页号预创建、删除页行的逻辑，同步与异步两份实现）改为在 page_hash 序列上对齐（TFROB-956）。
- **页码标签**：loader 产出的源页码标签（印刷页码、PDF PageLabels）写进 page_metadata；不再把由位置算出的序号写进任何 metadata。page_name 改写进页 title；Document 补 title 字段，映射到文档对象 `title`（源文档标题，不是 filename；TFROB-954 适配层）。
- **治理属性由服务端决定**：creator / group 等 ACL 不属于 DPE，由服务端依据调用者身份或 connector 实例配置写入，不经 commit、也不进 hash。
- **写入入口收窄**：DPE 字段只能经 commit 写入（服务端自身的修改也一样）；`aupdate_doc` 等绕过 commit 修改内容字段的入口应收掉。
