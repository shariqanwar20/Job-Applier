import httpx
import pytest
from notion_client import RequestTimeoutError
from tests.conftest import api_error

from jobapplier.notion import client as notion_client_mod
from jobapplier.notion.client import NotionError, call, make_client


def error_body(code, message="boom"):
    return {"object": "error", "status": 0, "code": code, "message": message}


def mock_http(responses):
    """An httpx.Client whose transport serves queued responses and records each request."""
    sent = []

    def handler(request):
        sent.append(request)
        return responses.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def test_make_client_sends_token_and_version_and_retries_429():
    http, sent = mock_http([httpx.Response(429, headers={"Retry-After": "0"}, json=error_body("rate_limited")), httpx.Response(200, json={"results": []})])
    client = make_client("tok", http)
    assert call(client.data_sources.query, data_source_id="ds1", page_size=1) == {"results": []}
    assert len(sent) == 2 and client.options.retry.max_retries == 5
    assert sent[0].headers["Authorization"] == "Bearer tok" and sent[0].headers["Notion-Version"] == "2025-09-03"
    assert str(sent[0].url) == "https://api.notion.com/v1/data_sources/ds1/query"


def test_page_create_is_not_resent_after_a_server_error():
    http, sent = mock_http([httpx.Response(500, json=error_body("internal_server_error")), httpx.Response(200, json={"id": "p1"})])
    with pytest.raises(NotionError, match="^500 boom$"):
        call(make_client("tok", http).pages.create, parent={}, properties={})
    assert len(sent) == 1


def test_call_paces_each_request(monkeypatch):
    slept = []
    monkeypatch.setattr(notion_client_mod, "sleep", slept.append)
    call(lambda: {}), call(lambda: {})
    assert slept == [0.35, 0.35]


def raises(exc):
    def endpoint(**kw):
        raise exc

    return endpoint


def test_call_wraps_sdk_and_transport_errors_without_the_url():
    with pytest.raises(NotionError, match="^400 bad$"):
        call(raises(api_error(400, "validation_error", "bad")))
    with pytest.raises(NotionError, match="timed out"):
        call(raises(RequestTimeoutError()))
    with pytest.raises(NotionError, match="^network: ConnectError$"):
        call(raises(httpx.ConnectError("https://api.notion.com/v1/pages boom")))
