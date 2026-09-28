"""示例：上游已产出的 DPE JSON（内核 ``Document`` dump 格式）→ Robot。

适用于上游已经完成解析的场景（例如解析服务的输出）：读取 ``*.dpe.json``，
经 :meth:`~dpe_protocol.Document.from_kernel_dump` 剔除内核字段后投递。

运行::

    uv run python examples/push_dpe_json.py <dir> [--push URL --token TOKEN]
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from dpe_protocol import Document, DPEPushClient, JsonFileStateStore, PushResult, StatefulPusher
from dpe_protocol.testing import ConformanceReport, check_documents


def load(path: Path) -> Document:
    return Document.from_kernel_dump(json.loads(path.read_bytes()))


async def check_directory(root: Path) -> ConformanceReport:
    report = ConformanceReport()
    for path in sorted(root.glob("**/*.dpe.json")):
        report.issues += (await check_documents(load(path), load(path))).issues
    return report


async def push_directory(root: Path, pusher: StatefulPusher) -> dict[str, PushResult | None]:
    results: dict[str, PushResult | None] = {}
    for path in sorted(root.glob("**/*.dpe.json")):
        doc = load(path)
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        uri = str(doc.file_uri)
        results[uri] = (
            await pusher.push(doc, fingerprint=fingerprint) if await pusher.needs_push(uri, fingerprint) else None
        )
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--push", metavar="ROBOT_URL")
    parser.add_argument("--token")
    parser.add_argument("--state", default=".dpe-state/dpe-json.json")
    args = parser.parse_args()

    report = await check_directory(args.root)
    print(f"conformance: {len(report.issues)} issues")
    report.raise_for_issues()
    if args.push:
        async with DPEPushClient(args.push, token=args.token) as client:
            results = await push_directory(args.root, StatefulPusher(client, JsonFileStateStore(args.state)))
        for uri, result in results.items():
            print(uri, "skipped" if result is None else f"{result.status} {result.counts}")


if __name__ == "__main__":
    asyncio.run(main())
