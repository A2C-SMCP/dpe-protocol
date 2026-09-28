# dpe-protocol（Python）

DPE（Document / Page / Element）投递的**协议层** SDK：以 Document 为边界，把上层产出的 DPE 文档通过 **dpe-push/1** 协议增量投递到 TFRobot 的 Memory。

飞书、网盘、三方系统等对接，以及 connector 的形态、何时拉取、拉取哪些，都由上层负责，本包不涉及。

- 符合 TFRobotServer `docs/protocol/dpe/push-protocol-v1.md` 与 `hash-contract-v1.md`
- 三层内容寻址 hash 与 TFRobot 内核逐字节一致，测试向量由内核生成
- 只传服务端缺失的内容；超过请求上限时自动分片

设计文档：[docs/design.md](../docs/design.md)。Rust 版本见 [rust/](../rust/)。

## 上层如何使用

**投递**：`StatefulPusher` 会自动带上次的 `base_doc_hash`，并记录源侧 fingerprint。

```python
from dpe_protocol import DPEPushClient, JsonFileStateStore, StatefulPusher, make_dpe_uri

async with DPEPushClient("https://robot.example.com", token="<JWT with dpe:push>") as client:
    pusher = StatefulPusher(client, JsonFileStateStore(".dpe-state/feishu.json"))
    for item in await feishu.list_changed():                 # 上层逻辑
        uri = make_dpe_uri("feishu-acme", f"wiki/{item.token}")
        if await pusher.needs_push(uri, item.revision):      # fingerprint 未变则跳过拉取
            await pusher.push(await feishu.build_document(item), fingerprint=item.revision)
```

也可以不用状态：`result = await client.push(doc, base_doc_hash=last_doc_hash)`，自行保存 `result.doc_hash`。

**自检**：对同一份未修改的源数据产出两次文档，在 CI 里做一致性检查。

```python
from dpe_protocol.testing import check_documents

first, second = await build_doc(item), await build_doc(item)
(await check_documents(first, second, expected_file_uri=uri)).raise_for_issues()
```

文档必须满足：`file_uri` 与 `page.number` 稳定、`doc_metadata` 确定、每次都是完整文档。详见设计文档 §4。

示例脚本见 `examples/push_markdown_dir.py`、`examples/push_dpe_json.py`。

## 目录结构

```
src/dpe_protocol/
├── schema/        DPE 数据模型（含 Document.from_kernel_dump）
├── hashing/       hash-contract-v1
├── push/          dpe-push/1 客户端（协商、增量、分片、幂等、重试）
├── uri.py         make_dpe_uri
├── validation.py  check_document
├── state.py       投递状态存储
├── pusher.py      StatefulPusher
└── testing/       FakeRobotServer、check_documents
examples/          示例脚本（不属于库 API）
```

## 开发

```bash
cd python
uv sync                       # 创建 .venv 并安装依赖
uv run pytest                 # 含共享内核向量 ../vectors/v1
uv run ruff format --check . && uv run ruff check . && uv run mypy src tests examples
```

## License

MIT
