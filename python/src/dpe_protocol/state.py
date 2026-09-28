"""同步状态：记录每个 ``file_uri`` 上次成功投递的结果。

- ``source_fingerprint``：调用方给出的源侧变更标记，未变时调用方可跳过拉取（见 :class:`~dpe_protocol.StatefulPusher`）。
- ``doc_hash``：服务端返回的权威 doc_hash，下次投递作为 ``base_doc_hash``。
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, TypeAdapter


class SyncRecord(BaseModel):
    file_uri: str
    source_fingerprint: str | None = None
    doc_hash: str
    hash_strategy_uri: str
    synced_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SyncStateStore(Protocol):
    async def get(self, file_uri: str) -> SyncRecord | None: ...

    async def put(self, record: SyncRecord) -> None: ...

    async def delete(self, file_uri: str) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self.records: dict[str, SyncRecord] = {}

    async def get(self, file_uri: str) -> SyncRecord | None:
        return self.records.get(file_uri)

    async def put(self, record: SyncRecord) -> None:
        self.records[record.file_uri] = record

    async def delete(self, file_uri: str) -> None:
        self.records.pop(file_uri, None)


_RECORDS = TypeAdapter(dict[str, SyncRecord])


class JsonFileStateStore:
    """单文件 JSON 存储，适合单进程的本地 / 定时任务场景。每次写入原子替换整个文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._records: dict[str, SyncRecord] | None = None

    async def _load(self) -> dict[str, SyncRecord]:
        if self._records is None:
            self._records = (
                _RECORDS.validate_json(await asyncio.to_thread(self.path.read_bytes)) if self.path.exists() else {}
            )
        return self._records

    async def _flush(self, records: dict[str, SyncRecord]) -> None:
        payload = json.dumps(_RECORDS.dump_python(records, mode="json"), ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        def write() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.path)

        await asyncio.to_thread(write)

    async def get(self, file_uri: str) -> SyncRecord | None:
        async with self._lock:
            return (await self._load()).get(file_uri)

    async def put(self, record: SyncRecord) -> None:
        async with self._lock:
            records = await self._load()
            records[record.file_uri] = record
            await self._flush(records)

    async def delete(self, file_uri: str) -> None:
        async with self._lock:
            records = await self._load()
            if records.pop(file_uri, None) is not None:
                await self._flush(records)
