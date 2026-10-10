"""pytest 夹具：见 ``helpers`` 的现场构造说明。"""

from __future__ import annotations

import pytest


@pytest.fixture
def fixture_files() -> dict[bytes | str, bytes]:
    """覆盖各映射分支的一份文件集合（目录与文件名都含非 ASCII）。"""
    return {
        "docs/说明.md": (
            b"---\ntitle: \xe8\xaf\xb4\xe6\x98\x8e\n---\n\n"
            b"# \xe6\xa0\x87\xe9\xa2\x98\n\n\xe6\xad\xa3\xe6\x96\x87\xe3\x80\x82\n"
        ),
        "notes.txt": b"first\n\nsecond\n",
        "list.md": b"# \xe6\xb8\x85\xe5\x8d\x95\n\n- \xe7\x94\xb2\n- \xe4\xb9\x99\n",
        "data.csv": b"name,age\nalice,30\n",
        "metrics.ndjson": b'{"a":1}\n{"b":2}\n',
        "config.json": b'{"z":1,"a":[true,null]}',
        "script.py": b"print('hi')\n\n# \xe6\xb3\xa8\xe9\x87\x8a\n",
        "Makefile": b"all:\n\techo hi\n",
        "img.png": b"\x89PNG\r\n\x1a\n\x00bad",
        "readme.html": b"<html><body>x</body></html>",
        "bad.md": b"\xff\xfe not utf8",
    }
