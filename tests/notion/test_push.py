from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobapplier.core.clusters import assign, connect
from jobapplier.core.rules import Rules
from jobapplier.mail.gmail import MailError
from jobapplier.models import Posting
from jobapplier.notion.client import NotionError
from jobapplier.notion.push import _page_properties, create_database, prune, push, set_status, style_database
from tests.conftest import FakeMail, FakeNotion, FakeViewsNotion, api_error

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
    assert create_database(n, "parent1", "Role Search") == ("db1", "ds1")
    name, kw = n.calls[0]
    assert name == "databases.create" and kw["parent"] == {"type": "page_id", "page_id": "parent1"}
    assert kw["is_inline"] is True and kw["title"] == [{"text": {"content": "Role Search"}}]
    props = kw["initial_data_source"]["properties"]
    assert props["Job ID"] == {"rich_text": {}} and props["Title"] == {"title": {}}
    assert [o["name"] for o in props["Rank"]["select"]["options"]] == [
        "1 New grad",
        "2 Under 3 yrs",
        "3 Other",
    ]


def test_style_database_titles_the_data_source_and_groups_the_default_view_by_rank():
    n, v = FakeNotion(), FakeViewsNotion()
    style_database(n, v, "db1", "ds1", "Role Search")
    assert n.calls == [
        ("data_sources.update", {"data_source_id": "ds1", "title": [{"text": {"content": "Role Search"}}]}),
        ("data_sources.retrieve", {"data_source_id": "ds1"}),
    ]
    assert v.calls == [
        ("views.list", {"database_id": "db1"}),
        (
            "views.update",
            {
                "view_id": "view1",
                "name": "Inbox",
                "filter": {"property": "Decision", "select": {"does_not_equal": "Skip"}},
                "sorts": [{"property": "First seen", "direction": "descending"}],
                "configuration": {
                    "type": "table",
                    "group_by": {
                        "type": "select",
                        "property_id": "rank%3Aid",
                        "sort": {"type": "manual"},
                        "hide_empty_groups": True,
                    },
                },
            },
        ),
    ]


def test_style_database_without_a_view_is_a_notion_error():
    with pytest.raises(NotionError, match="no view"):
        style_database(FakeNotion(), FakeViewsNotion(views=()), "db1", "ds1", "t")


def test_style_database_wraps_views_api_errors():
    v = FakeViewsNotion(fail=api_error(400, "validation_error", "unsupported"))
    with pytest.raises(NotionError, match=r"^400 unsupported$"):
        style_database(FakeNotion(), v, "db1", "ds1", "t")


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
        assert assign(conn, p, rules, NOW, False) == i
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


def _row_flags(n, company):
    page = next(p for p in n.rows.values() if p["Company"]["rich_text"][0]["text"]["content"] == company)
    return [f["name"] for f in page["Flags"]["multi_select"]]


def test_push_flags_a_row_whose_company_was_applied_to_and_logs_the_hit(rules, caplog):
    conn, n = seed(rules), FakeNotion()
    mail = FakeMail({"Co2": "2026-09-01: Thank you for applying to Co2"})
    with caplog.at_level("INFO", logger="jobapplier"):
        assert push(conn, n, "ds1", rules, mail) == (3, [])
    assert "Applied before" in _row_flags(n, "Co2")
    assert "Applied before" not in _row_flags(n, "Co1") + _row_flags(n, "Co3")
    assert "2026-09-01: Thank you for applying to Co2" in caplog.text
    assert mail.calls == ["Co1", "Co2", "Co3"]


def test_push_asks_mail_once_per_company_and_only_for_new_rows(rules):
    conn = seed(rules, 1)
    conn.execute("UPDATE clusters SET notion_page_id = NULL")
    assign(
        conn,
        Posting(
            "freehire",
            "x",
            "Software Developer",
            "Co1",
            "Toronto, ON",
            "https://boards.greenhouse.io/co1/jobs/9",
        ),
        rules,
        NOW,
        False,
    )
    conn.commit()
    n, mail = FakeNotion(), FakeMail()
    n.rows["old"] = {"Job ID": {"rich_text": [{"text": {"content": "J1"}}]}}  # J1 exists in Notion already
    assert push(conn, n, "ds1", rules, mail) == (1, []) and mail.calls == ["Co1"]


def test_push_with_a_failing_mail_records_one_error_and_still_pushes_every_row(rules):
    conn, n = seed(rules), FakeNotion()
    mail = FakeMail(error=MailError("mail: HTTP 500"))
    assert push(conn, n, "ds1", rules, mail) == (3, ["mail: HTTP 500"])
    assert mail.calls == ["Co1"] and all("Applied before" not in _row_flags(n, f"Co{i}") for i in (1, 2, 3))


def test_push_without_mail_adds_no_flag(rules):
    conn, n = seed(rules, 1), FakeNotion()
    push(conn, n, "ds1", rules)
    assert _row_flags(n, "Co1") == ["Aggregator link"]


def _rules_with(config, **overrides):
    return Rules.from_config({**config, "rules": {**config["rules"], **overrides}})


def _decision(n, page_id):
    return n.rows[page_id]["Decision"]["select"]["name"]


def _flags(n, page_id):
    return [f["name"] for f in n.rows[page_id]["Flags"]["multi_select"]]


def _pruned(conn):
    return {r["cluster_id"]: r["reason"] for r in conn.execute("SELECT * FROM pruned")}


def test_prune_skips_a_row_the_rules_no_longer_push(rules, config):
    conn, n = seed(rules), FakeNotion()
    push(conn, n, "ds1", rules)
    strict = _rules_with(config, blocked_companies=["co2"])
    assert prune(conn, n, strict, NOW) == (1, [])
    assert _decision(n, "page-2") == "Skip" and _flags(n, "page-2") == ["Aggregator link", "Filtered"]
    assert _decision(n, "page-1") == "New" and _decision(n, "page-3") == "New"
    assert _pruned(conn) == {2: "filtered"}
    assert not any(name == "pages.update" and kw["page_id"] != "page-2" for name, kw in n.calls)


def _duplicate_pair(rules, config, days=1):
    """Two pushed clusters for one employer, split because they were assigned under looser rules."""
    loose = _rules_with(config, company_noise=[], company_aliases={})
    conn, n = connect(":memory:"), FakeNotion()
    for i, (company, delta) in enumerate((("TD", 0), ("TD Bank Group", days)), 1):
        p = Posting(
            "linkedin",
            f"li{i}",
            "Software Developer",
            company,
            "Toronto, ON",
            f"https://www.linkedin.com/jobs/view/{i}",
        )
        assert assign(conn, p, loose, NOW + timedelta(days=delta), False) == i
    conn.commit()
    push(conn, n, "ds1", rules)
    n.rows["page-2"]["Flags"] = {"multi_select": [{"name": "Remote US"}]}
    return conn, n


def test_prune_skips_a_later_duplicate_and_leaves_the_earlier_row(rules, config):
    conn, n = _duplicate_pair(rules, config)
    assert prune(conn, n, rules, NOW) == (1, [])
    assert _decision(n, "page-2") == "Skip" and _flags(n, "page-2") == ["Remote US", "Duplicate"]
    assert _decision(n, "page-1") == "New" and ("pages.update", "page-1") not in [
        (name, kw.get("page_id")) for name, kw in n.calls
    ]
    assert _pruned(conn) == {2: "duplicate"}


def test_prune_leaves_a_same_employer_row_seen_over_a_week_later(rules, config):
    conn, n = _duplicate_pair(rules, config, days=8)
    assert prune(conn, n, rules, NOW) == (0, []) and _pruned(conn) == {}


def test_prune_does_not_touch_a_page_the_owner_already_decided(rules, config):
    conn, n = seed(rules), FakeNotion()
    push(conn, n, "ds1", rules)
    n.rows["page-2"]["Decision"] = {"select": {"name": "Approve"}}
    assert prune(conn, n, _rules_with(config, blocked_companies=["co2"]), NOW) == (0, [])
    assert _decision(n, "page-2") == "Approve" and _pruned(conn) == {2: "kept: owner decided"}
    assert not any(name == "pages.update" for name, _ in n.calls)


def test_prune_tolerates_a_missing_flags_property(rules, config):
    conn, n = seed(rules), FakeNotion()
    push(conn, n, "ds1", rules)
    del n.rows["page-2"]["Flags"]
    assert prune(conn, n, _rules_with(config, blocked_companies=["co2"]), NOW) == (1, [])
    assert _decision(n, "page-2") == "Skip" and _flags(n, "page-2") == ["Filtered"]


def test_prune_treats_a_missing_decision_as_decided_and_does_not_touch_the_page(rules, config):
    conn, n = seed(rules), FakeNotion()
    push(conn, n, "ds1", rules)
    del n.rows["page-2"]["Decision"]
    assert prune(conn, n, _rules_with(config, blocked_companies=["co2"]), NOW) == (0, [])
    assert _pruned(conn) == {2: "kept: owner decided"}
    assert not any(name == "pages.update" for name, _ in n.calls)


def test_second_prune_makes_no_notion_calls(rules, config):
    conn, n = seed(rules), FakeNotion()
    push(conn, n, "ds1", rules)
    strict = _rules_with(config, blocked_companies=["co2", "co3"])
    n.rows["page-3"]["Decision"] = {"select": {"name": "Skip"}}
    assert prune(conn, n, strict, NOW) == (1, [])
    seen = len(n.calls)
    assert prune(conn, n, strict, NOW) == (0, []) and len(n.calls) == seen


def test_prune_reports_a_notion_error_and_retries_that_row_next_time(rules, config):
    conn, n = seed(rules), FakeNotion(fail_on_update={"page-2": api_error(500, "internal_server_error")})
    push(conn, n, "ds1", rules)
    strict = _rules_with(config, blocked_companies=["co2", "co3"])
    assert prune(conn, n, strict, NOW) == (1, ["notion: J2: 500 boom"])
    assert _decision(n, "page-3") == "Skip" and _decision(n, "page-2") == "New" and set(_pruned(conn)) == {3}
    n.fail_on_update.clear()
    assert prune(conn, n, strict, NOW) == (1, []) and _decision(n, "page-2") == "Skip"
