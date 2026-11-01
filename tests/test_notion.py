import subprocess
from datetime import datetime, timezone

import pytest
import requests
from conftest import FakeNotion, FakeSession, Resp

from jobapplier import db, notion
from jobapplier.dedupe import assign
from jobapplier.models import Posting
from jobapplier.notion import Notion, NotionError, page_properties

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SUMMARY = {"title": "SWE", "company": "Affirm", "city": "Toronto", "alt_locations": "", "rank": 2, "ats": "Greenhouse",
           "posted": "", "flags": ["Remote US"], "link": "https://x", "identity": "greenhouse:a:1"}


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


def test_page_properties_shape():
    props = page_properties("J7", SUMMARY, "2026-10-31T12:00:00+00:00")
    assert props["Job ID"]["rich_text"][0]["text"]["content"] == "J7"
    assert props["Rank"]["select"]["name"] == "2 Under 3 yrs"
    assert props["Decision"]["select"]["name"] == "New" and props["Flags"]["multi_select"] == [{"name": "Remote US"}]
    assert props["Status"]["select"]["name"] == "New" and props["Link"]["url"] == "https://x"
    assert props["First seen"]["date"]["start"] == "2026-10-31T12:00:00+00:00"
    assert "Posted" not in props and props["Alt locations"] == {"rich_text": []}


def test_page_properties_posted_and_long_text():
    props = page_properties("J7", {**SUMMARY, "posted": "2026-10-30", "alt_locations": "x" * 5000}, "t")
    assert props["Posted"]["date"]["start"] == "2026-10-30"
    assert len(props["Alt locations"]["rich_text"][0]["text"]["content"]) == 2000


def test_create_database_posts_schema_and_returns_ids():
    s = FakeSession([Resp(200, {"id": "db1", "data_sources": [{"id": "ds1"}]})])
    assert notion.create_database(Notion("tok", session=s, sleep=lambda _: None), "parent1") == ("db1", "ds1")
    call = s.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/databases")
    assert call["json"]["parent"] == {"type": "page_id", "page_id": "parent1"}
    assert call["json"]["initial_data_source"] == {"properties": notion.SCHEMA}


def test_find_page_queries_by_job_id():
    s = FakeSession([Resp(200, {"results": []}), Resp(200, {"results": [{"id": "p9"}]})])
    n = Notion("tok", session=s, sleep=lambda _: None)
    assert notion.find_page(n, "ds1", "J1") is None and notion.find_page(n, "ds1", "J1") == "p9"
    assert s.calls[0]["url"].endswith("/v1/data_sources/ds1/query")
    assert s.calls[0]["json"] == {"filter": {"property": "Job ID", "rich_text": {"equals": "J1"}}, "page_size": 1}


def test_set_status_patches_description_cut_to_2000():
    s = FakeSession([Resp(200, {})])
    notion.set_status(Notion("tok", session=s, sleep=lambda _: None), "db1", "x" * 3000)
    assert s.calls[0]["method"] == "PATCH" and s.calls[0]["url"].endswith("/v1/databases/db1")
    assert len(s.calls[0]["json"]["description"][0]["text"]["content"]) == 2000


def test_keychain_strips_stdout(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "secret\n", ""))
    assert notion.keychain("svc") == "secret"
    assert calls == [["security", "find-generic-password", "-s", "svc", "-w"]]


def seed(rules, count=3):
    conn = db.connect(":memory:")
    for i in range(1, count + 1):
        p = Posting("freehire", f"fh{i}", "Software Engineer", f"Co{i}", "Toronto, ON", f"https://boards.greenhouse.io/co{i}/jobs/{i}")
        assert assign(conn, p, NOW, False) == i
    conn.commit()
    return conn


def test_push_skips_a_failing_row_and_resumes_without_duplicates(rules):
    conn, n = seed(rules), FakeNotion(fail_on_create={2})
    created, errors = notion.push(conn, n, "ds1", rules)
    assert created == 2 and len(errors) == 1 and errors[0].startswith("notion: J2: ")
    stored = {r["id"]: r["notion_page_id"] for r in conn.execute("SELECT id, notion_page_id FROM clusters")}
    assert stored[1] and stored[3] and stored[2] is None
    n.fail_on_create.clear()
    assert notion.push(conn, n, "ds1", rules) == (1, [])
    ids = [p["Job ID"]["rich_text"][0]["text"]["content"] for p in n.pages.values()]
    assert sorted(ids) == ["J1", "J2", "J3"]


def test_push_reuses_existing_page_found_by_job_id(rules):
    conn, n = seed(rules, 1), FakeNotion()
    n.pages["old-page"] = {"Job ID": {"rich_text": [{"text": {"content": "J1"}}]}}
    assert notion.push(conn, n, "ds1", rules) == (0, [])
    assert conn.execute("SELECT notion_page_id FROM clusters").fetchone()[0] == "old-page" and n.creates == 0


def test_push_sends_the_repost_flag_and_data_source_parent(rules):
    conn = seed(rules, 1)
    conn.execute("UPDATE clusters SET repost = 1")
    sent = []
    n = FakeNotion()
    real = n.request
    n.request = lambda m, p, b=None: sent.append((p, b)) or real(m, p, b)
    notion.push(conn, n, "ds1", rules)
    page = next(b for p, b in sent if p == "/v1/pages")
    assert page["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
    assert {"name": "Repost"} in page["properties"]["Flags"]["multi_select"]


def test_push_treats_a_network_error_as_a_per_row_error(rules):
    ok = lambda body: Resp(200, body)
    s = FakeSession([ok({"results": []}), ok({"id": "p1"}), ok({"results": []}), requests.ConnectionError("https://api.notion.com boom"),
                     ok({"results": []}), ok({"id": "p3"})])
    conn = seed(rules)
    created, errors = notion.push(conn, Notion("tok", session=s, sleep=lambda _: None), "ds1", rules)
    assert created == 2 and errors == ["notion: J2: network: ConnectionError"]
    assert [r[0] for r in conn.execute("SELECT notion_page_id FROM clusters ORDER BY id")] == ["p1", None, "p3"]
