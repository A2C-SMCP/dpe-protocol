# dpe-conformance

[DPE](https://doc.turingfocus.cn/dpe/) **服务端一致性跑分器**：黑盒对任意实现了 [core.md](https://doc.turingfocus.cn/dpe/latest/spec/core/) + [HTTP 绑定](https://doc.turingfocus.cn/dpe/latest/spec/bindings/http/) 的 remote 运行协议检查（检查面见仓库 [`conformance/README.md`](https://github.com/A2C-SMCP/dpe-protocol/blob/main/conformance/README.md)）。跑分器本体只依赖 `dpe-hash`（期望 hash）与 HTTP，不依赖任何服务端实现。

> **从 0.4 升级**：全部不兼容变更与行为变化按读者汇总在 [0.5 升级须知](https://doc.turingfocus.cn/dpe/latest/docs/upgrade/v0.5/)，下文各处「升级须知」只是摘要。

## 用法

```console
$ pip install dpe-conformance==X.Y.Z   # X.Y.* 对应规范文档 X.Y.*
$ dpe-conformance https://host/r/1 --junit-xml report.xml --counts-json counts.json
```

- `REMOTE-URL` 须符合 HTTP 绑定 §1 的 remote URL 文法（与 SDK 同一判定，`dpe_hash.check_remote_url`）：不合法即以退出码 2 拒绝、不开跑，消息不回显 URL——SDK 连不上的 remote（如带查询串、尾部 `/`、大写 scheme）跑分全绿没有意义。
- **规范变化（#183）**：演练契约由 `dpe2` 改名为 `dpex`（契约 1 §1，#183），按旧文档同时声明 `dpe1` 与 `dpe2` 做演练的服务端须改为声明 `dpex`——否则契约升级期一章与以演练契约为用例契约的向量用例会被如实 skip（不报错），若 CI 没有守住执行数基线就察觉不到覆盖的丢失；
- `--section NAME` 按章过滤（可重复；缺省全部章节；未知章节以退出码 2 拒绝——拼错章节名不会静默跑出一个「假绿」的空套件）；
- `--junit-xml PATH` 输出 JUnit XML（CI 直接消费）；
- `--counts-json PATH` 输出机器可读计数，供 CI 断言**执行数不低于基线**（防止因 skip 过多而假绿）：

结构如下（示意：数字为虚构，`sections` 只节选两章；实际每章一项，键为章节名）：

```json
{
  "schema": 1,
  "target": "https://host/r/1",
  "totals": {"executed": 40, "passed": 39, "failed": 1, "errors": 0, "skipped": 2, "collection_errors": 0},
  "sections": {
    "smoke": {"executed": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0},
    "staging": {"executed": 10, "passed": 9, "failed": 1, "errors": 0, "skipped": 2}
  }
}
```

章节名即 `src/dpe_conformance/checks/` 下的一级模块名；章节与检查条数随版本增加，本文不列具体数字——当前版本的章节列表见 `--section` 给出未知章节名时的报错，各章条数以实际输出的 `counts.json` 为准。CI 的执行数基线按你接入的身份与钩子自行记录。

在服务端仓库 CI 中的接入步骤与最小 GitHub Actions 示例见 [在 CI 中运行跑分器](https://github.com/A2C-SMCP/dpe-protocol/blob/main/conformance/ci.md)。

`executed` = 取得通过/失败结论的用例（即 `passed + failed`）；`errors` 与 `collection_errors` 单列。退出码：`0` 全部通过（skip 不算失败），`1` 有失败，`2` 用法/配置/运行故障（含「没有任何检查被执行」）。

## 代理

跑分器**只从环境变量取代理**（`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`），从不读操作系统的代理设置（macOS / Windows 的系统代理、PAC 都不看）。规则与 Python / Rust SDK 相同、共用一张用例表（`sdk/testdata/proxy_env.json`），详见 `dpe-sdk` README 传输一节的「代理只来自环境变量」。要点：

- 按**每个请求的目的地**判定：被测服务端与 HTTP 钩子（可在另一个 origin）各自套用规则，例如钩子地址可以经 `NO_PROXY` 直连而 target 走代理；
- 大写变量优先，空值视为未设置；`NO_PROXY` 支持 `*`、域名及子域、IP 与 CIDR；
- 代理 URL 按固定文法校验（只支持 `http://` / `https://`，密码里的 `@` 写成 `%40`）；不合法时在发出任何请求之前以退出码 `2` 结束，报错只含变量名、不回显值；
- 关闭代理：不设这些变量，或设 `NO_PROXY=*`。

## 配置

可选配置文件（TOML，封闭 schema，未知键一律拒绝），缺省在 CWD 下找 `dpe-conformance.toml`，或以 `--config` 指定。**凭证不进命令行与日志**，只放上 `Authorization` 请求头；身份建议经 `env` 引用环境变量，`authorization` 字面量仅限低权限/一次性测试凭证。

配置加载阶段按 HTTP 绑定 §6 校验 `authorization` 的值（字面量与 `env` 解析结果都算）：空值合法，或首尾为 VCHAR、中间只允许 VCHAR / SP / HTAB；不合规的配置在开跑前以退出码 2 拒绝，错误消息不回显值。

```toml
schema = 1
uri-prefix = "dpe://conformance/"          # 测试 URI 命名空间（缺省值即此；每次运行另加一次性 token）

[identities.full]                          # 全量写权（core §8 安全检查的「有写授权」侧）
env = "DPE_CONF_FULL_AUTH"
[identities.scoped]                        # 仅前缀可写（服务端按该前缀授予写权；检查会在该前缀下新建文档）
authorization = "Bearer scoped"
prefix = "dpe://acme/scoped/"
[identities.no-force]                      # 无 force 权限（force 的 403 检查；需对测试
env = "DPE_CONF_NO_FORCE_AUTH"             # 命名空间可写，否则该检查显式 skip 并计数）

[hooks.self-write]                         # 服务端自身写入（core §2.4 同级写入）
http = "http://127.0.0.1:8000/__hooks__/self-write"
[hooks.expire-session]                     # 会话立即过期（否则 410 检查要等 ≥ 1 小时）；与 command 二选一
command = ["my-server", "expire-session"]
[hooks.clock-advance]                      # 推进服务端时钟 N 秒（会话 TTL 按推进后的时间判定）
http = "http://127.0.0.1:8000/__hooks__/clock/advance"

[session]
staging-ttl-seconds = 30                   # 服务端以短 TTL 运行时的声明
```

身份与钩子都是可选的：**未配置时，依赖它们的检查显式 skip 并计入计数**（`--counts-json` 的 `skipped`），CI 以执行数基线拦截「skip 太多」的假绿。

配置 scoped 身份后，部分检查需要「scoped 有写权」的文档，会在它的可写前缀下（测试命名空间 `uri-prefix` 之外）新建文档，跑完不清理。请为 scoped 身份配置一个专供跑分的前缀。

另有两类 skip 取决于服务端本身，同样如实计数：认证顺序的检查先发一个不带凭证的探针，只有服务端对它返回 `401` 时才执行（接受匿名请求的服务端无从观测「认证先于报文校验」）；一致性向量用例只在输入涉及的契约前缀于用例口径与服务端声明的 `hash_contracts` 中受支持与否一致时执行（见 [在 CI 中运行跑分器](https://github.com/A2C-SMCP/dpe-protocol/blob/main/conformance/ci.md)）。

## 服务端钩子契约

钩子是测试侧信道，**不进入 DPE 协议面**；跑分器按「HTTP 或命令」的入口配置驱动，不假定服务端如何实现。载荷成员名全部是 DPE 协议词汇。

- **`self-write`**：载荷 = `file_uri` + `document`（可附 `pages` / `objects` / `force`，与 commit 请求体成员同名）与前置条件 `base_hash` 或 `if_absent`；
- **`expire-session`**：载荷 `{"session_id": "…"}`；成功响应体给 `{"expired": true|false}`；
- **`clock-advance`**：载荷 `{"advance_seconds": N}`（正整数）——把服务端时钟推进 N 秒，**会话 TTL 由此按推进后的时间判定**（用于「过期 = 最近一次成功操作 + TTL」与续期时刻的精确断言，跑分器不靠睡眠）；成功响应体给 `{"now": "<RFC 3339>"}`；
- **`inject-error`**（#123）：载荷 `{"code", "probe", "count"?, "retry_after"?}`——让带请求头
  `DPE-Conformance-Probe: <probe>` 的请求接下来 `count` 次（缺省 1）直接得到该错误
  （`code` 只接受 `DPE_RATE_LIMITED` / `DPE_UNAVAILABLE`）；`retry_after` 缺省时由服务端给
  缺省提示值 `1`（HTTP 绑定 §5）。成功响应体给 `{"armed": true, "count": N}`。探针值由跑分器
  每次生成，服务端只拦截同一探针的请求：不绑定身份、不携带凭证，未带该头（或头值不同）的
  请求不受影响；该头只在钩子启用时被识别；
- 成功判据：HTTP 入口 `POST` 得 2xx；命令入口从 stdin 收一行 JSON、退出码 0（stdout 若为 JSON 对象则作为响应体）。

## 章节与扩展

套件以 pytest 实现，一节一章：`src/dpe_conformance/checks/` 下每个一级模块或包是一章（`--section` 的粒度）。新增章节即新增一个 pytest 模块/包，用例通过 `dpe_conformance.plugin` 的夹具取得被测 remote、身份与钩子；需要而未配置的依赖用对应夹具使用例显式 skip。

自测对手是 [参考服务端](https://github.com/A2C-SMCP/dpe-protocol/tree/main/sdk/python/packages/dpe-sdk/src/dpe_sdk/testing)（`dpe_sdk.testing`，`python -m dpe_sdk.testing`），仅出现在本包测试里，跑分器本体不 import 它。
