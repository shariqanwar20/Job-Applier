import pytest
from tests.conftest import FakeSession, Resp

from jobapplier.notion.client import Notion, NotionError


def test_request_throttles_and_retries_429():
    slept = []
    s = FakeSession([Resp(429, {}, {"Retry-After": "2"}), Resp(200, {"ok": True})])
    n = Notion("tok", session=s, sleep=slept.append)
    assert n.request("GET", "/v1/pages/x") == {"ok": True}
    assert 2 in slept and len(s.calls) == 2 and s.calls[0]["headers"]["Notion-Version"] == "2025-09-03"
    assert slept.count(0.35) == 2 and s.calls[0]["url"] == "https://api.notion.com/v1/pages/x"


def test_request_retries_529_then_gives_up_after_five_retries():
    slept = []
    s = FakeSession([Resp(529, {})] * 6)
    n = Notion("tok", session=s, sleep=slept.append)
    with pytest.raises(NotionError):
        n.request("GET", "/v1/pages/x")
    assert len(s.calls) == 6 and slept.count(1) == 5  # Retry-After defaults to 1


def test_request_raises_on_400():
    n = Notion("tok", session=FakeSession([Resp(400, {"message": "bad"})]), sleep=lambda _: None)
    with pytest.raises(NotionError, match="400"):
        n.request("POST", "/v1/pages", {})
