# dpe-hash

[DPE（Document / Page / Element）协议](https://doc.turingfocus.cn/dpe/)的独立 hash 核心：按 hash 契约计算元素、页、文档三层 hash（`dpe1:` 前缀），并导出契约常量。

- 零运行时依赖、纯 Python（≥ 3.11），带类型标记（`py.typed`）；
- 逐字节通过规范仓库的一致性向量；
- 规范与源码：<https://github.com/A2C-SMCP/dpe-protocol>。

> 当前为工程骨架，hash 核心见 #7。
