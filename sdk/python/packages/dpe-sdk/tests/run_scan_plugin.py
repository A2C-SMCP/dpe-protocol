"""脚本化插件：供 ``test_run_round`` 以真实子进程驱动一轮编排。

用法：``python run_scan_plugin.py``。身份为 ``scan 1.0``，行为全部由 initialize 的
``instance.config`` 给出（运行器原样传递，§4.2）：

- ``batches``：每批的产出项数组（原样作为 ``items`` 返回），按顺序以 ``next`` 续批；
- ``cursor``：结束批的游标（缺省 ``null``）；``incremental``：结束批的标记，缺省为「轮首
  ``scan`` 带了游标」；``supports_cursor``：initialize 的能力（缺省 true）；
- ``blobs``：句柄 → base64 字节（或 ``{"file": 路径}``，大对象不经实例配置传递）；
  ``read_blob`` 按 ``offset`` / ``max_bytes`` 切块返回。
  ``hang`` 中的句柄首次读取挂起，收到 ``cancel`` 后以 -32001 响应；不在 ``blobs`` 中的句柄以
  -32002 拒绝；
- ``scan_error``：轮首 ``scan`` 以该 JSON-RPC 错误（``{code, message, data?}``）响应；
- ``record``：文件路径；每个请求的 ``method`` 与 ``params`` 以 JSON 行追加写入，供用例断言；
- ``stderr``：initialize 时逐行写到 stderr 的文本（可用 ``$NAME`` 引用环境变量，验证脱敏）；
- ``crash_on_scan``：收到轮首 ``scan`` 时以退出码 3 退出；
- ``scan_delay``：轮首 ``scan`` 前等待的秒数；
- ``script``：文件路径；给出时上述配置从该 JSON 文件读取（与 ``script`` 之外的键合并）。命令行
  测试用它让实例配置保持不变（配置属于内容身份，变化即全量轮）。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    config: dict[str, Any] = {}
    hanging: dict[int, str] = {}  # 挂起中的 read_blob 请求 id → 句柄
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
            config = params["instance"]["config"]
            if "script" in config:
                with open(config["script"], encoding="utf-8") as fh:
                    config = {**json.load(fh), **config}
            for line in config.get("stderr", []):
                sys.stderr.write(os.path.expandvars(line) + "\n")
            sys.stderr.flush()
            send(
                {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "protocol_version": "dpe-connector/1",
                        "plugin": {"name": "scan", "version": "1.0"},
                        "capabilities": {"supports_cursor": config.get("supports_cursor", True)},
                    },
                }
            )
        elif method == "scan":
            if "next" not in params and config.get("crash_on_scan"):
                sys.exit(3)
            if "next" not in params and config.get("scan_delay"):
                time.sleep(config["scan_delay"])
            if "next" not in params and config.get("scan_error"):
                send({"jsonrpc": "2.0", "id": id_, "error": config["scan_error"]})
                continue
            batches = config.get("batches", [[]])
            index = int(params["next"]) if "next" in params else 0
            result: dict[str, Any] = {"items": batches[index] if index < len(batches) else []}
            if index + 1 < len(batches):
                result["next"] = str(index + 1)
            else:
                result["cursor"] = config.get("cursor")
                result["incremental"] = config.get("incremental", "cursor" in params)
            send({"jsonrpc": "2.0", "id": id_, "result": result})
        elif method == "read_blob":
            handle = params["handle"]
            if handle in config.get("hang", []) and handle not in hung_once:
                hung_once.add(handle)
                hanging[id_] = handle
                continue
            blobs = config.get("blobs", {})
            if handle not in blobs:
                send({"jsonrpc": "2.0", "id": id_, "error": {"code": -32002, "message": "gone"}})
                continue
            source = blobs[handle]
            if isinstance(source, dict):  # {"file": 路径}：大对象不经实例配置传递
                with open(source["file"], "rb") as fh:
                    data = fh.read()
            else:
                data = base64.b64decode(source)
            offset, size = params.get("offset", 0), params["max_bytes"]
            chunk = data[offset : offset + size]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "data": base64.b64encode(chunk).decode(),
                        "eof": offset + len(chunk) >= len(data),
                    },
                }
            )
        elif method == "cancel":
            target = params.get("id")
            if target in hanging:
                del hanging[target]
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": target,
                        "error": {"code": -32001, "message": "cancelled"},
                    }
                )
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": id_, "result": {}})
            return


if __name__ == "__main__":
    main()
