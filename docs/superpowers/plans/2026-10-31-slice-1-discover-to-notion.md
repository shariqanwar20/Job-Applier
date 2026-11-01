# Slice 1: Discover → Notion Inbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Also follow the ponytail rules (smallest complete change, stdlib first, no unrequested abstractions).

**Goal:** Every 2 hours, pull fresh Canadian software postings from FreeHire, Indeed and LinkedIn, drop duplicates, rank them, and add each new job as one row in a Notion database. Nothing is applied to.

**Architecture:** One Python package run by one LaunchAgent. Three sources each return a list of `Posting`. A pure core decides filtering, identity, clustering and rank. SQLite (`data/jobs.db`) is the record. Notion gets create-only pushes, keyed on a `Job ID` property.

**Tech Stack:** Python 3.12 via `uv`, stdlib `sqlite3`/`dataclasses`/`fcntl`/`subprocess`, `python-jobspy==1.4.1`, `requests`, `pyyaml`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-31-job-applier-design.md`. **§18 overrides earlier sections**; its last paragraph lists slice 1 scope.

## Global Constraints

- Python 3.12 (`uv`). Runtime deps only: `python-jobspy==1.4.1`, `requests`, `pyyaml`. Dev: `pytest`. No ORM, no async, no framework, no Notion SDK.
- Tests never touch the network. Network sits behind objects passed in (a `requests.Session`-like object, a `scrape` function, a `Notion` client) so tests pass fakes.
- Notion: `Notion-Version: 2025-09-03`, data-source endpoints, ≥0.35 s between requests, retry 429/529 after `Retry-After` (max 5). Never delete or trash pages.
- LinkedIn: list only (`linkedin_fetch_description=False`), no login, 4–8 s jittered sleep between search terms, stop all LinkedIn calls for the run on the first 429. LinkedIn or Indeed failure never fails the run.
- Secrets only in macOS Keychain (service `jobapplier-notion`). Never log the token.
- `lookback_days: 3` for every source (FreeHire `open_within_days=3`, JobSpy `hours_old=72`).
- Notion Job ID = `"J" + str(cluster_id)`.
- Comment only the why. A known limit gets `# shortcut: <limit>, <when to upgrade>`.

## Review Focus

1. JobSpy rows with pandas `NaN` in company, location, description or date must become `""`, never the string `"nan"`. Test in Task 5.
2. City text variants must rank correctly: `"Montréal, Canada"` → 2, `"Mississauga Ontario Canada"` → 0, `"CAD Remote – ON"` → 3, `"Canada, BC, Vancouver"` → 1. Test in Task 3.
3. A Notion error after some pages were created: the next run must create only the missing pages (query by Job ID first). Test in Task 7.
4. FreeHire answers 429, or reports `meta.ignored_params`: the run records the error, keeps the other sources, and notifies. Tests in Tasks 4 and 8.
5. A second run starting while the first holds the lock exits 0 and does nothing. Test in Task 8.

## File Structure

```
pyproject.toml                    uv project, deps, pytest config
config.yaml                       owner-editable settings (no secrets)
jobapplier/__init__.py            empty
jobapplier/models.py              Posting dataclass
jobapplier/filters.py             text normalization, title and intern rules, cluster key
jobapplier/identity.py            posting identity from a URL
jobapplier/rank.py                city preference, remote-US, years, rank tier
jobapplier/sources/__init__.py    empty
jobapplier/sources/freehire.py    FreeHire /agent/jobs/search
jobapplier/sources/jobspy_sources.py  LinkedIn + Indeed via JobSpy
jobapplier/db.py                  SQLite schema + small query helpers
jobapplier/dedupe.py              cluster assignment + row summary
jobapplier/notion.py              HTTP client, database setup, push, status
jobapplier/discover.py            one run end to end; main() with lock/keychain/notify
launchd/local.jobapplier.plist    LaunchAgent (every 2 h + RunAtLoad)
README.md                         setup steps for the owner
tests/fixtures/freehire_page.json (committed) 14 trimmed FreeHire rows from the spike
tests/test_*.py                   one test file per module
```

---

### Task 1: Project scaffold, `Posting`, and title filters

**Files:**
- Create: `pyproject.toml`, `config.yaml`, `jobapplier/__init__.py`, `jobapplier/models.py`, `jobapplier/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Produces:
  - `models.Posting` dataclass: `source: str` (`"freehire"|"linkedin"|"indeed"`), `source_id: str`, `title: str`, `company: str`, `location: str`, `url: str`, `posted: str = ""` (`YYYY-MM-DD`), `description: str = ""`, `language: str = ""`, `seniority: str = ""`, `no_location: bool = False`, `identity: str | None = None`.
  - `filters.norm(text: str) -> str`: strip accents (NFKD), lowercase, every non-alphanumeric run → one space, trimmed.
  - `filters.is_target_title(title: str) -> bool`
  - `filters.is_intern(title: str) -> bool`
  - `filters.is_notable(company: str, notable: list[str]) -> bool`: whole-word match of `norm(name)` inside `norm(company)`.
  - `filters.keep(p: Posting, notable: list[str]) -> bool`: target title and (not intern or notable company).
  - `filters.cluster_key(company: str, title: str) -> str`: `norm(company)` without the words `inc ltd llc corp corporation company co group plc limited sa the`, then `"|"`, then `norm(title)` after removing `(...)` parts.

- [ ] **Step 1: Scaffold.** `pyproject.toml`: `[project] name="jobapplier" requires-python=">=3.12"`, the three runtime deps, `[dependency-groups] dev=["pytest"]`, `[tool.pytest.ini_options] pythonpath=["."]`, no build-system. `config.yaml`:

```yaml
lookback_days: 3
include_reposts: false
notion:
  database_id: ""        # printed by `python -m jobapplier.notion setup`
  data_source_id: ""
linkedin: {terms: ["software engineer", "full stack engineer"], results_wanted: 100}
indeed: {terms: ["software engineer", "full stack engineer"], results_wanted: 100}
notable_companies: [google, amazon, microsoft, apple, meta, netflix, nvidia, shopify, stripe, uber, airbnb, databricks, snowflake, salesforce, adobe, intel, amd, ibm, oracle, cisco, atlassian, cohere, wealthsimple, rbc, td, scotiabank, bmo, cibc, disney, bloomberg, jane street, citadel, two sigma, palantir, openai, anthropic]
```

Run `uv sync`. Expected: `.venv` created, exit 0.

- [ ] **Step 2: Write the failing tests** in `tests/test_filters.py`:

```python
import pytest
from jobapplier.filters import norm, is_target_title, is_intern, is_notable, keep, cluster_key
from jobapplier.models import Posting

def test_norm():
    assert norm("Développeur·euse  Full-Stack!") == "developpeur euse full stack"

@pytest.mark.parametrize("title,ok", [
    ("Software Engineer II, Full Stack (Card Pre-Swipe Experience)", True),
    ("Associate Software Engineer", True),
    ("Software Development Engineer in Test, Evisor", True),
    ("Développeur·euse généraliste intermédiaire (Développement)", True),
    ("Fullstack Developer - Node.js", True),
    ("Sr. Software Engineer", False),
    ("Engineering Manager, Conversational AI", False),
    ("Développeur·euse généraliste principal·e", False),
    ("Staff Fullstack Developer- Simulation", False),
    ("Lead Full Stack Developer", False),
    ("Salesforce Administrator", False),
])
def test_is_target_title(title, ok):
    assert is_target_title(title) is ok

def test_intern_kept_only_at_notable_company():
    notable = ["disney", "google"]
    intern = Posting("freehire", "1", "Software Engineer Intern, Pipeline Infrastructure", "Disney Company", "Vancouver, Canada", "u")
    other = Posting("freehire", "2", "Full-stack AI Developer Intern", "Revvity", "CAD Remote – ON", "u")
    coop = Posting("freehire", "3", "Software Developer Co-op", "Google", "Toronto", "u")
    assert is_intern(intern.title) and is_intern(coop.title)
    assert keep(intern, notable) and keep(coop, notable)
    assert not keep(other, notable)
    assert not is_notable("Tdx Labs", ["td"])

def test_cluster_key_ignores_suffixes_and_parentheticals():
    assert cluster_key("Affirm, Inc.", "Software Engineer II, Full Stack (Card Pre-Swipe Experience)") == \
           cluster_key("Affirm", "Software Engineer II, Full Stack")
```

- [ ] **Step 3: Run** `uv run pytest tests/test_filters.py -v`. Expected: FAIL (`ModuleNotFoundError: jobapplier.filters`).
- [ ] **Step 4: Implement** `models.py` and `filters.py`. Regexes run on `norm()` output. Include: `\b(developer|engineer|programmer|software|full ?stack|back ?end|front ?end|sde|mobile|developpeur|developpeuse|ingenieure? logiciel)\b`. Exclude: `\b(senior|sr|staff|principal|lead|manager|director|architect|head of)\b`. Intern: `\b(intern|internship|co ?op|stagiaire|stage)\b`.
- [ ] **Step 5: Run** `uv run pytest tests/test_filters.py -v`. Expected: PASS.
- [ ] **Step 6: Commit** `git add pyproject.toml uv.lock config.yaml jobapplier tests/test_filters.py && git commit -m "feat: scaffold, Posting, title filters"`

---

### Task 2: Posting identity

**Files:** Create `jobapplier/identity.py` · Test `tests/test_identity.py`

**Interfaces:**
- Produces: `identity.posting_identity(url: str) -> str | None`.
  - Greenhouse (`boards.greenhouse.io`, `job-boards.greenhouse.io`, and their `.eu.` hosts), path `/{board}/jobs/{id}` or `/embed/job_app?for={board}&token={id}` → `greenhouse:{board}:{id}`.
  - Lever `jobs.lever.co/{co}/{uuid}[/apply]` → `lever:{co}:{uuid}`.
  - Ashby `jobs.ashbyhq.com/{org}/{uuid}[/application]` → `ashby:{org}:{uuid}`.
  - Workday host `{tenant}.wd{n}.myworkdayjobs.com` → `workday:{tenant}:{last path segment}`, ignoring a trailing `/apply`. The locale and location path parts don't matter.
  - Aggregator hosts (`whatjobs.com`, `adzuna.`, `linkedin.com`, `indeed.com`, `torre.ai`) or an empty URL → `None`.
  - Anything else → `url:{host}{path}` with no query, no fragment and no trailing slash.
  - Everything is lowercased.

- [ ] **Step 1: Write the failing tests:**

```python
import pytest
from jobapplier.identity import posting_identity

@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/affirm/jobs/8042399003?utm_source=freehire.me", "greenhouse:affirm:8042399003"),
    ("https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed", "greenhouse:affirm:8042399003"),
    ("https://boards.greenhouse.io/embed/job_app?for=affirm&token=8042399003", "greenhouse:affirm:8042399003"),
    ("https://jobs.lever.co/acme/1b2c3d4e-0000-1111-2222-333344445555/apply", "lever:acme:1b2c3d4e-0000-1111-2222-333344445555"),
    ("https://jobs.ashbyhq.com/pebl/03cd41b2-5acd-4f6c-be73-5c6126170af0?utm_source=freehire.me", "ashby:pebl:03cd41b2-5acd-4f6c-be73-5c6126170af0"),
    ("https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Toronto-Ontario/Software-Engineer-II_R_1516370?utm_source=freehire.me", "workday:td:software-engineer-ii_r_1516370"),
    ("https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Mississauga/Software-Engineer-II_R_1516370/apply", "workday:td:software-engineer-ii_r_1516370"),
    ("https://jobs.apple.com/en-us/details/200692697/software-engineer-xcode-cloud/?utm_source=freehire.me", "url:jobs.apple.com/en-us/details/200692697/software-engineer-xcode-cloud"),
    ("https://en-ca.whatjobs.com/pub_api__cpl__156344138__7103?geoID=6399", None),
    ("https://www.linkedin.com/jobs/view/4485119071", None),
    ("", None),
])
def test_posting_identity(url, expected):
    assert posting_identity(url) == expected
```

- [ ] **Step 2: Run** `uv run pytest tests/test_identity.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** with `urllib.parse.urlsplit`/`parse_qs` and one regex per ATS.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: posting identity from URLs"` (add the new files first).

---

### Task 3: City preference and rank

**Files:** Create `jobapplier/rank.py` · Test `tests/test_rank.py`

**Interfaces:**
- Consumes: `filters.norm`.
- Produces:
  - `rank.city_rank(location: str) -> int`:
    - 0: GTA (`toronto mississauga markham brampton vaughan oakville richmond hill north york etobicoke scarborough`).
    - 1: `vancouver burnaby`.
    - 2: `montreal`.
    - 3: remote Canada (the text has `remote` and doesn't count as remote US).
    - 4: remote US.
    - 5: anything else, including empty.
  - `rank.is_remote_us(location: str) -> bool`: `remote`, plus `united states`, `usa` or the word `us`, and no `canada`.
  - `rank.min_years(description: str) -> int | None`: the smallest year count that has `experience` within the next 8 words (English or French, on `norm()` text). Ranges count by their lower end.
  - `rank.rank(title: str, description: str, seniority: str, notable_intern: bool) -> int`:
    - Return 1 if `notable_intern`, or if the normalized title matches `new grad|graduate|entry level|junior|jr|associate`, or `(engineer|developer|sde) (i|1)` at the end of the title.
    - Otherwise, if `min_years` is not `None`: return 2 when it is under 3, else 3.
    - Otherwise return 1 if `seniority` is `junior` or `intern`.
    - Otherwise return 3.

- [ ] **Step 1: Write the failing tests:**

```python
import pytest
from jobapplier.rank import city_rank, is_remote_us, min_years, rank

@pytest.mark.parametrize("loc,r", [
    ("Toronto, ON", 0), ("Mississauga Ontario Canada", 0), ("Canada, BC, Vancouver", 1),
    ("Montréal, Canada", 2), ("CAD Remote – ON", 3), ("Remote Canada", 3),
    ("Remote, United States", 4), ("Ottawa, ON, ca", 5), ("", 5),
])
def test_city_rank(loc, r):
    assert city_rank(loc) == r

def test_is_remote_us():
    assert is_remote_us("Remote - USA") and not is_remote_us("Remote Canada") and not is_remote_us("Toronto")

@pytest.mark.parametrize("text,y", [
    ("You have 3+ years of experience with Python.", 3),
    ("2-4 years of professional experience", 2),
    ("Founded 20 years ago. 1 year of experience required.", 1),
    ("Au moins 3 ans d'expérience en développement", 3),
    ("Great culture.", None),
])
def test_min_years(text, y):
    assert min_years(text) == y

def test_rank():
    assert rank("Associate Software Engineer", "", "", False) == 1
    assert rank("Software Engineer I", "5 years of experience", "", False) == 1
    assert rank("Software Engineer II", "2+ years of experience", "middle", False) == 2
    assert rank("Software Engineer II", "5+ years of experience", "junior", False) == 3
    assert rank("Software Developer", "", "junior", False) == 1
    assert rank("Software Developer", "", "", False) == 3
    assert rank("Software Engineer Intern", "", "intern", True) == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_rank.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `rank.py`.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: city preference and rank tiers"` (add the files first).

---

### Task 4: FreeHire source

**Files:** Create `jobapplier/sources/__init__.py`, `jobapplier/sources/freehire.py` · Test `tests/test_freehire.py` (uses `tests/fixtures/freehire_page.json`)

**Interfaces:**
- Produces:
  - `freehire.parse(row: dict) -> Posting`:
    - `source="freehire"`, `source_id=row["public_slug"]`, and `url`, `title`, `company` and `location` copied from the row.
    - `posted=row["created_at"][:10]`.
    - `description=row["description"] or ""`.
    - `language` and `seniority` come from `row["enrichment"]` (`posting_language`, `seniority`); `None` becomes `""`, and so does a null `enrichment`.
    - `no_location = not row["countries"]`.
  - `freehire.fetch(session, lookback_days: int) -> tuple[list[Posting], str | None]`:
    - `GET https://freehire.me/api/v1/agent/jobs/search` with `timeout=30` and these params, as a list of tuples: `countries=ca`, `regions=none`, `category` repeated for `software_engineering fullstack backend frontend ai_engineering mobile`, `open_within_days=lookback_days`, `description_format=text`, `limit=100`, `offset`.
    - Pages until a short page, `offset + 100 >= meta.total`, or `offset + 100 > 10000`.
    - Returns `(rows_so_far, error)` instead of raising. Errors: non-200 (`"freehire: HTTP 429"`), a `requests` exception, or a non-empty `meta.ignored_params` or `meta.ignored_values` (`"freehire: ignored params ..."`; rows from that response are discarded).

- [ ] **Step 1: Write the failing tests.** A `FakeSession` records each `get(url, params=, timeout=)` and returns queued `FakeResponse(status_code, json_body)` objects.

```python
import json, pathlib
from jobapplier.sources import freehire

PAGE = json.loads((pathlib.Path(__file__).parent / "fixtures/freehire_page.json").read_text())

def test_parse_greenhouse_row():
    row = next(r for r in PAGE["data"] if r["source"] == "greenhouse")
    p = freehire.parse(row)
    assert (p.source, p.company, p.posted, p.language, p.no_location) == ("freehire", "Affirm", row["created_at"][:10], "en", False)
    assert p.url.startswith("https://job-boards.greenhouse.io/affirm/jobs/8042399003")

def test_parse_no_location_row():
    assert freehire.parse(PAGE["data"][-1]).no_location is True

def test_fetch_sends_one_query_and_parses_all_rows():
    s = FakeSession([FakeResponse(200, PAGE)])
    rows, err = freehire.fetch(s, 3)
    assert err is None and len(rows) == 14
    params = s.calls[0]["params"]
    assert ("regions", "none") in params and ("countries", "ca") in params
    assert {v for k, v in params if k == "category"} == {"software_engineering", "fullstack", "backend", "frontend", "ai_engineering", "mobile"}
    assert ("open_within_days", 3) in params and ("description_format", "text") in params

def test_fetch_reports_ignored_params():
    body = {"data": PAGE["data"], "meta": {**PAGE["meta"], "ignored_params": ["open_within_days"]}}
    rows, err = freehire.fetch(FakeSession([FakeResponse(200, body)]), 3)
    assert rows == [] and "ignored" in err

def test_fetch_reports_429():
    rows, err = freehire.fetch(FakeSession([FakeResponse(429, {})]), 3)
    assert rows == [] and err == "freehire: HTTP 429"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_freehire.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `freehire.py`.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: FreeHire source"` (add the files first).

---

### Task 5: LinkedIn and Indeed via JobSpy

**Files:** Create `jobapplier/sources/jobspy_sources.py` · Test `tests/test_jobspy_sources.py`

**Interfaces:**
- Produces:
  - `jobspy_sources.from_record(site: str, rec: dict) -> Posting`:
    - `source=site`, `source_id=str(rec["id"])`.
    - `url` = `rec["job_url_direct"]` if non-empty, else `rec["job_url"]`.
    - `title`, `company`, `location` and `description` copied over; `posted=str(rec["date_posted"])`.
    - Every value goes through `_s(v)`, which returns `""` when `pd.isna(v)` (None, NaN, NaT) and `str(v)` otherwise.
  - `jobspy_sources.fetch(site: str, terms: list[str], results_wanted: int, hours_old: int, scrape=jobspy.scrape_jobs, sleep=time.sleep) -> tuple[list[Posting], str | None]`:
    - One `scrape(site_name=[site], search_term=term, location="Canada", results_wanted=..., hours_old=...)` per term. LinkedIn adds `linkedin_fetch_description=False`; Indeed adds `country_indeed="Canada"`.
    - Records come from `df.to_dict("records")`.
    - Between terms, `sleep(random.uniform(4, 8))`.
    - JobSpy does not raise on HTTP errors. It puts the message in `df.attrs["errors"][site]` (e.g. `"response status code 429 (blocked: use a proxy)"`) and returns the rows it already has. If that message contains `429`, keep those rows, stop the remaining terms, and return error `f"{site}: 429"`. Any other recorded error or raised exception becomes `f"{site}: {message}"` and the loop continues.

- [ ] **Step 1: Write the failing tests.** A fake `scrape` returns a `pandas.DataFrame` built from given records, with `attrs` set as given.

```python
import pandas as pd
from jobapplier.sources.jobspy_sources import from_record, fetch

INDEED = {"id": "in-d22c5896bdbb2211", "site": "indeed", "title": "Software Engineer II, Full Stack (Card Pre-Swipe Experience)",
          "company": "Affirm", "location": "Toronto, ON, CA", "date_posted": "2026-10-30",
          "job_url": "https://ca.indeed.com/viewjob?jk=d22c5896bdbb2211",
          "job_url_direct": "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed", "description": "2+ years of experience"}
LINKEDIN_NAN = {"id": "li-4485119071", "site": "linkedin", "title": "Software Engineer II, Full Stack (Card Pre-Swipe Experience)",
                "company": "Affirm", "location": float("nan"), "date_posted": float("nan"),
                "job_url": "https://www.linkedin.com/jobs/view/4485119071", "job_url_direct": float("nan"), "description": None}

def test_from_record_prefers_direct_url():
    p = from_record("indeed", INDEED)
    assert p.url.startswith("https://boards.greenhouse.io/affirm") and p.posted == "2026-10-30"

def test_from_record_nan_becomes_empty():
    p = from_record("linkedin", LINKEDIN_NAN)
    assert (p.location, p.posted, p.description) == ("", "", "")
    assert from_record("linkedin", {**LINKEDIN_NAN, "date_posted": pd.NaT}).posted == ""
    assert p.url == "https://www.linkedin.com/jobs/view/4485119071"

def test_linkedin_429_stops_remaining_terms():
    calls = []
    def scrape(**kw):
        calls.append(kw)
        df = pd.DataFrame([LINKEDIN_NAN]); df.attrs = {"errors": {"linkedin": "response status code 429 (blocked: use a proxy)"}}
        return df
    rows, err = fetch("linkedin", ["software engineer", "full stack engineer"], 100, 72, scrape=scrape, sleep=lambda s: None)
    assert len(calls) == 1 and len(rows) == 1 and err == "linkedin: 429"
    assert calls[0]["linkedin_fetch_description"] is False and calls[0]["hours_old"] == 72

def test_indeed_passes_country_and_sleeps_between_terms():
    slept = []
    def scrape(**kw):
        df = pd.DataFrame([INDEED]); df.attrs = {}; return df
    rows, err = fetch("indeed", ["a", "b"], 100, 72, scrape=scrape, sleep=slept.append)
    assert err is None and len(rows) == 2 and len(slept) == 1 and 4 <= slept[0] <= 8
```

- [ ] **Step 2: Run** `uv run pytest tests/test_jobspy_sources.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `jobspy_sources.py`.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: LinkedIn and Indeed sources via JobSpy"` (add the files first).

---

### Task 6: SQLite store and clustering

**Files:** Create `jobapplier/db.py`, `jobapplier/dedupe.py` · Test `tests/test_dedupe.py`

**Interfaces:**
- Consumes: `Posting`, `filters.cluster_key`, `filters.is_intern`, `filters.is_notable`, `rank.city_rank`, `rank.is_remote_us`, `rank.rank`.
- Produces:
  - `db.connect(path: str) -> sqlite3.Connection` (`row_factory=sqlite3.Row`; creates the schema below; `":memory:"` works).
  - `db.members(conn, cluster_id: int) -> list[Posting]`.
  - `dedupe.assign(conn, p: Posting, now: datetime, include_reposts: bool) -> int | None`. `p.identity` is already set. Returns the cluster id, or `None` if `(source, source_id)` is already stored. Rules, in order:
    1. If `p.identity` matches a stored posting's identity, join that cluster. Exception: the cluster was first seen more than 48 h before `now` and `include_reposts` is on. Then start a new cluster with `repost=1`.
    2. Otherwise join the newest cluster with the same `cluster_key` first seen within 7 days of `now`.
    3. Otherwise start a new cluster.
  - `dedupe.pushable(conn) -> list[sqlite3.Row]`: clusters with `notion_page_id IS NULL` that have at least one member with `no_location = 0`.
  - `dedupe.summarize(members: list[Posting], notable: list[str], repost: bool) -> dict`:
    - The returned keys: `title company city alt_locations rank ats posted flags link identity`.
    - Sort the members once by (has no identity, `city_rank(location)`, source order freehire, indeed, linkedin). The first member gives title, company, `city` (its location), `link` (its URL) and `identity`, so the City shown is the city of the posting the Link applies to. Rank uses the first member with a non-empty description.
    - **`alt_locations`** lists the other distinct non-empty locations, joined with `"; "`.
    - **`ats`** maps the identity prefix: `Greenhouse`, `Lever`, `Ashby` or `Workday`; anything else is `Other`.
    - **`posted`** is the earliest non-empty `posted`.
    - **`flags`** is a subset of `["Remote US", "Aggregator link", "Repost"]`, in that order. `Remote US` when `is_remote_us(city)`. `Aggregator link` when the identity is `None`. `Repost` when `repost` is true.
    - **`rank`** is `rank.rank(...)`, with `notable_intern = is_intern(title) and is_notable(company, notable)`.
  - `db.mark_pushed(conn, cluster_id: int, page_id: str, link: str, identity: str | None) -> None`.

Schema:

```sql
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, first_seen TEXT NOT NULL,
  repost INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT, link TEXT, target_identity TEXT);
CREATE INDEX IF NOT EXISTS clusters_key ON clusters(key);
CREATE TABLE IF NOT EXISTS postings (
  source TEXT NOT NULL, source_id TEXT NOT NULL, cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  identity TEXT, title TEXT, company TEXT, location TEXT, url TEXT, posted TEXT, description TEXT,
  language TEXT, seniority TEXT, no_location INTEGER NOT NULL, first_seen TEXT NOT NULL,
  PRIMARY KEY (source, source_id));
CREATE INDEX IF NOT EXISTS postings_identity ON postings(identity);
```

- [ ] **Step 1: Write the failing tests** (in-memory DB; `NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)`). Helper `P(source, sid, title, company, location, url, **kw)` builds a `Posting` and sets `identity=posting_identity(url)`.

```python
def test_affirm_four_sources_collapse_to_one_row():
    conn = db.connect(":memory:")
    t = "Software Engineer II, Full Stack (Card Pre-Swipe Experience)"
    ids = {assign(conn, p, NOW, False) for p in [
        P("freehire", "fh1", t, "Affirm", "Remote Canada", "https://job-boards.greenhouse.io/affirm/jobs/8042399003?utm_source=freehire.me", description="2+ years of experience"),
        P("indeed", "in1", t, "Affirm", "Toronto, ON, CA", "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed"),
        P("linkedin", "li1", t, "Affirm", "Calgary, Alberta, Canada", "https://www.linkedin.com/jobs/view/4485119071"),
        P("linkedin", "li2", t, "Affirm", "Winnipeg, Manitoba, Canada", "https://www.linkedin.com/jobs/view/4485123048")]}
    assert len(ids) == 1
    s = summarize(db.members(conn, ids.pop()), [], False)
    assert s["city"] == "Toronto, ON, CA" and s["ats"] == "Greenhouse" and s["rank"] == 2
    assert s["link"] == "https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed"
    assert s["identity"] == "greenhouse:affirm:8042399003" and s["flags"] == []

def test_same_source_row_is_ignored():
    # assign the same Posting twice → second call returns None

def test_same_title_different_reqs_share_a_row_within_7_days_but_not_after():
    # two Revvity workday URLs, same title: same cluster at NOW and NOW+1d; a third at NOW+8d → new cluster

def test_repost_off_joins_old_cluster_silently():
    # identity seen at NOW-3d; new source row with same identity at NOW, include_reposts=False → same cluster id

def test_repost_on_makes_new_flagged_cluster():
    # same setup, include_reposts=True → new cluster; summarize(..., repost=True)["flags"] == ["Repost"]

def test_no_location_cluster_is_not_pushable_until_a_located_row_joins():
    # FreeHire no_location=True row alone → pushable() == []; then a LinkedIn row with same company+title → 1 pushable

def test_linkedin_only_row_gets_aggregator_flag():
    # summarize of one LinkedIn member → identity None, link = linkedin URL, "Aggregator link" in flags
```

Write the commented tests in full, with the values the comments give.

- [ ] **Step 2: Run** `uv run pytest tests/test_dedupe.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `db.py`, then `dedupe.py`. `first_seen` is stored as ISO UTC (`now.isoformat()`).
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: SQLite store and clustering"` (add the files first).

---

### Task 7: Notion client, setup, push, status

**Files:** Create `jobapplier/notion.py` · Test `tests/test_notion.py`

**Interfaces:**
- Consumes: `db.mark_pushed`, `db.members`, `dedupe.pushable`, `dedupe.summarize`.
- Produces:
  - `class NotionError(Exception)`.
  - `class Notion(token: str, session=None, sleep=time.sleep)` with `request(method: str, path: str, body: dict | None = None) -> dict`:
    - Base URL `https://api.notion.com`, headers `Authorization: Bearer`, `Notion-Version: 2025-09-03` and JSON.
    - Calls `sleep(0.35)` before every request (keeps under ~3 req/s).
    - On 429 or 529, sleeps `int(Retry-After)` (default 1) and retries, up to 5 times.
    - Any other non-2xx raises `NotionError(f"{status} {text[:300]}")`.
  - `notion.SCHEMA: dict`. These are the data-source properties:
    - Title: `title`.
    - Company, City, Alt locations, Job ID: `rich_text`.
    - Rank: `select` with `1 New grad`, `2 Under 3 yrs`, `3 Other`.
    - ATS: `select` with `Greenhouse Lever Ashby Workday Other`.
    - First seen, Posted: `date`.
    - Flags: `multi_select` with `Remote US`, `Aggregator link`, `Repost`.
    - Link: `url`.
    - Decision: `select` with `New Approve "Approve + review" Skip`.
    - Status: `select` with `New`.
  - `notion.create_database(n: Notion, parent_page_id: str) -> tuple[str, str]`: `POST /v1/databases` with `parent={"type":"page_id","page_id":...}`, `title` "Jobs" and `initial_data_source={"properties": SCHEMA}`. Returns `(response["id"], response["data_sources"][0]["id"])`.
  - `notion.page_properties(job_id: str, s: dict, first_seen: str) -> dict`: `s` is a `summarize()` dict. Rank `n` maps to the option starting with `f"{n} "`. Decision and Status are both `New`. Rich text is cut to 2,000 characters. `Posted` is left out when `s["posted"]` is empty (Notion rejects an empty date).
  - `notion.find_page(n, ds_id: str, job_id: str) -> str | None`: `POST /v1/data_sources/{ds_id}/query` with `{"filter": {"property": "Job ID", "rich_text": {"equals": job_id}}, "page_size": 1}`.
  - `notion.push(conn, n, ds_id: str, notable: list[str]) -> tuple[int, list[str]]` (the Repost flag comes from the cluster row's `repost` column):
    - For each `pushable` cluster: `find_page` or `POST /v1/pages` with `parent={"type":"data_source_id","data_source_id":ds_id}`.
    - Then `mark_pushed` and `conn.commit()` **before** the next cluster.
    - A `NotionError` on one cluster is collected and the loop continues, so one bad row never blocks the rest.
    - Returns `(created: int, errors: list[str])`, each error `f"notion: J{id}: {exc}"`.
  - `notion.set_status(n, database_id: str, text: str) -> None`: `PATCH /v1/databases/{database_id}` with `{"description": [{"text": {"content": text[:2000]}}]}`.
  - `notion.keychain(service: str) -> str`: `security find-generic-password -s <service> -w`, stdout stripped.
  - CLI: `python -m jobapplier.notion setup <parent_page_id>` reads the token with `keychain("jobapplier-notion")` and prints `database_id` and `data_source_id` for `config.yaml`.

- [ ] **Step 1: Write the failing tests.** `FakeSession.request(method, url, headers=, json=, timeout=)` serves queued responses and records calls. `FakeNotion` keeps pages in a dict and can be told to raise on the Nth create.

```python
def test_request_throttles_and_retries_429():
    slept = []
    s = FakeSession([Resp(429, {}, {"Retry-After": "2"}), Resp(200, {"ok": True})])
    n = Notion("tok", session=s, sleep=slept.append)
    assert n.request("GET", "/v1/pages/x") == {"ok": True}
    assert 2 in slept and len(s.calls) == 2 and s.calls[0]["headers"]["Notion-Version"] == "2025-09-03"

def test_request_raises_on_400():
    # Resp(400, {"message": "bad"}) → pytest.raises(NotionError)

def test_page_properties_shape():
    props = page_properties("J7", {"title": "SWE", "company": "Affirm", "city": "Toronto", "alt_locations": "", "rank": 2,
        "ats": "Greenhouse", "posted": "", "flags": ["Remote US"], "link": "https://x", "identity": "greenhouse:a:1"}, "2026-10-31T12:00:00+00:00")
    assert props["Job ID"]["rich_text"][0]["text"]["content"] == "J7"
    assert props["Rank"]["select"]["name"] == "2 Under 3 yrs"
    assert props["Decision"]["select"]["name"] == "New" and props["Flags"]["multi_select"] == [{"name": "Remote US"}]
    assert "Posted" not in props

def test_push_skips_a_failing_row_and_resumes_without_duplicates():
    # 3 pushable clusters; FakeNotion raises NotionError on the 2nd create → push returns (2, ["notion: J2: ..."]);
    # clusters 1 and 3 have notion_page_id set. Second push with a healthy FakeNotion creates exactly 1 page,
    # and FakeNotion holds 3 distinct Job IDs in total.

def test_push_reuses_existing_page_found_by_job_id():
    # FakeNotion already holds a page with Job ID "J1" → push creates 0 pages and stores that page id.
```

Write the commented tests in full.

- [ ] **Step 2: Run** `uv run pytest tests/test_notion.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `notion.py`.
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: Notion client, setup, push, status"` (add the files first).

---

### Task 8: One discover run and `main()`

**Files:** Create `jobapplier/discover.py` · Test `tests/test_discover.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `discover.run_discover(conn, cfg: dict, sources: list[tuple[str, Callable[[], tuple[list[Posting], str | None]]]], notion, now: datetime) -> dict`:
    - Calls the sources in the given order. An exception from a source becomes the error `f"{name}: {exc}"`.
    - Each posting is kept if `filters.keep` passes; it then gets `identity` set and goes through `dedupe.assign`, followed by `conn.commit()`.
    - Then runs `notion.push` (its errors join the list) and `notion.set_status`.
    - Returns `{"new_rows": int, "errors": list[str], "alert": bool}`. `alert` is true when there is any error other than `linkedin: 429` (expected now and then; shown in the status only).
    - Status text: `f"Last discover {local time %Y-%m-%d %H:%M} · {new_rows} new · {total clusters} total · errors: {'; '.join(errors) or 'none'}"`.
    - A `NotionError` from `set_status` is caught and added as `f"notion: {exc}"`.
  - `discover.locked(path: str) -> ContextManager[bool]`: `fcntl.flock(LOCK_EX | LOCK_NB)`. Yields `False` if another process holds it, and writes the PID otherwise.
  - `discover.notify(title: str, message: str) -> None`: `osascript -e 'display notification ...'`, with both strings quoted via `json.dumps`.
  - `discover.main(argv: list[str] | None = None) -> int`:
    - Reads `config.yaml` from the repo root (`Path(__file__).resolve().parents[1]`) and opens `data/jobs.db` (creating `data/`).
    - Returns 0 without working if the lock is busy. (No "ran recently" gate in slice 1: the agent runs every 2 h. Slice 3 adds the gate when the agent also applies every 30 min.)
    - Builds the real sources in the order FreeHire, Indeed, LinkedIn, plus the Notion client from the Keychain token.
    - Calls `notify` when `alert` is true, or on any unexpected exception (which also logs a traceback and returns 1).
    - `argv` is unused in slice 1; it exists so `main()` is callable from tests.
    - Logs to stderr with `logging`.
  - `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 1: Write the failing tests** (in-memory DB, fake sources, `FakeNotion` from the Task 7 tests; move the fakes to `tests/conftest.py`):

```python
def test_run_discover_end_to_end():
    # sources: freehire → the 14 fixture rows parsed; indeed → [Affirm INDEED posting]; linkedin → ([], "linkedin: 429")
    # expect: result["errors"] == ["linkedin: 429"], alert False (linkedin 429 alone does not alert), every page in FakeNotion has a unique Job ID,
    # the Affirm row exists once, the Unity "Sr." row, the Pebl manager row and both Revvity intern rows are absent, the Disney intern row is present, the no-location row is absent,
    # and FakeNotion's last database description starts with "Last discover".

def test_freehire_error_sets_alert_and_other_sources_still_push():
    # freehire source returns ([], "freehire: HTTP 429"); indeed returns the Affirm posting → 1 page, alert True

def test_source_exception_is_recorded_and_alerts():
    # a source named "indeed" that raises RuntimeError("boom") → "indeed: boom" in errors, alert True; run still returns

def test_lock_is_exclusive(tmp_path):
    with locked(str(tmp_path / "l")) as a:
        with locked(str(tmp_path / "l")) as b:
            assert a is True and b is False
```

Write the commented tests in full.

- [ ] **Step 2: Run** `uv run pytest tests/test_discover.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `discover.py`.
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: discover run, lock, notifications"` (add the files first).

---

### Task 9: LaunchAgent, README, live run (with the owner)

**Files:** Create `launchd/local.jobapplier.plist`, `README.md`

- [ ] **Step 1: Plist.**
  - Label `local.jobapplier`, `StartInterval` 7200, `RunAtLoad` true (launchd runs a missed interval once on wake).
  - `ProgramArguments`: `/usr/bin/caffeinate -i /opt/homebrew/bin/uv run --project /Users/shariqanwar/PersonalProjects/JobApplier python -m jobapplier.discover`.
  - `WorkingDirectory` is the repo; `EnvironmentVariables` `PATH=/opt/homebrew/bin:/usr/bin:/bin`.
  - `StandardOutPath` and `StandardErrorPath` go to `logs/jobapplier.log` (README: `mkdir logs`).
  - Run `plutil -lint launchd/local.jobapplier.plist`. Expected: `OK`.
- [ ] **Step 2: README.** Short setup steps, in order:
  1. The owner creates a Notion internal integration and shares one empty page with it.
  2. Store the token: `security add-generic-password -a "$USER" -s jobapplier-notion -w` (it prompts; the token is never typed on the command line).
  3. Run `uv run python -m jobapplier.notion setup <page_id>` and paste both IDs into `config.yaml`.
  4. In Notion, create an **Inbox** view with the filter `Status is New` and `Decision is not Skip`, sorted by Rank ascending, then First seen descending.
  5. Install the agent: `cp launchd/local.jobapplier.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.jobapplier.plist`.
  6. Uninstall line: `launchctl bootout gui/$(id -u)/local.jobapplier`.
  7. Note that approving rows does nothing until slice 2.
- [ ] **Step 3: Live run (needs the owner for steps 1–2 of the README).** Run `uv run python -m jobapplier.discover`.
  - Expected: exit 0.
  - Rows appear in Notion, and the database description shows `Last discover … errors: none` (or only `linkedin: 429`).
  - Run it a second time: `0 new`, and no duplicate rows.
  - Spot-check five rows against their links.
- [ ] **Step 4: Install the agent** (README step 5). Check that `launchctl print gui/$(id -u)/local.jobapplier` shows `state = running` or a last exit code of 0, and that a notification appears for a forced error (temporarily set a bad `data_source_id`, run once by hand, then restore it).
- [ ] **Step 5: Commit** `git add launchd README.md && git commit -m "feat: LaunchAgent and setup guide"`
