# 内核现状（TFRobotV2）迁移到 hash 契约 1 的对照

> 非规范文档。原为 `spec/hash-contract-1.md` §7，按 Issue #4 C2 移出规范正文：规范只保留与实现无关的内容。
> 内核现状基于 TFRobotV2 `a130da93`（见 Issue #3「内核现状速查」）。

| 项 | 内核现状 | 契约 1 |
| --- | --- | --- |
| 值格式 | 截断 32 hex、版本记在旁路字段 | `dpe1:` + 完整 64 hex |
| doc_hash 范围 | 含 `file_uri`、`file_type`、title（恒空）、`json.dumps(doc_metadata)` | 含 `file_type` 与经保留键过滤 + null 键删除 + JCS 的 doc_metadata；无 file_uri / title |
| category | 不进 hash（基类只有 text） | 进 hash，封闭枚举，未知拒绝 |
| metadata | 不进 element / page hash | 源提供的 metadata 进全部三层 hash（经保留键过滤） |
| 随位字段 | `coordinates`、`image_url`、`parent_id` 等与内容 metadata 存在一处 | 保留键过滤后不进内容 hash；线上经骨架 entry 的 `occurrence` 投递，进 state_hash。内核适配层按 SDK 导出的 `occurrence_keys` 拆分 / 合并 |
| 投递状态 | 无；只改访问控制字段时 doc_hash 相等，走「只写 creator_id / group_id」分支 | 新增 state_hash 覆盖 attributes 与 occurrence；unchanged ⇔ state_hash 相等 |
| 页号 | 只用于排序 | 进 page_hash |
| 图片 | url / base64 / path 取首个非空 | 身份只认 `blob:sha256`，url 为随位访问方式 |
| hash 策略 | 按文件类型选择（default / image-source） | 不存在策略，只由契约版本 + category 决定 |
| 结构化值 | Python `json.dumps` | RFC 8785 JCS |
| 升级行为 | 策略 URI 不同即全删全建 | 原位重算，身份不变，禁止重新配对 |
| 私有衍生键 | 全局指代字典等写在 doc_metadata / page_metadata | 由内核在适配层以「SDK 保留键 ∪ 内核私有衍生键」过滤；私有集合只能含服务端自己写入的键（契约 1 §2.1） |
