from datetime import datetime, timedelta, timezone

from jobapplier.core import db
from jobapplier.core.dedupe import assign, pushable, summarize
from jobapplier.core.identity import posting_identity
from jobapplier.models import Posting

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def P(rules, source, sid, title, company, location, url, **kw):
    return Posting(source, sid, title, company, location, url, identity=posting_identity(url, rules), **kw)


def test_affirm_four_sources_collapse_to_one_row(rules):
    conn = db.connect(":memory:")
    t = "Software Engineer II, Full Stack (Card Pre-Swipe Experience)"
    ids = {assign(conn, p, NOW, False) for p in [
        P(rules, "freehire", "fh1", t, "Affirm", "Remote Canada", "https://job-boards.greenhouse.io/affirm/jobs/8042399003?utm_source=freehire.me", description="2+ years of experience"),
        P(rules, "indeed", "in1", t, "Affirm", "Toronto, ON, CA", "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed"),
        P(rules, "linkedin", "li1", t, "Affirm", "Calgary, Alberta, Canada", "https://www.linkedin.com/jobs/view/4485119071"),
        P(rules, "linkedin", "li2", t, "Affirm", "Winnipeg, Manitoba, Canada", "https://www.linkedin.com/jobs/view/4485123048")]}
    assert len(ids) == 1
    s = summarize(db.members(conn, ids.pop()), rules, False)
    assert s["city"] == "Toronto, ON, CA" and s["ats"] == "Greenhouse" and s["rank"] == 2
    assert s["link"] == "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed"
    assert s["identity"] == "greenhouse:affirm:8042399003" and s["flags"] == []


def test_same_source_row_is_ignored(rules):
    conn = db.connect(":memory:")
    p = P(rules, "freehire", "fh1", "Software Engineer", "Acme", "Toronto, ON", "https://jobs.lever.co/acme/abc")
    assert assign(conn, p, NOW, False) is not None
    assert assign(conn, p, NOW, False) is None


def test_same_title_different_reqs_share_a_row_within_7_days_but_not_after(rules):
    conn = db.connect(":memory:")
    t = "Software Engineer"
    a = assign(conn, P(rules, "freehire", "1", t, "Revvity", "Toronto, ON", "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-1"), NOW, False)
    b = assign(conn, P(rules, "freehire", "2", t, "Revvity", "Toronto, ON", "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-2"), NOW + timedelta(days=1), False)
    c = assign(conn, P(rules, "freehire", "3", t, "Revvity", "Toronto, ON", "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-3"), NOW + timedelta(days=8), False)
    assert a == b and c != a


def _repost_setup(rules, include_reposts):
    conn = db.connect(":memory:")
    url = "https://jobs.lever.co/acme/abc"
    first = assign(conn, P(rules, "freehire", "1", "Software Engineer", "Acme", "Toronto, ON", url), NOW - timedelta(days=3), include_reposts)
    second = assign(conn, P(rules, "indeed", "2", "Software Engineer", "Acme", "Toronto, ON", url), NOW, include_reposts)
    return conn, first, second


def test_repost_off_joins_old_cluster_silently(rules):
    _, first, second = _repost_setup(rules, False)
    assert first == second


def test_repost_on_makes_new_flagged_cluster(rules):
    conn, first, second = _repost_setup(rules, True)
    assert first != second
    assert summarize(db.members(conn, second), rules, True)["flags"] == ["Repost"]


def test_no_location_cluster_is_not_pushable_until_a_located_row_joins(rules):
    conn = db.connect(":memory:")
    t = "Software Engineer"
    a = assign(conn, P(rules, "freehire", "1", t, "Acme", "", "https://jobs.lever.co/acme/abc", no_location=True), NOW, False)
    assert pushable(conn) == []
    b = assign(conn, P(rules, "linkedin", "2", t, "Acme", "Toronto, ON", "https://www.linkedin.com/jobs/view/1"), NOW, False)
    assert a == b
    assert [r["id"] for r in pushable(conn)] == [a]


def test_linkedin_only_row_gets_aggregator_flag(rules):
    conn = db.connect(":memory:")
    url = "https://www.linkedin.com/jobs/view/4485119071"
    cid = assign(conn, P(rules, "linkedin", "1", "Software Engineer", "Acme", "Toronto, ON", url), NOW, False)
    s = summarize(db.members(conn, cid), rules, False)
    assert s["identity"] is None and s["link"] == url and "Aggregator link" in s["flags"]


def test_mark_pushed_removes_cluster_from_pushable(rules):
    conn = db.connect(":memory:")
    cid = assign(conn, P(rules, "freehire", "1", "Software Engineer", "Acme", "Toronto, ON", "https://jobs.lever.co/acme/abc"), NOW, False)
    db.mark_pushed(conn, cid, "page1", "https://jobs.lever.co/acme/abc", "lever:acme:abc")
    assert pushable(conn) == []


def test_city_falls_back_to_a_located_member(rules):
    conn = db.connect(":memory:")
    t = "Software Engineer"
    assign(conn, P(rules, "freehire", "1", t, "Acme", "", "https://jobs.lever.co/acme/abc", no_location=True), NOW, False)
    cid = assign(conn, P(rules, "linkedin", "2", t, "Acme", "Toronto, ON", "https://www.linkedin.com/jobs/view/1"), NOW, False)
    s = summarize(db.members(conn, cid), rules, False)
    assert s["city"] == "Toronto, ON" and s["alt_locations"] == ""
    assert s["identity"] == "lever:acme:abc" and s["link"] == "https://jobs.lever.co/acme/abc"
