"""示例脚本本身也要通过一致性检查，保证可以作为上层调用方的模板。"""

import json
from pathlib import Path

from push_dpe_json import check_directory as check_json_dir
from push_dpe_json import push_directory as push_json_dir
from push_markdown_dir import check_directory, markdown_to_elements, push_directory

from dpe_protocol import DPEPushClient, MemoryStateStore, StatefulPusher
from dpe_protocol.testing import FakeRobotServer

MD = """# 员工手册

欢迎加入。
这是第二行。

- 第一条
- 第二条

```python
print("hi")
```

| 名称 | 值 |
| --- | --- |
| a | 1 |
"""


def test_markdown_to_elements() -> None:
    elements = markdown_to_elements(MD)
    assert [(e.category.value, e.text) for e in elements] == [
        ("Title", "员工手册"),
        ("NarrativeText", "欢迎加入。\n这是第二行。"),
        ("ListItem", "第一条"),
        ("ListItem", "第二条"),
        ("CodeSnippet", 'print("hi")'),
        ("Table", "名称 值\na 1"),
    ]


async def test_markdown_example_conforms_and_pushes(
    tmp_path: Path, client: DPEPushClient, server: FakeRobotServer
) -> None:
    (tmp_path / "handbook.md").write_text(MD, encoding="utf-8")
    (tmp_path / "faq.md").write_text("# FAQ\n\nQ: 如何请假？\n", encoding="utf-8")
    (await check_directory(tmp_path, tenant="acme", prefix="wiki")).raise_for_issues()

    pusher = StatefulPusher(client, MemoryStateStore())
    first = await push_directory(tmp_path, pusher, tenant="acme", prefix="wiki")
    assert all(r is not None and r.status == "created" for r in first.values())
    assert set(server.documents) == {"dpe://acme/wiki/handbook.md", "dpe://acme/wiki/faq.md"}

    (tmp_path / "faq.md").write_text("# FAQ\n\nQ: 如何请假？\n\nQ: 如何报销？\n", encoding="utf-8")
    second = await push_directory(tmp_path, pusher, tenant="acme", prefix="wiki")
    assert second["dpe://acme/wiki/handbook.md"] is None  # 未变，跳过
    faq = second["dpe://acme/wiki/faq.md"]
    assert faq is not None and faq.contents_sent == 1


async def test_dpe_json_example_conforms_and_pushes(
    tmp_path: Path, client: DPEPushClient, server: FakeRobotServer
) -> None:
    kernel_dump = {
        "doc_id": 7, "doc_hash": "x", "file_uri": "dpe://acme/a", "file_type": "txt",
        "doc_metadata": {"created_at": None},
        "pages": [{"page_id": 1, "number": 0, "title": None, "elements": [{"ele_id": 3, "text": "hello"}]}],
    }  # fmt: skip
    (tmp_path / "a.dpe.json").write_text(json.dumps(kernel_dump), encoding="utf-8")
    (await check_json_dir(tmp_path)).raise_for_issues()
    results = await push_json_dir(tmp_path, StatefulPusher(client, MemoryStateStore()))
    assert results["dpe://acme/a"] is not None and set(server.documents) == {"dpe://acme/a"}
