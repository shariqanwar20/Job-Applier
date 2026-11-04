from jobapplier.http import ApiClient, ApiError
from jobapplier.models import Posting
from jobapplier.sources.base import Source

__all__ = ["FreeHire"]

_BASE_URL = "https://freehire.me/api/v1"
_PAGE = 100
_MAX_OFFSET = 10000  # API pagination cap


def _parse(row: dict) -> Posting:
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
        company_type=enrichment.get("company_type") or "",
    )


class FreeHire(Source):
    name = "freehire"

    def __init__(self, cfg: dict, lookback_days: int, api: ApiClient | None = None):
        self.api = api or ApiClient(_BASE_URL, timeout=30)
        self.params = [("countries", c) for c in cfg["countries"]] + [("regions", r) for r in cfg["regions"]]
        self.params += [("category", c) for c in cfg["categories"]]
        self.params.append(("via_recruiter", str(cfg["via_recruiter"]).lower()))
        self.params += [("open_within_days", lookback_days), ("description_format", "text"), ("limit", _PAGE)]

    def fetch(self) -> tuple[list[Posting], str | None]:
        postings: list[Posting] = []
        offset = 0
        while True:
            try:
                body = self.api.get_json("/agent/jobs/search", [*self.params, ("offset", offset)])
                meta = body["meta"]
                # Silently ignored filters would return unfiltered rows, so drop the page.
                ignored = meta.get("ignored_params") or meta.get("ignored_values")
                if ignored:
                    return postings, f"freehire: ignored params {ignored}"
                page = [_parse(r) for r in body["data"]]
                total = meta["total"]
            except ApiError as e:
                return postings, f"freehire: {e}"
            except (KeyError, TypeError, ValueError):
                return postings, "freehire: malformed response"
            postings += page
            offset += _PAGE
            # The API rejects offset + limit > _MAX_OFFSET, so the last request is offset 9900.
            if len(page) < _PAGE or offset >= total or offset >= _MAX_OFFSET:
                return postings, None
