# dpe-sdk

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的 Python SDK：把结构化文档正确、增量、可靠地投递到任意 DPE 服务端。

- sans-IO 协议核心 + httpx 传输适配（sync / async）；
- 增量推送器 `StatefulPusher` / `AsyncStatefulPusher`（本地状态只作缓存，冲突如实上报、绝不自动 force）；
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

- 校验全部交给 `dpe-hash`，顺序与错误码遵循 core §2.8；封闭 schema、category 允许的字段、file_type 的语法校验都取自 `dpe_hash`（file_type 是开放取值，未登记但语法合法的取值照常接受）；
- 构造、`model_validate`、`model_validate_json` 失败时抛 `dpe_hash.DpeHashError`，带规范错误码 `code` 与违例位置 `path`（RFC 6901）；`model_validate_json` 按 I-JSON 严格解析（拒绝重复键）；
- 模型保留输入的原样表示，序列化只输出显式给出的字段；null 与缺省、metadata 缺省与 `{}` 是否等价由 hash 判定（core §2.7）。

## 协议错误与响应报文

- `dpe_sdk.errors`：core §6 的每个错误码一个异常类（基类 `DpeError`），带 `code` 与 `retryable`（只表示原样重试可能成功）；`from_problem(problem, retry_after)` 按 problem 体的 `code` 构造异常（含 `missing` 清单与 `Retry-After`），不按 HTTP 状态码分派；core §6 之外的码构造为 `UnknownCodeError`；
- 客户端本地判定不经 `DpeError`：本地判定为不合法时（如 `get_skeleton`、`commit`、`delete`、`move` 的 URI 不符合 core §1.1，`base_hash` 不是本次声明契约的 hash）在发请求前即抛 `dpe_hash` 的错误（`ValidationError` / `ContractUnsupportedError`，均为 `ValueError` 子类，带规范错误码）；响应不合规抛 `UnexpectedResponseError`；force 提交的响应丢失且事后 `head` 未能确认生效时抛 `ForceNotConfirmedError`（SDK 不重放 force，core §5.2）。捕获 `DpeError` 的调用方请按需一并处理；
- `dpe_sdk.wire`：各端点的响应模型（`Capabilities`、`Head`、`Skeleton`、`ListPage`、`CommitResult` 等）；信封对未知成员宽容，内嵌的三层对象仍是封闭 schema。

## 传输适配（httpx）

`dpe_sdk.transport` 把 sans-IO 核心接到 httpx 上：`DpeClient`（同步）与 `AsyncDpeClient`（异步）一对一包装 `ProtocolCore` 的全部操作。传输层只做 I/O——拼 URL（remote 之后接核心产出的已编码 target，不再编码）、注入凭证、按策略 gzip、还原响应；协议判定与重试规则全部在核心（分工细节见模块 docstring）。

```python
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.protocol import BaseHash, IfAbsent
from dpe_sdk.transport import DpeClient

with DpeClient(
    "https://example.com/anything/r/42",  # remote：base URL，不含查询串与凭证
    credential=StaticCredentialProvider("Bearer <token>"),
) as client:
    created = client.commit("s3://bucket/a.md", document, IfAbsent())
    client.deliver("s3://bucket/a.md", document, BaseHash(created.doc_hash))
    conf = client.capabilities().limits  # capabilities 快照（契约与 limits 与它绑定）
```

- **remote**：只接受 http / https、有主机名、无查询串与片段、不带凭证信息（凭证走 `CredentialProvider`）；capabilities 在实例内快照一次，服务端变更能力后新建客户端；
- **凭证**：`CredentialProvider`（`dpe_sdk.credentials`）在每个请求发送前调用一次（传输层内部的重发同样重新取，轮换中的短时凭证在长等待后仍可用），返回 `Authorization` 头的完整值（含方案名）；SDK 不感知鉴权方案，也不解释凭证内容；
- **gzip**：服务端 capabilities 声明 `gzip`、请求体是 `application/json`、不小于 1024 字节且不是分块请求（带 `Content-Range`）时才压缩；分块上传按规范（§7）MUST NOT 压缩；
- **重试**：传输层只自行重发三类——`retry_safe` 的请求（只读操作与断点查询，含走 POST 的 `batch_head`）遇上的网络失败、任何请求上确定未发出的建连失败、以及可重试的 DPE 错误（`DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`，按 `code` 的 `retryable` 判定）按 `Retry-After`（秒数或 HTTP 日期）等待后原样重发（等待上限 5 分钟，更长则抛给调用方）。写请求可能已发出后的超时与响应丢失一律抛回核心，由核心按 core §5.2 处理，**绝不自动 force**；
- **异常**：httpx 的连接失败、超时等异常原样抛出、按类型区分，不包装成协议错误；
- **依赖边界**：`dpe-hash` 保持零依赖；httpx 是 `dpe-sdk` 的运行时依赖，且 `dpe_sdk` 顶层不 import 传输模块（按需 `from dpe_sdk.transport import …`）。

## 媒体上传辅助（#15）

`dpe_sdk.media` 把调用方显式给出的字节来源适配成核心要的 `BlobSource`（`sha256:` 引用 + 总长 + 任意偏移读取）。**只做来源适配**——分块、断点续传与「400 + `DPE-Upload-Offset` 重同步」都在核心（`ProtocolCore.upload_blob` / `deliver` 的 `blobs` 参数）。来源一律由调用方显式给出：SDK 不解析 url、不识别 metadata 里的路径字符串；内存字节直接用 `BlobSource.from_bytes`。

```python
from dpe_sdk import Document
from dpe_sdk.media import source_from_path, source_from_read_blob, source_from_stream
from dpe_sdk.protocol import IfAbsent

image = source_from_path("diagram.png")  # 本地路径：分块流式哈希，不整读进内存
document = Document.model_validate(
    {
        "file_type": "md",
        "pages": [
            {"elements": [{"category": "Image", "blob": image.ref, "mime_type": "image/png"}]}
        ],
    }
)
client.deliver("s3://bucket/a.md", document, IfAbsent(), blobs=lambda ref: image)

# connector 契约 §6.7 的 read_blob（句柄已绑定）：read(offset, max_bytes) -> (data, eof)
source = source_from_read_blob(read_blob_call, expected_size=20480)
source = source_from_stream(open("big.bin", "rb"))  # 可 seek 的句柄
```

- **内存不随文件大小增长**：路径与流分块流式哈希（内存 O(`chunk_bytes`)）；`read_blob` 的来源分两趟——先分块读一遍算出摘要与总长（契约 §6.7：摘要与总长确定后才可开始上传），`read` 再按偏移请求同一份快照（句柄保证任意偏移、同一偏移结果一致），不整读、不落盘；
- **只在缺 blob 时触碰来源**：`deliver` 只对缺失的 blob 调用 `blobs`——未变文档（`unchanged`）完全不读取来源（大文件与 `read_blob` 往返都可能很贵）。注意来源在**构造时**就完成哈希：大文件与 `read_blob` 来源应把构造放进回调（`blobs=lambda ref: source_from_path(...)`）——未变文档连哈希都不会发生，前提是 `ref` 不靠本次构造取得（例如来自上一次投递）；需要先构造拿 `ref` 时，把同一个来源复用给回调（如上面的示例）；
- **不替调用方解引用**：url 与凭证属于 connector（契约 1 §4.2）；不可 seek 的流请改用 `read_blob` 回调或先落为临时文件（`source_from_stream` 会如实拒绝）。

## 增量推送（StatefulPusher）

`dpe_sdk.pusher` 面向 connector / 上层：给它一批文档，只推送变化的部分。判定机制按 connector 契约 §7.2 实现（同步状态、前置条件判定表、意图日志）；冲突处置策略归调用方。

```python
from dpe_sdk.pusher import MemoryStateStore, StatefulPusher

with DpeClient("https://example.com/anything/r/42") as client:
    pusher = StatefulPusher(client, state=MemoryStateStore())  # 状态只作缓存；落盘实现由调用方注入
    results = pusher.push({uri: document for uri, document in batch})
    for result in results:
        print(result.status)  # created / updated / unchanged / achieved / conflict / failed
```

- **只推变化**：本地算 doc_hash（N）与 `batch_head` 读到的 remote 值（R）比较；R == N 记 `achieved` 并跳过写入。快路径 / 逐层协商的分界完全复用核心 `deliver`（inline 请求体 ≤ `capabilities.limits.max_payload_bytes`），本模块不引入任何阈值；`concurrency` 只限制并发度（各篇独立）；
- **同步状态 + 意图日志**（§7.2）：同步状态（L）是每篇文档最近一次确认的 remote doc_hash，决定写入的前置条件（`if_absent` / `base_hash`）；写入前先把意图持久化到 `StateStore`（`save` 返回后才发写请求），结果确定即清除、只有结果未知（`unknown=True`）才保留，下一轮按意图对账恢复。缺省 `MemoryStateStore` 只在进程内、**不保证崩溃持久**；落盘实现属运行器的状态目录（契约 §4.4）；
- **冲突如实上报**：`status == "conflict"`，带读到的（或冲突后重读的）remote 状态；**绝不自动 force、不自动重提**——`source_wins` / `report` 等策略与轮内重试上限归调用方，重提用 `expected_base` 覆盖前置条件（值为 `None` 表示确认「不存在」）；
- **错误与凭证**：复用 `DpeClient` 的传输层（gzip、`Retry-After`、`CredentialProvider` 全部适用）；失败按异常原类型如实回报，`unknown=True` 表示结果未确定——由核心的 `outcome_unknown` 判据给出（写请求任何一次尝试可能已到达服务端即未知，含「首发丢响应后重发得到 DPE 错误或建连失败」等序列），此时意图保留、下一轮对账恢复；
- 本版本覆盖 commit 路径（create / update / 冲突上报）；delete / move 意图随 #14 的后续部分提供。

## 参考服务端（testing）

`dpe_sdk.testing.Engine` 是内存版 DPE 服务端的核心引擎（不含传输），按规范的求值顺序实现 core 语义（含暂存会话与分块上传，#43），供集成测试与第三方实现对照：

```python
from dpe_sdk.testing import Engine, EngineConfig, IfAbsent

engine = Engine(EngineConfig(max_payload_bytes=1 << 20))
body = b'{"document": {"file_type": "md", "pages": []}}'
engine.commit(
    "caller", "s3://bucket/a.md", body, "dpe1", IfAbsent()
)  # CommitResult(status="created", …)
engine.head("caller", "s3://bucket/a.md", "dpe1")

# 暂存路径：negotiate → upload_*（页 / 元素 / blob，支持分块与断点查询）→ commit 引用会话
session = engine.negotiate("caller", negotiate_body, "dpe1")  # NegotiateResult
engine.upload_page("caller", session.staging_session.id, page_hash, page_bytes, "dpe1")
engine.upload_offset(
    "caller", session.staging_session.id, page_hash, "dpe1", kind="page"
)  # 断点查询，不续期
```

- 带 JSON 请求体的操作（`batch_head`、`negotiate`、`commit`、`move`）接收原始字节，传输层上限、I-JSON、契约声明、请求信封、对象校验、授权、前置条件都在引擎内按 core §3.3、§3.4、§5.2 的顺序判定；`upload_page` / `upload_element` / `upload_blob` 按 HTTP 绑定 §4.6、§4.7 的两套阶梯处理，结果为 `UploadResult`（含续期后的 `expires_at` 与下一层缺失清单）；
- negotiate 与上传响应的缺失清单、commit 的可得性判定使用同一去重范围——固定为本文档（当前状态引用的对象 ∪ 本次暂存会话，core §3.3，不做跨文档比较）；可配置限额、受支持契约（第一个为主契约）与可插拔授权器（`can_write` / `can_force`）；
- `EngineConfig` 的 `clock` 与 `session_id_factory` 可注入（会话过期与确定性测试）；
- 所有接受 file_uri 的入口按 core §1.1 校验与规范化（`dpe_hash.normalize_file_uri`），规范化形式即身份；`list` 的前缀按原样匹配；
- 错误以 `dpe_sdk.errors` 抛出（分块偏移不连续为 `UploadOffsetError`，带 `offset`）。绑定层无法解释的条件头与分块参数以 `InvalidPrecondition` / `InvalidChunk` 传入，在规范规定的那一步判定。

### HTTP 绑定（ASGI）

`create_app(engine, prefix=…)` 按 [HTTP 绑定](https://doc.turingfocus.cn/dpe/) 把引擎暴露为 ASGI 应用（#44），可挂到 httpx 的 `ASGITransport` 上做零网络测试：

```python
import httpx
from dpe_sdk.testing import Engine, create_app

app = create_app(Engine(), prefix="/r/1")
async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
    await client.get("http://test/r/1/capabilities")
```

- `authenticate`：`Authorization` 头 → 调用者身份，返回 `None` 时 401（缺省放行，身份取头原值）；`public_url`：反向代理之后的对外 base URL（move 的 `Content-Location`）；`max_threads`（缺省 64）：本应用专用的工作线程上限，引擎、授权器与认证器都在其中运行——它们经 HTTP 回调本应用时每层嵌套各占一个线程，并发请求数 × 嵌套深度须小于该上限；`prefix` 只能含无需百分号编码的 path 字符；
- 独立监听（Rust SDK 集成测试、conformance 跑分器的对手）需要 extra `server`：

  ```bash
  pip install 'dpe-sdk[server]'
  python -m dpe_sdk.testing --port 0 --prefix /r/1   # stdout 第一行：{"url": "http://127.0.0.1:PORT/r/1"}
  ```

  可用 `--max-payload-bytes` 等参数调整限额；钩子见下。

### 一致性测试钩子（#45）

一致性测试需要服务端配合的地方（多身份授权、服务端自身写入、会话过期、多契约、确定性会话 id）
由**启动配置**提供。它们**不进入 DPE 协议面**（capabilities 与全部端点不变），默认全部关闭；
不启用时行为与规范默认一致。

```python
import itertools

from dpe_sdk.testing import (
    AdjustableClock,
    Engine,
    EngineConfig,
    HooksConfig,
    PrefixAuthorizer,
    create_app,
)

counter = itertools.count()
clock = AdjustableClock()  # 可拨动的时钟：真实时间 + 可累计的偏移
engine = Engine(
    EngineConfig(
        clock=clock,  # ③ 会话过期与续期用它
        contracts=("dpe1", "dpe2"),  # ④ 同时声明两个契约（契约升级期）
        session_id_factory=lambda: f"st-{next(counter)}",  # ⑤ 确定性且互不重复的会话 id
        authorizer=PrefixAuthorizer(  # ① 多身份与按 URI 前缀的写授权
            {"Bearer full": ("",), "Bearer limited": ("dpe://docs/a/",)},
            force=frozenset({"Bearer full"}),
        ),  # 空前缀 "" 匹配任意 URI；未列出的调用者不可写
    )
)
app = create_app(engine, prefix="/r/1", hooks=HooksConfig(path="/__hooks__", clock=clock))
```

`HooksConfig.clock` 必须是注入引擎的那**同一个**实例（`create_app` 会校验）。各钩子的驱动方式：

| 钩子 | 驱动方式 |
| --- | --- |
| ① 多身份 / 前缀授权 / force | 无需钩子通道：按 `Authorization` 头发 DPE 请求（身份 = 头原值），授权由 `PrefixAuthorizer` 判定 |
| ② 服务端自身写入（core §2.4 同级写入） | `POST {hooks}/self-write` |
| ③ 会话强制过期 | `POST {hooks}/expire-session` |
| ③ 拨钟（精确断言续期与过期；参考服务端专有，跑分器不依赖它） | `POST {hooks}/clock/advance` |
| ④ 同时声明 dpe1 与 dpe2 | `EngineConfig.contracts` |
| ⑤ 可注入会话 id | `EngineConfig.session_id_factory` |

```bash
# ② 服务端自身写入：请求体是 commit 请求体加 file_uri 与前置条件（base_hash / if_absent / force
#    至多其一，缺省即 428；前置条件在请求体里，冲突取 409，条件头才是 412）。同一求值顺序、
#    同样受 CAS 约束；服务端身份只绕过对外授权。
curl -X POST http://127.0.0.1:8000/__hooks__/self-write -d '{
  "file_uri": "s3://bucket/a.md",
  "document": {"file_type": "md", "pages": []},
  "if_absent": true
}'   # → 201/200 {status, doc_hash, delta}（与 DPE commit 同形容）；错误为 problem+json

# ③ 让会话立即过期：之后的 negotiate / upload / commit 用它一律 DPE_SESSION_EXPIRED
curl -X POST http://127.0.0.1:8000/__hooks__/expire-session -d '{"session_id": "st-0"}'

# ③ 拨钟：「闲置超过 TTL 的会话过期」与「续期 = 此刻 + TTL」都可精确断言
curl -X POST http://127.0.0.1:8000/__hooks__/clock/advance -d '{"advance_seconds": 3600}'
```

独立监听时钩子同样由命令行开启（`--hooks`、`--grant` / `--force`、`--contract`、
`--session-id-prefix`），公布的一行 JSON 会带上钩子通道的 base URL：

```bash
python -m dpe_sdk.testing --port 0 --prefix /r/1 --hooks /__hooks__ \
  --contract dpe1 --contract dpe2 \
  --grant 'Bearer full' '*' --grant 'Bearer limited' 'dpe://docs/a/' --force 'Bearer full' \
  --session-id-prefix st-test-
# {"url": "http://127.0.0.1:PORT/r/1", "hooks": "http://127.0.0.1:PORT/__hooks__"}
```

钩子通道是测试侧信道：不做 DPE 认证、默认关闭、只建议监听本机（缺省 127.0.0.1）。

> dpe-run（运行器与示例插件）尚在开发中（#16–#17）；增量推送的 delete / move 意图随 #14 的后续部分提供。
