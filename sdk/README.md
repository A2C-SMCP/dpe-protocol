# SDK

两份独立的 SDK 实现，行为对等（CLAUDE.md「两个 SDK 行为对等」）：

- `python/`：uv workspace，两个可独立发布的包——`dpe-hash`（零运行时依赖的 hash 核心，内核只依赖它）与 `dpe-sdk`（协议核心、传输、推送、testing、dpe-run）。见 [python/README.md](python/README.md)。
- `rust/`：对等 cargo workspace，两个可独立发布的 crate——`dpe-hash`（hash 核心与契约常量，仅依赖 sha2 + serde + serde_json + ryu）与 `dpe-sdk`（sans-IO 协议核心，默认 feature 提供 reqwest / tokio 适配）。见 [rust/README.md](rust/README.md)。

两个 SDK 都按 `spec/` 独立实现，不共享代码、不 import 生成器或内核；测试**直接读取仓库根目录的 `vectors/`**（不复制），逐字节校验。SDK 测试失败而 `make check-vectors` 通过时，错的是 SDK。

**Rust 消费方注意**：喂给 hash 的 JSON 若含浮点，解析必须正确舍入——serde_json 需开 `float_roundtrip` feature，否则极端指数下差 1 ULP，JCS 序列化结果与规范不一致（向量 `jcs_numbers` 的 `1e-30` 用例即为此而设）。dpe-hash 已为 serde_json 开启该 feature 与 `arbitrary_precision`（越界数值留到校验第 4 步再拒绝，core §2.8）；后者在同一构建内统一生效，`#[serde(flatten)]` 与 untagged enum 遇到数字会反序列化失败，Rust 侧模型须避开这两种写法。
