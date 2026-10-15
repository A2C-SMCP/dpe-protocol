"""工单 → document 条目（#223 §4）：三页结构、字段顺序与规范化、评论排序与 metadata、时间戳。"""

from __future__ import annotations

from typing import Any

import pytest
from fake_jira import adf

from dpe_jira.mapping import MappingError, issue_document, utc

URI = "jira://example.atlassian.net/issue/ABC-1"


def issue(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "登录失败",
        "description": adf("复现步骤"),
        "created": "2026-09-01T16:00:00.000+0800",
    }
    base.update(fields)
    return {"id": "10001", "key": "ABC-1", "fields": base}


def comment(
    comment_id: str, created: str, body: str, *, updated: str | None = None
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "author": {"accountId": f"acc-{comment_id}", "displayName": f"用户{comment_id}"},
        "body": adf(body),
        "created": created,
        "updated": updated or created,
    }


def test_utc_normalization() -> None:
    assert utc("2026-09-01T16:00:00.000+0800") == "2026-09-01T08:00:00.000Z"
    assert utc("2026-09-01T08:00:00+0000") == "2026-09-01T08:00:00.000Z"
    assert utc("2026-09-01T08:00:00.123456+0000") == "2026-09-01T08:00:00.123Z"
    for bad in (None, "", "2026-09-01", "yesterday"):
        with pytest.raises(MappingError):
            utc(bad)


def test_document_object_and_three_pages() -> None:
    item = issue_document(URI, issue(), [])
    assert item["kind"] == "document" and item["file_uri"] == URI
    assert item["document"] == {
        "file_type": "jira_issue",
        "title": "登录失败",
        "doc_metadata": {"issue_id": "10001", "created_at": "2026-09-01T08:00:00.000Z"},
    }
    assert [page["page_metadata"] for page in item["pages"]] == [
        {"section": "body"},
        {"section": "comments"},
        {"section": "fields"},
    ]
    assert item["pages"][0]["elements"] == [
        {"category": "NarrativeText", "text": "复现步骤", "metadata": {}}
    ]
    assert item["pages"][1]["elements"] == []


def test_empty_description_is_an_empty_body_page() -> None:
    item = issue_document(URI, issue(description=None), [])
    assert item["pages"][0]["elements"] == []


def test_comments_are_sorted_locally_and_carry_metadata() -> None:
    comments = [
        comment("30", "2026-09-03T00:00:00.000+0000", "第三"),
        comment("20", "2026-09-02T00:00:00.000+0000", "同时间较大 id"),
        comment(
            "10",
            "2026-09-02T00:00:00.000+0000",
            "同时间较小 id",
            updated="2026-09-05T00:00:00.000+0000",
        ),
    ]
    elements = issue_document(URI, issue(), comments)["pages"][1]["elements"]
    assert [e["text"] for e in elements] == ["同时间较小 id", "同时间较大 id", "第三"]
    assert elements[0]["metadata"] == {
        "comment_id": "10",
        "author": "用户10",
        "author_account_id": "acc-10",
        "created_at": "2026-09-02T00:00:00.000Z",
        "updated_at": "2026-09-05T00:00:00.000Z",
    }


def test_field_page_order_values_and_empties() -> None:
    fields = {
        "duedate": "2026-12-31",
        "status": {"id": "3", "name": "进行中"},
        "issuetype": {"id": "10002", "name": "Bug"},
        "priority": None,
        "assignee": {"accountId": "acc-a", "displayName": "张三"},
        "reporter": None,
        "labels": ["ui", "login"],
        "components": [{"id": "1", "name": "Web"}, {"id": "2", "name": "API"}],
        "fixVersions": [],
        "parent": {"id": "9", "key": "ABC-0"},
        "resolution": None,
    }
    elements = issue_document(URI, issue(**fields), [])["pages"][2]["elements"]
    assert [e["metadata"]["field"] for e in elements] == [
        "issuetype",
        "status",
        "assignee",
        "labels",
        "components",
        "parent",
        "duedate",
    ]
    by_field = {e["metadata"]["field"]: e for e in elements}
    assert by_field["status"] == {
        "category": "NarrativeText",
        "text": "Status: 进行中",
        "metadata": {"field": "status", "value": {"id": "3", "name": "进行中"}},
    }
    assert by_field["assignee"]["text"] == "Assignee: 张三"
    assert by_field["assignee"]["metadata"] == {
        "field": "assignee",
        "value": "张三",
        "account_id": "acc-a",
    }
    assert by_field["labels"]["metadata"]["value"] == ["ui", "login"]
    assert by_field["components"]["text"] == "Components: Web, API"
    assert by_field["parent"]["metadata"]["value"] == "ABC-0"
    assert by_field["duedate"]["text"] == "Due Date: 2026-12-31"


def test_changing_status_changes_only_that_element() -> None:
    before = issue_document(URI, issue(status={"id": "1", "name": "待办"}, labels=["x"]), [])
    after = issue_document(URI, issue(status={"id": "3", "name": "进行中"}, labels=["x"]), [])
    assert before["pages"][:2] == after["pages"][:2]
    old, new = before["pages"][2]["elements"], after["pages"][2]["elements"]
    assert [e for e in old if e["metadata"]["field"] != "status"] == [
        e for e in new if e["metadata"]["field"] != "status"
    ]


@pytest.mark.parametrize(
    "broken",
    [
        {"id": 10001, "key": "ABC-1", "fields": {}},
        {"id": "10001", "key": "ABC-1"},
        {"id": "10001", "key": "ABC-1", "fields": {"summary": "x"}},  # 缺 created
    ],
)
def test_broken_issues_raise_mapping_error(broken: dict[str, Any]) -> None:
    with pytest.raises(MappingError):
        issue_document(URI, broken, [])


def test_comment_without_numeric_id_is_rejected() -> None:
    with pytest.raises(MappingError):
        issue_document(URI, issue(), [comment("abc", "2026-09-02T00:00:00.000+0000", "x")])
