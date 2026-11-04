import random
import time

import jobspy
import pandas as pd

from jobapplier.models import Posting
from jobapplier.sources.base import Source

__all__ = ["Indeed", "LinkedIn"]


def _s(v) -> str:
    return "" if pd.isna(v) else str(v)


def _from_record(site: str, rec: dict) -> Posting:
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


class _JobSpySource(Source):
    def __init__(self, cfg: dict, lookback_days: int, scrape=None, sleep=time.sleep):
        self.cfg, self.hours_old = cfg, lookback_days * 24
        self.scrape, self.sleep = scrape or jobspy.scrape_jobs, sleep

    def extra(self) -> dict:
        """Site-specific scrape_jobs arguments."""
        return {}

    def _scrape_term(self, term: str) -> tuple[dict[str, Posting], str | None]:
        """One scrape: the postings by id, and the error message if it failed. Never raises."""
        try:
            df = self.scrape(
                site_name=[self.name],
                search_term=term,
                location=self.cfg["location"],
                results_wanted=self.cfg["results_wanted"],
                hours_old=self.hours_old,
                **self.extra(),
            )
        except Exception as e:
            return {}, str(e)
        rows = (_from_record(self.name, r) for r in df.to_dict("records"))
        # JobSpy reports HTTP failures here instead of raising, and still returns the rows it got.
        return {p.source_id: p for p in rows}, df.attrs.get("errors", {}).get(self.name)

    def fetch(self) -> tuple[list[Posting], str | None]:
        """Never raises. A 429 waits and retries the term once; a second 429 stops the remaining terms.

        Other failures are reported and the loop goes on.
        """
        site = self.name
        postings: list[Posting] = []
        error = None
        for i, term in enumerate(self.cfg["terms"]):
            if i:
                self.sleep(random.uniform(*self.cfg["delay_seconds"]))
            rows, message = self._scrape_term(term)
            if message and "429" in message:
                self.sleep(self.cfg["retry_after_429_seconds"])
                retried, message = self._scrape_term(term)
                rows |= retried  # a retry may repeat rows the first attempt got
                if message and "429" in message:
                    return [*postings, *rows.values()], f"{site}: 429"
            postings += rows.values()
            if message:
                error = f"{site}: {message}"
        return postings, error


class LinkedIn(_JobSpySource):
    name = "linkedin"

    def extra(self) -> dict:
        return {"linkedin_fetch_description": False}  # list only: descriptions cost one request per job


class Indeed(_JobSpySource):
    name = "indeed"

    def extra(self) -> dict:
        return {"country_indeed": self.cfg["country"]}
