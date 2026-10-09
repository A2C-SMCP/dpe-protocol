"""HTTP 绑定：暂存路径（negotiate → 逐层上传 → commit 引用会话）、分块与断点查询（#44）。"""

from __future__ import annotations

import gzip
import json
from typing import Any

import dpe_hash
import httpx
import pytest
from engine_helpers import FakeClock, make_engine
from http_helpers import Remote, problem, remote

pytestmark = pytest.mark.anyio

C = dpe_hash.CONTRACT
URI = "test://docs/a"
TARGET = "documents?uri=test%3A%2F%2Fdocs%2Fa"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _element(body: dict[str, Any]) -> tuple[bytes, str]:
    return json.dumps(body).encode(), dpe_hash.object_hash(body, "element", C)


def _page(elements: list[str]) -> tuple[bytes, str]:
    body = {"elements": elements}
    return json.dumps(body).encode(), dpe_hash.object_hash(body, "page", C)


async def _negotiate(r: Remote, document: dict[str, Any]) -> dict[str, Any]:
    payload = {"file_uri": URI, "document": document}
    response = await r.raw("POST", "negotiate", content=json.dumps(payload).encode())
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_staging_path_end_to_end() -> None:
    clock = FakeClock()
    async with remote(make_engine(clock=clock, blob_chunk_bytes=40, max_payload_bytes=200)) as r:
        blob = b"0123456789"
        ref = dpe_hash.blob_ref(blob)
        text_raw, text_hash = _element({"category": "NarrativeText", "text": "hello"})
        image_raw, image_hash = _element(
            {"category": "Image", "blob": ref, "mime_type": "image/png"}
        )
        more_raw, more_hash = _element({"category": "Title", "text": "t"})
        page_raw, page_hash = _page([text_hash, image_hash, more_hash])
        document = {"file_type": "md", "pages": [page_hash]}
        doc_hash = dpe_hash.object_hash(document, "document", C)

        negotiated = await _negotiate(r, document)
        assert negotiated["missing_pages"] == [page_hash]
        sid = negotiated["staging_session"]["id"]
        base = f"staging/{sid}"

        # 页对象分块上传：中间块 202 + 偏移，断点查询不续期，最后一块给出缺失元素
        assert len(page_raw) > 200  # 单个页对象超过 max_payload_bytes，只能分块
        offset = await r.raw("HEAD", f"{base}/pages/{page_hash}")
        assert (offset.status_code, offset.headers["dpe-upload-offset"]) == (200, "0")
        step = 40
        for start in range(0, len(page_raw), step):
            piece = page_raw[start : start + step]
            end = start + len(piece) - 1
            clock.advance(10)
            response = await r.raw(
                "PUT",
                f"{base}/pages/{page_hash}",
                headers={"Content-Range": f"bytes {start}-{end}/{len(page_raw)}"},
                content=piece,
            )
            if end + 1 < len(page_raw):
                assert (response.status_code, response.content) == (202, b"")
                assert response.headers["dpe-upload-offset"] == str(end + 1)
                expires = response.headers["dpe-session-expires"]
                clock.advance(10)
                head = await r.raw("HEAD", f"{base}/pages/{page_hash}")
                assert head.headers["dpe-session-expires"] == expires  # 不续期
                assert head.headers["dpe-upload-offset"] == str(end + 1)
            else:
                assert response.status_code == 201
                assert response.json() == {
                    "missing_content_hashes": [text_hash, image_hash, more_hash]
                }
                assert "dpe-session-expires" in response.headers
        # 偏移不连续：400 + DPE-Upload-Offset（重发已完成的块则命中「已完成」→ 200）
        again = await r.raw(
            "PUT",
            f"{base}/pages/{page_hash}",
            headers={"Content-Range": f"bytes 0-3/{len(page_raw)}"},
            content=page_raw[:4],
        )
        assert again.status_code == 200

        for raw, h in ((text_raw, text_hash), (more_raw, more_hash)):
            response = await r.raw("PUT", f"{base}/objects/{h}", content=raw)
            assert response.status_code == 201 and response.json() == {"missing_blobs": []}
        response = await r.raw("PUT", f"{base}/objects/{image_hash}", content=image_raw)
        assert response.status_code == 201
        assert response.json() == {"missing_blobs": [ref]}
        duplicate = await r.raw("PUT", f"{base}/objects/{image_hash}", content=image_raw)
        assert duplicate.status_code == 200

        # blob 分块，含偏移错误带头
        first = await r.raw(
            "PUT",
            f"{base}/blobs/{ref}",
            headers={"Content-Range": "bytes 0-3/10"},
            content=blob[:4],
        )
        assert first.status_code == 202
        wrong = await r.raw(
            "PUT",
            f"{base}/blobs/{ref}",
            headers={"Content-Range": "bytes 8-9/10"},
            content=blob[8:],
        )
        assert (wrong.status_code, problem(wrong)["code"]) == (400, "DPE_VALIDATION")
        assert wrong.headers["dpe-upload-offset"] == "4"
        middle = await r.raw(
            "PUT",
            f"{base}/blobs/{ref}",
            headers={"Content-Range": "bytes 4-7/10"},
            content=blob[4:8],
        )
        assert middle.status_code == 202
        last = await r.raw(
            "PUT",
            f"{base}/blobs/{ref}",
            headers={"Content-Range": "bytes 8-9/10"},
            content=blob[8:],
        )
        assert last.status_code == 201 and last.json() == {}
        done = await r.raw("HEAD", f"{base}/blobs/{ref}")
        assert done.headers["dpe-upload-offset"] == "10"

        # 读接口看不到中间态
        assert (await r.raw("HEAD", TARGET)).status_code == 404

        commit = json.dumps({"document": document, "staging_session": sid}).encode()
        response = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=commit)
        assert response.status_code == 201 and response.json()["doc_hash"] == doc_hash
        replay = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=commit)
        assert replay.json()["status"] == "unchanged"  # 原样重试不得 410
        consumed = await r.raw("HEAD", f"{base}/blobs/{ref}")
        assert (consumed.status_code, consumed.headers["dpe-error-code"]) == (
            410,
            "DPE_SESSION_EXPIRED",
        )


async def test_whole_blob_upload_and_gzip_hash_is_over_decoded_bytes() -> None:
    async with remote() as r:
        sid = (await _negotiate(r, {"file_type": "md", "pages": []}))["staging_session"]["id"]
        blob = b"\x00\x01binary" * 10
        ref = dpe_hash.blob_ref(blob)
        response = await r.raw(
            "PUT",
            f"staging/{sid}/blobs/{ref}",
            headers={"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
            content=gzip.compress(blob),
        )
        assert response.status_code == 201
        mismatch = await r.raw("PUT", f"staging/{sid}/blobs/{ref}", content=b"other")
        assert (mismatch.status_code, problem(mismatch)["code"]) == (400, "DPE_HASH_MISMATCH")


async def test_chunk_ladder_order_over_http() -> None:
    async with remote() as r:
        sid = (await _negotiate(r, {"file_type": "md", "pages": []}))["staging_session"]["id"]
        blob = b"abcdef"
        ref = dpe_hash.blob_ref(blob)
        target = f"staging/{sid}/blobs/{ref}"

        async def put(headers: dict[str, str], content: bytes = b"x", **kw: Any) -> httpx.Response:
            return await r.raw("PUT", target, headers=headers, content=content, **kw)

        # 会话先于分块参数；契约先于会话
        expired = await r.raw(
            "PUT", f"staging/st-none/blobs/{ref}", headers={"Content-Range": "junk"}, content=b"x"
        )
        assert (expired.status_code, problem(expired)["code"]) == (410, "DPE_SESSION_EXPIRED")
        no_contract = await put({"Content-Range": "junk"}, contract=None)
        assert problem(no_contract)["code"] == "DPE_CONTRACT_UNSUPPORTED"
        for headers in (
            {"Content-Range": "junk"},
            {"Content-Range": "bytes */6"},
            {"Content-Range": "bytes 0-" + "9" * 5000 + "/6"},  # 超长数字不逃出应用
            {"Content-Range": f"bytes 0-0/{2**63}"},
            {"Content-Range": "bytes 0-0/6", "Content-Encoding": "gzip"},
        ):
            assert problem(await put(headers))["code"] == "DPE_VALIDATION", headers
        # identity 不是编码：分块照常进行
        partial_upload = await put(
            {"Content-Range": "bytes 0-2/6", "Content-Encoding": "identity"}, content=blob[:3]
        )
        assert partial_upload.status_code == 202
        assert (await r.raw("PUT", target, content=blob)).status_code == 201
        # 已完成：任何分块参数（含语法非法、带 Content-Encoding）都幂等 200
        for headers in (
            {"Content-Range": "junk"},
            {"Content-Range": "bytes 0-" + "9" * 5000 + "/6"},
            {"Content-Range": "bytes 0-0/1", "Content-Encoding": "gzip"},
        ):
            assert (await put(headers)).status_code == 200, headers


async def test_target_syntax_and_element_content_range() -> None:
    async with remote() as r:
        sid = (await _negotiate(r, {"file_type": "md", "pages": []}))["staging_session"]["id"]
        for target in (f"staging/{sid}/pages/sha256:{'0' * 64}", f"staging/{sid}/blobs/dpe1:x"):
            head = await r.raw("HEAD", target)
            assert (head.status_code, head.headers["dpe-error-code"]) == (400, "DPE_VALIDATION")
        raw, h = _element({"category": "NarrativeText", "text": "x"})
        response = await r.raw(
            "PUT",
            f"staging/{sid}/objects/{h}",
            headers={"Content-Range": "bytes 0-0/1"},
            content=raw,
        )
        assert problem(response)["code"] == "DPE_VALIDATION"
        # 位置：413（第 0 步）→ 契约 → Content-Range，先于 I-JSON
        not_json = await r.raw(
            "PUT",
            f"staging/{sid}/objects/{h}",
            headers={"Content-Range": "bytes 0-0/1"},
            content=b"{",
        )
        assert problem(not_json)["code"] == "DPE_VALIDATION"
        assert "Content-Range" in problem(not_json)["detail"]
        no_contract = await r.raw(
            "PUT",
            f"staging/{sid}/objects/{h}",
            contract=None,
            headers={"Content-Range": "bytes 0-0/1"},
            content=b"{",
        )
        assert problem(no_contract)["code"] == "DPE_CONTRACT_UNSUPPORTED"
        # 不带 Content-Range：§3.1 顺序不变（I-JSON 先于契约）
        plain = await r.raw("PUT", f"staging/{sid}/objects/{h}", contract=None, content=b"{")
        assert problem(plain)["code"] == "DPE_VALIDATION"
        assert "Content-Range" not in problem(plain)["detail"]
        # 非分块：重算 hash 与路径比较（§4.6 第 2 步）
        tampered = await r.raw("PUT", f"staging/{sid}/objects/dpe1:{'0' * 64}", content=raw)
        assert problem(tampered)["code"] == "DPE_HASH_MISMATCH"


async def test_element_content_range_payload_limit_first() -> None:
    """元素对象带 Content-Range：413（第 0 步）先于契约（#83）。"""
    async with remote(make_engine(max_payload_bytes=64)) as r:
        response = await r.raw(
            "PUT",
            f"staging/st-x/objects/dpe1:{'0' * 64}",
            contract=None,
            headers={"Content-Range": "bytes 0-64/65"},
            content=b"x" * 65,
        )
        assert (response.status_code, problem(response)["code"]) == (413, "DPE_PAYLOAD_TOO_LARGE")


async def test_session_isolation_by_caller() -> None:
    async with remote() as r:
        payload = json.dumps({"file_uri": URI, "document": {"file_type": "md", "pages": []}})
        opened = await r.raw(
            "POST", "negotiate", headers={"Authorization": "alice"}, content=payload.encode()
        )
        sid = opened.json()["staging_session"]["id"]
        target = f"staging/{sid}/pages/dpe1:{'0' * 64}"
        assert (await r.raw("HEAD", target, headers={"Authorization": "alice"})).status_code == 200
        other = await r.raw("HEAD", target, headers={"Authorization": "bob"})
        assert (other.status_code, other.headers["dpe-error-code"]) == (410, "DPE_SESSION_EXPIRED")
