from pathlib import Path

from dpe_protocol import DPEPushClient, JsonFileStateStore, MemoryStateStore, StatefulPusher
from dpe_protocol.testing import FakeRobotServer
from tests.conftest import make_doc


async def test_pusher_tracks_base_doc_hash_and_fingerprint(
    tmp_path: Path, client: DPEPushClient, server: FakeRobotServer
) -> None:
    pusher = StatefulPusher(client, JsonFileStateStore(tmp_path / "state.json"))
    uri = "dpe://acme/a"

    assert await pusher.needs_push(uri, "rev-1")
    first = await pusher.push(make_doc("一", file_uri=uri), fingerprint="rev-1")
    assert first.status == "created"
    assert not await pusher.needs_push(uri, "rev-1")
    assert await pusher.needs_push(uri, "rev-2")
    assert await pusher.needs_push(uri, None)

    second = await pusher.push(make_doc("一", "二", file_uri=uri), fingerprint="rev-2")
    assert second.status == "updated" and second.contents_sent == 1

    # 状态持久化：新实例读取同一文件，base_doc_hash 自动带上
    record = await JsonFileStateStore(tmp_path / "state.json").get(uri)
    assert record is not None and record.doc_hash == server.documents[uri].doc_hash
    assert record.source_fingerprint == "rev-2"


async def test_pusher_sends_previous_doc_hash_as_base(client: DPEPushClient, server: FakeRobotServer) -> None:
    import gzip
    import json

    pusher = StatefulPusher(client, MemoryStateStore())
    first = await pusher.push(make_doc("a", file_uri="dpe://acme/a"))
    await pusher.push(make_doc("b", file_uri="dpe://acme/a"))
    negotiate = [r for r in server.requests if r.url.path.endswith(":negotiate")][-1]
    assert json.loads(gzip.decompress(negotiate.content))["base_doc_hash"] == first.doc_hash


async def test_forget_clears_local_state(client: DPEPushClient) -> None:
    pusher = StatefulPusher(client, MemoryStateStore())
    await pusher.push(make_doc("a", file_uri="dpe://acme/a"), fingerprint="rev-1")
    await pusher.forget("dpe://acme/a")
    assert await pusher.needs_push("dpe://acme/a", "rev-1")
