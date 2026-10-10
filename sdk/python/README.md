# Python SDK workspace

```
pyproject.toml            # workspace 根（不是包）：开发工具链、ruff / mypy / pytest 配置、SDK 版本管理
conftest.py               # vectors_dir 夹具：读取仓库根目录 vectors/（可用 DPE_VECTORS_DIR 覆盖）
tools/check_packages.py   # 打包约束检查（CI 与发布共用）
tools/bench_hash.py       # dpe-hash 10 万元素原位重算基准（CI 只报告、不设门槛）
packages/dpe-hash/        # import dpe_hash：零运行时依赖、纯 Python 的 hash 核心
packages/dpe-sdk/         # import dpe_sdk：依赖 dpe-hash（同版本精确依赖）、pydantic v2、httpx、jsonschema（dpe-run 校验插件配置）
```

## 开发

在本目录下运行：

```bash
uv sync                      # 安装两个包（可编辑）与开发工具链
uv run ruff check && uv run ruff format --check
uv run mypy                  # strict
uv run pytest
uv build --all-packages -o dist && uv run --no-project python tools/check_packages.py dist
uv run python tools/bench_hash.py   # 可选：--pages / --per-page / --repeat
```

CI（`.github/workflows/python-sdk.yml`）先跑 `make check-vectors`，再在 Python 3.11 / 3.12 矩阵上跑 lint / mypy / pytest，打包约束与基准在 3.12 上各跑一次（基准结果写进 job summary）。

`check_packages.py` 只检查构建出的 wheel / sdist：纯 Python（`py3-none-any`）、dpe-hash 零运行时依赖、dpe-sdk 精确依赖同版本 dpe-hash；并按白名单扫描静态 import——只允许标准库、自身与已声明的依赖，内核与向量生成器不必列名即被拦下（动态 import 由评审把关）。

## 版本与发布

SDK 版本是独立的一条轴，与文档版本（根 `pyproject.toml`）、协议版本（DPE v1）、hash 契约版本（`dpe1:`）互不牵动；两个包始终同版本，由本目录的 bump-my-version 管理：

```bash
uv run bump-my-version bump pre_l     # X.Y.Z-dev → X.Y.Z：commit + 标签 py-vX.Y.Z
git push --atomic origin main py-vX.Y.Z   # 原子推送：main 被拒时标签也不会推上去
uv run bump-my-version bump patch     # 开下一周期：X.Y.Z → X.Y.(Z+1)-dev（minor / major 同理）
```

- 标签触发 `release-python.yml`：先跑与 PR 相同的完整门禁（`python-sdk.yml`），再校验标签指向 main 上的提交，构建后经 environment 审批发布；
- bump 的 `pre_commit_hooks` 会连带刷新**以 path 引用 SDK 的独立 uv 项目**的锁文件并一并提交（目前为 `connectors/git`；新增消费者时在 `pyproject.toml` 追加一对 hook）——漏刷会被 `python-sdk.yml` 的 `locks` 守卫在 SDK 侧拦住（它枚举仓库内全部已提交的 `uv.lock`，新消费者零维护即被覆盖，见 #106）；
- 发布走 PyPI Trusted Publishing（OIDC），每个包一个 GitHub environment（`pypi-dpe-hash` / `pypi-dpe-sdk`），登记约定见 Issue #10 评论；
- 开发版标签 `py-v*-dev` 不触发发布，`check_packages.py --expect-version` 也拒绝发布开发版；
- dpe-hash 先发、dpe-sdk 后发（后者依赖前者）。
