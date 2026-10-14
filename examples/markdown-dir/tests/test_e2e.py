"""端到端（#53 验收，connector 契约 §8.3 判据 1–3）：真实 dpe-run + 插件进程 + 参考服务端。

``dpe-run``（``python -m dpe_sdk.run``）以子进程跑每一轮，插件是本包的 ``dpe-markdown-dir``
子进程，remote 是独立监听的参考服务端（``python -m dpe_sdk.testing``，真实 TCP）。

- 收敛（判据 3）：从空前缀做一次全量轮，此后源依次变化——新增、修改、中间插入一页、删除、
  改名、替换图片字节、新增一张需多次 ``read_blob`` 才读完的大图——每次变化后跑一轮增量轮，
  断言 remote 前缀内的 ``file_uri → doc_hash`` 等于同一源状态在另一个空前缀上的全量结果；
- 图片：从 remote 读回页对象，断言 Image 元素的 content_hash 等于「blob 引用 = 图片字节的
  ``sha256:``」时的元素 hash——即运行器经 ``read_blob`` 取得的正是那份字节；
- 确定性（判据 1）：同一源状态的两次全量轮得到相同映射。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import blob_ref, content_hash
from dpe_sdk.transport import DpeClient

from dpe_markdown_dir import PLUGIN_NAME, __version__

MANIFEST = Path(__file__).resolve().parents[1] / "dpe-connector.json"
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(2048)
#: 超过运行器单次 read_blob 的上限（1 MiB），必然分多块读取
BIG = os.urandom(3 * 1024 * 1024 + 17)


def command() -> list[str]:
    """插件启动命令：与解释器同目录的入口脚本（测试不经 shell，PATH 里没有 venv 的 bin）。"""
    script = Path(sys.executable).with_name("dpe-markdown-dir")
    return [str(script)]


@contextmanager
def reference_server() -> Iterator[str]:
    with tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", "--prefix", "/r/1"],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        assert proc.stdout is not None
        stdout = proc.stdout
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.append(stdout.readline()))
        reader.start()
        reader.join(timeout=20)
        try:
            if not lines or not lines[0]:
                log.seek(0)
                pytest.fail(f"参考服务端未公布地址：{log.read()}")
            yield str(json.loads(lines[0])["url"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class Runner:
    def __init__(self, tmp: Path, remote: str, root: Path) -> None:
        self.tmp, self.remote, self.root = tmp, remote, root
        self.fresh = 0
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "NO_PROXY": "127.0.0.1,localhost",  # 回环地址不经代理（macOS 会读系统代理）
            "no_proxy": "127.0.0.1,localhost",
        }

    def run(self, instance_id: str, prefix: str) -> dict[str, Any]:
        definition = {
            "definition_version": 1,
            "id": instance_id,
            "remote": {"url": self.remote},
            "uri_prefix": prefix,
            "plugin": {"manifest": str(MANIFEST), "command": command()},
            "config": {"root": str(self.root)},
        }
        path = self.tmp / f"{instance_id}.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "dpe_sdk.run",
                "--state-dir",
                str(self.tmp / f"state-{instance_id}"),
                str(path),
            ],
            capture_output=True,
            text=True,
            env=self.environ,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        report: dict[str, Any] = json.loads(proc.stdout)
        return report

    def incremental(self) -> dict[str, Any]:
        return self.run("inc", "file://inc/")

    def full_mapping(self) -> dict[str, str]:
        self.fresh += 1
        prefix = f"file://full{self.fresh}/"
        report = self.run(f"full{self.fresh}", prefix)
        assert report["incremental"] is False and report["outcome"] == "succeeded"
        return self.listing(prefix)

    def listing(self, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        with DpeClient(self.remote) as client:
            cursor: str | None = None
            while True:
                page = client.list_page(prefix, cursor)
                out.update({e.file_uri[len(prefix) :]: e.doc_hash for e in page.documents})
                cursor = page.next_cursor
                if cursor is None:
                    return out

    def element_hashes(self, uri: str) -> set[str]:
        with DpeClient(self.remote) as client:
            skeleton = client.get_skeleton(uri)
        assert skeleton is not None
        return {h for page in skeleton.pages for h in page.elements}


def image_hash(alt: str, data: bytes) -> str:
    """Image 元素在 remote 上应有的 content_hash：blob 引用为字节的 ``sha256:``。"""
    return content_hash(
        {"category": "Image", "text": alt, "mime_type": "image/png", "blob": blob_ref(data)}
    )


def test_manifest_matches_plugin_identity() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert (manifest["name"], manifest["version"]) == (PLUGIN_NAME, __version__)


def test_incremental_rounds_converge_and_images_arrive(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    (root / "img").mkdir(parents=True)
    (root / "img" / "logo.png").write_bytes(PNG)
    (root / "a.md").write_text("# 甲\n\n第一页\n\n![标志](img/logo.png)\n", encoding="utf-8")
    (root / "b.md").write_text("# 乙\n\n正文\n", encoding="utf-8")
    with reference_server() as remote:
        runner = Runner(tmp_path, remote, root)
        first = runner.incremental()
        assert first["outcome"] == "succeeded" and first["counts"]["created"] == 2
        assert image_hash("标志", PNG) in runner.element_hashes("file://inc/a.md")
        assert runner.full_mapping() == runner.full_mapping()  # 判据 1：确定性

        def step(expected: dict[str, int]) -> None:
            report = runner.incremental()
            assert report["outcome"] == "succeeded", report
            assert report["incremental"] is True, report
            for key, value in expected.items():
                assert report["counts"][key] == value, (key, report["counts"])
            assert runner.listing("file://inc/") == runner.full_mapping()

        (root / "c.md").write_text("# 丙\n", encoding="utf-8")
        step({"created": 1})
        (root / "b.md").write_text("# 乙\n\n改过的正文\n", encoding="utf-8")
        step({"updated": 1, "created": 0})
        (root / "a.md").write_text(
            "# 甲\n\n第一页\n\n![标志](img/logo.png)\n\n---\n\n插入的一页\n\n---\n\n末页\n",
            encoding="utf-8",
        )
        step({"updated": 1})
        (root / "c.md").unlink()
        step({"deleted": 1})
        os.rename(root / "b.md", root / "b2.md")
        step(
            {"moved": 1, "updated": 0, "created": 0, "unchanged": 1}
        )  # 目标 document 判为 unchanged

        changed = PNG[::-1]
        (root / "img" / "logo.png").write_bytes(changed)  # md 未动，只换图片字节
        step({"updated": 1})
        assert image_hash("标志", changed) in runner.element_hashes("file://inc/a.md")

        # 图片缺失：不带 blob 的 Image 经运行器校验流水线照常提交（§6.6）
        (root / "e.md").write_text("![缺失](img/none.png)\n", encoding="utf-8")
        step({"created": 1})
        missing = content_hash({"category": "Image", "text": "缺失"})  # 无 blob、无 mime_type
        assert missing in runner.element_hashes("file://inc/e.md")

        (root / "img" / "big.png").write_bytes(BIG)
        (root / "d.md").write_text("![大图](img/big.png)\n", encoding="utf-8")
        step({"created": 1})
        assert image_hash("大图", BIG) in runner.element_hashes("file://inc/d.md")
        assert set(runner.listing("file://inc/")) == {"a.md", "b2.md", "d.md", "e.md"}
