"""ADF → 元素的确定性规则（#223 §4.1）：逐类节点、行内渲染、兜底与确定性。"""

from __future__ import annotations

import copy
import json
from typing import Any

from dpe_jira.adf import to_elements


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(inline)}


def text(value: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def test_empty_and_missing_documents() -> None:
    assert to_elements(None) == []
    assert to_elements({"type": "doc", "content": []}) == []
    assert to_elements(doc(para())) == []  # 空段落不承载内容


def test_paragraph_heading_code_quote() -> None:
    elements = to_elements(
        doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [text("背景")]},
            para(text("第一段 "), text("加粗", {"type": "strong"})),
            {"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("x = 1\n")]},
            {"type": "codeBlock", "content": [text("plain")]},
            {"type": "blockquote", "content": [para(text("引用一")), para(text("引用二"))]},
            {"type": "rule"},
        )
    )
    assert elements == [
        {"category": "Title", "text": "背景", "metadata": {"level": 2}},
        {"category": "NarrativeText", "text": "第一段 加粗", "metadata": {}},
        {"category": "CodeSnippet", "text": "x = 1\n", "metadata": {"language": "python"}},
        {"category": "CodeSnippet", "text": "plain", "metadata": {}},
        {"category": "NarrativeText", "text": "引用一\n引用二", "metadata": {"quote": True}},
    ]


def test_nested_lists_record_depth_and_order() -> None:
    elements = to_elements(
        doc(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            para(text("外层")),
                            {
                                "type": "orderedList",
                                "content": [{"type": "listItem", "content": [para(text("内层"))]}],
                            },
                        ],
                    },
                    {"type": "listItem", "content": [para(text("第二项"))]},
                ],
            }
        )
    )
    assert elements == [
        {"category": "ListItem", "text": "外层", "metadata": {"depth": 0, "ordered": False}},
        {"category": "ListItem", "text": "内层", "metadata": {"depth": 1, "ordered": True}},
        {"category": "ListItem", "text": "第二项", "metadata": {"depth": 0, "ordered": False}},
    ]


def test_table_text_and_html_are_deterministic() -> None:
    def cell(kind: str, value: str) -> dict[str, Any]:
        return {"type": kind, "content": [para(text(value))]}

    elements = to_elements(
        doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [cell("tableHeader", "名"), cell("tableHeader", "值")],
                    },
                    {
                        "type": "tableRow",
                        "content": [cell("tableCell", "a<b"), cell("tableCell", "1")],
                    },
                ],
            }
        )
    )
    assert elements == [
        {
            "category": "Table",
            "text": "名\t值\na<b\t1",
            "text_as_html": (
                "<table><tr><th>名</th><th>值</th></tr><tr><td>a&lt;b</td><td>1</td></tr></table>"
            ),
            "metadata": {},
        }
    ]


def test_inline_rendering() -> None:
    link = {"type": "link", "attrs": {"href": "https://example.com/x"}}
    elements = to_elements(
        doc(
            para(
                text("见 "),
                text("文档", link),
                text(" "),
                text("https://example.com/x", link),  # 链接文字与地址相同：不重复
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"id": "acc-1", "text": "@张三"}},
                text(" "),
                {"type": "mention", "attrs": {"id": "acc-2", "text": "李四"}},
                text(" "),
                {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}},
                text(" "),
                {"type": "inlineCard", "attrs": {"url": "https://jira/browse/ABC-1"}},
                text(" "),
                {"type": "date", "attrs": {"timestamp": "1760400000000"}},
                text(" "),
                {"type": "status", "attrs": {"text": "进行中", "color": "blue"}},
            )
        )
    )
    assert elements[0]["text"] == (
        "见 文档 (https://example.com/x) https://example.com/x\n"
        "@张三 @李四 :smile: https://jira/browse/ABC-1 2025-10-14 进行中"
    )


def test_media_becomes_image_without_bytes() -> None:
    elements = to_elements(
        doc(
            {
                "type": "mediaSingle",
                "content": [
                    {"type": "media", "attrs": {"id": "uuid-1", "type": "file", "alt": "a.png"}}
                ],
            },
            {
                "type": "mediaGroup",
                "content": [{"type": "media", "attrs": {"id": "uuid-2", "type": "file"}}],
            },
        )
    )
    assert elements == [
        {"category": "Image", "text": "a.png", "metadata": {"media_id": "uuid-1"}},
        {"category": "Image", "text": "", "metadata": {"media_id": "uuid-2"}},
    ]


def test_unknown_nodes_keep_their_text() -> None:
    elements = to_elements(
        doc(
            {"type": "panel", "attrs": {"panelType": "info"}, "content": [para(text("提示内容"))]},
            {
                "type": "expand",
                "content": [para(text("折叠")), {"type": "codeBlock", "content": [text("c")]}],
            },
            {
                "type": "mysteryBlock",
                "content": [para({"type": "mysteryInline", "content": [text("深处")]})],
            },
            {"type": "panel", "content": []},  # 没有文本：不产出
        )
    )
    assert elements == [
        {"category": "UncategorizedText", "text": "提示内容", "metadata": {}},
        {"category": "UncategorizedText", "text": "折叠\nc", "metadata": {}},
        {"category": "UncategorizedText", "text": "深处", "metadata": {}},
    ]


def test_extra_metadata_is_merged_into_every_element() -> None:
    elements = to_elements(
        doc(para(text("a")), {"type": "heading", "attrs": {"level": 1}, "content": [text("b")]}),
        {"comment_id": "7"},
    )
    assert [e["metadata"] for e in elements] == [
        {"comment_id": "7"},
        {"level": 1, "comment_id": "7"},
    ]


def test_malformed_input_does_not_raise() -> None:
    weird: list[Any] = [
        {"type": "doc", "content": "not a list"},
        {"type": "doc", "content": [None, 1, {"type": "paragraph", "content": [{"type": "text"}]}]},
        {"type": "doc", "content": [{"type": "heading", "attrs": {"level": True}, "content": []}]},
        {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "date", "attrs": {"timestamp": "x"}}]}
            ],
        },
    ]
    for value in weird:
        to_elements(value)


def test_same_input_gives_byte_identical_output() -> None:
    source = doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [text("标题")]},
        para(text("正文", {"type": "link", "attrs": {"href": "https://a"}})),
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [{"type": "tableCell", "content": [para(text("x"))]}],
                }
            ],
        },
    )
    first = json.dumps(to_elements(copy.deepcopy(source)), ensure_ascii=False, sort_keys=True)
    for _ in range(3):
        assert (
            json.dumps(to_elements(copy.deepcopy(source)), ensure_ascii=False, sort_keys=True)
            == first
        )
