"""dpe-hash 原位重算基准：只报告耗时，不设门槛（供内核评估 TFROB-958 的迁移窗口）。

合成一篇 ``--pages`` × ``--per-page`` 个元素的文档（混合 category、带版面坐标 metadata），
分别在 dpe1 与升级演练契约 dpe2 下计时 ``document_hashes``，按 Markdown 表格输出，
CI 把输出写进 job summary。

用法::

    uv run python tools/bench_hash.py [--pages 1000] [--per-page 100] [--repeat 3]
"""

from __future__ import annotations

import argparse
import platform
import time

from dpe_hash import DRILL_CONTRACT, ElementObject, ExpandedDocument, blob_ref, document_hashes


def _element(page: int, i: int) -> ElementObject:
    box = {"points": [[0.1, i / 100], [0.9, i / 100 + 0.01]], "system": "PixelSpace"}
    if i % 20 == 0:
        return {
            "category": "Image",
            "blob": blob_ref(f"{page}-{i}".encode()),
            "mime_type": "image/png",
            "metadata": {"coordinates": box, "image_url": f"https://cdn.example/{page}/{i}.png"},
        }
    if i % 10 == 0:
        return {
            "category": "Table",
            "text": f"表 {page}.{i}",
            "text_as_html": f"<table><tr><td>{page}</td><td>{i}</td></tr></table>",
            "metadata": {"coordinates": box},
        }
    return {
        "category": "Title" if i == 1 else "NarrativeText",
        "text": f"第 {page} 页第 {i} 段：" + "正文内容 " * 20,
        "metadata": {"coordinates": box, "lang": "zh"},
    }


def synth(pages: int, per_page: int) -> ExpandedDocument:
    return {
        "file_type": "pdf",
        "title": "基准文档",
        "doc_metadata": {"author": "bench", "created_at": "2026-10-02T00:00:00Z"},
        "pages": [
            {
                "title": f"p{p}",
                "page_metadata": {"page_label": str(p + 1)},
                "elements": [_element(p, i) for i in range(per_page)],
            }
            for p in range(pages)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    doc = synth(args.pages, args.per_page)
    total = args.pages * args.per_page
    print(f"### dpe-hash 原位重算基准（{args.pages} 页 × {args.per_page} = {total:,} 元素）\n")
    py = f"Python {platform.python_version()}（{platform.machine()}）"
    print(f"{py}，取 {args.repeat} 次最小值\n")
    print("| 契约 | 耗时 (s) | 元素/秒 |")
    print("| --- | ---: | ---: |")
    for contract in ("dpe1", DRILL_CONTRACT):
        best = float("inf")
        for _ in range(args.repeat):
            start = time.perf_counter()
            document_hashes(doc, contract)
            best = min(best, time.perf_counter() - start)
        print(f"| {contract} | {best:.2f} | {total / best:,.0f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
