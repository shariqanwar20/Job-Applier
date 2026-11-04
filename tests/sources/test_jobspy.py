import pandas as pd

from jobapplier.sources.jobspy import Indeed, LinkedIn

INDEED = {
    "id": "in-d22c5896bdbb2211",
    "site": "indeed",
    "title": "Software Engineer II, Full Stack (Card Pre-Swipe Experience)",
    "company": "Affirm",
    "location": "Toronto, ON, CA",
    "date_posted": "2026-10-30",
    "job_url": "https://ca.indeed.com/viewjob?jk=d22c5896bdbb2211",
    "job_url_direct": "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed",
    "description": "2+ years of experience",
}
LINKEDIN_NAN = {
    "id": "li-4485119071",
    "site": "linkedin",
    "title": "Software Engineer II, Full Stack (Card Pre-Swipe Experience)",
    "company": "Affirm",
    "location": float("nan"),
    "date_posted": float("nan"),
    "job_url": "https://www.linkedin.com/jobs/view/4485119071",
    "job_url_direct": float("nan"),
    "description": None,
}

CFG = {
    "terms": ["a", "b"],
    "location": "Canada",
    "country": "Canada",
    "results_wanted": 100,
    "delay_seconds": [4, 8],
    "retry_after_429_seconds": 120,
}


def no_sleep(seconds):
    pass


def one_row(source, rec):
    """Fetches a single term whose scrape returns rec, and gives back the one posting."""
    df = pd.DataFrame([rec])
    df.attrs = {}
    rows, err = source({**CFG, "terms": ["a"]}, 3, scrape=lambda **kw: df, sleep=no_sleep).fetch()
    assert err is None and len(rows) == 1
    return rows[0]


def test_record_prefers_direct_url():
    p = one_row(Indeed, INDEED)
    assert p.url.startswith("https://boards.greenhouse.io/affirm") and p.posted == "2026-10-30"
    assert (p.source, p.source_id) == ("indeed", "in-d22c5896bdbb2211")


def test_record_nan_becomes_empty():
    p = one_row(LinkedIn, LINKEDIN_NAN)
    assert (p.location, p.posted, p.description) == ("", "", "")
    assert one_row(LinkedIn, {**LINKEDIN_NAN, "date_posted": pd.NaT}).posted == ""
    assert p.url == "https://www.linkedin.com/jobs/view/4485119071"


def rate_limited(rec):
    df = pd.DataFrame([rec])
    df.attrs = {"errors": {"linkedin": "response status code 429 (blocked: use a proxy)"}}
    return df


def clean(rec):
    df = pd.DataFrame([rec])
    df.attrs = {}
    return df


def test_linkedin_429_then_success_retries_the_term_after_waiting():
    slept, terms = [], []
    answers = iter([rate_limited(LINKEDIN_NAN), clean(LINKEDIN_NAN), clean({**LINKEDIN_NAN, "id": "li-2"})])

    def scrape(**kw):
        terms.append(kw["search_term"])
        return next(answers)

    rows, err = LinkedIn(CFG, 3, scrape=scrape, sleep=slept.append).fetch()
    assert err is None and terms == ["a", "a", "b"]
    assert [r.source_id for r in rows] == ["li-4485119071", "li-2"]  # the repeated row is not duplicated
    assert slept[0] == 120 and len(slept) == 2 and 4 <= slept[1] <= 8


def test_linkedin_429_twice_stops_remaining_terms_and_keeps_rows():
    terms, slept = [], []

    def scrape(**kw):
        terms.append(kw["search_term"])
        return rate_limited({**LINKEDIN_NAN, "id": f"li-{len(terms)}"})

    rows, err = LinkedIn(CFG, 3, scrape=scrape, sleep=slept.append).fetch()
    assert terms == ["a", "a"] and slept == [120] and err == "linkedin: 429"
    assert [r.source_id for r in rows] == ["li-1", "li-2"]


def test_linkedin_scrape_arguments():
    calls = []
    LinkedIn(CFG, 3, scrape=lambda **kw: calls.append(kw) or clean(LINKEDIN_NAN), sleep=no_sleep).fetch()
    assert calls[0]["linkedin_fetch_description"] is False and calls[0]["hours_old"] == 72


def test_indeed_passes_country_and_sleeps_between_terms():
    slept, calls = [], []

    def scrape(**kw):
        calls.append(kw)
        df = pd.DataFrame([INDEED])
        df.attrs = {}
        return df

    rows, err = Indeed(CFG, 3, scrape=scrape, sleep=slept.append).fetch()
    assert err is None and len(rows) == 2 and len(slept) == 1 and 4 <= slept[0] <= 8
    assert calls[0]["country_indeed"] == "Canada" and calls[0]["location"] == "Canada"


def test_other_error_is_reported_and_loop_continues():
    def scrape(**kw):
        if kw["search_term"] == "a":
            raise RuntimeError("boom")
        df = pd.DataFrame([INDEED])
        df.attrs = {"errors": {"indeed": "timeout"}}
        return df

    rows, err = Indeed(CFG, 3, scrape=scrape, sleep=no_sleep).fetch()
    assert len(rows) == 1 and err == "indeed: timeout"  # last error wins


def test_raised_429_is_retried_once_then_stops_remaining_terms():
    calls, slept = [], []

    def scrape(**kw):
        calls.append(kw)
        raise RuntimeError("HTTP 429 Too Many Requests")

    rows, err = LinkedIn(CFG, 3, scrape=scrape, sleep=slept.append).fetch()
    assert len(calls) == 2 and slept == [120] and rows == [] and err == "linkedin: 429"


def test_raised_429_then_success_has_no_error():
    answers = iter([RuntimeError("HTTP 429"), clean(INDEED), clean(INDEED)])

    def scrape(**kw):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    slept = []
    rows, err = Indeed(CFG, 3, scrape=scrape, sleep=slept.append).fetch()
    assert err is None and len(rows) == 2
    assert slept[0] == 120
