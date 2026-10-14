"""Markdown → 页与元素：块切分、页切分、元数据、图片引用、确定性。"""

from __future__ import annotations

from dpe_markdown_dir.markdown import ImageRef, parse_markdown

SAMPLE = """# 标题

第一段，
跨两行。

- 项一
  续行
- 项二

```python
print("---")

x = 1
```

![架构图](img/arch.png)

行内的 ![图](a.png) 不是独立图片。

![远端图](https://example.com/x.png)

---

## 第二页
~~~
未闭合围栏到文末
"""


def test_blocks_pages_and_metadata() -> None:
    pages, images = parse_markdown(SAMPLE)
    assert len(pages) == 2
    first = pages[0]
    assert first[0] == {"category": "Title", "text": "标题", "metadata": {"level": 1}}
    assert first[1] == {"category": "NarrativeText", "text": "第一段，\n跨两行。"}
    assert first[2] == {"category": "ListItem", "text": "项一\n续行"}
    assert first[3] == {"category": "ListItem", "text": "项二"}
    # 代码块里的 --- 与空行都原样保留，不切页、不切块
    assert first[4] == {
        "category": "CodeSnippet",
        "text": 'print("---")\n\nx = 1',
        "metadata": {"language": "python"},
    }
    assert first[5] == {"category": "Image", "text": "架构图"}
    assert first[6]["category"] == "NarrativeText"  # 行内图片不是 Image
    assert first[7] == {"category": "NarrativeText", "text": "![远端图](https://example.com/x.png)"}
    assert images == [
        ImageRef(page=0, element=5, path="img/arch.png", source="![架构图](img/arch.png)")
    ]
    second = pages[1]
    assert second[0] == {"category": "Title", "text": "第二页", "metadata": {"level": 2}}
    assert second[1] == {"category": "CodeSnippet", "text": "未闭合围栏到文末"}


def test_page_break_variants() -> None:
    pages, _ = parse_markdown("---\n甲\n---\n---\n乙")
    assert [len(page) for page in pages] == [0, 1, 0, 1]  # 空页是合法内容
    assert parse_markdown("")[0] == [[]]


def test_determinism_and_closing_hashes() -> None:
    text = "### 收尾的井号 ###\n\n正文"
    assert parse_markdown(text) == parse_markdown(text)
    assert parse_markdown(text)[0][0][0] == {
        "category": "Title",
        "text": "收尾的井号",
        "metadata": {"level": 3},
    }
    # 不是标题：井号后没有空白，或超过六级
    assert parse_markdown("#标签")[0][0][0]["category"] == "NarrativeText"
    assert parse_markdown("####### 七级")[0][0][0]["category"] == "NarrativeText"
    assert parse_markdown("# 讲 C#")[0][0][0]["text"] == "讲 C#"


def test_rules_are_language_neutral() -> None:
    """只按 \\n 切行、只把空格与制表符当空白：Unicode 行界与全角空格原样留在文本里。"""
    pages, _ = parse_markdown("甲\u2028乙\x0c丙\r\n\u3000---\r\n- 项\u3000")
    assert pages == [
        [
            {"category": "NarrativeText", "text": "甲\u2028乙\x0c丙\n\u3000---"},
            {"category": "ListItem", "text": "项\u3000"},
        ]
    ]
    # 全角数字不是有序列表的序号
    assert parse_markdown("１. 不是列表")[0][0][0]["category"] == "NarrativeText"
