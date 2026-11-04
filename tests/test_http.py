import pytest
import requests

from jobapplier import http
from jobapplier.http import ApiClient, ApiError


class Resp:
    def __init__(self, status_code, body=None):
        self.status_code, self._body = status_code, body or {}

    def json(self):
        return self._body


class FakeSession(requests.Session):
    """A real Session (headers, adapters) whose get serves queued responses instead of the network."""

    def __init__(self, responses):
        super().__init__()
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_get_json_joins_url_and_sends_headers_params_timeout():
    s = FakeSession([Resp(200, {"ok": True})])
    api = ApiClient("https://api.example.com/v1", headers={"X-Key": "k"}, timeout=12, session=s)
    assert api.get_json("/jobs", [("a", 1)]) == {"ok": True}
    assert s.calls == [{"url": "https://api.example.com/v1/jobs", "params": [("a", 1)], "timeout": 12}]
    assert s.headers["X-Key"] == "k"


def test_get_json_sends_per_call_headers_only_when_given():
    s = FakeSession([Resp(200), Resp(200)])
    api = ApiClient("https://x", session=s)
    api.get_json("/a")
    api.get_json("/b", headers={"Authorization": "Bearer t"})
    assert "headers" not in s.calls[0] and s.calls[1]["headers"] == {"Authorization": "Bearer t"}
    assert "Authorization" not in s.headers


def test_retry_is_mounted_for_get_on_rate_limits_and_server_errors():
    retry = ApiClient("https://x", session=FakeSession([])).session.get_adapter("https://x/y").max_retries
    assert retry.total == 3 and set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    assert set(retry.allowed_methods) == {"GET"} and retry.respect_retry_after_header is True
    assert retry.raise_on_status is False


def test_non_2xx_and_transport_errors_raise_api_error_without_the_url():
    api = ApiClient(
        "https://x", session=FakeSession([Resp(503), requests.ConnectionError("https://x/secret boom")])
    )
    with pytest.raises(ApiError, match=r"^HTTP 503$"):
        api.get_json("/a")
    with pytest.raises(ApiError, match=r"^network: ConnectionError$"):
        api.get_json("/a")


def test_min_interval_spaces_calls(monkeypatch):
    slept = []
    monkeypatch.setattr(http, "sleep", slept.append)
    api = ApiClient("https://x", min_interval=5, session=FakeSession([Resp(200), Resp(200)]))
    api.get_json("/a")
    api.get_json("/a")
    assert len(slept) == 1 and 4 < slept[0] <= 5
