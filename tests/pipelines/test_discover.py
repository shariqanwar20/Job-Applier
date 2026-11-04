import json
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace

from jobapplier.core.clusters import connect
from jobapplier.core.rules import Rules
from jobapplier.mail.gmail import MailError
from jobapplier.models import Posting
from jobapplier.pipelines.discover import run_discover
from jobapplier.sources.freehire import FreeHire
from tests.conftest import AFFIRM_INDEED, FakeMail, FakeNotion, FakeSource, api_error

PAGE = json.loads((pathlib.Path(__file__).parents[1] / "fixtures/freehire_page.json").read_text())
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def fixture_rows():
    api = SimpleNamespace(get_json=lambda path, params: PAGE)
    rows, err = FreeHire(
        {"via_recruiter": False, "countries": [], "regions": [], "categories": []}, 3, api=api
    ).fetch()
    assert err is None
    return rows


def titles(n):
    return [p["Title"]["title"][0]["text"]["content"] for p in n.rows.values()]


def test_run_discover_end_to_end(rules, config):
    conn, n = connect(":memory:"), FakeNotion()
    sources = [
        FakeSource("freehire", (fixture_rows(), None)),
        FakeSource("indeed", ([AFFIRM_INDEED], None)),
        FakeSource("linkedin", ([], "linkedin: 429")),
    ]
    r = run_discover(conn, config, rules, sources, n, NOW)
    assert (
        r["errors"] == ["linkedin: 429"] and r["alert"] is False
    )  # a lone LinkedIn 429 is expected, status only
    ids = [p["Job ID"]["rich_text"][0]["text"]["content"] for p in n.rows.values()]
    assert len(ids) == len(set(ids)) and r["new_rows"] == len(n.rows)
    t = titles(n)
    # Affirm arrives from FreeHire and Indeed (same ATS id): one cluster, one row.
    assert sum(p["Company"]["rich_text"][0]["text"]["content"] == "Affirm" for p in n.rows.values()) == 1
    assert "Sr. Software Engineer" not in t and "Engineering Manager, Conversational AI" not in t
    assert "Full-stack AI Developer Intern" not in t  # both Revvity rows: intern at a non-notable company
    assert "Software Engineer Intern, Pipeline Infrastructure" in t  # Disney: notable intern
    assert "PlanIT Search" not in [
        p["Company"]["rich_text"][0]["text"]["content"] for p in n.rows.values()
    ]  # no location
    assert n.description.startswith("Last discover")
    assert f"{r['new_rows']} new" in n.description and "errors: linkedin: 429" in n.description


def test_second_run_creates_nothing(rules, config):
    conn, n = connect(":memory:"), FakeNotion()
    srcs = [FakeSource("freehire", (fixture_rows(), None))]
    first = run_discover(conn, config, rules, srcs, n, NOW)
    again = run_discover(conn, config, rules, srcs, n, NOW)
    assert first["new_rows"] > 0 and again["new_rows"] == 0 and len(n.rows) == first["new_rows"]
    assert "errors: none" in n.description


def test_freehire_error_sets_alert_and_other_sources_still_push(rules, config):
    n = FakeNotion()
    sources = [
        FakeSource("freehire", ([], "freehire: HTTP 429")),
        FakeSource("indeed", ([AFFIRM_INDEED], None)),
    ]
    r = run_discover(connect(":memory:"), config, rules, sources, n, NOW)
    assert len(n.rows) == 1 and r["errors"] == ["freehire: HTTP 429"] and r["alert"] is True


def test_source_exception_is_recorded_and_alerts(rules, config):
    n = FakeNotion()
    sources = [FakeSource("indeed", exc=RuntimeError("boom")), FakeSource("linkedin", ([], None))]
    r = run_discover(connect(":memory:"), config, rules, sources, n, NOW)
    assert (
        r["errors"] == ["indeed: boom"] and r["alert"] is True and n.description.startswith("Last discover")
    )


def test_malformed_posting_is_skipped_not_fatal(rules, config):
    bad = Posting(
        "indeed", "bad-1", "Software Engineer", "Acme", "Toronto, ON", "http://[broken", "2026-10-30"
    )
    n = FakeNotion()
    sources = [FakeSource("indeed", ([bad, AFFIRM_INDEED], None))]
    r = run_discover(connect(":memory:"), config, rules, sources, n, NOW)
    assert len(n.rows) == 1  # the good posting still lands
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("skipped 1 postings: ") and r["alert"] is True


def test_notion_status_error_is_recorded(rules, config):
    n = FakeNotion()
    n.databases.update = lambda **kw: (_ for _ in ()).throw(api_error(500, "internal_server_error"))
    r = run_discover(
        connect(":memory:"), config, rules, [FakeSource("indeed", ([AFFIRM_INDEED], None))], n, NOW
    )
    assert r["errors"] == ["notion: 500 boom"] and r["alert"] is True and r["new_rows"] == 1


def test_push_errors_join_the_list(rules, config):
    n = FakeNotion(fail_on_create={1: api_error(400, "validation_error")})
    r = run_discover(
        connect(":memory:"), config, rules, [FakeSource("indeed", ([AFFIRM_INDEED], None))], n, NOW
    )
    assert r["errors"] == ["notion: J1: 400 boom"] and r["alert"] is True and r["new_rows"] == 0


def test_run_discover_prunes_rows_the_rules_no_longer_push(rules, config):
    conn, n = connect(":memory:"), FakeNotion()
    srcs = [FakeSource("indeed", ([AFFIRM_INDEED], None))]
    run_discover(conn, config, rules, srcs, n, NOW)
    strict = Rules.from_config({**config, "rules": {**config["rules"], "blocked_companies": ["affirm"]}})
    r = run_discover(conn, config, strict, srcs, n, NOW)
    assert n.rows["page-1"]["Decision"]["select"]["name"] == "Skip"
    assert r["new_rows"] == 0 and "· 1 pruned ·" in n.description and "errors: none" in n.description


def test_prune_errors_join_the_list(rules, config):
    conn, n = connect(":memory:"), FakeNotion(fail_on_update={"page-1": api_error(400, "validation_error")})
    srcs = [FakeSource("indeed", ([AFFIRM_INDEED], None))]
    run_discover(conn, config, rules, srcs, n, NOW)
    strict = Rules.from_config({**config, "rules": {**config["rules"], "blocked_companies": ["affirm"]}})
    r = run_discover(conn, config, strict, srcs, n, NOW)
    assert r["errors"] == ["notion: J1: 400 boom"] and r["alert"] is True and "· 0 pruned ·" in n.description


def test_run_discover_flags_rows_for_companies_applied_to(rules, config):
    n, mail = FakeNotion(), FakeMail({"Affirm": "2026-05-02: Thank you for applying to Affirm"})
    srcs = [FakeSource("indeed", ([AFFIRM_INDEED], None))]
    r = run_discover(connect(":memory:"), config, rules, srcs, n, NOW, mail)
    assert [f["name"] for f in n.rows["page-1"]["Flags"]["multi_select"]] == ["Applied before"]
    assert r["errors"] == [] and r["alert"] is False


def test_run_discover_mail_failure_is_one_alerting_error_and_rows_still_land(rules, config):
    n, mail = (
        FakeNotion(),
        FakeMail(error=MailError("mail: sign in again with `python -m jobapplier mail-login`")),
    )
    srcs = [FakeSource("freehire", (fixture_rows(), None))]
    r = run_discover(connect(":memory:"), config, rules, srcs, n, NOW, mail)
    assert len(n.rows) == r["new_rows"] > 1 and len(mail.calls) == 1
    assert (
        r["errors"] == ["mail: sign in again with `python -m jobapplier mail-login`"] and r["alert"] is True
    )
    assert "errors: mail: sign in again" in n.description
