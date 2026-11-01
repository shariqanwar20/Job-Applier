import requests

from jobapplier.models import Posting

URL = "https://freehire.me/api/v1/agent/jobs/search"
CATEGORIES = ("software_engineering", "fullstack", "backend", "frontend", "ai_engineering", "mobile")
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
        language=enrichment.get("posting_language") or "",
        seniority=enrichment.get("seniority") or "",
        no_location=not row["countries"],
    )


def fetch(session, lookback_days: int) -> tuple[list[Posting], str | None]:
    """Never raises: returns the postings gathered so far plus an error string."""
    postings: list[Posting] = []
    offset = 0
    while True:
        params = [("countries", "ca"), ("regions", "none")]
        params += [("category", c) for c in CATEGORIES]
        params += [("open_within_days", lookback_days), ("description_format", "text"), ("limit", PAGE), ("offset", offset)]
        try:
            resp = session.get(URL, params=params, timeout=30)
            if resp.status_code != 200:
                return postings, f"freehire: HTTP {resp.status_code}"
            body = resp.json()
        except requests.RequestException as e:
            return postings, f"freehire: {e}"
        meta = body["meta"]
        # Silently ignored filters would return unfiltered rows, so drop the page.
        ignored = meta.get("ignored_params") or meta.get("ignored_values")
        if ignored:
            return postings, f"freehire: ignored params {ignored}"
        postings += [parse(r) for r in body["data"]]
        offset += PAGE
        if len(body["data"]) < PAGE or offset >= meta["total"] or offset > MAX_OFFSET:
            return postings, None
