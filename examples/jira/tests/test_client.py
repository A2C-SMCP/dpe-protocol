"""Jira 客户端的凭证安全与限流：不跟随重定向、Retry-After 上限、无提示时的指数退避。"""

from __future__ import annotations

import pytest
from fake_jira import EMAIL, TOKEN, FakeJira, serving

from dpe_jira.client import JiraClient, RateLimited, UnexpectedResponse


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
