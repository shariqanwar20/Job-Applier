import json, pathlib
from jobapplier.sources import freehire

PAGE = json.loads((pathlib.Path(__file__).parent / "fixtures/freehire_page.json").read_text())


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.responses.pop(0)


def test_parse_greenhouse_row():
    row = next(r for r in PAGE["data"] if r["source"] == "greenhouse")
    p = freehire.parse(row)
    assert (p.source, p.company, p.posted, p.language, p.no_location) == ("freehire", "Affirm", row["created_at"][:10], "en", False)
    assert p.url.startswith("https://job-boards.greenhouse.io/affirm/jobs/8042399003")


def test_parse_no_location_row():
    assert freehire.parse(PAGE["data"][-1]).no_location is True


def test_parse_null_enrichment():
    p = freehire.parse({**PAGE["data"][0], "enrichment": None, "description": None})
    assert (p.language, p.seniority, p.description) == ("", "", "")


def test_fetch_sends_one_query_and_parses_all_rows():
    s = FakeSession([FakeResponse(200, PAGE)])
    rows, err = freehire.fetch(s, 3)
    assert err is None and len(rows) == 14
    params = s.calls[0]["params"]
    assert ("regions", "none") in params and ("countries", "ca") in params
    assert {v for k, v in params if k == "category"} == {"software_engineering", "fullstack", "backend", "frontend", "ai_engineering", "mobile"}
    assert ("open_within_days", 3) in params and ("description_format", "text") in params
    assert s.calls[0]["timeout"] == 30


def test_fetch_pages_until_total_reached():
    page = lambda total: {"data": PAGE["data"][:1] * 100, "meta": {"total": total}}
    s = FakeSession([FakeResponse(200, page(150)), FakeResponse(200, {"data": PAGE["data"][:1] * 50, "meta": {"total": 150}})])
    rows, err = freehire.fetch(s, 3)
    assert err is None and len(rows) == 150
    assert [dict(c["params"])["offset"] for c in s.calls] == [0, 100]


def test_fetch_reports_ignored_params():
    body = {"data": PAGE["data"], "meta": {**PAGE["meta"], "ignored_params": ["open_within_days"]}}
    rows, err = freehire.fetch(FakeSession([FakeResponse(200, body)]), 3)
    assert rows == [] and "ignored" in err


def test_fetch_reports_429():
    rows, err = freehire.fetch(FakeSession([FakeResponse(429, {})]), 3)
    assert rows == [] and err == "freehire: HTTP 429"


def test_fetch_keeps_earlier_pages_on_later_error():
    first = {"data": PAGE["data"][:1] * 100, "meta": {"total": 300}}
    rows, err = freehire.fetch(FakeSession([FakeResponse(200, first), FakeResponse(500, {})]), 3)
    assert len(rows) == 100 and err == "freehire: HTTP 500"


def test_fetch_stops_at_offset_cap_without_error():
    full = {"data": PAGE["data"][:1] * 100, "meta": {"total": 20000}}
    s = FakeSession([FakeResponse(200, full) for _ in range(101)])
    rows, err = freehire.fetch(s, 3)
    assert err is None and len(rows) == 10000
    assert dict(s.calls[-1]["params"])["offset"] == 9900 and len(s.calls) == 100


def test_fetch_malformed_response_keeps_earlier_pages():
    first = {"data": PAGE["data"][:1] * 100, "meta": {"total": 300}}
    rows, err = freehire.fetch(FakeSession([FakeResponse(200, first), FakeResponse(200, {"data": [{}], "meta": {"total": 300}})]), 3)
    assert len(rows) == 100 and err == "freehire: malformed response"
