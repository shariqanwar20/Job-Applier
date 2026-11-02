import json
import pathlib

from jobapplier.http import ApiError
from jobapplier.sources import freehire
from jobapplier.sources.freehire import FreeHire

PAGE = json.loads((pathlib.Path(__file__).parents[1] / "fixtures/freehire_page.json").read_text())


class FakeApi:
    """Stands in for ApiClient: serves queued bodies, raises queued ApiErrors, records each call."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get_json(self, path, params=None):
        self.calls.append({"path": path, "params": params})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fetch(api, config):
    return FreeHire(config["sources"]["freehire"], config["lookback_days"], api=api).fetch()


def test_parse_greenhouse_row():
    row = next(r for r in PAGE["data"] if r["source"] == "greenhouse")
    p = freehire.parse(row)
    assert (p.source, p.company, p.posted, p.no_location) == ("freehire", "Affirm", row["created_at"][:10], False)
    assert p.url.startswith("https://job-boards.greenhouse.io/affirm/jobs/8042399003")


def test_parse_no_location_row():
    assert freehire.parse(PAGE["data"][-1]).no_location is True


def test_parse_null_enrichment():
    p = freehire.parse({**PAGE["data"][0], "enrichment": None, "description": None})
    assert (p.seniority, p.description) == ("", "")


def test_fetch_sends_one_query_and_parses_all_rows(config):
    s = FakeApi([PAGE])
    rows, err = fetch(s, config)
    assert err is None and len(rows) == 14
    params = s.calls[0]["params"]
    assert ("regions", "none") in params and ("countries", "ca") in params
    assert {v for k, v in params if k == "category"} == {"software_engineering", "fullstack", "backend", "frontend", "ai_engineering", "mobile"}
    assert ("open_within_days", 3) in params and ("description_format", "text") in params
    assert s.calls[0]["path"] == "/agent/jobs/search"


def test_fetch_pages_until_total_reached(config):
    page = lambda total: {"data": PAGE["data"][:1] * 100, "meta": {"total": total}}
    s = FakeApi([page(150), {"data": PAGE["data"][:1] * 50, "meta": {"total": 150}}])
    rows, err = fetch(s, config)
    assert err is None and len(rows) == 150
    assert [dict(c["params"])["offset"] for c in s.calls] == [0, 100]


def test_fetch_reports_ignored_params(config):
    body = {"data": PAGE["data"], "meta": {**PAGE["meta"], "ignored_params": ["open_within_days"]}}
    rows, err = fetch(FakeApi([body]), config)
    assert rows == [] and "ignored" in err


def test_fetch_reports_429(config):
    rows, err = fetch(FakeApi([ApiError("HTTP 429")]), config)
    assert rows == [] and err == "freehire: HTTP 429"


def test_fetch_keeps_earlier_pages_on_later_error(config):
    first = {"data": PAGE["data"][:1] * 100, "meta": {"total": 300}}
    rows, err = fetch(FakeApi([first, ApiError("HTTP 500")]), config)
    assert len(rows) == 100 and err == "freehire: HTTP 500"


def test_fetch_stops_at_offset_cap_without_error(config):
    full = {"data": PAGE["data"][:1] * 100, "meta": {"total": 20000}}
    s = FakeApi([full for _ in range(101)])
    rows, err = fetch(s, config)
    assert err is None and len(rows) == 10000
    assert dict(s.calls[-1]["params"])["offset"] == 9900 and len(s.calls) == 100


def test_fetch_malformed_response_keeps_earlier_pages(config):
    first = {"data": PAGE["data"][:1] * 100, "meta": {"total": 300}}
    rows, err = fetch(FakeApi([first, {"data": [{}], "meta": {"total": 300}}]), config)
    assert len(rows) == 100 and err == "freehire: malformed response"


def test_fetch_reports_a_network_error(config):
    rows, err = fetch(FakeApi([ApiError("network: ConnectionError")]), config)
    assert rows == [] and err == "freehire: network: ConnectionError"


def test_default_api_targets_freehire_with_a_30s_timeout(config):
    api = FreeHire(config["sources"]["freehire"], config["lookback_days"]).api
    assert api.base_url == "https://freehire.me/api/v1" and api.timeout == 30
