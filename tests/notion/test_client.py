import logging

import httpx
import pytest
from notion_client import RequestTimeoutError

from jobapplier.notion import client as notion_client_mod
from jobapplier.notion.client import NotionError, call, make_client
from tests.conftest import api_error


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
    http, sent = mock_http(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, json=error_body("rate_limited")),
            httpx.Response(200, json={"results": []}),
        ]
    )
    client = make_client("tok", http)
    assert call(client.data_sources.query, data_source_id="ds1", page_size=1) == {"results": []}
    assert len(sent) == 2 and client.options.retry.max_retries == 5
    assert (
        sent[0].headers["Authorization"] == "Bearer tok" and sent[0].headers["Notion-Version"] == "2025-09-03"
    )
    assert str(sent[0].url) == "https://api.notion.com/v1/data_sources/ds1/query"


def test_make_client_can_pin_a_notion_version():
    http, sent = mock_http([httpx.Response(200, json={"results": []})])
    call(make_client("tok", http, notion_version="2026-03-11").views.list, database_id="db1")
    assert sent[0].headers["Notion-Version"] == "2026-03-11" and sent[0].url.path == "/v1/views"


def test_page_create_is_not_resent_after_a_server_error():
    http, sent = mock_http(
        [
            httpx.Response(500, json=error_body("internal_server_error")),
            httpx.Response(200, json={"id": "p1"}),
        ]
    )
    with pytest.raises(NotionError, match=r"^500 boom$"):
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
    with pytest.raises(NotionError, match=r"^400 bad$"):
        call(raises(api_error(400, "validation_error", "bad")))
    with pytest.raises(NotionError, match="timed out"):
        call(raises(RequestTimeoutError()))
    with pytest.raises(NotionError, match=r"^network: ConnectError$"):
        call(raises(httpx.ConnectError("https://api.notion.com/v1/pages boom")))


def test_a_non_json_success_body_is_a_notion_error():
    http, _ = mock_http([httpx.Response(200, text="<html>gateway</html>")])
    with pytest.raises(NotionError, match="malformed response"):
        call(make_client("tok", http).pages.create, parent={}, properties={})


def test_sdk_warnings_go_to_our_logger_once(caplog):
    http, _ = mock_http([httpx.Response(400, json=error_body("validation_error"))])
    with pytest.raises(NotionError):
        call(make_client("tok", http).pages.create, parent={}, properties={})
    warnings = [r.name for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["jobapplier.notion"] and not logging.getLogger("notion_client").handlers
