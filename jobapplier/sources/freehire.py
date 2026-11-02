import requests

from jobapplier.models import Posting
from jobapplier.sources.base import Source

URL = "https://freehire.me/api/v1/agent/jobs/search"
PAGE = 100
MAX_OFFSET = 10000  # API pagination cap


def parse(row: dict) -> Posting:
    enrichment = row["enrichment"] or {}
    return Posting(
        source="freehire",
        source_id=row["public_slug"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"],
        posted=row["created_at"][:10],
        description=row["description"] or "",
        seniority=enrichment.get("seniority") or "",
        no_location=not row["countries"],
    )


class FreeHire(Source):
    name = "freehire"

    def __init__(self, cfg: dict, lookback_days: int, session=None):
        self.session = session or requests.Session()
        self.params = [("countries", c) for c in cfg["countries"]] + [("regions", r) for r in cfg["regions"]]
        self.params += [("category", c) for c in cfg["categories"]]
        self.params += [("open_within_days", lookback_days), ("description_format", "text"), ("limit", PAGE)]

    def fetch(self) -> tuple[list[Posting], str | None]:
        postings: list[Posting] = []
        offset = 0
        while True:
            try:
                resp = self.session.get(URL, params=[*self.params, ("offset", offset)], timeout=30)
                if resp.status_code != 200:
                    return postings, f"freehire: HTTP {resp.status_code}"
                body = resp.json()
                meta = body["meta"]
                # Silently ignored filters would return unfiltered rows, so drop the page.
                ignored = meta.get("ignored_params") or meta.get("ignored_values")
                if ignored:
                    return postings, f"freehire: ignored params {ignored}"
                page = [parse(r) for r in body["data"]]
                total = meta["total"]
            except requests.RequestException as e:
                return postings, f"freehire: {e}"
            except (KeyError, TypeError, ValueError):
                return postings, "freehire: malformed response"
            postings += page
            offset += PAGE
            # The API rejects offset + limit > MAX_OFFSET, so the last request is offset 9900.
            if len(page) < PAGE or offset >= total or offset >= MAX_OFFSET:
                return postings, None
