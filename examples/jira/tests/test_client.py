"""Jira 客户端的凭证安全与限流：不跟随重定向、Retry-After 上限、无提示时的指数退避。"""

from __future__ import annotations

import email.message
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from fake_jira import EMAIL, TOKEN, FakeJira, FakeMedia, serving

from dpe_jira.client import (
    JiraClient,
    RateLimited,
    SpoolFailed,
    Unavailable,
    UnexpectedResponse,
)


def client(site: str, sleeps: list[float]) -> JiraClient:
    return JiraClient(site, EMAIL, TOKEN, sleep=sleeps.append)


def test_redirects_are_not_followed_so_credentials_stay_on_the_site() -> None:
    elsewhere = FakeJira()
    with serving(elsewhere) as other:
        redirecting = FakeJira(redirect={"/rest/api/3/myself": other + "/rest/api/3/myself"})
        with serving(redirecting) as site, pytest.raises(UnexpectedResponse, match="302"):
            client(site, []).myself()
    assert elsewhere.requests == []  # 凭证没有被带到别的主机


def test_retry_after_beyond_the_cap_gives_up_without_waiting() -> None:
    fake = FakeJira(retry_after="120")
    fake.throttle_forever.add("/rest/api/3/myself")
    sleeps: list[float] = []
    with serving(fake) as site, pytest.raises(RateLimited):
        client(site, sleeps).myself()
    assert sleeps == []
    assert len(fake.requests) == 1


def test_missing_retry_after_backs_off_exponentially_then_gives_up() -> None:
    fake = FakeJira(retry_after=None)
    fake.throttle_forever.add("/rest/api/3/myself")
    sleeps: list[float] = []
    with serving(fake) as site, pytest.raises(RateLimited):
        client(site, sleeps).myself()
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(fake.requests) == 4


def test_retry_after_hint_is_honoured_then_succeeds() -> None:
    fake = FakeJira(retry_after="3")
    fake.throttle["/rest/api/3/myself"] = 2
    sleeps: list[float] = []
    with serving(fake) as site:
        assert client(site, sleeps).myself()["accountId"] == "bot"
    assert sleeps == [3.0, 3.0]


def test_attachment_redirect_to_another_origin_drops_credentials(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    media = FakeMedia(fake)
    with serving(media) as media_url, serving(fake) as site:
        fake.media_url = media_url
        got = client(site, []).attachment_range("7", 2, 4, lambda: str(tmp_path / "spool"), 10)
    assert got.partial and (got.first, got.total, got.data) == (2, 10, b"2345")
    assert media.requests == [("/file/7", False)]  # 到了媒体服务，没带凭证
    auth = [r for r in fake.requests if r[1] == "/rest/api/3/attachment/content/7"]
    assert auth  # 站点本身那一跳带凭证（模拟 Jira 不带凭证一律 401）


def test_same_origin_redirect_keeps_credentials(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 8, "b.png", b"abc")
    with serving(fake) as site:
        fake.redirect["/rest/api/3/attachment/content/9"] = (
            site + "/rest/api/3/attachment/content/8"
        )
        fake.blobs["9"] = b""
        got = client(site, []).attachment_range("9", 0, 3, lambda: str(tmp_path / "spool"), 3)
    assert got.data == b"abc"


def test_redirect_loops_are_bounded(tmp_path: Path) -> None:
    fake = FakeJira()
    with serving(fake) as site:
        fake.redirect["/rest/api/3/attachment/content/1"] = (
            site + "/rest/api/3/attachment/content/1"
        )
        with pytest.raises(UnexpectedResponse, match="重定向"):
            client(site, []).attachment_range("1", 0, 1, lambda: str(tmp_path / "spool"), 1)
    assert len(fake.requests) == 6  # 首次 + 5 跳


def test_https_to_http_redirect_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jira = JiraClient("https://example.atlassian.net", EMAIL, TOKEN)

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            headers = email.message.Message()
            headers["Location"] = "http://media.example/file/1"
            raise urllib.error.HTTPError(request.full_url, 303, "See Other", headers, None)

    monkeypatch.setattr(jira, "_opener", Opener())
    with pytest.raises(UnexpectedResponse, match="非 https"):
        jira.attachment_range("1", 0, 1, lambda: str(tmp_path / "spool"), 1)


def test_server_ignoring_range_spools_the_whole_file(tmp_path: Path) -> None:
    fake = FakeJira(honor_range=False)
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    spool = tmp_path / "spool"
    with serving(fake) as site:
        got = client(site, []).attachment_range("7", 2, 4, lambda: str(spool), 10)
        assert not got.partial and got.written == 10 and spool.read_bytes() == b"0123456789"
        capped = client(site, []).attachment_range("7", 0, 1, lambda: str(spool), 4)
    assert capped.written == 5  # 超过声明的大小即停：limit + 1 字节足以判断不符


def test_redirect_chain_that_left_the_site_never_gets_credentials_again(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    media = FakeMedia(fake)
    with serving(media) as media_url, serving(fake) as site:
        fake.media_url = media_url
        # 媒体服务把请求再弹回站点的另一个路径：这一跳不得带凭证
        media.responder = lambda path, headers: (
            302,
            b"",
            {"Location": site + "/rest/api/3/myself"},
        )
        with pytest.raises(Unavailable):  # 没带凭证被 401；离站后的 401 不归咎于 Jira 凭证
            client(site, []).attachment_range("7", 0, 4, lambda: str(tmp_path / "spool"), 10)
    assert fake.auth_seen[0] == ("/rest/api/3/attachment/content/7", True)
    assert fake.auth_seen[-1] == ("/rest/api/3/myself", False)


def test_malformed_redirect_target_is_an_unexpected_response(tmp_path: Path) -> None:
    fake = FakeJira()
    with serving(fake) as site:
        fake.redirect["/rest/api/3/attachment/content/1"] = "http://127.0.0.1:99999/x"
        with pytest.raises(UnexpectedResponse, match="不合法"):
            client(site, []).attachment_range("1", 0, 1, lambda: str(tmp_path / "spool"), 1)


def test_same_origin_second_hop_carries_credentials(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 8, "b.png", b"abc")
    with serving(fake) as site:
        fake.redirect["/rest/api/3/attachment/content/9"] = (
            site + "/rest/api/3/attachment/content/8"
        )
        client(site, []).attachment_range("9", 0, 3, lambda: str(tmp_path / "spool"), 3)
    assert fake.auth_seen[-1] == ("/rest/api/3/attachment/content/8", True)


def test_unwritable_spool_is_not_reported_as_a_network_failure(tmp_path: Path) -> None:
    fake = FakeJira(honor_range=False)
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    with serving(fake) as site, pytest.raises(SpoolFailed) as caught:
        client(site, []).attachment_range(
            "7", 0, 4, lambda: str(tmp_path / "missing" / "spool"), 10
        )
    assert caught.value.retryable is False


def test_expired_media_link_is_retryable_not_a_credential_error(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    media = FakeMedia(fake)
    media.responder = lambda path, headers: (403, b"", {})  # 签名地址过期
    with serving(media) as media_url, serving(fake) as site:
        fake.media_url = media_url
        with pytest.raises(Unavailable) as caught:
            client(site, []).attachment_range("7", 0, 4, lambda: str(tmp_path / "s"), 10)
    assert caught.value.retryable is True and "凭证" not in str(caught.value)


def test_rate_limited_media_restarts_from_the_site(tmp_path: Path) -> None:
    fake = FakeJira()
    fake.add_issue(1, "ABC-1")
    fake.attach(1, 7, "a.png", b"0123456789")
    media = FakeMedia(fake)
    hits: list[str] = []

    def respond(path: str, headers: Any) -> tuple[int, Any, dict[str, str]]:
        hits.append(path)
        if len(hits) == 1:
            return 429, b"", {"Retry-After": "0"}
        return 206, b"0123", {"Content-Range": "bytes 0-3/10"}

    media.responder = respond
    sleeps: list[float] = []
    with serving(media) as media_url, serving(fake) as site:
        fake.media_url = media_url
        got = client(site, sleeps).attachment_range("7", 0, 4, lambda: str(tmp_path / "s"), 10)
    assert got.data == b"0123" and sleeps == [0.0]
    site_hits = [p for p, _ in fake.auth_seen if p == "/rest/api/3/attachment/content/7"]
    assert len(site_hits) == 2  # 退避后从站点重新取跳转目标，而不是重放旧的签名地址
    assert all(auth for p, auth in fake.auth_seen if p == "/rest/api/3/attachment/content/7")


def test_redirect_without_host_is_rejected(tmp_path: Path) -> None:
    fake = FakeJira()
    with serving(fake) as site:
        fake.redirect["/rest/api/3/attachment/content/1"] = "http://:8080/x"
        with pytest.raises(UnexpectedResponse, match="不合法"):
            client(site, []).attachment_range("1", 0, 1, lambda: str(tmp_path / "s"), 1)
