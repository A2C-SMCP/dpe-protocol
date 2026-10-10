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

## 协议错误与响应报文

- `dpe_sdk.errors`：core §6 的每个错误码一个异常类（基类 `DpeError`），带 `code` 与 `retryable`（只表示原样重试可能成功）；`from_problem(problem, retry_after)` 按 problem 体的 `code` 构造异常（含 `missing` 清单与 `Retry-After`），不按 HTTP 状态码分派；core §6 之外的码构造为 `UnknownCodeError`；
- 客户端本地判定不经 `DpeError`：本地判定为不合法时（如 `get_skeleton`、`commit`、`delete`、`move` 的 URI 不符合 core §1.1，`base_hash` 不是本次声明契约的 hash）在发请求前即抛 `dpe_hash` 的错误（`ValidationError` / `ContractUnsupportedError`，均为 `ValueError` 子类，带规范错误码）；响应不合规抛 `UnexpectedResponseError`；force 提交的响应丢失且事后 `head` 未能确认生效时抛 `ForceNotConfirmedError`（SDK 不重放 force，core §5.2）。捕获 `DpeError` 的调用方请按需一并处理；
- `dpe_sdk.wire`：各端点的响应模型（`Capabilities`、`Head`、`Skeleton`、`ListPage`、`CommitResult` 等）；信封对未知成员宽容，内嵌的三层对象仍是封闭 schema。

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
- negotiate 与上传响应的缺失清单、commit 的可得性判定使用同一去重范围；可配置限额、受支持契约（第一个为主契约）、去重范围（`DedupScope.DOCUMENT` / `WRITABLE`）与可插拔授权器（`can_write` / `can_force`）；
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

> 客户端协议核心、传输适配与增量推送尚在开发中（#12–#17）。
