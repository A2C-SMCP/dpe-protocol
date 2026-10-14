"""connector 线协议夹具插件（connector 契约 §8.4）：验证**运行器**用的脚本化插件。

单文件、只用 Python 标准库，任何语言写的运行器都能把它当插件启动::

    python dpe_connector_fixture.py

行为全部由 initialize 的 ``instance.config`` 决定（运行器原样传递，§4.2）：

- ``{"scenario": "<名>"}``：§8.4 表中的一个场景（``SCENARIOS``），运行器对每个场景应得到表中
  写明的结果；
- 不带 ``scenario``：**脚本模式**，由显式配置逐项驱动（供运行器自己的细粒度测试）——
  ``batches``（每批的产出项，按顺序以 ``next`` 续批）、``cursor`` / ``incremental``（结束批）、
  ``supports_cursor``、``blobs``（句柄 → base64，或 ``{"file": 路径}``）、``hang``（首次读取
  挂起直到 ``cancel`` 的句柄）、``scan_error``（轮首 ``scan`` 的 JSON-RPC 错误）、``record``
  （把每个请求追加写入该文件）、``stderr``（initialize 时写到 stderr 的行，可用 ``$NAME``
  引用环境变量）、``crash_on_scan``、``scan_delay``（轮首 ``scan`` 前等待的秒数）、
  ``script``（从该 JSON 文件读取上述配置，与其余键合并——换脚本而不改变实例配置）。

身份为 ``connector-fixture 1.0``，与同目录的 ``dpe-connector.json`` 一致（``env_probe`` 用
``dpe-connector.env-probe.json``，多声明一个必填凭证）。产出的 URI 都以 initialize 收到的
``uri_prefix`` 为前缀（``out_of_prefix`` 场景刻意越界）。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any

NAME = "connector-fixture"
VERSION = "1.0"
PROTOCOL = "dpe-connector/1"

#: §8.4 的场景（运行器预期见规范表）
SCENARIOS = (
    "empty",
    "version_mismatch",
    "invalid_config",
    "identity_mismatch",
    "multi_batch",
    "segmented",
    "unclosed_segment",
    "error_item",
    "out_of_prefix",
    "invalid_item",
    "blobs",
    "blob_size_mismatch",
    "unknown_handle",
    "cancel",
    "crash",
    "oversize",
    "bad_framing",
    "scan_error",
    "fallback_full",
    "no_cursor",
    "env_probe",
)

#: ``blobs`` 等场景的句柄字节（确定性生成）
_BIG = bytes((i * 31 + 7) % 251 for i in range(5 * 512 * 1024))
#: 场景模式下单次 ``read_blob`` 应答的字节上限：无论运行器的 ``max_bytes`` 多大，``_BIG`` 都必须
#: 分多块读完（§6.7：块长不超过 ``max_bytes`` 即可，``eof: false`` 表示后面还有字节）
_SCENARIO_CHUNK = 64 * 1024
_SMALL = b"\x89PNG fixture inline bytes"

#: ``env_probe`` 不报告的名字：由平台在**插件进程自身**启动时加入，不是运行器传入的
#: （macOS 的 CoreFoundation 给每个进程补上 ``__CF_USER_TEXT_ENCODING``）。另注意：运行器未传
#: 任何 locale 变量时，Python 自身会按 PEP 538 补上 ``LC_CTYPE``——用本场景验收时请让运行器
#: 带上 ``LC_ALL``（它属于 §4.3 基础白名单的 ``LC_*``，会被传入并关闭这一补充）。
_PLATFORM_INJECTED = frozenset({"__CF_USER_TEXT_ENCODING"})


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(id_: Any, value: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": id_, "result": value})


def error(id_: Any, code: int, message: str, data: Any = None) -> None:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    send({"jsonrpc": "2.0", "id": id_, "error": body})


def text_document(uri: str, *texts: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "document",
        "file_uri": uri,
        "document": {"file_type": "txt"},
        "pages": [{"elements": [{"category": "NarrativeText", "text": t} for t in texts]}],
        **extra,
    }


def scenario_script(name: str, prefix: str) -> dict[str, Any]:
    """场景 → 脚本模式的配置（多数场景就是一组固定的批次与结束字段）。"""
    if name in ("empty", "identity_mismatch"):  # 后者只在 initialize 自报的版本上不同
        return {"batches": [[]], "cursor": None, "incremental": False}
    if name == "multi_batch":
        # 先建 x，再改名为 y，再删除一个不存在的 z（delete 不存在即达成）：落地后 remote 只有 y
        return {
            "batches": [
                [text_document(prefix + "x.txt", "多批次")],
                [{"kind": "move", "from_uri": prefix + "x.txt", "to_uri": prefix + "y.txt"}],
                [{"kind": "delete", "file_uri": prefix + "z.txt"}],
            ],
            "cursor": "multi-1",
        }
    if name == "segmented":
        uri = prefix + "seg.txt"
        return {
            "batches": [
                [
                    {**text_document(uri, "第一段"), "continued": True},
                    {
                        "kind": "document",
                        "file_uri": uri,
                        "pages": [{"elements": [{"category": "NarrativeText", "text": "第二段"}]}],
                        "continued": True,
                    },
                    {
                        "kind": "document",
                        "file_uri": uri,
                        "pages": [{"elements": [{"category": "NarrativeText", "text": "第三段"}]}],
                    },
                ]
            ],
            "cursor": "seg-1",
        }
    if name == "unclosed_segment":
        return {
            "batches": [[{**text_document(prefix + "open.txt", "未闭合"), "continued": True}]],
            "cursor": "unclosed-1",
        }
    if name == "error_item":
        return {
            "batches": [
                [
                    {
                        "kind": "error",
                        "file_uri": prefix + "broken.txt",
                        "code": "source_unavailable",
                        "message": "源暂时不可读",
                        "retryable": True,
                    }
                ]
            ],
            "cursor": "error-1",
        }
    if name == "out_of_prefix":
        return {
            "batches": [
                [
                    text_document("dpe://elsewhere/outside.txt", "越界"),
                    {
                        "kind": "move",
                        "from_uri": prefix + "a.txt",
                        "to_uri": "dpe://elsewhere/b.txt",
                    },
                ]
            ],
            "cursor": "oop-1",
        }
    if name == "invalid_item":
        return {
            "batches": [
                [
                    {"kind": "unknown", "file_uri": prefix + "u.txt"},
                    {**text_document(prefix + "f.txt", "多余字段"), "extra": 1},
                    {
                        "kind": "document",
                        "file_uri": prefix + "c.txt",
                        "document": {"file_type": "txt"},
                        "pages": [{"elements": [{"category": "NoSuchCategory", "text": "x"}]}],
                    },
                ]
            ],
            "cursor": "invalid-1",
        }
    if name in ("blobs", "blob_size_mismatch", "unknown_handle", "cancel"):
        handle = {"handle": "big", "size": len(_BIG)}
        if name == "blob_size_mismatch":
            handle = {"handle": "big", "size": len(_BIG) + 1}
        if name == "unknown_handle":
            handle = {"handle": "missing"}
        elements = [
            {
                "category": "Image",
                "text": "内联",
                "mime_type": "image/png",
                "blob": {"inline": base64.b64encode(_SMALL).decode()},
            },
            {"category": "Image", "text": "句柄", "mime_type": "image/png", "blob": handle},
        ]
        return {
            "batches": [
                [
                    {
                        "kind": "document",
                        "file_uri": prefix + "img.txt",
                        "document": {"file_type": "txt"},
                        "pages": [{"elements": elements}],
                    }
                ]
            ],
            "cursor": f"{name}-1",
            "big": True,
            "hang": ["big"] if name == "cancel" else [],
        }
    if name == "crash":
        return {"batches": [[], []], "crash_before": 1}
    if name == "oversize":
        return {"batches": [[]], "oversize": True}
    if name == "bad_framing":
        return {"batches": [[]], "bad_framing": True}
    if name == "scan_error":
        return {
            "scan_error": {"code": -32003, "message": "source down", "data": {"retryable": True}}
        }
    if name == "fallback_full":
        return {"batches": [[]], "cursor": "full-1", "incremental": False}
    if name == "no_cursor":
        return {"batches": [[]], "cursor": None, "incremental": False, "supports_cursor": False}
    if name == "env_probe":
        names = sorted(set(os.environ) - _PLATFORM_INJECTED)
        return {"batches": [[text_document(prefix + "env.txt", *names)]], "cursor": None}
    raise ValueError(f"未知场景 {name!r}")


def main() -> None:
    config: dict[str, Any] = {}
    hanging: dict[Any, str] = {}  # 挂起中的 read_blob 请求 id → 句柄
    hung_once: set[str] = set()
    for line in sys.stdin:
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        id_ = message.get("id")
        record = config.get("record")
        if record and method != "initialize":
            with open(record, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": method, "params": params}) + "\n")
        if method == "initialize":
            instance = params["instance"]
            config = dict(instance["config"])
            if "script" in config:
                with open(config["script"], encoding="utf-8") as fh:
                    config = {**json.load(fh), **config}
            scenario = config.get("scenario")
            if scenario == "version_mismatch":
                error(id_, -32004, "没有共同支持的线协议版本")
                return
            if scenario == "invalid_config":
                error(id_, -32005, "夹具拒绝该配置")
                return
            if scenario is not None and scenario not in SCENARIOS:
                # 拼错或尚未实现的场景名不得退化为一轮空同步而被误判通过
                error(id_, -32005, f"未知场景 {scenario!r}")
                return
            if scenario is not None:
                config = {**scenario_script(scenario, instance["uri_prefix"]), **config}
            config["max_message_bytes"] = params.get("max_message_bytes", 0)
            for text in config.get("stderr", []):
                sys.stderr.write(os.path.expandvars(text) + "\n")
            sys.stderr.flush()
            version = "0.0.0-mismatch" if scenario == "identity_mismatch" else VERSION
            result(
                id_,
                {
                    "protocol_version": PROTOCOL,
                    "plugin": {"name": NAME, "version": version},
                    "capabilities": {"supports_cursor": config.get("supports_cursor", True)},
                },
            )
        elif method == "scan":
            first = "next" not in params
            if first and config.get("crash_on_scan"):
                sys.exit(3)
            if first and config.get("scan_delay"):
                time.sleep(config["scan_delay"])
            if first and config.get("scan_error"):
                send({"jsonrpc": "2.0", "id": id_, "error": config["scan_error"]})
                continue
            if config.get("oversize"):  # 一行超过 max_message_bytes（§6.2）
                padding = "x" * (int(config["max_message_bytes"]) + 1)
                result(id_, {"items": [], "cursor": None, "incremental": False, "pad": padding})
                continue
            if config.get("bad_framing"):  # 空行，再一行非 JSON（§6.2）
                sys.stdout.write("\nnot json\n")
                sys.stdout.flush()
                continue
            batches = config.get("batches", [[]])
            index = int(params["next"]) if not first else 0
            if config.get("crash_before") is not None and index >= config["crash_before"]:
                sys.exit(3)  # 第二批之前以非零码退出
            out: dict[str, Any] = {"items": batches[index] if index < len(batches) else []}
            if index + 1 < len(batches):
                out["next"] = str(index + 1)
            else:
                out["cursor"] = config.get("cursor")
                out["incremental"] = config.get("incremental", "cursor" in params)
            result(id_, out)
        elif method == "read_blob":
            handle = params["handle"]
            if handle in config.get("hang", []) and handle not in hung_once:
                hung_once.add(handle)
                hanging[id_] = handle
                continue  # 挂起：等 cancel
            data = _blob_bytes(config, handle)
            if data is None:
                error(id_, -32002, "未知或已失效的句柄")
                continue
            offset, size = params.get("offset", 0), params["max_bytes"]
            if config.get("scenario") is not None:
                size = min(size, _SCENARIO_CHUNK)
            chunk = data[offset : offset + size]
            result(
                id_,
                {
                    "data": base64.b64encode(chunk).decode(),
                    "eof": offset + len(chunk) >= len(data),
                },
            )
        elif method == "cancel":
            target = params.get("id")
            if target in hanging:
                del hanging[target]
                error(target, -32001, "cancelled")
        elif method == "shutdown":
            result(id_, {})
            return


def _blob_bytes(config: dict[str, Any], handle: str) -> bytes | None:
    if handle == "big" and config.get("big"):
        return _BIG
    source = config.get("blobs", {}).get(handle)
    if source is None:
        return None
    if isinstance(source, dict):  # {"file": 路径}：大对象不经实例配置传递
        with open(source["file"], "rb") as fh:
            return fh.read()
    return base64.b64decode(source)


if __name__ == "__main__":
    main()
