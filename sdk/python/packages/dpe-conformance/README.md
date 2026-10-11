# dpe-conformance

[DPE](https://doc.turingfocus.cn/dpe/) **服务端一致性跑分器**：黑盒对任意实现了 [core.md](https://doc.turingfocus.cn/dpe/latest/spec/core/) + [HTTP 绑定](https://doc.turingfocus.cn/dpe/latest/spec/bindings/http/) 的 remote 运行协议检查（检查面见仓库 [`conformance/README.md`](../../../conformance/README.md)）。跑分器本体只依赖 `dpe-hash`（期望 hash）与 HTTP，不依赖任何服务端实现。

## 用法

```console
$ dpe-conformance https://host/r/1 --junit-xml report.xml --counts-json counts.json
```

- `--section NAME` 按章过滤（可重复；缺省全部章节；未知章节以退出码 2 拒绝——拼错章节名不会静默跑出一个「假绿」的空套件）；
- `--junit-xml PATH` 输出 JUnit XML（CI 直接消费）；
- `--counts-json PATH` 输出机器可读计数，供 CI 断言**执行数不低于基线**（防止因 skip 过多而假绿）：

```json
{
  "schema": 1,
  "target": "https://host/r/1",
  "totals": {"executed": 89, "passed": 89, "failed": 0, "errors": 0, "skipped": 2, "collection_errors": 0},
  "sections": {"capabilities": {"executed": 4, "passed": 4, "failed": 0, "errors": 0, "skipped": 0}, "read": {"executed": 18, "passed": 18, "failed": 0, "errors": 0, "skipped": 0}, "commit": {"executed": 18, "passed": 18, "failed": 0, "errors": 0, "skipped": 0}, "validation": {"executed": 7, "passed": 7, "failed": 0, "errors": 0, "skipped": 0}, "smoke": {"executed": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0}, "write_ops": {"executed": 20, "passed": 20, "failed": 0, "errors": 0, "skipped": 0}, "http_binding": {"executed": 19, "passed": 19, "failed": 0, "errors": 0, "skipped": 2}}
}
```

`executed` = 取得通过/失败结论的用例（即 `passed + failed`）；`errors` 与 `collection_errors` 单列。退出码：`0` 全部通过（skip 不算失败），`1` 有失败，`2` 用法/配置/运行故障（含「没有任何检查被执行」）。

## 配置

可选配置文件（TOML，封闭 schema，未知键一律拒绝），缺省在 CWD 下找 `dpe-conformance.toml`，或以 `--config` 指定。**凭证不进命令行与日志**，只放上 `Authorization` 请求头；身份建议经 `env` 引用环境变量，`authorization` 字面量仅限低权限/一次性测试凭证。

```toml
schema = 1
uri-prefix = "dpe://conformance/"          # 测试 URI 命名空间（缺省值即此；每次运行另加一次性 token）

[identities.full]                          # 全量写权（core §8 安全检查的「有写授权」侧）
env = "DPE_CONF_FULL_AUTH"
[identities.scoped]                        # 仅前缀可写（服务端按该前缀授予写权）
authorization = "Bearer scoped"
prefix = "dpe://acme/scoped/"
[identities.no-force]                      # 无 force 权限（force 的 403 检查；需对测试
env = "DPE_CONF_NO_FORCE_AUTH"             # 命名空间可写，否则该检查显式 skip 并计数）

[hooks.self-write]                         # 服务端自身写入（core §2.4 同级写入）
http = "http://127.0.0.1:8000/__hooks__/self-write"
[hooks.expire-session]                     # 会话立即过期（否则 410 检查要等 ≥ 1 小时）；与 command 二选一
command = ["my-server", "expire-session"]

[session]
staging-ttl-seconds = 30                   # 服务端以短 TTL 运行时的声明
```

身份与钩子都是可选的：**未配置时，依赖它们的检查显式 skip 并计入计数**（`--counts-json` 的 `skipped`），CI 以执行数基线拦截「skip 太多」的假绿。

## 服务端钩子契约

钩子是测试侧信道，**不进入 DPE 协议面**；跑分器按「HTTP 或命令」的入口配置驱动，不假定服务端如何实现。载荷成员名全部是 DPE 协议词汇。

- **`self-write`**：载荷 = `file_uri` + `document`（可附 `pages` / `objects` / `force`，与 commit 请求体成员同名）与前置条件 `base_hash` 或 `if_absent`；
- **`expire-session`**：载荷 `{"session_id": "…"}`；成功响应体给 `{"expired": true|false}`；
- 成功判据：HTTP 入口 `POST` 得 2xx；命令入口从 stdin 收一行 JSON、退出码 0（stdout 若为 JSON 对象则作为响应体）。

## 章节与扩展

套件以 pytest 实现，一节一章：`src/dpe_conformance/checks/` 下每个一级模块或包是一章（`--section` 的粒度）。新增章节即新增一个 pytest 模块/包，用例通过 `dpe_conformance.plugin` 的夹具取得被测 remote、身份与钩子；需要而未配置的依赖用对应夹具使用例显式 skip。

自测对手是 [参考服务端](../dpe-sdk/src/dpe_sdk/testing/)（`dpe_sdk.testing`，`python -m dpe_sdk.testing`），仅出现在本包测试里，跑分器本体不 import 它。
