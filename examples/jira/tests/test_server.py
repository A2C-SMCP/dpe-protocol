"""线协议（sans-IO）对模拟 Jira：握手校验、全量枚举、限流退避、条目级错误、分段、凭证不外泄。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fake_jira import EMAIL, TOKEN, FakeJira, adf, serving

from dpe_jira import PLUGIN_NAME, __version__
from dpe_jira.server import Serve

PREFIX = "jira://example/"
CREDS = {"JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN}
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
                    "config": {"site": site, "projects": ["ABC"]} if config is None else config,
                },
            },
        )

    def round(self, **extra: Any) -> list[dict[str, Any]]:
        """跑完一轮，返回全部条目；断言结束批的形状。"""
        params: dict[str, Any] = {"cursor": None, **extra}
        items: list[dict[str, Any]] = []
        while True:
            response = self.call("scan", params)
            assert "result" in response, response
            result = response["result"]
            items.extend(result["items"])
            if "next" not in result:
                assert result["cursor"] is None and result["incremental"] is False
                return items
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


def test_handshake_declares_full_scan_only(fake: FakeJira, site: str) -> None:
    session = Session()
    result = session.initialize(site)["result"]
    assert result == {
        "protocol_version": "dpe-connector/1",
        "plugin": {"name": PLUGIN_NAME, "version": __version__},
        "capabilities": {"supports_cursor": False},
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
    assert session.call("read_blob", {"handle": "h"})["error"]["code"] == -32002
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


def test_every_round_rescans_from_scratch(fake: FakeJira, site: str) -> None:
    populate(fake, 2)
    session = started(site)
    first = session.round()
    fake.add_issue(10003, "ABC-3", summary="新工单")
    second = session.round(cursor="ignored")  # 未声明游标：给了也按全量
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
