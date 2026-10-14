# connector 线协议夹具（运行器一致性）

[connector 契约](../../spec/connector-contract.md) §8.4 的**线协议夹具插件**：一个脚本化插件，由实例配置
`{"scenario": "<名>"}` 选择场景，覆盖全部线协议消息与失败路径。它是独立进程，**任何语言写的运行器**都能
用它验收自己：对每个场景跑一轮，断言得到 §8.4 表中的「运行器预期」。

- [`dpe_connector_fixture.py`](dpe_connector_fixture.py)：夹具本身，单文件、只用 Python（≥ 3.11）标准库；
- [`dpe-connector.json`](dpe-connector.json)：随附清单，身份 `connector-fixture 1.0`；
- [`dpe-connector.env-probe.json`](dpe-connector.env-probe.json)：`env_probe` 场景用的清单，多声明一个必填凭证 `FIXTURE_TOKEN`。

Python 运行器的验收用例在 `sdk/python/packages/dpe-sdk/tests/test_conformance_fixture.py`，可作为其他语言的参照。

## 用法

实例定义（§4.4）直接引用随附清单，`command` 用 Python 解释器启动夹具：

```json
{
  "definition_version": 1,
  "id": "fixture",
  "remote": { "url": "http://127.0.0.1:8080/r/1" },
  "uri_prefix": "dpe://fixture/",
  "plugin": {
    "manifest": "conformance/connector-fixture/dpe-connector.json",
    "command": ["python3", "conformance/connector-fixture/dpe_connector_fixture.py"]
  },
  "config": { "scenario": "multi_batch" }
}
```

remote 用参考服务端（`python -m dpe_sdk.testing --port 0`，属 `dpe-sdk` 包）或任何实现。每个场景用
新的状态目录与空前缀，`fallback_full`、`no_cursor`、`multi_batch` 需在同一状态目录下连跑两轮。

不认识的场景名（拼错或夹具尚未实现）在 initialize 以 `-32005` 拒绝，运行器得到 `invalid_config`——不会退化为
一轮空同步而被误判通过。

## 场景的具体产出

运行器预期以 §8.4 表为准；下表只补充断言 remote 状态时要用到的具体内容。URI 都以实例的 `uri_prefix` 为前缀
（下表省略前缀，`dpe://elsewhere/` 开头的除外），文档的 `file_type` 为 `txt`，正文元素都是 `NarrativeText`。

| 场景 | 夹具的具体产出 |
| --- | --- |
| `empty` | 一批空结束批 |
| `version_mismatch` / `invalid_config` | initialize 分别以 `-32004` / `-32005` 拒绝 |
| `identity_mismatch` | 自报版本 `0.0.0-mismatch` |
| `multi_batch` | 三批：建 `x.txt`（一页「多批次」）→ move 到 `y.txt` → delete `z.txt`（不存在即达成）；落地后前缀内只有 `y.txt`，游标 `multi-1` |
| `segmented` | `seg.txt` 分三段，每段一页，依次为「第一段」「第二段」「第三段」 |
| `unclosed_segment` | `open.txt` 的首段带 `continued: true`，之后结束本轮 |
| `error_item` | `broken.txt` 的 `error`：`source_unavailable`，`retryable: true` |
| `out_of_prefix` | `dpe://elsewhere/outside.txt` 的 document，以及 `a.txt → dpe://elsewhere/b.txt` 的 move |
| `invalid_item` | 未知 `kind`、带未定义字段 `extra` 的 document、category 为 `NoSuchCategory` 的 document，共三条 |
| `blobs` | `img.txt` 一页两个 `Image`（`mime_type: image/png`）：「内联」带内联 blob，「句柄」带 2.5 MiB 的句柄 blob（字节见夹具的 `_SMALL` / `_BIG`）；每次 `read_blob` 至多应答 64 KiB（`_SCENARIO_CHUNK`），无论运行器的 `max_bytes` 多大都必然多块读取 |
| `blob_size_mismatch` | 同上，句柄的 `size` 比实际多 1 |
| `unknown_handle` | 同上，句柄 `missing` 读取时返回 `-32002` |
| `cancel` | 同上，首次读取句柄时挂起，收到 `cancel` 后以 `-32001` 应答；用实例定义的 `runner.blob_timeout`（或运行器自己的等价设置）把超时调小 |
| `crash` | 第一批为空并带 `next`，第二批之前以退出码 3 退出 |
| `oversize` | `scan` 的应答一行超过 initialize 收到的 `max_message_bytes` |
| `bad_framing` | `scan` 的应答是一个空行，再一行 `not json` |
| `scan_error` | 轮首 `scan` 以 `-32003` 失败 |
| `fallback_full` | 结束批给出游标 `full-1`，且始终 `incremental: false`（第二轮带着游标也回退全量） |
| `no_cursor` | `supports_cursor: false`，结束批 `cursor: null` |
| `env_probe` | `env.txt` 一页：自身环境变量的名字（不含值）按字典序各一个元素 |

### `env_probe` 的注意事项

- 清单换成 `dpe-connector.env-probe.json`，实例定义的 `secrets` 为 `FIXTURE_TOKEN` 指定来源；不给来源即得到
  `secret_missing`。
- 夹具不报告平台在插件进程自身启动时加入的名字（macOS 的 `__CF_USER_TEXT_ENCODING`）。运行器没有传入任何
  locale 变量时，Python 解释器会按 PEP 538 自行补上 `LC_CTYPE`；验收时请让运行器的环境带上 `LC_ALL`
  （属于 §4.3 基础白名单的 `LC_*`，会被传入并关闭这一补充）。

## 脚本模式（仅供运行器自身的细粒度测试）

不带 `scenario` 时，夹具按 `batches`、`cursor`、`blobs`、`hang`、`record` 等显式配置逐项驱动（见夹具文件头部
说明），Python 运行器的测试用它构造表外的情形。脚本模式不属于 §8.4，键可能随运行器测试调整；跨语言验收只用
场景。
