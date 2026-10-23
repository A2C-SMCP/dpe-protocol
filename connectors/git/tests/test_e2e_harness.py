"""e2e harness 自身的前置校验（#68）：前缀规范化、凭证环境变量与清理范围。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import dpe_hash
import pytest
from dpe_sdk.errors import NotFoundError, PreconditionFailedError
from e2e_harness import (
    ENV_CREDENTIAL_ENV,
    ENV_REMOTE_URL,
    Runner,
    Seen,
    _active,
    reference_server,
    runner,
    sent_element_hashes,
    sent_page_hashes,
)
from helpers import requires_git

pytestmark = [requires_git, pytest.mark.scenarios]


def test_run_prefix_is_a_normalized_fixed_point(tmp_path: Path) -> None:
    with reference_server() as server:
        instance = Runner(tmp_path, server.url, {}, local_remote=True, root_prefix="git://e2e/")
        with _active(instance):
            assert instance.run_prefix.startswith("git://e2e/")
            assert instance.uri("repo").startswith(instance.run_prefix + "inc/")
            assert dpe_hash.normalize_file_uri(instance.run_prefix) == instance.run_prefix
            assert dpe_hash.normalize_file_uri(instance.uri("repo")) == instance.uri("repo")


@pytest.mark.parametrize("root", ["git://e2e", "git://E2E/", "git://e2e/x"])
def test_bad_root_prefix_is_rejected(tmp_path: Path, root: str) -> None:
    # 缺尾斜杠、非规范化不动点（host 大写）：都在构造 Runner 时失败，不启动代理
    with pytest.raises(ValueError):
        Runner(tmp_path, "http://127.0.0.1:1", {}, root_prefix=root)


def test_missing_credential_value_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DPE_E2E_MISSING"):
        Runner(tmp_path, "http://127.0.0.1:1", {}, credential_env="DPE_E2E_MISSING")


def test_external_mode_requires_credential_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_REMOTE_URL, "http://127.0.0.1:1")
    monkeypatch.setenv(ENV_CREDENTIAL_ENV, "DPE_E2E_MISSING")
    with pytest.raises(pytest.fail.Exception, match="DPE_E2E_MISSING"), runner(tmp_path, {}):
        pass


def test_external_mode_really_uses_the_external_remote(tmp_path: Path) -> None:
    """外部变体里确认环境确实生效——否则整套会静默退回本地参考服务端并照样通过。"""
    if not os.environ.get(ENV_REMOTE_URL):
        pytest.skip("只在外部模式运行")
    with runner(tmp_path, {"root": str(tmp_path / "root")}) as r:
        assert r.local_remote is False
        assert r.limits().max_payload_bytes >= 1  # 凭证 / CA 链路确实连到了外部 remote


def _element(text: str) -> dict[str, object]:
    return {"category": "NarrativeText", "text": text}


def _page(*elements: dict[str, object]) -> dict[str, object]:
    return {"elements": [dpe_hash.object_hash(element, "element") for element in elements]}


def test_sent_hashes_cover_inline_and_staging_forms() -> None:
    """集合断言同时认内联对象与 staging 上传路径（含分块页的路径 hash）。"""
    element = _element("x")
    page = _page(element)
    element_hash = dpe_hash.object_hash(element, "element")
    page_hash = dpe_hash.object_hash(page, "page")
    requests = [
        Seen("POST", "negotiate", json.dumps({"pages": [page]}).encode(), 200),
        Seen("PUT", "documents", json.dumps({"objects": [element]}).encode(), 200),
        Seen("PUT", f"staging/s1/pages/{page_hash}", json.dumps(page).encode(), 200),
        Seen("PUT", f"staging/s1/objects/{element_hash}", json.dumps(element).encode(), 200),
    ]
    assert sent_page_hashes(requests) == {page_hash}
    assert sent_element_hashes(requests) == {element_hash}


def test_sent_hashes_ignore_chunk_bodies_and_read_queries() -> None:
    """分块上传的请求体不是完整对象；断点查询是 GET，两者都不能被当成「送出」。"""
    element = _element("x")
    page = _page(element)
    page_hash = dpe_hash.object_hash(page, "page")
    element_hash = dpe_hash.object_hash(element, "element")
    other_page = _page(_element("y"))
    other_page_hash = dpe_hash.object_hash(other_page, "page")
    requests = [
        # 分块 PUT 的请求体是合法 JSON 的片段：若没有 Content-Range 过滤，会被误当完整页对象
        Seen(
            "PUT",
            f"staging/s1/pages/{page_hash}",
            b'{"elements": []}',
            100,
            {"content-range": "bytes 0-16/100"},
        ),
        # 断点查询是 GET，且指向另一个 page hash：若被计入，集合会多出 other_page_hash
        Seen("GET", f"staging/s1/pages/{other_page_hash}", b"", 200),
        Seen("GET", f"staging/s1/objects/{element_hash}", b"", 200),
    ]
    assert sent_page_hashes(requests) == {page_hash}
    assert sent_element_hashes(requests) == set()


class _FakeClient:
    def __init__(self, heads: list[str | None]) -> None:
        self.heads = heads
        self.deleted: list[str] = []

    def head(self, uri: str) -> str | None:
        return self.heads.pop(0)

    def delete(self, uri: str, base_hash: str) -> None:
        self.deleted.append(base_hash)


def test_cleanup_retries_cas_conflict_then_deletes() -> None:
    class Conflicting(_FakeClient):
        def delete(self, uri: str, base_hash: str) -> None:
            super().delete(uri, base_hash)
            if len(self.deleted) == 1:
                raise PreconditionFailedError("conflict")

    client = Conflicting(["dpe1:old", "dpe1:new"])
    Runner._delete_if_present(cast(Any, client), "git://x/y")
    assert client.deleted == ["dpe1:old", "dpe1:new"]  # 412 后重读再删


def test_cleanup_treats_missing_documents_as_done() -> None:
    missing = _FakeClient([None])
    Runner._delete_if_present(cast(Any, missing), "git://x/y")
    assert missing.deleted == []

    class Vanished(_FakeClient):
        def delete(self, uri: str, base_hash: str) -> None:
            raise NotFoundError("gone")

    vanisher = Vanished(["dpe1:x"])
    Runner._delete_if_present(cast(Any, vanisher), "git://x/y")  # 幂等，不抛
