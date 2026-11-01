import random
import time

import jobspy
import pandas as pd

from jobapplier.models import Posting


def _s(v) -> str:
    return "" if pd.isna(v) else str(v)


def from_record(site: str, rec: dict) -> Posting:
    return Posting(
        source=site,
        source_id=_s(rec["id"]),
        title=_s(rec["title"]),
        company=_s(rec["company"]),
        location=_s(rec["location"]),
        url=_s(rec["job_url_direct"]) or _s(rec["job_url"]),
        posted=_s(rec["date_posted"]),
        description=_s(rec["description"]),
    )


def fetch(site: str, terms: list[str], results_wanted: int, hours_old: int,
          scrape=jobspy.scrape_jobs, sleep=time.sleep) -> tuple[list[Posting], str | None]:
    """Never raises. A 429 stops the remaining terms; any other failure is reported and the loop continues."""
    extra = {"linkedin_fetch_description": False} if site == "linkedin" else {"country_indeed": "Canada"}
    postings: list[Posting] = []
    error = None
    for i, term in enumerate(terms):
        if i:
            sleep(random.uniform(4, 8))
        try:
            df = scrape(site_name=[site], search_term=term, location="Canada",
                        results_wanted=results_wanted, hours_old=hours_old, **extra)
        except Exception as e:
            if "429" in str(e):
                return postings, f"{site}: 429"
            error = f"{site}: {e}"
            continue
        postings += [from_record(site, r) for r in df.to_dict("records")]
        # JobSpy reports HTTP failures here instead of raising, and still returns the rows it got.
        message = df.attrs.get("errors", {}).get(site)
        if message and "429" in message:
            return postings, f"{site}: 429"
        if message:
            error = f"{site}: {message}"
    return postings, error
