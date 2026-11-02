from datetime import UTC, datetime

import httpx

from jobapplier.core.clusters import assign, connect
from jobapplier.models import Posting
from jobapplier.notion.push import _page_properties, create_database, push, set_status
from tests.conftest import FakeNotion, api_error

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
SUMMARY = {
    "title": "SWE",
    "company": "Affirm",
    "city": "Toronto",
    "alt_locations": "",
    "rank": 2,
    "ats": "Greenhouse",
    "posted": "",
    "flags": ["Remote US"],
    "link": "https://x",
    "identity": "greenhouse:a:1",
}


def test_page_properties_shape():
    props = _page_properties("J7", SUMMARY, "2026-10-31T12:00:00+00:00")
    assert props["Job ID"]["rich_text"][0]["text"]["content"] == "J7"
    assert props["Rank"]["select"]["name"] == "2 Under 3 yrs"
    assert props["Decision"]["select"]["name"] == "New" and props["Flags"]["multi_select"] == [
        {"name": "Remote US"}
    ]
    assert props["Status"]["select"]["name"] == "New" and props["Link"]["url"] == "https://x"
    assert props["First seen"]["date"]["start"] == "2026-10-31T12:00:00+00:00"
    assert "Posted" not in props and props["Alt locations"] == {"rich_text": []}


def test_page_properties_posted_and_long_text():
    props = _page_properties("J7", {**SUMMARY, "posted": "2026-10-30", "alt_locations": "x" * 5000}, "t")
    assert props["Posted"]["date"]["start"] == "2026-10-30"
    assert len(props["Alt locations"]["rich_text"][0]["text"]["content"]) == 2000


def test_set_status_patches_description_cut_to_2000():
    n = FakeNotion()
    set_status(n, "db1", "x" * 3000)
    name, kw = n.calls[0]
    assert (
        name == "databases.update"
        and kw["database_id"] == "db1"
        and len(kw["description"][0]["text"]["content"]) == 2000
    )


def test_create_database_posts_schema_and_returns_ids():
    n = FakeNotion()
    assert create_database(n, "parent1") == ("db1", "ds1")
    name, kw = n.calls[0]
    assert name == "databases.create" and kw["parent"] == {"type": "page_id", "page_id": "parent1"}
    props = kw["initial_data_source"]["properties"]
    assert props["Job ID"] == {"rich_text": {}} and props["Title"] == {"title": {}}
    assert [o["name"] for o in props["Rank"]["select"]["options"]] == [
        "1 New grad",
        "2 Under 3 yrs",
        "3 Other",
    ]


def seed(rules, count=3):
    conn = connect(":memory:")
    for i in range(1, count + 1):
        p = Posting(
            "freehire",
            f"fh{i}",
            "Software Engineer",
            f"Co{i}",
            "Toronto, ON",
            f"https://boards.greenhouse.io/co{i}/jobs/{i}",
        )
        assert assign(conn, p, NOW, False) == i
    conn.commit()
    return conn


def test_push_skips_a_failing_row_and_resumes_without_duplicates(rules):
    conn, n = seed(rules), FakeNotion(fail_on_create={2: api_error(400, "validation_error")})
    created, errors = push(conn, n, "ds1", rules)
    assert created == 2 and len(errors) == 1 and errors[0].startswith("notion: J2: 400 ")
    stored = {r["id"]: r["notion_page_id"] for r in conn.execute("SELECT id, notion_page_id FROM clusters")}
    assert stored[1] and stored[3] and stored[2] is None
    n.fail_on_create.clear()
    assert push(conn, n, "ds1", rules) == (1, [])
    ids = [p["Job ID"]["rich_text"][0]["text"]["content"] for p in n.rows.values()]
    assert sorted(ids) == ["J1", "J2", "J3"]


def test_push_looks_up_the_job_id_before_creating(rules):
    conn, n = seed(rules, 1), FakeNotion()
    push(conn, n, "ds1", rules)
    query = {
        "data_source_id": "ds1",
        "filter": {"property": "Job ID", "rich_text": {"equals": "J1"}},
        "page_size": 1,
    }
    assert n.calls[0] == ("data_sources.query", query) and n.calls[1][0] == "pages.create"


def test_push_reuses_existing_page_found_by_job_id(rules):
    conn, n = seed(rules, 1), FakeNotion()
    n.rows["old-page"] = {"Job ID": {"rich_text": [{"text": {"content": "J1"}}]}}
    assert push(conn, n, "ds1", rules) == (0, [])
    assert conn.execute("SELECT notion_page_id FROM clusters").fetchone()[0] == "old-page" and n.creates == 0


def test_push_sends_the_repost_flag_and_data_source_parent(rules):
    conn = seed(rules, 1)
    conn.execute("UPDATE clusters SET repost = 1")
    n = FakeNotion()
    push(conn, n, "ds1", rules)
    page = next(kw for name, kw in n.calls if name == "pages.create")
    assert page["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
    assert {"name": "Repost"} in page["properties"]["Flags"]["multi_select"]


def test_push_treats_a_network_error_as_a_per_row_error(rules):
    conn, n = seed(rules), FakeNotion(fail_on_create={2: httpx.ConnectError("https://api.notion.com boom")})
    created, errors = push(conn, n, "ds1", rules)
    assert created == 2 and errors == ["notion: J2: network: ConnectError"]
    assert [r[0] for r in conn.execute("SELECT notion_page_id FROM clusters ORDER BY id")] == [
        "page-1",
        None,
        "page-2",
    ]
