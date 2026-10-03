# DPE Protocol

DPE（Document / Page / Element）：把结构化文档的**内容面****正确、增量、可靠**地投递到一个远端的**标准协议**。就像 Git 之于 GitHub / GitLab / CNB，任何服务端都能实现 DPE，任何开发者都能用 SDK 接入自己的系统。

当前文档版本：**0.1.7**

!!! note "三个独立的版本轴"
    - **文档版本**（本站右上角的版本选择器，如 `0.1.0`）：规范文档的发布快照，由 `bump-my-version` 管理。
    - **协议版本**（DPE v1）：投递协议的操作语义与绑定。
    - **hash 契约版本**（契约 1，值前缀 `dpe1:`）：内容身份的计算规则。

    0.x 文档版本对应 M1 规范草案，定稿前仍可能调整。

## 阅读路径

| 文档 | 内容 |
| --- | --- |
| [Core v1](spec/core.md) | 抽象模型与操作语义（与传输无关） |
| [Hash 契约 1](spec/hash-contract-1.md) | 三层同构 tree 的 hash 规则（`dpe1:`） |
| [HTTP 绑定](spec/bindings/http.md) | v1 唯一的规范性传输绑定 |
| [Connector 契约](spec/connector-contract.md) | 中立 connector 契约（独立规范；§6 运行边界已定稿） |
| [一致性向量](vectors/README.md) | hash 契约的组成部分，实现 MUST 全部通过 |
| [服务端跑分器](conformance/README.md) | 黑盒 HTTP 一致性检查面 |
| [v1 计划](docs/plan/v1-plan.md) | 评审定稿稿，当前工作的依据 |

## 分层

```
Connector（中立契约）→ 运行器 + SDK（hash·协商·暂存·CAS·重试）→ 任意 DPE 服务端
```

协议只负责投递；接什么源、何时推送是 connector 的事，鉴权、租户、学习、召回是服务端实现的事。
