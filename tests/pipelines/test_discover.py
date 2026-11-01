import json
import pathlib
from datetime import datetime, timezone

from tests.conftest import AFFIRM_INDEED, FakeNotion, FakeSource

from jobapplier.core import db
from jobapplier.models import Posting
from jobapplier.pipelines.discover import run_discover
from jobapplier.sources import freehire

PAGE = json.loads((pathlib.Path(__file__).parents[1] / "fixtures/freehire_page.json").read_text())
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def fixture_rows():
    return [freehire.parse(r) for r in PAGE["data"]]


def titles(n):
    return [p["Title"]["title"][0]["text"]["content"] for p in n.pages.values()]


def test_run_discover_end_to_end(rules, config):
    conn, n = db.connect(":memory:"), FakeNotion()
    sources = [
        FakeSource("freehire", (fixture_rows(), None)),
        FakeSource("indeed", ([AFFIRM_INDEED], None)),
        FakeSource("linkedin", ([], "linkedin: 429")),
    ]
    r = run_discover(conn, config, rules, sources, n, NOW)
    assert r["errors"] == ["linkedin: 429"] and r["alert"] is False  # a lone LinkedIn 429 is expected, status only
    ids = [p["Job ID"]["rich_text"][0]["text"]["content"] for p in n.pages.values()]
    assert len(ids) == len(set(ids)) and r["new_rows"] == len(n.pages)
    t = titles(n)
    # Affirm arrives from FreeHire and Indeed (same ATS id): one cluster, one row.
    assert sum(p["Company"]["rich_text"][0]["text"]["content"] == "Affirm" for p in n.pages.values()) == 1
    assert "Sr. Software Engineer" not in t and "Engineering Manager, Conversational AI" not in t
    assert "Full-stack AI Developer Intern" not in t  # both Revvity rows: intern at a non-notable company
    assert "Software Engineer Intern, Pipeline Infrastructure" in t  # Disney: notable intern
    assert "PlanIT Search" not in [p["Company"]["rich_text"][0]["text"]["content"] for p in n.pages.values()]  # no location
    assert n.description.startswith("Last discover")
    assert f"{r['new_rows']} new" in n.description and "errors: linkedin: 429" in n.description


def test_second_run_creates_nothing(rules, config):
    conn, n = db.connect(":memory:"), FakeNotion()
    srcs = [FakeSource("freehire", (fixture_rows(), None))]
    first = run_discover(conn, config, rules, srcs, n, NOW)
    again = run_discover(conn, config, rules, srcs, n, NOW)
    assert first["new_rows"] > 0 and again["new_rows"] == 0 and len(n.pages) == first["new_rows"]
    assert "errors: none" in n.description


def test_freehire_error_sets_alert_and_other_sources_still_push(rules, config):
    n = FakeNotion()
    sources = [FakeSource("freehire", ([], "freehire: HTTP 429")), FakeSource("indeed", ([AFFIRM_INDEED], None))]
    r = run_discover(db.connect(":memory:"), config, rules, sources, n, NOW)
    assert len(n.pages) == 1 and r["errors"] == ["freehire: HTTP 429"] and r["alert"] is True


def test_source_exception_is_recorded_and_alerts(rules, config):
    n = FakeNotion()
    sources = [FakeSource("indeed", exc=RuntimeError("boom")), FakeSource("linkedin", ([], None))]
    r = run_discover(db.connect(":memory:"), config, rules, sources, n, NOW)
    assert r["errors"] == ["indeed: boom"] and r["alert"] is True and n.description.startswith("Last discover")


def test_malformed_posting_is_skipped_not_fatal(rules, config):
    bad = Posting("indeed", "bad-1", "Software Engineer", "Acme", "Toronto, ON", "http://[broken", "2026-10-30")
    n = FakeNotion()
    sources = [FakeSource("indeed", ([bad, AFFIRM_INDEED], None))]
    r = run_discover(db.connect(":memory:"), config, rules, sources, n, NOW)
    assert len(n.pages) == 1  # the good posting still lands
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("skipped 1 postings: ") and r["alert"] is True


def test_notion_status_error_is_recorded(rules, config):
    class StatusFails(FakeNotion):
        def request(self, method, path, body=None):
            if method == "PATCH":
                raise self.error("500 boom")
            return super().request(method, path, body)

    r = run_discover(db.connect(":memory:"), config, rules, [FakeSource("indeed", ([AFFIRM_INDEED], None))], StatusFails(), NOW)
    assert r["errors"] == ["notion: 500 boom"] and r["alert"] is True and r["new_rows"] == 1


def test_push_errors_join_the_list(rules, config):
    n = FakeNotion(fail_on_create={1})
    r = run_discover(db.connect(":memory:"), config, rules, [FakeSource("indeed", ([AFFIRM_INDEED], None))], n, NOW)
    assert r["errors"] == ["notion: J1: 400 validation_error"] and r["alert"] is True and r["new_rows"] == 0
