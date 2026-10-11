"""插件机制：Target 的请求契约（前缀合并、契约头、身份、URI 命名空间）与计数归章。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from dpe_conformance.config import ConformanceConfig, Identity
from dpe_conformance.plugin import Target, _section_of, _with_trailing_slash, checks_root

_CHECKS = checks_root()


def _target(recorded: list[httpx.Request]) -> Target:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={})

    return Target(
        base_url="http://dpe.test/r/1",
        config=ConformanceConfig.empty(),
        client=httpx.Client(
            base_url=_with_trailing_slash("http://dpe.test/r/1"),
            transport=httpx.MockTransport(handler),
        ),
        uri_prefix="dpe://conformance/abcdef12/",
    )


def test_with_trailing_slash() -> None:
    assert _with_trailing_slash("http://dpe.test/r/1") == "http://dpe.test/r/1/"
    assert _with_trailing_slash("http://dpe.test/r/1/") == "http://dpe.test/r/1/"


def test_target_send_keeps_path_prefix_and_headers() -> None:
    """remote 带路径前缀（如 /r/1）时，相对路径必须落在前缀之下（RFC 3986 合并）。"""
    recorded: list[httpx.Request] = []
    target = _target(recorded)
    target.send("GET", "capabilities", contract=None)
    assert recorded[0].url.path == "/r/1/capabilities"
    assert "DPE-Hash-Contract" not in recorded[0].headers

    target.send(
        "HEAD",
        "documents",
        identity=Identity(role="full", authorization="Bearer x"),
        params={"uri": "dpe://x"},
    )
    assert recorded[1].url.path == "/r/1/documents"
    assert recorded[1].url.params["uri"] == "dpe://x"
    assert recorded[1].headers["DPE-Hash-Contract"] == "dpe1"
    assert recorded[1].headers["Authorization"] == "Bearer x"


def test_target_uri_uses_run_namespace() -> None:
    target = _target([])
    assert target.uri("smoke/x") == "dpe://conformance/abcdef12/smoke/x"
    assert target.uri("/smoke/x") == "dpe://conformance/abcdef12/smoke/x"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # 一级模块：取文件名
        (_CHECKS / "smoke.py", "smoke"),
        # 包章节：取包名（嵌套子目录仍归一级包）
        (_CHECKS / "cas" / "test_commit.py", "cas"),
        (_CHECKS / "cas" / "nested" / "test_deep.py", "cas"),
        # 套件之外的用例（测试注入的合成章节）按文件名归章
        (Path("/tmp/elsewhere/test_synthetic.py"), "test_synthetic"),
    ],
)
def test_section_of_attribution(path: Path, expected: str) -> None:
    assert _section_of(path, "x::t") == expected


def test_section_of_keeps_same_named_modules_apart() -> None:
    """两个包各有同名测试模块时不得并桶。"""
    first = _section_of(_CHECKS / "cas" / "test_commit.py", "x::t")
    second = _section_of(_CHECKS / "merge" / "test_commit.py", "x::t")
    assert (first, second) == ("cas", "merge")


def test_section_of_without_recorded_path_falls_back_to_nodeid_stem() -> None:
    """logstart 未触发（防御路径）：退化为 nodeid 文件段的文件名。"""
    assert _section_of(None, "cas/test_commit.py::test_x") == "test_commit"
