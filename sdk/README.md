# SDK（M1 原型阶段）

当前只有两份**独立实现的 hash 核心**，用途是 M1 验收条件之一：「向量同时由两个 SDK 原型校验通过」。

- `python/`：`dpe-hash` 包——零运行时依赖、纯 Python（≥3.11），导出契约常量（`CATEGORY_CONTENT_FIELDS` 统一映射表、file_type 封闭枚举，见 #7），预演 #3 S1 要求的可单独安装形态。`python3 tests/test_vectors.py`
- `rust/`：`dpe-hash` crate——不绑定异步运行时，仅 `sha2` + `serde_json`。`cargo test`

两边各自按 `spec/hash-contract-1.md` 实现（不共享代码、不 import 生成器），以 `vectors/` 逐字节互证，并一并断言各向量的 `relations`（规范性质）。完整 SDK（sans-IO 协议核心 + 传输适配）在 M1 定稿后按计划 M2 交付。

**Rust 消费方注意**：喂给 hash 的 JSON 若含浮点，解析必须正确舍入——serde_json 需开 `float_roundtrip` feature，否则极端指数下差 1 ULP，JCS 序列化结果与规范不一致（向量 `jcs_numbers` 的 `1e-30` 用例即为此坑而设）。
