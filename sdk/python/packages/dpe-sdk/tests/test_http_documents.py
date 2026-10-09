"""HTTP 绑定：读接口、快路径 commit、delete / move、条件头与错误映射（#44）。

成功路径经 ``ProtocolCore``（真实客户端路径）驱动；边界与错误用直接的 HTTP 请求断言报文。
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import dpe_hash
import httpx
import pytest
from dpe_sdk import errors
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, Force, IfAbsent
from dpe_sdk.testing import create_app
from engine_helpers import PrefixAuthorizer, inline, make_engine, text_doc
from http_helpers import REMOTE, Remote, problem, remote

pytestmark = pytest.mark.anyio

URI = "feishu://doc/a"
TARGET = "documents?uri=feishu%3A%2F%2Fdoc%2Fa"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _doc(*texts: str) -> Document:
    return Document.model_validate(text_doc(list(texts)))


async def _create(r: Remote, uri: str = URI, *texts: str) -> str:
    core = await r.core()
    result = await r.run(core.commit(uri, _doc(*(texts or ("x",))), IfAbsent()))
    return str(result.doc_hash)


def _body(*texts: str) -> bytes:
    return inline(text_doc(list(texts or ("x",)))).body()


# ---------------------------------------------------------------------------
# 经真实客户端路径：快路径与读接口
# ---------------------------------------------------------------------------


async def test_fast_path_round_trip_through_protocol_core() -> None:
    async with remote() as r:
        core = await r.core()
        assert core.capabilities.protocol == "dpe/1"
        created = await r.run(core.commit(URI, _doc("a", "b"), IfAbsent()))
        assert created.status == "created"
        assert created.doc_hash == _doc("a", "b").doc_hash()
        assert await r.run(core.head(URI)) == created.doc_hash
        assert await r.run(core.batch_head([URI, "feishu://doc/none"])) == [created.doc_hash, None]
        skeleton = await r.run(core.get_skeleton("FEISHU://doc/%61"))  # 规范化身份
        assert skeleton is not None and skeleton.file_uri == URI
        updated = await r.run(core.commit(URI, _doc("a", "c"), BaseHash(created.doc_hash)))
        assert (updated.status, updated.delta.added, updated.delta.removed) == ("updated", 1, 1)
        replay = await r.run(core.commit(URI, _doc("a", "c"), BaseHash(created.doc_hash)))
        assert replay.status == "unchanged"  # unchanged 先于条件求值（§3.2）
        forced = await r.run(core.commit(URI, _doc("z"), Force()))
        assert forced.status == "updated"
        moved = await r.run(core.move(URI, "feishu://doc/b", forced.doc_hash))
        assert moved.doc_hash == forced.doc_hash
        page = await r.run(core.list_page(prefix="feishu://"))
        assert [e.file_uri for e in page.documents] == ["feishu://doc/b"]
        await r.run(core.delete("feishu://doc/b", forced.doc_hash))
        assert await r.run(core.head("feishu://doc/b")) is None


async def test_list_pagination_and_limit() -> None:
    async with remote(make_engine(list_page_max=2)) as r:
        for i in range(3):
            await _create(r, f"test://p/{i}", str(i))
        core = await r.core()
        first = await r.run(core.list_page(prefix="test://p/"))
        assert len(first.documents) == 2 and first.next_cursor is not None
        second = await r.run(core.list_page(prefix="test://p/", cursor=first.next_cursor))
        assert [e.file_uri for e in second.documents] == ["test://p/2"]
        assert second.next_cursor is None
        everything = await r.raw("GET", "documents")  # prefix 缺省视同空串
        assert len(everything.json()["documents"]) == 2
        padded = await r.raw("GET", "documents?prefix=&limit=" + "0" * 40 + "1")
        assert len(padded.json()["documents"]) == 1  # 前导零不影响取值
        huge = "9" * 5000  # 超过 int() 的位数上限：仍须是 DPE_VALIDATION，不能逃出应用
        for bad in ("limit=0", "limit=3", "limit=-1", "limit=1.0", "limit=١", f"limit={huge}",
                    "cursor=zzz"):  # fmt: skip
            response = await r.raw("GET", f"documents?prefix=&{bad}")
            assert problem(response)["code"] == "DPE_VALIDATION", bad


async def test_document_resource_headers() -> None:
    async with remote() as r:
        doc_hash = await _create(r)
        get = await r.raw("GET", TARGET)
        assert get.status_code == 200
        assert get.headers["dpe-doc-hash"] == doc_hash
        assert get.headers["etag"] == f'"{doc_hash}"'
        assert get.headers["vary"] == "DPE-Hash-Contract"
        head = await r.raw("HEAD", TARGET)
        assert (head.status_code, head.content) == (200, b"")
        assert head.headers["dpe-doc-hash"] == doc_hash and head.headers["etag"] == f'"{doc_hash}"'
        put = await r.raw("PUT", TARGET, headers={"If-Match": f'"{doc_hash}"'}, content=_body("y"))
        assert put.status_code == 200 and put.json()["status"] == "updated"
        assert put.headers["dpe-doc-hash"] == put.json()["doc_hash"]
        assert put.headers["vary"] == "DPE-Hash-Contract"
        listed = await r.raw("GET", "documents?prefix=")
        assert listed.headers["vary"] == "DPE-Hash-Contract"
        capabilities = await r.raw("GET", "capabilities", contract=None)
        assert capabilities.status_code == 200 and "vary" not in capabilities.headers


async def test_skeleton_if_none_match_304() -> None:
    async with remote() as r:
        doc_hash = await _create(r)
        for value in (f'"{doc_hash}"', f'W/"{doc_hash}"', f'"other", "{doc_hash}"', "*"):
            response = await r.raw("GET", TARGET, headers={"If-None-Match": value})
            assert (response.status_code, response.content) == (304, b""), value
            assert response.headers["etag"] == f'"{doc_hash}"'
            assert response.headers["dpe-doc-hash"] == doc_hash
            assert response.headers["vary"] == "DPE-Hash-Contract"
        miss = await r.raw("GET", TARGET, headers={"If-None-Match": '"dpe1:' + "0" * 64 + '"'})
        assert miss.status_code == 200


async def test_head_errors_carry_only_the_code_header() -> None:
    async with remote() as r:
        missing = await r.raw("HEAD", TARGET)
        assert (missing.status_code, missing.content) == (404, b"")
        assert missing.headers["dpe-error-code"] == "DPE_NOT_FOUND"
        no_contract = await r.raw("HEAD", TARGET, contract=None)
        assert no_contract.status_code == 400
        assert no_contract.headers["dpe-error-code"] == "DPE_CONTRACT_UNSUPPORTED"
        bad_uri = await r.raw("HEAD", "documents?uri=a%20b")
        assert bad_uri.headers["dpe-error-code"] == "DPE_VALIDATION"


# ---------------------------------------------------------------------------
# CAS 与状态码映射（§3.2、§5）
# ---------------------------------------------------------------------------


async def test_commit_cas_status_mapping() -> None:
    async with remote() as r:
        absent = '"dpe1:' + "0" * 64 + '"'
        cases: list[tuple[dict[str, str], int, str]] = [
            ({}, 428, "DPE_PRECONDITION_REQUIRED"),
            ({"If-Match": absent}, 412, "DPE_NOT_FOUND"),  # 不存在 + If-Match → 412
        ]
        for headers, status, code in cases:
            response = await r.raw("PUT", TARGET, headers=headers, content=_body())
            assert (response.status_code, problem(response)["code"]) == (status, code)
        created = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=_body())
        assert created.status_code == 201
        doc_hash = created.json()["doc_hash"]
        cases = [
            ({"If-None-Match": "*"}, 412, "DPE_ALREADY_EXISTS"),
            ({"If-Match": absent}, 412, "DPE_PRECONDITION_FAILED"),
            ({"If-Match": f'W/"{doc_hash}"'}, 412, "DPE_PRECONDITION_FAILED"),  # 弱校验器永不匹配
            ({"If-Match": '"abc"'}, 412, "DPE_PRECONDITION_FAILED"),  # 按值比较（core §5.2）
            ({"If-Match": '"dpe9:' + "0" * 64 + '"'}, 412, "DPE_PRECONDITION_FAILED"),
        ]
        for headers, status, code in cases:
            response = await r.raw("PUT", TARGET, headers=headers, content=_body("other"))
            assert (response.status_code, problem(response)["code"]) == (status, code), headers
        # unchanged 先于条件求值：If-None-Match: * 也返回 200 + unchanged（§3.2 有意偏离）
        same = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=_body())
        assert same.status_code == 200 and same.json()["status"] == "unchanged"
        assert same.headers["dpe-doc-hash"] == doc_hash


@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": "*"},
        {"If-Match": '"a", "b"'},
        {"If-Match": "dpe1:abc"},
        {"If-None-Match": '"x"'},
        {"If-Match": '"a"', "If-None-Match": "*"},
    ],
)
async def test_malformed_condition_headers_are_validation(headers: dict[str, str]) -> None:
    async with remote() as r:
        response = await r.raw("PUT", TARGET, headers=headers, content=_body())
        assert (response.status_code, problem(response)["code"]) == (400, "DPE_VALIDATION")
        # 报文校验末步：契约声明先于它
        response = await r.raw("PUT", TARGET, contract=None, headers=headers, content=_body())
        assert problem(response)["code"] == "DPE_CONTRACT_UNSUPPORTED"


async def test_force_with_condition_header_is_validation() -> None:
    async with remote() as r:
        body = json.loads(_body())
        body["force"] = True
        response = await r.raw(
            "PUT", TARGET, headers={"If-None-Match": "*"}, content=json.dumps(body).encode()
        )
        assert problem(response)["code"] == "DPE_VALIDATION"


async def test_delete_mapping() -> None:
    async with remote() as r:
        doc_hash = await _create(r)
        assert problem(await r.raw("DELETE", TARGET))["status"] == 428
        stale = await r.raw("DELETE", TARGET, headers={"If-Match": '"dpe1:' + "0" * 64 + '"'})
        assert (stale.status_code, problem(stale)["code"]) == (412, "DPE_PRECONDITION_FAILED")
        absent = await r.raw("DELETE", TARGET, headers={"If-None-Match": "*"})
        assert (absent.status_code, problem(absent)["code"]) == (400, "DPE_VALIDATION")
        done = await r.raw("DELETE", TARGET, headers={"If-Match": f'"{doc_hash}"'})
        assert (done.status_code, done.content) == (204, b"")
        assert "dpe-doc-hash" not in done.headers and "etag" not in done.headers
        again = await r.raw("DELETE", TARGET, headers={"If-Match": f'"{doc_hash}"'})
        assert (again.status_code, problem(again)["code"]) == (412, "DPE_NOT_FOUND")


async def test_move_mapping_and_content_location() -> None:
    async with remote() as r:
        doc_hash = await _create(r)
        await _create(r, "feishu://doc/taken", "t")

        async def move(payload: dict[str, Any]) -> Any:
            return await r.raw("POST", "move", content=json.dumps(payload).encode())

        ok = await move({"from_uri": URI, "to_uri": "FEISHU://doc/%62", "base_hash": doc_hash})
        assert ok.status_code == 200 and ok.json() == {"doc_hash": doc_hash}
        assert ok.headers["dpe-doc-hash"] == doc_hash
        assert ok.headers["content-location"] == f"{REMOTE}/documents?uri=feishu%3A%2F%2Fdoc%2Fb"
        assert "etag" not in ok.headers
        replay = await move({"from_uri": URI, "to_uri": "feishu://doc/b", "base_hash": doc_hash})
        assert replay.status_code == 200  # 已达成即成功（core §5.2 第 4 步）
        b, c = "feishu://doc/b", "feishu://doc/c"
        cases: list[tuple[dict[str, str], int, str]] = [
            ({"from_uri": URI, "to_uri": c, "base_hash": doc_hash}, 404, "DPE_NOT_FOUND"),
            ({"from_uri": b, "to_uri": c, "base_hash": "x"}, 409, "DPE_PRECONDITION_FAILED"),
            ({"from_uri": b, "to_uri": "feishu://doc/taken", "base_hash": doc_hash}, 409,
             "DPE_ALREADY_EXISTS"),
            ({"from_uri": b, "to_uri": c}, 428, "DPE_PRECONDITION_REQUIRED"),
        ]  # fmt: skip
        for payload, status, code in cases:
            response = await move(payload)
            assert (response.status_code, problem(response)["code"]) == (status, code), payload


async def test_public_url_overrides_content_location() -> None:
    async with remote(public_url="https://dpe.example/remote") as r:
        doc_hash = await _create(r)
        payload = {"from_uri": URI, "to_uri": "feishu://doc/b", "base_hash": doc_hash}
        response = await r.raw("POST", "move", content=json.dumps(payload).encode())
        location = response.headers["content-location"]
        assert location == "https://dpe.example/remote/documents?uri=feishu%3A%2F%2Fdoc%2Fb"


async def test_missing_content_problem_body() -> None:
    async with remote(make_engine(missing_max=1)) as r:
        req = inline(text_doc(["a"], ["b"]))  # 两页都缺，清单截断为一项
        body = json.dumps({"document": req.document}).encode()
        response = await r.raw("PUT", TARGET, headers={"If-None-Match": "*"}, content=body)
        detail = problem(response)
        assert (response.status_code, detail["code"]) == (400, "DPE_MISSING_CONTENT")
        assert detail["type"] == "urn:dpe:error:missing-content"
        assert detail["title"] == "missing content"
        assert detail["retryable"] is False
        assert detail["missing"] == {
            "pages": req.page_hashes[:1],
            "content_hashes": [],
            "blobs": [],
        }
        assert detail["missing_truncated"] is True


async def test_forbidden_and_problem_shape() -> None:
    engine = make_engine(authorizer=PrefixAuthorizer({"Bearer ok": ("feishu://",)}))
    async with remote(engine) as r:
        response = await r.raw(
            "PUT",
            TARGET,
            headers={"If-None-Match": "*", "Authorization": "Bearer no"},
            content=_body(),
        )
        assert problem(response) == {
            "type": "urn:dpe:error:forbidden",
            "title": "forbidden",
            "status": 403,
            "detail": "无权写入 feishu://doc/a",
            "code": "DPE_FORBIDDEN",
            "retryable": False,
        }
        allowed = await r.raw(
            "PUT",
            TARGET,
            headers={"If-None-Match": "*", "Authorization": "Bearer ok"},
            content=_body(),
        )
        assert allowed.status_code == 201


# ---------------------------------------------------------------------------
# 传输层：认证、路由、请求体编码、查询参数
# ---------------------------------------------------------------------------


async def test_authenticator_401_comes_first() -> None:
    def authenticate(authorization: str | None) -> str | None:
        return "alice" if authorization == "Bearer alice" else None

    async with remote(authenticate=authenticate) as r:
        response = await r.raw("PUT", TARGET, contract=None, content=b"not json")
        assert response.status_code == 401
        # 认证先于查询参数的形态校验
        bad_query = await r.raw("GET", "documents?uri=a&uri=b")
        assert bad_query.status_code == 401 and "dpe-error-code" not in bad_query.headers
        assert response.headers["www-authenticate"] == 'Bearer realm="dpe"'
        assert "dpe-error-code" not in response.headers  # 不是 DPE 错误
        ok = await r.raw("GET", "capabilities", headers={"Authorization": "Bearer alice"})
        assert ok.status_code == 200


async def test_routes_and_methods_are_not_dpe_errors() -> None:
    async with remote() as r:
        for target in ("nope", "documents/x", "staging/s/pages", "staging//pages/h"):
            response = await r.raw("GET", target)
            assert response.status_code == 404 and "dpe-error-code" not in response.headers
        outside = await r.client.get("http://dpe.test/capabilities")  # remote 之外的路径
        assert outside.status_code == 404
        response = await r.raw("POST", "documents")
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD, PUT, DELETE"
        assert "dpe-error-code" not in response.headers
        response = await r.raw("HEAD", "staging/s/objects/h")
        assert (response.status_code, response.headers["allow"]) == (405, "PUT")


async def test_gzip_request_body() -> None:
    async with remote(make_engine(max_payload_bytes=4096)) as r:
        body = _body("x" * 3000)
        response = await r.raw(
            "PUT",
            TARGET,
            headers={"If-None-Match": "*", "Content-Encoding": "gzip"},
            content=gzip.compress(body),
        )
        assert response.status_code == 201
        # 解码后超限 → 413（按压缩前计算，§7），即使压缩后很小
        big = gzip.compress(_body("y" * 5000))
        assert len(big) < 4096
        response = await r.raw(
            "PUT", TARGET, headers={"If-Match": '"x"', "Content-Encoding": "gzip"}, content=big
        )
        assert (response.status_code, problem(response)["code"]) == (413, "DPE_PAYLOAD_TOO_LARGE")
        corrupt = await r.raw(
            "PUT", TARGET, headers={"If-Match": '"x"', "Content-Encoding": "gzip"}, content=b"oops"
        )
        assert problem(corrupt)["code"] == "DPE_VALIDATION"
        truncated = await r.raw(
            "PUT",
            TARGET,
            headers={"If-Match": '"x"', "Content-Encoding": "gzip"},
            content=gzip.compress(body)[:-8],
        )
        assert problem(truncated)["code"] == "DPE_VALIDATION"
        unsupported = await r.raw("PUT", TARGET, headers={"Content-Encoding": "br"}, content=body)
        assert unsupported.status_code == 415
        assert unsupported.headers["accept-encoding"] == "gzip"
        assert "dpe-error-code" not in unsupported.headers


async def test_payload_too_large_is_transport_step() -> None:
    async with remote(make_engine(max_payload_bytes=64)) as r:
        response = await r.raw("POST", "heads", contract=None, content=b"x" * 65)
        assert (response.status_code, problem(response)["code"]) == (413, "DPE_PAYLOAD_TOO_LARGE")


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "documents?uri=test%3A%2F%2Fa&prefix=test"),
        ("GET", "documents?uri=test%3A%2F%2Fa&limit=1"),
        ("GET", "documents?uri=test%3A%2F%2Fa&uri=test%3A%2F%2Fb"),
        ("GET", "documents?prefix=%FF"),
        ("HEAD", "documents"),
        ("HEAD", "documents?prefix=test"),
        ("DELETE", "documents?prefix=test"),
        ("PUT", "documents"),
    ],
)
async def test_query_shape_is_validation(method: str, target: str) -> None:
    async with remote() as r:
        response = await r.raw(method, target, headers={"If-Match": '"x"'}, content=_body())
        assert response.status_code == 400
        assert response.headers["dpe-error-code"] == "DPE_VALIDATION"


async def test_query_of_other_endpoints_is_ignored() -> None:
    async with remote() as r:
        assert (await r.raw("GET", "capabilities?x=%FF&uri=a&uri=b")).status_code == 200
        response = await r.raw("POST", "heads?uri=a&uri=b&x=%FF", content=b'{"uris": []}')
        assert response.status_code == 200
        # 文档资源：未定义的参数（含无法解码的）忽略
        assert (await r.raw("GET", "documents?prefix=&x=%FF&%FF=1")).status_code == 200


def test_create_app_rejects_non_ascii_urls() -> None:
    rejected: list[dict[str, str]] = [
        {"prefix": "/r/中"},
        {"prefix": "/r 1"},
        {"prefix": "/r%201"},  # 路由比较的是已解码的 path：带编码的前缀无法同时用于两处
        {"public_url": "https://例.cn/r"},
        {"public_url": "http://h/%zz"},
        {"public_url": "http://h/?q=1"},
        {"public_url": "not-a-url"},
    ]
    for options in rejected:
        with pytest.raises(ValueError):
            create_app(make_engine(), **options)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        create_app(make_engine(), max_threads=0)
    for url in ("http://[::1]:8080/r", "https://u:p@h.example:443/a%20b", "http://h/api;v=1"):
        create_app(make_engine(), public_url=url)
    create_app(make_engine(), prefix="/api;v=1/r:1@x")


async def test_prefix_with_sub_delims_routes() -> None:
    engine = make_engine()
    app = create_app(engine, prefix="/api;v=1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        assert (await client.get("http://t/api;v=1/capabilities")).status_code == 200


async def test_root_path_mount() -> None:
    """挂载在 root_path 下（ASGI）：按段边界剥离；Content-Location 保留 sub-delims。"""
    app = create_app(make_engine(), prefix="/r/1")
    transport = httpx.ASGITransport(app=app, root_path="/m;v=1")
    async with httpx.AsyncClient(transport=transport) as client:
        base = "http://t/m;v=1/r/1"
        headers = {"DPE-Hash-Contract": "dpe1", "If-None-Match": "*"}
        created = await client.put(f"{base}/{TARGET}", headers=headers, content=_body())
        assert created.status_code == 201
        payload = {
            "from_uri": URI,
            "to_uri": "feishu://doc/b",
            "base_hash": created.json()["doc_hash"],
        }
        moved = await client.post(
            f"{base}/move",
            headers={"DPE-Hash-Contract": "dpe1"},
            content=json.dumps(payload).encode(),
        )
        assert moved.headers["content-location"].startswith(f"{base}/documents?uri=")
        outside = await client.get("http://t/m;v=1x/r/1/capabilities")
        assert outside.status_code == 404


async def test_ipv6_public_url_in_content_location() -> None:
    async with remote(public_url="http://[::1]:8080/r") as r:
        doc_hash = await _create(r)
        payload = {"from_uri": URI, "to_uri": "feishu://doc/b", "base_hash": doc_hash}
        response = await r.raw("POST", "move", content=json.dumps(payload).encode())
        assert response.headers["content-location"].startswith("http://[::1]:8080/r/documents?")


async def test_authorizer_and_authenticator_may_call_back_into_the_app() -> None:
    """授权器 / 认证器在工作线程里运行：经 HTTP 回调本应用不阻塞事件循环。"""
    import anyio.from_thread

    seen: list[int] = []

    def authenticate(authorization: str | None) -> str | None:
        if authorization == "outer":
            reply = anyio.from_thread.run(r.raw, "GET", "capabilities")
            seen.append(reply.status_code)
        return authorization or ""

    async with remote(authenticate=authenticate) as r:
        response = await r.raw("GET", "documents?prefix=", headers={"Authorization": "outer"})
        assert response.status_code == 200 and seen == [200]


async def test_query_plus_is_not_space() -> None:
    async with remote() as r:
        doc_hash = await _create(r, "test://a+b")
        response = await r.raw("HEAD", "documents?uri=test%3A%2F%2Fa+b")
        assert response.headers["dpe-doc-hash"] == doc_hash


async def test_engine_state_is_shared_with_direct_calls() -> None:
    """HTTP 写入与引擎直接读互相可见（同一个引擎实例）。"""
    async with remote() as r:
        doc_hash = await _create(r)
        assert r.engine.head("", URI, dpe_hash.CONTRACT) is not None
        assert doc_hash.startswith("dpe1:")


async def test_protocol_core_sees_dpe_errors_by_code() -> None:
    async with remote() as r:
        core = await r.core()
        await _create(r)
        with pytest.raises(errors.AlreadyExistsError):
            await r.run(core.commit(URI, _doc("other"), IfAbsent()))
        with pytest.raises(errors.PreconditionFailedError):
            await r.run(core.delete(URI, "dpe1:" + "0" * 64))
