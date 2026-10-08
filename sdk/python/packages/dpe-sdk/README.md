# dpe-sdk

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的 Python SDK：把结构化文档正确、增量、可靠地投递到任意 DPE 服务端。

- sans-IO 协议核心 + httpx 传输适配（sync / async）；
- 只接受 remote URL 与可插拔的 `CredentialProvider`，不封装任何服务端私有端点；
- hash 由 [`dpe-hash`](https://pypi.org/project/dpe-hash/) 计算（同版本精确依赖）；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

## 数据模型

三层对象（core §2）是 pydantic v2 模型：线上对象 `ElementObject` / `PageObject` / `DocumentObject`（子对象以 hash 列表引用），以及展开视图 `Document` / `Page`（页与元素内联）。

```python
from dpe_sdk import Document, ElementObject, Page

doc = Document(
    file_type="md",
    title="A",
    pages=[Page(elements=[ElementObject(category="NarrativeText", text="hello")])],
)
doc.doc_hash()  # "dpe1:…"
doc.hashes()  # {"doc_hash", "pages": [{"page_hash", "elements"}…]}
```

- 校验全部交给 `dpe-hash`，顺序与错误码遵循 core §2.8；封闭 schema、category 允许的字段、file_type 枚举都取自 `dpe_hash` 的常量；
- 构造、`model_validate`、`model_validate_json` 失败时抛 `dpe_hash.DpeHashError`，带规范错误码 `code` 与违例位置 `path`（RFC 6901）；`model_validate_json` 按 I-JSON 严格解析（拒绝重复键）；
- 模型保留输入的原样表示，序列化只输出显式给出的字段；null 与缺省、metadata 缺省与 `{}` 是否等价由 hash 判定（core §2.7）。

> 协议核心、传输适配与增量推送尚在开发中（#12–#17）。
