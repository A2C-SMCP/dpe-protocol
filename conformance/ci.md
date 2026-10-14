# 在 CI 中运行一致性跑分器

本页写给 DPE 服务端实现的维护者：在自己仓库的 CI 里安装 `dpe-conformance`，对被测服务黑盒跑一遍协议检查，产出 JUnit XML 与执行/跳过计数。检查面见 [服务端跑分器](README.md)；CLI 选项、配置文件与服务端钩子契约的完整说明见 [包 README](https://github.com/A2C-SMCP/dpe-protocol/tree/main/sdk/python/packages/dpe-conformance#readme)。

## 版本对应

- `dpe-conformance X.Y.*` 校验规范文档 `X.Y.*`。规范出现不兼容变更时，文档与 `dpe-conformance` 同步升次版本（`X.Y` 一起变），补丁号各自独立；
- CI 里建议**精确锁定** `dpe-conformance==X.Y.Z`，升级跑分器作为一次显式变更提交，不让上游发布悄悄改变你的 CI 结果；
- 需要以范围声明依赖时，写 `dpe-conformance>=X.Y.0,<X.(Y+1)`。

跑分器本体只依赖 `dpe-hash`（计算期望 hash）、`httpx` 与 `pytest`，不依赖任何服务端实现，也不需要和被测服务装在同一个环境里。

## 步骤

1. **安装**：`pip install dpe-conformance==X.Y.Z`（Python ≥ 3.11）。
2. **启动被测服务**：以 CI 专用配置启动，并等它就绪后再开始跑分——就绪判定交给服务自己的健康检查（如容器 healthcheck），跑分器不负责等待。
3. **写配置**（可选，`dpe-conformance.toml`，缺省在当前目录查找）：
    - 身份（`[identities.*]`）用 `env` 引用 CI secrets，凭证不进命令行与日志；
    - 钩子（`[hooks.*]`）是测试侧信道，不属于 DPE 协议面，只在 CI 配置下开启；
    - 没配置的身份与钩子，对应检查会显式 skip 并计入计数，不会失败。
4. **运行**：

    ```console
    $ dpe-conformance https://host/remote --junit-xml junit.xml --counts-json counts.json
    ```

    `REMOTE-URL` 须符合 [HTTP 绑定](../spec/bindings/http.md) §1 的 remote URL 文法（与 SDK 同一判定）：SDK 连不上的写法（查询串、尾部 `/`、大写 scheme、显式默认端口等）以退出码 `2` 拒绝、不开跑。退出码 `0` 全部通过（skip 不算失败），`1` 有失败，`2` 用法/配置/运行故障（含「没有任何检查被执行」）。可用 `--section NAME` 只跑部分章节，未知章节名以退出码 `2` 拒绝。
5. **守住执行数**：skip 不算失败，因此「配置漏了导致大量 skip」也会是绿的。用 `counts.json` 断言执行数不低于你记录的基线，基线随你接入的身份与钩子逐步提高。

有两类检查的 skip 取决于服务端本身，而不是配置。它们对真实服务端是正常现象，不算失败：

- **认证顺序**：「认证先于一切报文校验」只能在服务端要求认证时观测。跑分器先发一个不带凭证的探针请求，得到 `401` 才执行这类检查；服务端接受匿名请求时如实 skip。
- **契约集合**：一致性向量按「向量主契约 ∪ 用例契约」判定受支持与否（`vectors/README.md`）。只有当输入涉及的每个契约前缀，在用例口径下与服务端声明的 `hash_contracts` 里受支持与否都一致时，该用例才适用。例如只声明 `dpe1` 的服务端会 skip 以演练契约 `dpex` 为用例契约的那几条。

## 最小 GitHub Actions 示例

以下示例假定被测服务可用 `docker compose` 启动并带 healthcheck；启动方式可以换成任何你自己的方式，只要跑分前服务已就绪。

```yaml
name: dpe-conformance

on: [pull_request]

jobs:
  conformance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install dpe-conformance==X.Y.Z

      # 启动被测服务；--wait 等到所有服务的 healthcheck 通过
      - run: docker compose -f ci/compose.yml up -d --wait

      - name: 运行一致性跑分器
        env:
          DPE_CONF_FULL_AUTH: ${{ secrets.DPE_CONF_FULL_AUTH }}
        run: |
          cat > dpe-conformance.toml <<'EOF'
          schema = 1

          [identities.full]
          env = "DPE_CONF_FULL_AUTH"
          EOF
          dpe-conformance http://127.0.0.1:8000/ --junit-xml junit.xml --counts-json counts.json

      # 执行数基线：防止因 skip 过多而假绿。min 是示例值，首次接入后改为实际执行数并随接入提高
      - run: jq -e --argjson min 7 '.totals.executed >= $min and .totals.collection_errors == 0' counts.json

      - if: always()
        uses: actions/upload-artifact@v4
        with:
          name: dpe-conformance
          path: |
            junit.xml
            counts.json
```

本仓库自身的 CI 也按上述方式（从构建出的 wheel 单独安装跑分器、CLI 产出 JUnit 与计数）对参考服务端运行，配置齐全（全部身份与钩子，并以 `--require-auth` 只认已授予的凭证），共跑两轮：

1. 同时声明 `dpe1` 与演练契约 `dpex`，运行全部章节；
2. 只声明 `dpe1`，只运行 `vectors` 章。

部分向量用例对契约集合的前提互斥，因此每轮各有个别 skip。**零 skip 按两轮的并集判定**：任一轮 skip 的用例必须在另一轮执行并通过。见 `.github/workflows/python-sdk.yml` 的 `conformance` job。
