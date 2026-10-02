# dpe-hash

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的独立 hash 核心：按 [hash 契约 1](https://doc.turingfocus.cn/dpe/latest/spec/hash-contract-1/) 计算元素、页、文档三层 hash（`dpe1:` 前缀），并导出契约常量。

- 不绑定异步运行时、不含 I/O，仅依赖 `sha2` 与 `serde_json`；
- 逐字节通过规范仓库的一致性向量；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

> 当前为工程骨架，hash 入口与契约常量随 Milestone `rust-sdk v0.1.6`（#19）落地。
