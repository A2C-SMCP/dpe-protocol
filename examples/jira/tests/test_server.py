"""线协议（sans-IO）对模拟 Jira：握手校验、全量枚举、限流退避、条目级错误、分段、凭证不外泄。"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fake_jira import EMAIL, TOKEN, FakeJira, FakeMedia, adf, serving

from dpe_jira import PLUGIN_NAME, __version__
from dpe_jira.mapping import issue_document
from dpe_jira.server import Serve

PREFIX = "jira://example/"
CREDS = {"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN}
#: 缺省范围：只看工单（项目文档由专门的用例打开）
SCOPE = {"projects": ["ABC"], "include_project_docs": False}
SECRETS = (TOKEN, base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode())


class Session:
    """一个插件进程的线协议会话：记下全部响应行，供凭证检查。"""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.serve = Serve(CREDS if environ is None else environ)
        self.lines: list[str] = []
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        id_ = self.next_id
        self.next_id += 1
        line = self.serve.handle(
            json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        )
        assert line is not None
        self.lines.append(line)
        if self.serve.max_message_bytes:  # 每条响应都不超过单条消息上限（含行尾 LF，§6.2）
            assert len(line.encode("utf-8")) + 1 <= self.serve.max_message_bytes
        response: dict[str, Any] = json.loads(line)
        assert response["id"] == id_
        return response

    def initialize(
        self, site: str, config: dict[str, Any] | None = None, max_message_bytes: int = 1 << 20
    ) -> dict[str, Any]:
        return self.call(
            "initialize",
            {
                "protocol_versions": ["dpe-connector/1"],
                "max_message_bytes": max_message_bytes,
                "instance": {
                    "uri_prefix": PREFIX,
                    "config": {"site": site, **SCOPE} if config is None else config,
                },
            },
        )

    def round(self, **extra: Any) -> list[dict[str, Any]]:
        """跑完一轮，返回全部条目；结束批的游标与 incremental 记在 ``cursor`` / ``incremental``。"""
        params: dict[str, Any] = {"cursor": None, **extra}
        full = params["cursor"] is None
        items: list[dict[str, Any]] = []
        self.batches = 0
        while True:
            response = self.call("scan", params)
            assert "result" in response, response
            result = response["result"]
            items.extend(result["items"])
            self.batches += 1
            if "next" not in result:
                assert "cursor" in result and isinstance(result["incremental"], bool)
                assert isinstance(result["cursor"], str | None)
                if full:
                    assert result["incremental"] is False
                self.cursor: str | None = result["cursor"]
                self.incremental: bool = result["incremental"]
                return items
            assert "cursor" not in result and "incremental" not in result  # 中间批（§6.4）
            params = {"next": result["next"]}

    def assert_no_secrets(self) -> None:
        for line in self.lines:
            for secret in SECRETS:
                assert secret not in line


@pytest.fixture
def fake() -> FakeJira:
    return FakeJira()


@pytest.fixture
def site(fake: FakeJira) -> Iterator[str]:
    with serving(fake) as url:
        yield url


def started(site: str, **kwargs: Any) -> Session:
    session = Session()
    response = session.initialize(site, **kwargs)
    assert "result" in response, response
    return session


def uris(items: list[dict[str, Any]]) -> list[str | None]:
    return [item.get("file_uri") for item in items]


# ---------------------------------------------------------------------- 握手


def test_handshake_declares_cursor_support(fake: FakeJira, site: str) -> None:
    session = Session()
    result = session.initialize(site)["result"]
    assert result == {
        "protocol_version": "dpe-connector/1",
        "plugin": {"name": PLUGIN_NAME, "version": __version__},
        "capabilities": {"supports_cursor": True},
    }
    assert ("GET", "/rest/api/3/myself") in fake.requests


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"site": "https://example.atlassian.net/", "projects": ["ABC"]}, "site"),
        ({"site": "https://bot:hunter2@example.atlassian.net", "projects": ["ABC"]}, "site"),
        ({"site": "HTTPS://example.atlassian.net", "projects": ["ABC"]}, "site"),
        ({"site": 1, "projects": ["ABC"]}, "site"),
        ({"site": "https://e.atlassian.net"}, "projects"),
        ({"site": "https://e.atlassian.net", "projects": ["ABC"], "jql": "x"}, "projects"),
        ({"site": "https://e.atlassian.net", "projects": []}, "projects"),
        ({"site": "https://e.atlassian.net", "projects": ["abc"]}, "projects"),
        ({"site": "https://e.atlassian.net", "jql": "project = A order  by key"}, "ORDER BY"),
        ({"site": "https://e.atlassian.net", "jql": "  "}, "jql"),
        (
            {"site": "https://e.atlassian.net", "projects": ["A"], "include_project_docs": 1},
            "include_project_docs",
        ),
        ({"site": "https://e.atlassian.net", "projects": ["A"], "extra": True}, "配置只认"),
    ],
)
def test_invalid_config_is_rejected_without_echoing_values(
    config: dict[str, Any], needle: str
) -> None:
    session = Session()
    error = session.initialize("unused", config=config)["error"]
    assert error["code"] == -32005
    assert needle in error["message"]
    assert "hunter2" not in error["message"] and "bot:" not in error["message"]
    assert session.serve.fatal


def test_missing_credentials(site: str) -> None:
    for environ in ({}, {"JIRA_EMAIL": EMAIL}, {"JIRA_API_TOKEN": TOKEN}):
        session = Session(environ)
        error = session.initialize(site)["error"]
        assert error["code"] == -32005 and "JIRA_API_TOKEN" in error["message"]


def test_rejected_credentials(site: str) -> None:
    session = Session({"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": "wrong"})
    error = session.initialize(site)["error"]
    assert error["code"] == -32005 and "401" in error["message"]
    assert "wrong" not in error["message"]


def test_site_without_jira_api(site: str) -> None:
    error = Session().initialize(site + "/wiki")["error"]  # 路径下没有 REST API：404
    assert error["code"] == -32005 and "404" in error["message"]


def test_unreachable_site_is_a_retryable_scan_failure_not_bad_config() -> None:
    with serving(FakeJira()) as dead:
        pass  # 服务已关：端口不再监听
    session = Session()
    assert "result" in session.initialize(dead)  # 网络失败不是配置问题
    error = session.call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": True}


@pytest.mark.parametrize("trouble", ["throttle", "server_error"])
def test_transient_probe_failure_does_not_fail_the_handshake(
    fake: FakeJira, site: str, trouble: str
) -> None:
    if trouble == "throttle":
        fake.throttle_forever.add("/rest/api/3/myself")
    else:
        fake.server_error.add("/rest/api/3/search/jql")
    before = len(fake.requests)
    assert "result" in Session().initialize(site)
    assert len(fake.requests) - before <= 2  # 探测不重试，不拖住握手


def test_scope_rejected_by_jira_is_invalid_config(fake: FakeJira, site: str) -> None:
    fake.reject_search = True  # JQL 不合法或项目不存在：Jira 回 400
    error = Session().initialize(site)["error"]
    assert error["code"] == -32005 and "400" in error["message"]


def test_scope_error_is_caught_even_when_the_credential_probe_is_transient(
    fake: FakeJira, site: str
) -> None:
    fake.server_error.add("/rest/api/3/myself")
    fake.reject_search = True
    assert Session().initialize(site)["error"]["code"] == -32005


@pytest.mark.parametrize("path", ["/rest/api/3/myself", "/rest/api/3/search/jql"])
@pytest.mark.parametrize("kind", ["redirect", "not_json"])
def test_site_that_is_not_the_jira_api_is_invalid_config(
    fake: FakeJira, site: str, kind: str, path: str
) -> None:
    if kind == "redirect":
        fake.redirect[path] = "https://elsewhere.example" + path
    else:
        fake.not_json.add(path)
    assert Session().initialize(site)["error"]["code"] == -32005


@pytest.mark.parametrize("kind", ["not_json", "gone"])
def test_search_endpoint_breaking_mid_life_is_not_retryable(
    fake: FakeJira, site: str, kind: str
) -> None:
    session = started(site)
    if kind == "not_json":
        fake.not_json.add("/rest/api/3/search/jql")
    else:
        fake.gone_endpoints.add("/rest/api/3/search/jql")  # 端点被移除：404
    error = session.call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


def test_non_object_comment_aborts_instead_of_dropping_it(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1")
    malformed: Any = [None]  # 评论页里混进不是对象的条目
    fake.comments["10001"] = malformed
    error = started(site).call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


def test_requests_before_handshake_and_unknown_methods(site: str) -> None:
    session = Session()
    assert session.call("scan", {"cursor": None})["error"]["code"] == -32600
    session.initialize(site)
    assert session.call("read_blob", {"handle": "h", "max_bytes": 10})["error"]["code"] == -32002
    assert session.call("nope", {})["error"]["code"] == -32601
    assert session.call("shutdown", {})["result"] == {}


# ---------------------------------------------------------------------- 全量枚举


def populate(fake: FakeJira, count: int) -> None:
    for n in range(count, 0, -1):  # 倒序加入：顺序由 id 决定，不由插入顺序
        fake.add_issue(10000 + n, f"ABC-{n}", summary=f"工单 {n}", description=adf(f"正文 {n}"))


def test_full_round_enumerates_every_issue_in_id_order(fake: FakeJira, site: str) -> None:
    populate(fake, 5)
    fake.search_page_size = 2  # 三页搜索
    session = started(site)
    items = session.round(max_items=2)
    assert uris(items) == [f"{PREFIX}issue/ABC-{n}" for n in range(1, 6)]
    assert all(item["kind"] == "document" for item in items)
    assert items[0]["document"]["title"] == "工单 1"
    searches = [r for r in fake.requests if r == ("POST", "/rest/api/3/search/jql")]
    assert len(searches) == 1 + 3  # initialize 的范围探测 + 三页


def test_empty_scope_ends_with_an_empty_final_batch(site: str) -> None:
    assert started(site).round() == []


def test_round_without_cursor_rescans_from_scratch(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    first = session.round()
    fake.add_issue(10003, "ABC-3", summary="新工单")
    second = session.round()
    assert uris(second) == [*uris(first), f"{PREFIX}issue/ABC-3"]
    assert second[:2] == first


def test_comments_are_fetched_across_pages(fake: FakeJira, site: str) -> None:
    comments = [
        {
            "id": str(n),
            "author": {"accountId": "a", "displayName": "A"},
            "body": adf(f"评论 {n}"),
            "created": f"2026-09-0{n}T00:00:00.000+0000",
        }
        for n in range(1, 6)
    ]
    fake.add_issue(10001, "ABC-1", comments=comments)
    fake.comment_page_size = 2
    [item] = started(site).round()
    texts = [e["text"] for e in item["pages"][1]["elements"]]
    assert texts == [f"评论 {n}" for n in range(1, 6)]


def test_stale_next_token_is_rejected(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    session = started(site)
    first = session.call("scan", {"cursor": None, "max_items": 1})["result"]
    second = session.call("scan", {"next": first["next"], "max_items": 1})["result"]
    stale = session.call("scan", {"next": first["next"]})
    assert stale["error"]["code"] == -32602
    assert "next" in second
    assert session.call("scan", {"next": "garbage"})["error"]["code"] == -32602


# ---------------------------------------------------------------------- 限流与错误


def test_rate_limit_is_retried_after_the_hint(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    fake.throttle["/rest/api/3/search/jql"] = 2
    fake.throttle["/rest/api/3/issue/10002"] = 3
    items = session.round()
    assert [item["kind"] for item in items] == ["document", "document"]
    assert fake.throttle == {"/rest/api/3/search/jql": 0, "/rest/api/3/issue/10002": 0}


def test_exhausted_rate_limit_aborts_the_round_as_retryable(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    session = started(site)
    fake.throttle_forever.add("/rest/api/3/issue/10002")
    first = session.call("scan", {"cursor": None, "max_items": 1})["result"]
    assert uris(first["items"]) == [f"{PREFIX}issue/ABC-1"]
    error = session.call("scan", {"next": first["next"]})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": True}
    attempts = [r for r in fake.requests if r == ("GET", "/rest/api/3/issue/10002")]
    assert len(attempts) == 4
    # 中止后旧 token 失效；限流解除后重开一轮即可成功
    assert session.call("scan", {"next": first["next"]})["error"]["code"] == -32602
    fake.throttle_forever.clear()
    assert len(session.round()) == 3


def test_search_failure_aborts_before_any_item(fake: FakeJira, site: str) -> None:
    populate(fake, 1)
    session = started(site)
    fake.throttle_forever.add("/rest/api/3/search/jql")
    error = session.call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": True}


def test_revoked_credentials_abort_the_round_as_not_retryable(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    fake.token = "rotated"
    error = session.call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


def _hit(issue_id: str) -> dict[str, Any]:
    return {"id": issue_id, "key": f"ABC-{issue_id}", "fields": {"updated": "x"}}


def test_pagination_follows_next_page_token_without_is_last(fake: FakeJira, site: str) -> None:
    for n in (1, 2):
        fake.add_issue(n, f"ABC-{n}")
    fake.search_override = [
        {"issues": [_hit("1")], "nextPageToken": "1"},  # 没有 isLast：以 token 为准
        {"issues": [_hit("2")]},
    ]
    assert len(started(site).round()) == 2


@pytest.mark.parametrize(
    "pages",
    [
        [{"issues": [_hit("1")], "nextPageToken": "1", "isLast": True}],
        [{"issues": [_hit("1")], "isLast": False}],
        [{"issues": [_hit("1")], "nextPageToken": "1"}, {"issues": [], "nextPageToken": "1"}],
    ],
    ids=["is-last-with-token", "not-last-without-token", "repeated-token"],
)
def test_inconsistent_pagination_aborts_instead_of_truncating(
    fake: FakeJira, site: str, pages: list[dict[str, Any]]
) -> None:
    fake.add_issue(1, "ABC-1")
    session = started(site)
    fake.search_override = pages
    error = session.call("scan", {"cursor": None})["error"]
    # 响应不符协议：重跑不会成功，不让运行器空转
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


def test_issue_full_text_is_read_by_id_not_key(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", summary="按 id 读取")
    session = started(site)
    first = session.call("scan", {"cursor": None, "max_items": 1})
    assert "result" in first
    assert ("GET", "/rest/api/3/issue/10001") in fake.requests
    assert all(path != "/rest/api/3/issue/ABC-1" for _, path in fake.requests)


def test_issue_deleted_after_enumeration_is_an_item_error(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    fake.gone.add("ABC-2")
    items = started(site).round()
    assert [item["kind"] for item in items] == ["document", "error", "document"]
    assert items[1] == {
        "kind": "error",
        "file_uri": f"{PREFIX}issue/ABC-2",
        "code": "source_unavailable",
        "message": items[1]["message"],
        "retryable": True,
    }


def test_unmappable_issue_is_content_invalid(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", created="not a timestamp")
    fake.add_issue(10002, "ABC-2")
    items = started(site).round()
    assert items[0]["kind"] == "error"
    assert items[0]["code"] == "content_invalid" and items[0]["retryable"] is False
    assert items[1]["kind"] == "document"


# ---------------------------------------------------------------------- 分段


def test_large_issue_is_split_at_page_boundaries(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", summary="大工单", description=adf("x" * 1500))
    fake.comments["10001"] = [
        {
            "id": "1",
            "author": {"accountId": "a", "displayName": "A"},
            "body": adf("y" * 1500),
            "created": "2026-09-01T00:00:00.000+0000",
        }
    ]
    fake.add_issue(10002, "ABC-2", summary="小工单")
    whole = started(site).round()
    session = started(site, max_message_bytes=4096)
    items = session.round()
    assert len(items) > len(whole)
    segments = [item for item in items if item["file_uri"] == f"{PREFIX}issue/ABC-1"]
    assert [s.get("continued", False) for s in segments][-1] is False
    assert all(s.get("continued") for s in segments[:-1])
    assert "document" in segments[0] and all("document" not in s for s in segments[1:])
    assert [page for s in segments for page in s["pages"]] == whole[0]["pages"]
    assert items[-1] == whole[1]


def test_page_larger_than_budget_is_content_invalid(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", description=adf("z" * 5000))
    fake.add_issue(10002, "ABC-2")
    items = started(site, max_message_bytes=4096).round()
    assert items[0]["kind"] == "error" and items[0]["code"] == "content_invalid"
    assert items[1]["kind"] == "document"


# ---------------------------------------------------------------------- 凭证


def test_no_output_ever_contains_credentials(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    fake.gone.add("ABC-3")
    session = started(site)
    session.round()
    fake.throttle_forever.add("/rest/api/3/search/jql")
    session.call("scan", {"cursor": None})
    session.assert_no_secrets()
    rejected = Session({"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN + "x"})
    rejected.initialize(site)
    for line in rejected.lines:
        assert TOKEN not in line


# ---------------------------------------------------------------------- 增量


def kinds(items: list[dict[str, Any]]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for item in items:
        if item["kind"] == "move":
            out.append(("move", item["from_uri"], item["to_uri"]))
        else:
            out.append((item["kind"], item["file_uri"]))
    return out


def uri(key: str) -> str:
    return f"{PREFIX}issue/{key}"


def test_full_round_gives_a_cursor_and_quiet_rounds_are_empty(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    session = started(site)
    assert len(session.round()) == 3
    assert session.incremental is False and isinstance(session.cursor, str)
    cursor = session.cursor
    assert session.round(cursor=cursor) == []
    assert session.incremental is True and session.cursor == cursor
    fetched = [path for _, path in fake.requests if path.startswith("/rest/api/3/issue/")]
    assert len(fetched) == 3 * 2  # 只有全量轮取过全文与评论


def test_incremental_round_expresses_every_kind_of_change(fake: FakeJira, site: str) -> None:
    populate(fake, 5)
    session = started(site)
    session.round()
    fake.touch(10002, "2026-10-02T08:00:00.000+0000", summary="改过标题")
    fake.remove(10004)
    fake.remove(10001)
    fake.rekey(10003, "XYZ-9")  # 换项目：updated 不变也要表达为 move + document
    fake.add_issue(10006, "ABC-6", summary="新工单")
    items = session.round(cursor=session.cursor)
    assert session.incremental is True
    assert kinds(items) == [
        ("delete", uri("ABC-1")),
        ("delete", uri("ABC-4")),
        ("document", uri("ABC-2")),
        ("move", uri("ABC-3"), uri("XYZ-9")),
        ("document", uri("XYZ-9")),
        ("document", uri("ABC-6")),
    ]
    assert items[2]["document"]["title"] == "改过标题"
    assert session.round(cursor=session.cursor) == []  # 新游标已吸收这些变化


def test_unusable_cursors_fall_back_to_a_full_round(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    other = Session()
    other.initialize(site, config={"site": site, "projects": ["ABC", "XYZ"]})
    other.round()
    session = started(site)
    for cursor in ("garbage", other.cursor):
        items = session.round(cursor=cursor)
        assert session.incremental is False
        assert [item["kind"] for item in items] == ["document", "document"]


@pytest.mark.parametrize(
    "breakage",
    ["page-2-fails", "is-last-contradiction", "repeated-token", "rate-limited"],
)
def test_incomplete_enumeration_never_deletes(fake: FakeJira, site: str, breakage: str) -> None:
    populate(fake, 6)
    fake.search_page_size = 2
    session = started(site)
    session.round()
    cursor = session.cursor
    fake.remove(10006)  # 一个真实的删除：截断时也不得在这一轮表达
    if breakage == "page-2-fails":
        fake.search_fail_from = 2  # 第 1 页正常，之后的页 500
    elif breakage == "is-last-contradiction":
        fake.search_override = [{"issues": [_hit("10001")], "isLast": True, "nextPageToken": "1"}]
    elif breakage == "repeated-token":
        fake.search_override = [
            {"issues": [_hit("10001")], "nextPageToken": "1"},
            {"issues": [_hit("10002")], "nextPageToken": "1"},
        ]
    else:
        fake.throttle_forever.add("/rest/api/3/search/jql")
    response = session.call("scan", {"cursor": cursor})
    assert response["error"]["code"] == -32003  # 整轮失败：没有任何条目，更没有 delete
    assert all('"delete"' not in line and '"move"' not in line for line in session.lines)
    # 恢复后，从同一个游标重跑才表达那次删除
    fake.search_fail_from = None
    fake.search_override = None
    fake.throttle_forever.clear()
    assert kinds(session.round(cursor=cursor)) == [("delete", uri("ABC-6"))]


def test_failed_fetch_keeps_the_old_record_so_the_next_round_retries(
    fake: FakeJira, site: str
) -> None:
    populate(fake, 3)
    session = started(site)
    session.round()
    fake.touch(10002, "2026-10-02T08:00:00.000+0000")
    fake.add_issue(10004, "ABC-4")
    fake.gone.update({"ABC-2", "ABC-4"})  # 枚举到了，取全文时 404
    items = session.round(cursor=session.cursor)
    assert [(i["kind"], i.get("code")) for i in items] == [
        ("error", "source_unavailable"),
        ("error", "source_unavailable"),
    ]
    fake.gone.clear()
    retry = session.round(cursor=session.cursor)
    assert kinds(retry) == [("document", uri("ABC-2")), ("document", uri("ABC-4"))]


def test_move_whose_target_cannot_be_read_is_not_emitted(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    session.round()
    fake.rekey(10001, "XYZ-1")
    fake.gone.add("XYZ-1")
    items = session.round(cursor=session.cursor)
    assert kinds(items) == [("error", uri("XYZ-1"))]  # 不先改名再建不出内容
    fake.gone.clear()
    assert kinds(session.round(cursor=session.cursor)) == [
        ("move", uri("ABC-1"), uri("XYZ-1")),
        ("document", uri("XYZ-1")),
    ]


def test_cursor_that_does_not_fit_the_final_batch_gets_its_own(fake: FakeJira, site: str) -> None:
    for n in range(1, 41):
        fake.add_issue(10000 + n, f"ABC-{n}", summary="x" * 40)
    session = started(site, max_message_bytes=8192)
    session.round()
    cursor = session.cursor
    assert cursor is not None
    # 只改最后一个，并把它撑到「单独装得下、加上游标就装不下」的大小
    fake.touch(10040, "2026-10-02T08:00:00.000+0000")
    issue = fake.issues["10040"]
    budget = 8192 - 1024 - len(str(session.next_id))
    base = len(json.dumps(issue_document(uri("ABC-40"), issue, []), ensure_ascii=False))
    filler = budget - base - len(cursor) // 2
    fake.touch(10040, "2026-10-02T08:00:00.000+0000", description=adf("y" * filler))
    items = session.round(cursor=cursor)
    assert kinds(items) == [("document", uri("ABC-40"))]
    assert session.batches == 2 and session.cursor is not None  # 第二批：空 items + 游标
    assert session.incremental is True


def test_cursor_larger_than_any_message_is_withheld(fake: FakeJira, site: str) -> None:
    for n in range(1, 400):
        fake.add_issue(10000 + n, f"ABC-{n}", updated=f"2026-10-01T08:00:{n % 60:02d}.{n:03d}+0000")
    session = started(site, max_message_bytes=2048)
    session.round()
    assert session.cursor is None  # 下一轮全量，而不是发超限消息


def test_incremental_cursor_larger_than_any_message_is_withheld(fake: FakeJira, site: str) -> None:
    fake.add_issue(10000, "ABC-0")
    session = started(site, max_message_bytes=2048)
    session.round()
    cursor = session.cursor
    assert cursor is not None
    for n in range(1, 400):  # 新游标长到单条消息装不下
        fake.add_issue(10000 + n, f"ABC-{n}", updated=f"2026-10-01T08:00:{n % 60:02d}.{n:03d}+0000")
    assert len(session.round(cursor=cursor)) == 399
    assert session.incremental is True and session.cursor is None


def test_oversized_document_does_not_advance_the_cursor(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site, max_message_bytes=4096)
    session.round()
    fake.touch(10002, "2026-10-02T08:00:00.000+0000", description=adf("z" * 5000))
    items = session.round(cursor=session.cursor)
    assert [(i["kind"], i.get("code")) for i in items] == [("error", "content_invalid")]
    fake.touch(10002, "2026-10-02T08:00:00.000+0000", description=adf("改小了"))
    # updated 没再变：只有游标没越过上次的失败，这一轮才会重新产出它
    assert kinds(session.round(cursor=session.cursor)) == [("document", uri("ABC-2"))]


def test_move_whose_target_is_too_large_is_not_emitted(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site, max_message_bytes=4096)
    session.round()
    fake.rekey(10001, "XYZ-1")
    fake.touch(10001, "2026-10-02T08:00:00.000+0000", description=adf("z" * 5000))
    items = session.round(cursor=session.cursor)
    assert kinds(items) == [("error", uri("XYZ-1"))]  # move 不落单
    fake.touch(10001, "2026-10-02T08:00:00.000+0000", description=adf("改小了"))
    assert kinds(session.round(cursor=session.cursor)) == [
        ("move", uri("ABC-1"), uri("XYZ-1")),
        ("document", uri("XYZ-1")),
    ]


def _segmented(fake: FakeJira) -> None:
    fake.touch(10002, "2026-10-02T08:00:00.000+0000", description=adf("x" * 1500))
    fake.comments["10002"] = [
        {
            "id": "1",
            "author": {"accountId": "a", "displayName": "A"},
            "body": adf("y" * 1500),
            "created": "2026-09-01T00:00:00.000+0000",
        }
    ]


def test_segmented_document_round_trip(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site, max_message_bytes=4096)
    session.round()
    _segmented(fake)
    items = session.round(cursor=session.cursor, max_items=1)
    assert len(items) >= 2 and all(i["file_uri"] == uri("ABC-2") for i in items)
    assert items[0].get("continued") is True and "continued" not in items[-1]
    assert session.round(cursor=session.cursor) == []


def test_segment_abandoned_midway_does_not_advance_the_cursor(
    fake: FakeJira, site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(fake, 2)
    session = started(site, max_message_bytes=4096)
    session.round()
    _segmented(fake)
    with monkeypatch.context() as patch:  # 首段发出后续段装不下（兜底路径）
        patch.setattr(Serve, "_next_segment", lambda self, round_, room: None)
        items = session.round(cursor=session.cursor)
    assert items[0].get("continued") is True
    assert items[-1]["kind"] == "error" and items[-1]["file_uri"] == uri("ABC-2")
    # 首段已发出也不算产出成功：下一轮重新产出
    again = session.round(cursor=session.cursor)
    assert {i["file_uri"] for i in again} == {uri("ABC-2")}


def test_oversized_header_is_content_invalid(fake: FakeJira, site: str) -> None:
    populate(fake, 1)
    fake.touch(10001, "2026-10-02T08:00:00.000+0000", summary="长" * 400)
    items = started(site, max_message_bytes=2048).round()
    assert [(i["kind"], i.get("code")) for i in items] == [("error", "content_invalid")]


def test_contradictory_cursor_falls_back_to_a_full_round(fake: FakeJira, site: str) -> None:
    from dpe_jira.cursor import encode_cursor, source_digest

    populate(fake, 2)
    session = started(site)
    assert fake.account_id is not None
    source = source_digest(PREFIX, {"site": site, **SCOPE}, fake.account_id)
    duplicated = encode_cursor(source, {"10001": ("ABC-1", "t"), "9": ("ABC-1", "t")}, {})
    stale = encode_cursor(source, {"9": ("ABC-2", "t"), "10001": ("ABC-1", "t")}, {})
    # 10001 上一轮在 ABC-2、本轮在 ABC-1，而 ABC-1 上一轮属于 10002：move 会搬走别人的文档
    swapped = encode_cursor(source, {"10001": ("ABC-2", "t"), "10002": ("ABC-1", "t")}, {})
    for cursor in (duplicated, stale, swapped):
        items = session.round(cursor=cursor)
        assert session.incremental is False
        assert all(item["kind"] == "document" for item in items)


def test_switching_credential_account_never_deletes(fake: FakeJira, site: str) -> None:
    populate(fake, 3)
    session = started(site)
    session.round()
    cursor = session.cursor
    # 换成另一个（权限更小的）账号：它看不到 ABC-3
    fake.account_id = "narrower"
    fake.remove(10003)
    switched = started(site)
    items = switched.round(cursor=cursor)
    assert switched.incremental is False  # 旧游标作废，回退全量轮
    assert all(item["kind"] == "document" for item in items)
    assert kinds(switched.round(cursor=switched.cursor)) == []  # 新账号自己的游标照常增量


def test_account_is_fetched_at_scan_when_the_probe_was_transient(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    session.round()
    cursor = session.cursor
    fake.throttle["/rest/api/3/myself"] = 1  # 握手时探测被限流（探测不重试）
    later = started(site)
    assert kinds(later.round(cursor=cursor)) == []  # 首轮取到同一账号：游标照常可用
    assert later.incremental is True
    fake.throttle_forever.add("/rest/api/3/myself")
    stuck = started(site)
    error = stuck.call("scan", {"cursor": cursor})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": True}
    assert "确认凭证账号失败" in error["message"]


def test_missing_account_id_is_rejected(fake: FakeJira, site: str) -> None:
    fake.account_id = None  # /myself 不返回 accountId：无法把游标绑定到账号
    session = Session()
    error = session.initialize(site)["error"]
    assert error["code"] == -32005 and session.serve.fatal
    # 握手时探测暂时失败、轮首再取仍缺 accountId：本轮不可重试地失败，而不是用空账号继续
    fake.throttle["/rest/api/3/myself"] = 1
    later = started(site)
    error = later.call("scan", {"cursor": None})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


# ---------------------------------------------------------------------- 附件与 read_blob

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20  # 5 KiB 的「图片」


def read_all(session: Session, handle: str, max_bytes: int = 1000) -> bytes:
    out = b""
    while True:
        result = session.call(
            "read_blob", {"handle": handle, "offset": len(out), "max_bytes": max_bytes}
        )["result"]
        chunk = base64.b64decode(result["data"])
        assert len(chunk) <= max_bytes
        out += chunk
        if result["eof"]:
            return out
        assert chunk  # 空块必须带 eof（§6.7）


def with_attachments(fake: FakeJira) -> None:
    fake.add_issue(10001, "ABC-1", summary="带附件")
    fake.attach(10001, 501, "shot.png", PNG)
    fake.attach(10001, 502, "spec.pdf", b"%PDF-1.7", mime="application/pdf")


@pytest.mark.parametrize("honor_range", [True, False], ids=["range", "spooled"])
@pytest.mark.parametrize("via_media", [True, False], ids=["media", "site"])
def test_read_blob_returns_the_attachment_bytes(
    fake: FakeJira, site: str, honor_range: bool, via_media: bool
) -> None:
    with_attachments(fake)
    fake.honor_range = honor_range
    media = FakeMedia(fake, honor_range=honor_range)
    with serving(media) as media_url:
        if via_media:
            fake.media_url = media_url
        session = started(site)
        [item] = session.round()
        page = item["pages"][-1]
        assert page["page_metadata"] == {"section": "attachments"}
        image = page["elements"][0]
        assert image["blob"] == {"handle": "501", "size": len(PNG)}
        assert read_all(session, "501") == PNG
        # 断点重试：同一偏移重复读取结果一致
        first = session.call("read_blob", {"handle": "501", "offset": 100, "max_bytes": 50})
        again = session.call("read_blob", {"handle": "501", "offset": 100, "max_bytes": 50})
        assert first == {**again, "id": first["id"]}
        assert base64.b64decode(first["result"]["data"]) == PNG[100:150]
    assert all(not authorized for _, authorized in media.requests)


def test_read_blob_edges(fake: FakeJira, site: str) -> None:
    with_attachments(fake)
    session = started(site, max_message_bytes=2048)
    session.round()
    end = session.call("read_blob", {"handle": "501", "offset": len(PNG), "max_bytes": 10})
    assert end["result"] == {"data": "", "eof": True}
    beyond = session.call("read_blob", {"handle": "501", "offset": len(PNG) + 5, "max_bytes": 10})
    assert beyond["result"] == {"data": "", "eof": True}
    big = session.call("read_blob", {"handle": "501", "offset": 0, "max_bytes": 1 << 20})
    assert len(base64.b64decode(big["result"]["data"])) < 1024  # 受单条上限约束
    assert read_all(session, "501", max_bytes=1 << 20) == PNG
    for params in (
        {"handle": "501"},
        {"handle": "501", "max_bytes": 0},
        {"handle": 1, "max_bytes": 1},
        {"handle": "501", "offset": -1, "max_bytes": 1},
    ):
        assert session.call("read_blob", params)["error"]["code"] == -32602
    # 非图片附件、别的工单的附件都不是句柄
    assert session.call("read_blob", {"handle": "502", "max_bytes": 1})["error"]["code"] == -32002


def test_handles_expire_with_the_next_round_and_spool_files_are_removed(
    fake: FakeJira, site: str
) -> None:
    with_attachments(fake)
    fake.honor_range = False
    session = started(site)
    session.round()
    assert read_all(session, "501") == PNG
    spool_dir = session.serve._spool_dir
    assert spool_dir is not None and os.listdir(spool_dir)
    quiet = session.round(cursor=session.cursor)  # 新的一轮：上一轮的句柄失效
    assert quiet == []
    assert session.call("read_blob", {"handle": "501", "max_bytes": 1})["error"]["code"] == -32002
    assert os.listdir(spool_dir) == []
    session.round()
    read_all(session, "501")
    assert os.listdir(spool_dir)
    session.call("shutdown", {})
    assert os.listdir(spool_dir) == []


@pytest.mark.parametrize("honor_range", [True, False], ids=["range", "spooled"])
def test_size_mismatch_is_a_permanent_read_failure(
    fake: FakeJira, site: str, honor_range: bool
) -> None:
    fake.add_issue(10001, "ABC-1")
    fake.attach(10001, 501, "shot.png", PNG, size=len(PNG) + 7)  # 元数据比实际多 7 字节
    fake.honor_range = honor_range
    session = started(site)
    session.round()
    error = session.call("read_blob", {"handle": "501", "offset": 0, "max_bytes": 100})["error"]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}
    assert "size" in error["message"]


def test_adding_an_attachment_changes_only_the_attachments_page(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", summary="会加附件", description=adf("正文"))
    session = started(site)
    [before] = session.round()
    fake.attach(10001, 501, "shot.png", PNG)
    fake.touch(10001, "2026-10-02T08:00:00.000+0000")  # Jira 加附件时更新 updated
    [after] = session.round(cursor=session.cursor)
    assert after["pages"][:3] == before["pages"]
    assert after["pages"][3]["page_metadata"] == {"section": "attachments"}


@pytest.mark.parametrize("kind", ["wrong-start", "malformed", "unsatisfiable"])
def test_strict_range_responses(fake: FakeJira, site: str, kind: str) -> None:
    with_attachments(fake)
    media = FakeMedia(fake)

    def respond(path: str, headers: Any) -> tuple[int, Any, dict[str, str]]:
        if kind == "wrong-start":
            return 206, PNG[:10], {"Content-Range": f"bytes 0-9/{len(PNG)}"}
        if kind == "malformed":
            return 206, PNG[:10], {"Content-Range": "bytes 0-9"}
        return 416, b"", {"Content-Range": f"bytes */{len(PNG)}"}

    media.responder = respond
    with serving(media) as media_url:
        fake.media_url = media_url
        session = started(site)
        session.round()
        error = session.call("read_blob", {"handle": "501", "offset": 100, "max_bytes": 50})[
            "error"
        ]
    assert error["code"] == -32003 and error["data"] == {"retryable": False}


def test_short_range_responses_are_continued(fake: FakeJira, site: str) -> None:
    with_attachments(fake)
    media = FakeMedia(fake)

    def respond(path: str, headers: Any) -> tuple[int, Any, dict[str, str]]:
        first = int(headers["Range"].removeprefix("bytes=").split("-")[0])
        last = min(first + 6, len(PNG) - 1)  # 每次只给 7 字节，少于请求的量（HTTP 允许）
        return 206, PNG[first : last + 1], {"Content-Range": f"bytes {first}-{last}/{len(PNG)}"}

    media.responder = respond
    with serving(media) as media_url:
        fake.media_url = media_url
        session = started(site)
        session.round()
        assert read_all(session, "501", max_bytes=100) == PNG


def test_new_round_clears_every_file_in_the_spool_directory(fake: FakeJira, site: str) -> None:
    with_attachments(fake)
    fake.honor_range = False
    session = started(site)
    session.round()
    read_all(session, "501")
    spool_dir = session.serve._spool_dir
    assert spool_dir is not None
    stray = os.path.join(spool_dir, "999.bin")  # 例如整份下载中途失败、没登记上的半截文件
    with open(stray, "wb") as handle:
        handle.write(b"half")
    session.round(cursor=session.cursor)
    assert os.listdir(spool_dir) == []


def test_document_that_was_never_emitted_registers_no_handles(fake: FakeJira, site: str) -> None:
    fake.add_issue(10001, "ABC-1", description=adf("z" * 5000))  # 单页超预算：改判 error
    fake.attach(10001, 501, "shot.png", PNG)
    session = started(site, max_message_bytes=4096)
    [item] = session.round()
    assert item["kind"] == "error"
    assert session.call("read_blob", {"handle": "501", "max_bytes": 10})["error"]["code"] == -32002


def test_spool_file_cleaned_up_mid_round_is_downloaded_again(fake: FakeJira, site: str) -> None:
    with_attachments(fake)
    fake.honor_range = False
    session = started(site)
    session.round()
    first = session.call("read_blob", {"handle": "501", "offset": 0, "max_bytes": 100})
    spool_dir = session.serve._spool_dir
    assert spool_dir is not None
    for name in os.listdir(spool_dir):  # 系统清理程序在轮内删掉了临时文件
        os.unlink(os.path.join(spool_dir, name))
    assert read_all(session, "501") == PNG
    assert "result" in first


# ---------------------------------------------------------------------- 项目文档

WITH_PROJECTS = {"projects": ["ABC"]}


def projects_session(site: str, **config: Any) -> Session:
    session = Session()
    response = session.initialize(site, config={"site": site, **(config or WITH_PROJECTS)})
    assert "result" in response, response
    return session


def test_project_documents_follow_the_issues(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    fake.components["ABC"] = [{"id": "1", "name": "Web"}]
    session = projects_session(site)
    items = session.round()
    assert kinds(items) == [
        ("document", uri("ABC-1")),
        ("document", uri("ABC-2")),
        ("document", f"{PREFIX}project/ABC"),
    ]
    project = items[-1]
    assert project["document"]["file_type"] == "jira_project"
    assert project["pages"][1]["elements"][0]["text"] == "Web"
    assert session.round(cursor=session.cursor) == []  # 没变：项目文档也不再产出


def test_jql_scope_uses_the_projects_of_its_issues(fake: FakeJira, site: str) -> None:
    fake.add_issue(1, "ABC-1")
    fake.add_issue(2, "XYZ-7")
    session = projects_session(site, jql="labels = sync")
    items = session.round()
    assert [i["file_uri"] for i in items if "/project/" in i["file_uri"]] == [
        f"{PREFIX}project/ABC",
        f"{PREFIX}project/XYZ",
    ]


def test_include_project_docs_false_produces_none(fake: FakeJira, site: str) -> None:
    populate(fake, 1)
    session = projects_session(site, projects=["ABC"], include_project_docs=False)
    assert kinds(session.round()) == [("document", uri("ABC-1"))]


def test_project_changes_are_expressed_incrementally(fake: FakeJira, site: str) -> None:
    fake.add_issue(1, "ABC-1")
    fake.add_issue(2, "XYZ-1")
    session = projects_session(site, jql="labels = sync")
    session.round()
    fake.versions["ABC"] = [{"id": "9", "name": "1.0"}]  # 改项目：工单都不变
    assert kinds(session.round(cursor=session.cursor)) == [("document", f"{PREFIX}project/ABC")]
    # 项目改 key：同一 id 下 ABC → ABD（Jira 允许改项目 key）
    fake.projects["ABD"] = {**fake.projects.pop("ABC"), "key": "ABD"}
    fake.rekey(1, "ABD-1")
    items = session.round(cursor=session.cursor)
    assert kinds(items) == [
        ("move", uri("ABC-1"), uri("ABD-1")),
        ("document", uri("ABD-1")),
        ("move", f"{PREFIX}project/ABC", f"{PREFIX}project/ABD"),
        ("document", f"{PREFIX}project/ABD"),
    ]
    # XYZ 的最后一个工单离开范围：项目也随之离开
    fake.remove(2)
    items = session.round(cursor=session.cursor)
    assert kinds(items) == [("delete", uri("XYZ-1")), ("delete", f"{PREFIX}project/XYZ")]


def test_incomplete_enumeration_never_deletes_projects(fake: FakeJira, site: str) -> None:
    for n in range(1, 5):
        fake.add_issue(n, f"XYZ-{n}" if n == 4 else f"ABC-{n}")
    fake.search_page_size = 2
    session = projects_session(site, jql="labels = sync")
    session.round()
    cursor = session.cursor
    fake.search_fail_from = 2  # XYZ 的工单在第 2 页：截断会让 XYZ 看似消失
    assert session.call("scan", {"cursor": cursor})["error"]["code"] == -32003
    assert all('"delete"' not in line for line in session.lines)


@pytest.mark.parametrize("mode", ["projects", "jql"])
def test_unreadable_project_at_enumeration_fails_the_round(
    fake: FakeJira, site: str, mode: str
) -> None:
    populate(fake, 1)
    session = projects_session(
        site, **({"projects": ["ABC"]} if mode == "projects" else {"jql": "x"})
    )
    session.round()
    del fake.projects["ABC"]  # 枚举时项目读不到：整轮失败，不当作删除
    error = session.call("scan", {"cursor": session.cursor})["error"]
    # 配置写明的项目读不到要改配置（不可重试）；按工单推出的项目读不到是竞态（可重试）
    assert error["code"] == -32003 and error["data"] == {"retryable": mode == "jql"}
    assert all('"delete"' not in line for line in session.lines)


def test_project_details_404_is_an_item_error_retried_next_round(fake: FakeJira, site: str) -> None:
    populate(fake, 1)
    session = projects_session(site)
    session.round()
    fake.versions["ABC"] = [{"id": "9", "name": "1.0"}]
    fake.gone_endpoints.add("/rest/api/3/project/ABC/versions")
    items = session.round(cursor=session.cursor)
    assert [(i["kind"], i.get("code")) for i in items] == [("error", "source_unavailable")]
    fake.gone_endpoints.clear()
    assert kinds(session.round(cursor=session.cursor)) == [("document", f"{PREFIX}project/ABC")]


def test_moved_project_whose_details_fail_is_not_moved(fake: FakeJira, site: str) -> None:
    fake.add_issue(1, "ABC-1")
    session = projects_session(site, jql="x")
    session.round()
    fake.projects["ABD"] = {**fake.projects.pop("ABC"), "key": "ABD"}
    fake.rekey(1, "ABD-1")
    fake.gone_endpoints.add("/rest/api/3/project/ABD/components")
    items = session.round(cursor=session.cursor)
    assert kinds(items)[-1] == ("error", f"{PREFIX}project/ABD")
    assert ("move", f"{PREFIX}project/ABC", f"{PREFIX}project/ABD") not in kinds(items)
    fake.gone_endpoints.clear()
    assert kinds(session.round(cursor=session.cursor)) == [
        ("move", f"{PREFIX}project/ABC", f"{PREFIX}project/ABD"),
        ("document", f"{PREFIX}project/ABD"),
    ]


def test_new_project_that_fails_is_not_recorded(fake: FakeJira, site: str) -> None:
    fake.add_issue(1, "ABC-1")
    session = projects_session(site, jql="x")
    session.round()
    fake.add_issue(2, "XYZ-1")
    fake.gone_endpoints.add("/rest/api/3/project/XYZ/versions")
    items = session.round(cursor=session.cursor)
    assert kinds(items) == [("document", uri("XYZ-1")), ("error", f"{PREFIX}project/XYZ")]
    fake.gone_endpoints.clear()
    assert kinds(session.round(cursor=session.cursor)) == [("document", f"{PREFIX}project/XYZ")]


def test_project_alias_keys_are_deduplicated(fake: FakeJira, site: str) -> None:
    fake.projects["OLD"] = fake.projects["ABC"]  # 改名前的 key 被重定向到同一个项目
    populate(fake, 1)
    session = projects_session(site, projects=["ABC", "OLD"])
    items = session.round()
    assert [i["file_uri"] for i in items].count(f"{PREFIX}project/ABC") == 1


def test_contradictory_project_table_falls_back_to_a_full_round(fake: FakeJira, site: str) -> None:
    from dpe_jira.cursor import encode_cursor, source_digest

    populate(fake, 1)
    session = projects_session(site)
    session.round()
    assert fake.account_id is not None
    source = source_digest(PREFIX, {"site": site, **WITH_PROJECTS}, fake.account_id)
    # ABC 上一轮属于另一个项目 id：按增量会把别人的文档删掉或搬走
    stale = encode_cursor(source, {}, {"999": ("ABC", "d"), "100": ("ABD", "d")})
    items = session.round(cursor=stale)
    assert session.incremental is False
    assert all(item["kind"] == "document" for item in items)


def test_v1_cursor_falls_back_to_a_full_round(fake: FakeJira, site: str) -> None:
    import base64 as b64
    import zlib

    from dpe_jira.cursor import source_digest

    populate(fake, 1)
    session = projects_session(site)
    assert fake.account_id is not None
    source = source_digest(PREFIX, {"site": site, **WITH_PROJECTS}, fake.account_id)
    body = json.dumps({"v": 1, "src": source, "issues": {"10001": ["ABC-1", "t"]}}).encode()
    v1 = "j1." + b64.urlsafe_b64encode(zlib.compress(body)).decode().rstrip("=")
    items = session.round(cursor=v1)
    assert session.incremental is False and len(items) == 2
