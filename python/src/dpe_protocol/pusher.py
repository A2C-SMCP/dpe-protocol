"""有状态推送器：在 :class:`~dpe_protocol.push.DPEPushClient` 之上记住每个 ``file_uri`` 的投递状态。

本项目以 **Document** 为边界：何时拉取、拉取哪些、如何编排，都由上层决定。
推送器只负责两件与协议相关、且每个调用方都需要的事：

- 自动携带上次投递成功时服务端返回的 ``doc_hash``（作为 ``base_doc_hash``）；
- 记录调用方给出的源侧 ``fingerprint``，供 :meth:`StatefulPusher.needs_push` 判断是否可以跳过拉取。

典型用法（由上层 connector 驱动）::

    pusher = StatefulPusher(client, JsonFileStateStore(".dpe-state/feishu.json"))
    for item in await feishu.list_changed():
        uri = make_dpe_uri("feishu-acme", f"wiki/{item.token}")
        if await pusher.needs_push(uri, item.revision):
            await pusher.push(await feishu.build_document(item), fingerprint=item.revision)
"""

from dpe_protocol.push import DPEPushClient, PushResult
from dpe_protocol.schema import Document
from dpe_protocol.state import SyncRecord, SyncStateStore


class StatefulPusher:
    def __init__(self, client: DPEPushClient, state: SyncStateStore) -> None:
        self.client = client
        self.state = state

    async def needs_push(self, file_uri: str, fingerprint: str | None) -> bool:
        """``fingerprint`` 与上次成功投递时记录的一致即返回 ``False``，调用方可跳过拉取与推送。

        ``fingerprint`` 为 ``None`` 表示调用方无法廉价判断变更，总是返回 ``True``（由 hash 协商兜底）。
        """
        if fingerprint is None:
            return True
        previous = await self.state.get(file_uri)
        return previous is None or previous.source_fingerprint != fingerprint

    async def push(self, doc: Document, *, fingerprint: str | None = None) -> PushResult:
        """投递完整文档，并在成功后记录服务端权威 ``doc_hash`` 与 ``fingerprint``。"""
        file_uri = str(doc.file_uri)
        previous = await self.state.get(file_uri)
        result = await self.client.push(doc, base_doc_hash=previous.doc_hash if previous else None)
        await self.state.put(
            SyncRecord(
                file_uri=file_uri,
                source_fingerprint=fingerprint,
                doc_hash=result.doc_hash,
                hash_strategy_uri=result.hash_strategy_uri,
            )
        )
        return result

    async def forget(self, file_uri: str) -> None:
        """清除本地状态（例如上层发现源文档已删除）。不会删除 Robot 中的文档：dpe-push/1 尚无删除语义。"""
        await self.state.delete(file_uri)
