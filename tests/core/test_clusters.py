from datetime import UTC, datetime, timedelta

import pytest

from jobapplier.core.clusters import (
    _cluster_key,
    assign,
    connect,
    mark_pushed,
    members,
    prunable,
    pushable,
    summarize,
)
from jobapplier.core.identity import posting_identity
from jobapplier.core.rules import Rules
from jobapplier.models import Posting

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def posting(rules, source, sid, title, company, location, url, **kw):
    return Posting(source, sid, title, company, location, url, identity=posting_identity(url, rules), **kw)


def test_affirm_four_sources_collapse_to_one_row(rules):
    conn = connect(":memory:")
    t = "Software Engineer II, Full Stack (Card Pre-Swipe Experience)"
    ids = {
        assign(conn, p, rules, NOW, False)
        for p in [
            posting(
                rules,
                "freehire",
                "fh1",
                t,
                "Affirm",
                "Remote Canada",
                "https://job-boards.greenhouse.io/affirm/jobs/8042399003?utm_source=freehire.me",
                description="2+ years of experience",
            ),
            posting(
                rules,
                "indeed",
                "in1",
                t,
                "Affirm",
                "Toronto, ON, CA",
                "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed",
            ),
            posting(
                rules,
                "linkedin",
                "li1",
                t,
                "Affirm",
                "Calgary, Alberta, Canada",
                "https://www.linkedin.com/jobs/view/4485119071",
            ),
            posting(
                rules,
                "linkedin",
                "li2",
                t,
                "Affirm",
                "Winnipeg, Manitoba, Canada",
                "https://www.linkedin.com/jobs/view/4485123048",
            ),
        ]
    }
    assert len(ids) == 1
    s = summarize(members(conn, ids.pop()), rules, False)
    assert s["city"] == "Toronto, ON, CA" and s["ats"] == "Greenhouse" and s["rank"] == 2
    assert s["link"] == "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed"
    assert s["identity"] == "greenhouse:affirm:8042399003" and s["flags"] == []


def test_same_source_row_is_ignored(rules):
    conn = connect(":memory:")
    p = posting(
        rules, "freehire", "fh1", "Software Engineer", "Acme", "Toronto, ON", "https://jobs.lever.co/acme/abc"
    )
    assert assign(conn, p, rules, NOW, False) is not None
    assert assign(conn, p, rules, NOW, False) is None


def test_same_title_different_reqs_share_a_row_within_7_days_but_not_after(rules):
    conn = connect(":memory:")
    t = "Software Engineer"
    a = assign(
        conn,
        posting(
            rules,
            "freehire",
            "1",
            t,
            "Revvity",
            "Toronto, ON",
            "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-1",
        ),
        rules,
        NOW,
        False,
    )
    b = assign(
        conn,
        posting(
            rules,
            "freehire",
            "2",
            t,
            "Revvity",
            "Toronto, ON",
            "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-2",
        ),
        rules,
        NOW + timedelta(days=1),
        False,
    )
    c = assign(
        conn,
        posting(
            rules,
            "freehire",
            "3",
            t,
            "Revvity",
            "Toronto, ON",
            "https://revvity.wd1.myworkdayjobs.com/en-US/Revvity/job/Toronto/Software-Engineer_R-3",
        ),
        rules,
        NOW + timedelta(days=8),
        False,
    )
    assert a == b and c != a


def _repost_setup(rules, include_reposts):
    conn = connect(":memory:")
    url = "https://jobs.lever.co/acme/abc"
    first = assign(
        conn,
        posting(rules, "freehire", "1", "Software Engineer", "Acme", "Toronto, ON", url),
        rules,
        NOW - timedelta(days=3),
        include_reposts,
    )
    second = assign(
        conn,
        posting(rules, "indeed", "2", "Software Engineer", "Acme", "Toronto, ON", url),
        rules,
        NOW,
        include_reposts,
    )
    return conn, first, second


def test_repost_off_joins_old_cluster_silently(rules):
    _, first, second = _repost_setup(rules, False)
    assert first == second


def test_repost_on_makes_new_flagged_cluster(rules):
    conn, first, second = _repost_setup(rules, True)
    assert first != second
    assert summarize(members(conn, second), rules, True)["flags"] == ["Repost"]


def test_prunable_never_calls_a_deliberate_repost_a_duplicate(rules):
    conn, first, second = _repost_setup(rules, True)
    assert first != second
    for cluster_id in (first, second):
        mark_pushed(conn, cluster_id, f"page-{cluster_id}", "https://x", None)
    assert prunable(conn, rules) == []


def test_no_location_cluster_is_not_pushable_until_a_located_row_joins(rules):
    conn = connect(":memory:")
    t = "Software Engineer"
    a = assign(
        conn,
        posting(rules, "freehire", "1", t, "Acme", "", "https://jobs.lever.co/acme/abc", no_location=True),
        rules,
        NOW,
        False,
    )
    assert pushable(conn) == []
    b = assign(
        conn,
        posting(rules, "linkedin", "2", t, "Acme", "Toronto, ON", "https://www.linkedin.com/jobs/view/1"),
        rules,
        NOW,
        False,
    )
    assert a == b
    assert [r["id"] for r in pushable(conn)] == [a]


def test_linkedin_only_row_gets_aggregator_flag(rules):
    conn = connect(":memory:")
    url = "https://www.linkedin.com/jobs/view/4485119071"
    cid = assign(
        conn,
        posting(rules, "linkedin", "1", "Software Engineer", "Acme", "Toronto, ON", url),
        rules,
        NOW,
        False,
    )
    s = summarize(members(conn, cid), rules, False)
    assert s["identity"] is None and s["link"] == url and "Aggregator link" in s["flags"]


def test_mark_pushed_removes_cluster_from_pushable(rules):
    conn = connect(":memory:")
    cid = assign(
        conn,
        posting(
            rules,
            "freehire",
            "1",
            "Software Engineer",
            "Acme",
            "Toronto, ON",
            "https://jobs.lever.co/acme/abc",
        ),
        rules,
        NOW,
        False,
    )
    mark_pushed(conn, cid, "page1", "https://jobs.lever.co/acme/abc", "lever:acme:abc")
    assert pushable(conn) == []


def test_city_falls_back_to_a_located_member(rules):
    conn = connect(":memory:")
    t = "Software Engineer"
    assign(
        conn,
        posting(rules, "freehire", "1", t, "Acme", "", "https://jobs.lever.co/acme/abc", no_location=True),
        rules,
        NOW,
        False,
    )
    cid = assign(
        conn,
        posting(rules, "linkedin", "2", t, "Acme", "Toronto, ON", "https://www.linkedin.com/jobs/view/1"),
        rules,
        NOW,
        False,
    )
    s = summarize(members(conn, cid), rules, False)
    assert s["city"] == "Toronto, ON" and s["alt_locations"] == ""
    assert s["identity"] == "lever:acme:abc" and s["link"] == "https://jobs.lever.co/acme/abc"


def test_clustering_ignores_company_suffixes_and_title_parentheticals(rules):
    conn = connect(":memory:")
    a = assign(
        conn,
        posting(
            rules,
            "linkedin",
            "1",
            "Software Engineer II, Full Stack (Card Pre-Swipe Experience)",
            "Affirm, Inc.",
            "Toronto, ON",
            "https://www.linkedin.com/jobs/view/1",
        ),
        rules,
        NOW,
        False,
    )
    b = assign(
        conn,
        posting(
            rules,
            "linkedin",
            "2",
            "Software Engineer II, Full Stack",
            "Affirm",
            "Toronto, ON",
            "https://www.linkedin.com/jobs/view/2",
        ),
        rules,
        NOW,
        False,
    )
    assert a == b


def test_clustering_ignores_of_and_and_in_company_names(rules):
    conn = connect(":memory:")
    a = posting(
        rules,
        "linkedin",
        "1",
        "Software Developer",
        "House of Commons (Canada)",
        "Ottawa",
        "https://www.linkedin.com/jobs/view/1",
    )
    b = posting(
        rules,
        "indeed",
        "2",
        "Software Developer",
        "House of Commons of Canada",
        "Ottawa",
        "https://ca.indeed.com/viewjob?jk=2",
    )
    assert assign(conn, a, rules, NOW, False) == assign(conn, b, rules, NOW, False)


def test_company_type_is_stored_and_read_back(rules):
    conn = connect(":memory:")
    p = posting(
        rules,
        "freehire",
        "1",
        "Software Developer",
        "Acme",
        "Toronto",
        "https://jobs.lever.co/acme/1",
        company_type="product",
    )
    cid = assign(conn, p, rules, NOW, False)
    assert members(conn, cid)[0].company_type == "product"


@pytest.mark.parametrize(
    "names",
    [
        ("TD", "TD Bank Group", "The Toronto-Dominion Bank (Canada)"),
        ("Amazon", "Amazon Web Services (AWS)", "AWS"),
        (
            "House of Commons (Canada) / Chambre des communes (Canada)",
            "House of Commons of Canada Chambre des communes du Canada",
        ),
        ("KPMG", "KPMG Canada"),
        ("SS&C", "SS&C Technologies"),
        ("Salesforce", "Salesforce.com, Inc.", "110 Salesforce.com"),
        ("National Bank of Canada", "Banque Nationale"),
    ],
)
def test_one_employer_under_several_spellings_has_one_key(names, rules):
    assert len({_cluster_key(n, "Software Developer", rules) for n in names}) == 1


def test_different_employers_keep_different_keys(rules):
    assert _cluster_key("Shopify", "Developer", rules) != _cluster_key("Spotify", "Developer", rules)


def test_user_aliases_and_noise_words_extend_the_rules(config):
    cfg = {**config, "rules": {**config["rules"], "company_aliases": {"Acme Labs": "Acme"}}}
    r = Rules.from_config(cfg)
    assert _cluster_key("ACME Labs Inc.", "Dev", r) == _cluster_key("Acme", "Dev", r)
    cfg = {**config, "rules": {**config["rules"], "company_noise": ["labs"]}}
    r = Rules.from_config(cfg)
    assert _cluster_key("Acme Labs", "Dev", r) == _cluster_key("Acme", "Dev", r)
