# 服务端一致性跑分器（M2）

黑盒 HTTP 跑分器：对着任意实现了 [core.md](../spec/core.md) + [HTTP 绑定](../spec/bindings/http.md) 的 remote 跑一遍协议语义（CAS、暂存原子切换、delta 如实回报、错误格式等），供各服务端在自己的 CI 中执行。

M1 只保留占位；实现随 M2 交付（plan §14）。计划中的检查面：

- capabilities 协商与限额如实声明；
- 快路径 / 暂存路径的 commit 语义与原子性（分批期间读接口不可见中间态）；
- delta 与 `get_skeleton` 读回结果互相印证（不制造伪变更）；
- CAS 全矩阵（if_absent / base_revision / 缺前置 / force）与删除复活防护；
- delete / move 语义；
- RFC 9457 错误格式与 `code` / `retryable` / `Retry-After`；
- 增量推送、删除、move 收敛后与全量推送一致（判据 plan §13）。
