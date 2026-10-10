# CLAUDE.md

DPE（Document / Page / Element）**标准协议**的权威仓库：规范、hash 契约、一致性向量、服务端跑分器、connector 契约、两份 SDK 都在这里。类比 Git 之于 GitHub / GitLab / CNB：任何服务端都能实现 DPE，**TFRS 端点只是第一个实现**。

**当前依据**：`docs/plan/v1-plan.md`（CTO 对 Issue #1 的评审定稿稿）。行为与计划冲突时以计划为准；对计划或规范的异议另开 Issue（须带章节号）。**当前阶段：M1 规范已定稿（2026-10-06，文档版本 0.1.8），M2 进行中**（SDK、跑分器、参考服务端）。初版 SDK 留在 `archive/initial-sdk` 分支，仅供参考。

## 结构

- `spec/core.md`：抽象模型与操作语义（与传输无关）
- `spec/hash-contract-1.md`：hash 契约 1（`dpe1:`）
- `spec/bindings/http.md`：HTTP 绑定（v1 唯一规范性绑定）
- `spec/connector-contract.md`：中立 connector 契约（独立规范，全文定稿；配置与凭证见 §4，运行边界见 §6，错误与可观测见 §7）
- `vectors/`：一致性向量（规范的一部分）
- `scripts/gen_vectors.py`：向量生成器 = hash 契约与 connector §4.1.1 `config_schema` 正则子集向量的**规范参考实现**（仅标准库；子集实现见同目录 `pattern_subset.py`）
- `scripts/pattern_subset.py`：connector §4.1.1 正则子集的规范参考实现（文法解析、合法性判定、显式码点区间转译）
- `scripts/pattern_budget/`：§4.1.1 结构上界的资源余量探针（独立 cargo 工程，不进 CI）
- `conformance/`：黑盒 HTTP 跑分器（M2）
- `sdk/python/`：uv workspace，`dpe-hash`（零依赖 hash 核心）+ `dpe-sdk` 两个包（M2，见 `sdk/python/README.md`）；`sdk/rust/`：对等 cargo workspace，`dpe-hash`（hash 核心）+ `dpe-sdk` 两个 crate（M2，见 `sdk/rust/README.md`）

## 命令

```bash
make vectors          # 重新生成向量（仅在规范变更时）
make check-vectors    # CI：校验已提交向量与生成器一致
uv run inv docs.serve # 本地预览文档站点（mkdocs strict，死链即失败）
# Python SDK（在 sdk/python/ 下）：uv sync / uv run ruff check / uv run mypy / uv run pytest
# Rust SDK（在 sdk/rust/ 下）：cargo test / cargo clippy --all-targets --all-features -- -D warnings / cargo fmt --check
```

## 版本管理

- **当前文档版本**：0.3.1-dev
- 文档版本单一来源是 `pyproject.toml`，只用 `bump-my-version` 修改（发布 `bump pre_l`，开周期 `bump patch|minor|major`）；它与协议版本（DPE v1）、hash 契约版本（`dpe1:`）独立，升契约版本不等于升文档版本。
- 多版本站点由 mike 管理在 `gh-pages` 分支，`inv docs.deploy` 发布到 doc.turingfocus.cn/dpe（默认 mode=upload）；`-dev` 版本只占 `dev` 别名，不得顶替 `latest`。
- **SDK 版本是独立的第四条轴**：两个 SDK 各自同版本、各自用所在目录的 bump-my-version 管理（同样 dev/final 两态），互不牵动——Python 两包发布标签 `py-vX.Y.Z` 触发 `release-python.yml`（PyPI Trusted Publishing）；Rust 两 crate 发布标签 `rs-vX.Y.Z` 触发 `release-rust.yml`（crates.io；首发 token bootstrap 后转 OIDC，见 `sdk/rust/README.md`）；`*-dev` 标签不发布；文档发版不牵动 SDK 版本。**SDK bump 会连带刷新以 path 引用 SDK 的独立 uv 项目锁文件**（`pre_commit_hooks` 自动完成，目前是 `connectors/git`；漏刷由 `python-sdk.yml` 的 `locks` 守卫拦截——它枚举仓库内全部已提交的 `uv.lock`，见 #106）。
- **不兼容变更同步升次版本**（0.x 阶段，#60 起）：规范或 SDK 出现不兼容变更（字段改名、hash 值变化等）时，文档与两个 SDK 一起 `bump minor`，保持相同的 `X.Y`；兼容变更只给受影响的一方 `bump patch`，补丁号允许不同。下游依赖按 `>=X.Y.0,<X.(Y+1)` 声明。
- 站点源是 `website/`（符号链接聚合 spec / docs / vectors / conformance），新增规范文档须同步 `mkdocs.yml` 的 nav。

## 不可破坏的约束

- **规范先行**：要改 hash 规则，必须先改规范，再改生成器重新生成向量。**何时必须升契约版本**：一旦存在落库数据，或者已发布的包、已部署的服务（含我方产品，如内核的已发布版本）依赖该契约的 hash 值，任何 hash 规则变更都必须升契约版本；协议仓自身发布的 `dpe-hash` / `dpe-sdk` 不算依赖。在此之前可在原契约内原位修订，文档与两个 SDK 同步 `bump minor`（见「版本管理」）。是否定稿不是判据。任何实现（含内核、TFRS）的现有行为都不能倒逼规范。
- **向量由本仓库产出**：生成器是 `scripts/gen_vectors.py`（不 import 任何 SDK / 内核代码），不得手改 `vectors/`。SDK 与各服务端实现只消费向量；SDK 测试失败而向量一致时，错的是 SDK。
- **依赖方向：内核 → Python SDK（#3 S1），SDK 运行时不依赖内核**。内核不保留自有 hash 实现，直接 import Python SDK 的 hash 核心；因此 M2 的 Python SDK 必须：hash 核心可单独安装（或主包零重依赖）、纯 Python（3.11 / 3.12）、导出契约常量（category 封闭枚举及各 category 允许的内容字段、file_type 推荐登记表与语法校验，见 core §2.5），PyPI 发布是内核 K2 的前置门禁。
- **规范与 SDK 中不出现任何服务端私有概念**：Robot、`vnd.tfrs`、`X-TFRS-Robot-Id`、tenant、TFRS 错误信封、JWT/scope 细节都不能进。SDK 只接受 remote URL + 可插拔 `CredentialProvider`。治理属性（ACL 等）不属于 DPE。
- **北极星原则（plan §0.1）优先于一切**：DPE 只表达内容面（P1）；源即内容，除 `file_uri` 外全部字段进 hash，不设保留键、不做过滤（P2）；hash 变化即内容变化，协议不为规避重学做设计（P3）；doc_hash 即版本令牌，不另设 revision（P4）；所有写入同级、经 commit 与 CAS（P5）。
- **字段只有一类：源内容**，组织为三层同构 tree（core §2、契约 1 §4–§5，对应 Git 的 tree / blob）：文档对象 `{file_type, title?, doc_metadata, pages: [page_hash…]}`、页对象 `{title?, page_metadata, elements: [content_hash…]}`、元素对象 `{category, 按 category 允许的内容字段, metadata}`，每层 hash = `sha256(JCS(norm(对象)))`。三层都是封闭 schema，新增字段必须先进规范，未定义字段一律拒绝；位置只由数组顺序表达（页没有页号），对象 hash 不含自身位置。`file_uri` 是身份、不进 hash；治理属性（ACL）与服务端衍生物（keywords、抽取产物、学习状态）不是 DPE 字段，由服务端在 DPE 之外独立存储；源字段不得带会自行变化的默认值。connector 契约 §4.1.1 的 `config_schema` 同样是封闭 schema：只允许列出的关键字，`$ref` 只有 `#/$defs/<名字>` 一种形式，`$defs` 仅在根且引用必须无环。
- **hash 值始终带契约前缀**（`dpe1:`），不暴露接受裸 hash 的公共 API；结构化值一律 RFC 8785 JCS，时间戳一律 RFC 3339 UTC。
- **写操作默认拒绝无 CAS 前置条件**（`DPE_PRECONDITION_REQUIRED`）；本地状态只作缓存，缓存丢失也必须正确，**绝不自动 force**。
- **两个 SDK 行为对等**（M2 起）：改一个 SDK 的协议行为必须同步另一个并补齐双方测试；两份实现出现分歧说明规范需要补正——修规范，而不是互相对齐。
- **偏离判据见 plan §13**：出现任一（伪变更、可见中间态、静默覆盖、契约升级引发重学、内核新增 diff 接口等）即为跑偏。

## 约定

- 文档与注释用中文，标识符用英文；关键词 MUST / SHOULD 按 RFC 2119。
- 规范草案中的待定项集中写在各文档末尾的「待评审决策点」小节，并在 plan §16 有登记的注明；有结论后并入正文对应章节、删去该条（全部关闭即删去整节），决策历史留在 plan 修订注记与 Issue 中。
- M2 起：Python ≥ 3.11（pydantic v2、sans-IO 核心、sync + async）；Rust ≥ 1.80（serde、sans-IO 核心不绑定异步运行时，默认 reqwest/tokio 适配）；`mypy --strict` / ruff / `cargo clippy -D warnings` / `cargo fmt` 必须通过。
