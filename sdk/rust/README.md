# Rust SDK workspace

```
Cargo.toml            # workspace 根（不是 crate）：成员、共享元数据、SDK 版本管理与依赖预算
.cargo/config.toml    # resolver MSRV 回退：依赖解析优先选与 rust-version 1.80 兼容的版本
pyproject.toml        # bump-my-version（版本工具是 Python 的，与 sdk/python/ 同款两态约定）
crates/dpe-hash/      # dpe-hash：hash 核心、file_uri 规范化与契约常量，仅依赖 sha2 + serde + serde_json + ryu
crates/dpe-sdk/       # dpe-sdk：sans-IO 协议核心；默认 feature `http` 提供 reqwest / tokio 适配
```

两个 crate 与 Python SDK 行为对等（CLAUDE.md「两个 SDK 行为对等」）：测试**直接读取仓库根目录的 `vectors/`**（不复制），逐字节校验；crate 测试失败而 `make check-vectors` 通过时，错的是 SDK。向量位置可用 `DPE_VECTORS_DIR` 覆盖（如在仓库之外运行测试）。

## 开发

在本目录下运行（MSRV 1.80，edition 2021）：

```bash
cargo test --locked                             # 另可 cargo +1.80.0 test --locked 专门验 MSRV
cargo clippy --all-targets --all-features --locked -- -D warnings
cargo fmt --all --check
cargo check -p dpe-sdk --no-default-features --locked   # sans-IO 核心：不引入 reqwest / tokio
cargo run --release -p dpe-hash --example bench_hash    # 可选：10 万元素原位重算基准，参数 --pages / --per-page / --repeat
```

CI（`.github/workflows/rust-sdk.yml`）先跑 `make check-vectors`，再跑 lint（stable）、测试（1.80.0 + stable 矩阵，`--locked`）、打包检查与原位重算基准（只报告、不设门槛，写进 job summary）；toolchain 由 `actions-rust-lang/setup-rust-toolchain` 提供。

打包与发布面：

```bash
cargo publish --dry-run -p dpe-hash --locked   # 完整演练（打包 + 验证构建 + 发布面检查）
cargo package --list -p dpe-sdk                # dpe-sdk 的打包面检查
```

> **dpe-sdk 的完整 dry-run 顺延到 dpe-hash 首发之后**：crates.io 上还没有 dpe-hash 时，cargo 对依赖它的 dpe-sdk 连发布面都无法准备（`no matching package named dpe-hash found`）。发布顺序必须是 dpe-hash → dpe-sdk；`cargo publish` 会等待 crate 进入索引后再返回，因此顺序发布不需要任何额外等待。dpe-hash 首发（#19）后立即补跑 `cargo publish --dry-run -p dpe-sdk`。

## 版本与发布

SDK 版本是独立的一条轴，与文档版本、协议版本、hash 契约版本、Python SDK 版本互不牵动；两个 crate 始终同版本（`[workspace.package]` 单一来源），由本目录的 bump-my-version 管理：

```bash
uv run bump-my-version bump pre_l        # X.Y.Z-dev → X.Y.Z：同步 dpe-hash 依赖串与 Cargo.lock，commit + 标签 rs-vX.Y.Z
git push --atomic origin main rs-vX.Y.Z  # 原子推送：main 被拒时标签也不会推上去
uv run bump-my-version bump patch        # 开下一周期：X.Y.Z → X.Y.(Z+1)-dev（minor / major 同理）
```

- 标签触发 `release-rust.yml`：先跑与 PR 相同的完整门禁，再校验标签指向 main 上的提交、标签版本与 `Cargo.toml` 一致，然后按序发布 dpe-hash → dpe-sdk；
- 发布门是 GitHub environment `crates-io`（可设 required reviewers，与 PyPI 的审批门同款）；若改 environment 名或 workflow 文件名，须同步改 crates.io 侧的 Trusted Publishing 登记；
- 开发版标签 `rs-v*-dev` 不触发发布。

### crates.io 首发 bootstrap（与 PyPI 不同）

crate 不存在时 crates.io 无法登记 Trusted Publishing，**首发必须用 API token**：

1. 在 crates.io 创建 API token，配成仓库（或 `crates-io` environment）secret `CARGO_REGISTRY_TOKEN`——存在 token 时发布走 token；
2. 首发成功后，在 crates.io 的每个 crate → Settings → Trusted Publishing 登记：仓库 `A2C-SMCP/dpe-protocol`、workflow `release-rust.yml`、environment `crates-io`；
3. 删除 `CARGO_REGISTRY_TOKEN`——此后发布自动走 OIDC（[`rust-lang/crates-io-auth-action`](https://github.com/rust-lang/crates-io-auth-action) 换短时 token），不再有长期凭证。
