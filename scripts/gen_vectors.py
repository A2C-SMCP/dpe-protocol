"""用 TFRobot 内核生成 hash-contract-v1 一致性测试向量。

向量的期望值来自**内核**而非任何 SDK，各 SDK 只是被测对象。需要在装有 ``tfrobot`` 的环境运行，例如：

    <装有 tfrobot 的 python> scripts/gen_vectors.py

**临时托管，待移交**：按 hash-contract-v1 §7，向量应由规范方（TFRobot 内核仓库）生成并发布，
内核在自己的 CI 中先跑向量，dpe-protocol 只按版本拉取消费。本脚本暂放在 dpe-protocol 以便起步，
移交时可整体搬到内核仓库：它只依赖 ``tfrobot`` 与标准库，不依赖 dpe-protocol 的任何代码。
移交方案见 dpe-protocol ``docs/design.md`` §7.1。

每次生成都会把内核与 pydantic 的版本写入 ``manifest.json`` 的 ``provenance``，用于判断向量是否落后于内核。

注意：构造内核文档时需要逐个 element 先经 ``create_element`` 按 category 分派为具体类型再装配，
Image / Table / Formula 的 hash 规则才会生效，得到的才是 hash-contract-v1 规定的值。
"""

import itertools
import json
import platform
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from tfrobot.schema.document.base import DocPage, Document
from tfrobot.schema.document.elements import create_element
from tfrobot.schema.document.hasher import DefaultHashStrategy
from tfrobot.schema.document.meta import TFDocMetadata
from tfrobot.schema.document.types import TFFileType

OUT_DIR = Path(__file__).resolve().parent.parent / "vectors" / "v1"


_ids = itertools.count(1)


def ele(category: str, text: str | None = "", **meta: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"category": category, "element_id": f"e{next(_ids)}"}
    if text is not None:
        e["text"] = text
    if meta:
        e["ele_metadata"] = meta
    return e


def doc(
    pages: list[dict[str, Any]], *, file_uri: str = "dpe://acme/handbook", file_type: str = "md", **meta: Any
) -> dict[str, Any]:
    return {"file_uri": file_uri, "file_type": file_type, "doc_metadata": {"created_at": None, **meta}, "pages": pages}


def page(number: int, title: str | None, *elements: dict[str, Any]) -> dict[str, Any]:
    return {"number": number, "title": title, "elements": list(elements)}


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

CASES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "element_text_basic",
        "基础文本 element：Title + NarrativeText + ListItem（CJK）",
        doc(
            [page(0, "第一章", ele("Title", "员工手册"), ele("NarrativeText", "欢迎加入。"), ele("ListItem", "第一条"))]
        ),
    ),
    (
        "null_normalization",
        "text / text_as_html / image_mime_type 为 None 时等价空串",
        doc(
            [
                page(
                    0,
                    None,
                    ele("Table", "", text_as_html=None),
                    ele("Image", "", image_url="https://x/a.png"),
                    ele("NarrativeText", ""),
                )
            ]
        ),
    ),
    (
        "image_channel_priority",
        "Image 三通道：image_url 优先于 image_base64 与 image_path",
        doc(
            [
                page(
                    0,
                    "图",
                    ele(
                        "Image",
                        "",
                        image_url="https://cdn.acme/a.png",
                        image_base64=PNG_B64,
                        image_path="r1/a.png",
                        image_mime_type="image/png",
                    ),
                )
            ]
        ),
    ),
    (
        "image_channel_url_changed",
        "负例：仅 image_url 不同",
        doc(
            [
                page(
                    0,
                    "图",
                    ele(
                        "Image",
                        "",
                        image_url="https://cdn.acme/b.png",
                        image_base64=PNG_B64,
                        image_path="r1/a.png",
                        image_mime_type="image/png",
                    ),
                )
            ]
        ),
    ),
    (
        "image_channel_base64_only",
        "Image 仅 base64 通道",
        doc([page(0, "图", ele("Image", "示意图", image_base64=PNG_B64, image_mime_type="image/png"))]),
    ),
    (
        "image_mime_changed",
        "负例：仅 image_mime_type 不同",
        doc([page(0, "图", ele("Image", "示意图", image_base64=PNG_B64, image_mime_type="image/jpeg"))]),
    ),
    (
        "table_html",
        "Table 纳入 text_as_html",
        doc([page(0, "表", ele("Table", "a b", text_as_html="<table><tr><td>a</td><td>b</td></tr></table>"))]),
    ),
    (
        "table_html_changed",
        "负例：Table 仅 text_as_html 不同",
        doc([page(0, "表", ele("Table", "a b", text_as_html="<table><tr><td>a b</td></tr></table>"))]),
    ),
    (
        "formula_html",
        "Formula 纳入 text_as_html",
        doc([page(0, None, ele("Formula", "E=mc2", text_as_html="<math><mi>E</mi></math>"))]),
    ),
    (
        "non_identity_metadata_ignored",
        "非身份 metadata（filename 等）不进 content_hash",
        doc(
            [
                page(
                    0,
                    "第一章",
                    ele("Title", "员工手册", filename="h.md", page_number=1, text_as_html="<h1>x</h1>"),
                    ele("NarrativeText", "欢迎加入。"),
                    ele("ListItem", "第一条"),
                )
            ]
        ),
    ),
    ("page_order", "页内顺序：A, B", doc([page(0, "p", ele("NarrativeText", "A"), ele("NarrativeText", "B"))])),
    (
        "page_order_swapped",
        "负例：页内顺序 B, A（page_hash 必须变化，content_hash 集合不变）",
        doc([page(0, "p", ele("NarrativeText", "B"), ele("NarrativeText", "A"))]),
    ),
    (
        "unicode_text",
        "CJK / emoji（U+FFFF 以上）/ 组合字符 / RTL",
        doc(
            [
                page(
                    0,
                    "😀 标题",
                    ele("NarrativeText", "𠀋𩸽 emoji 👨‍👩‍👧"),
                    ele("NarrativeText", "e\u0301 cafe\u0301"),
                    ele("NarrativeText", "مرحبا بالعالم"),
                )
            ]
        ),
    ),
    (
        "doc_metadata_unicode",
        "doc_metadata：非 ASCII 键、emoji 键、嵌套对象、带时区时间、引用 URI",
        doc(
            [page(0, "p", ele("NarrativeText", "x"))],
            created_at="2026-01-02T03:04:05+00:00",
            last_modified="2026-09-01T12:00:00",
            languages=["zho", "eng"],
            filename="手册.md",
            summary="摘要 ✅",
            global_dict={"甲方": "图灵", "乙方": "Acme"},
            forward_citation_uris=["https://acme.com/a"],
            **{"x-来源": {"z": 1, "a": [1, "二"], "😀": True, "\uffff": 0, "𠀋": 1}},
        ),
    ),
    (
        "doc_metadata_numbers_escapes",
        "json_stable：浮点数 repr（科学计数法边界）、控制字符与引号转义",
        doc(
            [page(0, "p", ele("NarrativeText", "x"))],
            **{
                "x-num": {
                    "one": 1.0,
                    "half": 0.5,
                    "big": 1e16,
                    "below_big": 1234567890123456.0,
                    "small": 1.5e-05,
                    "edge": 0.0001,
                    "neg": -2.5,
                    "negzero": -0.0,
                    "int": 10,
                    "negint": -7,
                    "pi": 3.141592653589793,
                },
                "x-str": "tab\there\nnew \"quote\" back\\slash \u0001\u001f \u007f \u2028 \u00e9",
            },
        ),
    ),
    (
        "doc_metadata_datetimes",
        "datetime isoformat：Z→+00:00、非 UTC 偏移、微秒补零",
        doc(
            [page(0, "p", ele("NarrativeText", "x"))],
            created_at="2026-01-02T03:04:05.12+08:00",
            last_modified="2026-01-02T03:04:05Z",
        ),
    ),
    (
        "doc_metadata_datetime_naive_micro",
        "naive datetime 带微秒",
        doc([page(0, "p", ele("NarrativeText", "x"))], created_at="2026-12-31T23:59:59.000001"),
    ),
    (
        "page_numbers_unsorted",
        "页编号非连续、非 0 起始、数组乱序：doc_hash 按 number 升序",
        doc(
            [
                page(5, "五", ele("NarrativeText", "5")),
                page(2, "二", ele("NarrativeText", "2")),
                page(10, "十", ele("NarrativeText", "10")),
            ]
        ),
    ),
    ("empty_document", "边界：0 页", doc([])),
    ("empty_page", "边界：空页（0 element）", doc([page(0, "空")])),
    ("long_element", "边界：超长单 element", doc([page(0, None, ele("NarrativeText", "长" * 100_000))])),
    (
        "file_uri_normalization",
        "file_uri 经 AnyUrl 规范化（空格编码）后再进 doc_hash",
        doc([page(0, "p", ele("NarrativeText", "x"))], file_uri="dpe://acme/docs/a b.md"),
    ),
    ("file_type_changed", "负例：file_type 不同", doc([page(0, "p", ele("NarrativeText", "x"))], file_type="txt")),
]

DISTINCT_PAIRS = [
    ["image_channel_priority", "image_channel_url_changed"],
    ["image_channel_base64_only", "image_mime_changed"],
    ["table_html", "table_html_changed"],
    ["page_order", "page_order_swapped"],
    ["file_uri_normalization", "file_type_changed"],
]


def build_kernel_doc(data: dict[str, Any]) -> Document:
    pages = [
        DocPage(number=p["number"], title=p["title"], elements=[create_element(e) for e in p["elements"]])
        for p in data["pages"]
    ]
    return Document(
        file_uri=data["file_uri"],
        file_type=TFFileType(data["file_type"]),
        doc_metadata=TFDocMetadata(**data["doc_metadata"]),
        pages=pages,
        hash_strategy=DefaultHashStrategy(),
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()
    for case, description, data in CASES:
        kdoc = build_kernel_doc(data)
        vector = {
            "algo_version": "v1",
            "hash_strategy_uri": kdoc.hash_strategy_uri,
            "case": case,
            "description": description,
            "input": data,
            "expect": {
                "content_hashes": [[e.content_hash for e in p.elements] for p in kdoc.pages],
                "page_hashes": {str(p.number): p.page_hash for p in kdoc.pages},
                "doc_hash": kdoc.doc_hash,
            },
        }
        (OUT_DIR / f"{case}.json").write_text(json.dumps(vector, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "algo_version": "v1",
        "generator": "scripts/gen_vectors.py (TFRobot kernel DefaultHashStrategy)",
        # 生成环境：内核或 pydantic（AnyUrl 规范化、model_dump）升级后，应重新生成并比对
        "provenance": {
            "tfrobot_version": version("tfrobot"),
            "pydantic_version": version("pydantic"),
            "pydantic_core_version": version("pydantic-core"),
            "python_version": platform.python_version(),
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hosting": "temporary: hosted in dpe-protocol, to be handed over to the TFRobot kernel repository",
        },
        "cases": [c for c, _, _ in CASES],
        "distinct_pairs": DISTINCT_PAIRS,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(CASES)} vectors to {OUT_DIR}")


if __name__ == "__main__":
    main()
