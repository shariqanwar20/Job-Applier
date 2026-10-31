# JobApplier — Design

Date: 2026-10-31
Status: Draft v4 (two design reviews applied; one owner question open in §17)

Decisions marked **(owner)** were made by the owner. **(builder's call)** marks decisions made during design that the owner may override. Unmarked mechanics are part of the design.

## 1. Intent

A personal pipeline that finds fresh software-engineering postings in Canada, lets the owner approve them from a Notion table, then tailors a resume and submits the application with as little LLM and human effort as possible.

Why: applying manually is slow, and the open-source auto-appliers either target only LinkedIn Easy Apply or let an LLM agent drive every form (slow, costly, unpredictable). Here scraping is script-based; form filling is scripted wherever the ATS is predictable, with LLMs filling gaps; LLMs also tailor the resume and write free-text answers.

Success: every ~2 hours the Notion inbox shows only new, de-duplicated, ranked postings; approved jobs are submitted without the owner except for CAPTCHAs and review rows; nothing is ever applied to twice; the owner can see what the pipeline is using of their Claude plan.

## 2. Goals, non-goals, anti-goals

**Goals**
- Discover new postings (last 24 h) for software / full-stack / backend / frontend / AI / mobile engineering roles, entry-to-mid level, in Canada (plus US-remote roles tagged Canada).
- One de-duplicated Notion table; applied and skipped jobs leave the inbox.
- Human approval before anything is built or submitted.
- Tailored one-page resume PDF per job from the LaTeX master.
- Form filling: scripted for Greenhouse/Lever/Ashby, LLM-assisted for Workday and unknown ATSs.
- Automatic email code/link handling.
- Usage of the Claude plan visible in Notion, with one overall daily cap.
- $0 recurring beyond the existing Claude subscription.

**Non-goals**: roles outside Canada (except US-remote tagged Canada); LinkedIn Easy Apply or logged-in LinkedIn automation; cover letters; multi-user, hosting, custom UI.

**Anti-goals (failure even if it "works")**
- Applying to the same posting twice, or to anything the owner didn't approve (including approvals withdrawn mid-run).
- Submitting something other than what the owner reviewed, for review rows.
- A resume claim not supported by the master resume, or a strengthened claim.
- Automated CAPTCHA solving or other bot-detection evasion.
- Getting the owner's LinkedIn account restricted.
- Writing to, deleting from, or marking-as-read anything in the mailbox.
- Failing silently.

## 3. Constraints

- Runs on the owner's Mac in the background whenever it is on and the owner is logged in **(owner)**.
- No paid APIs, proxies, or hosting **(owner)**. LLM calls via **Claude Code headless (`claude -p`) on the owner's subscription** **(owner)**. The pipeline shares the plan's usage window with the owner's interactive use.
- Approval UI: **Notion** via the free API **(owner)**.
- Master resume: **LaTeX** **(owner)**.
- Email: **school Microsoft 365** mailbox (used on applications, kept after graduation), personal Gmail as fallback **(owner)**.
- Python 3.12 via `uv`. To install: `tectonic` (prime its package cache before the first scheduled run), `poppler` (`pdfinfo`), Playwright. Present: Claude Code 2.1.287, Chrome.

## 4. Architecture overview

Two LaunchAgents with separate locks **(builder's call)**, so a long apply session never blocks discovery, plus a watchdog:

```
discover agent (every 2h + RunAtLoad + once on wake)
  0. sync_notion_decisions()          Notion → SQLite (keyed on Job ID property)
  1. discover()                       FreeHire → LinkedIn(JobSpy) → Indeed(JobSpy)
  2. filter → cluster → resolve identity → location → repost → full description → rank
  3. push_new_rows()                  SQLite → Notion (idempotent)
  4. write heartbeat + status page

apply agent (starts after each discover run; also every 30 min while queue non-empty)
  0. recover()                        rows left in `refilling`/`submitting` → needs_you(verify)
  1. sync_notion_decisions()
  2. for up to N jobs (config, default 5 per run):
       re-check Decision in Notion → liveness → tailor → fill (tier 1/2/3) → review gate
       → re-check Decision + applied guard → `submitting` → click → `submitted`
  3. write heartbeat + status page

watchdog agent (hourly; bash + osascript only, no Python/uv/Keychain)
  if a heartbeat is older than 6h while the Mac was awake → macOS notification
```

SQLite (`data/jobs.db`) is the system of record. Notion is a view and input surface; Notion problems must never cause a duplicate application.

## 5. Discovery

### 5.1 Sources and priority (owner: FreeHire > LinkedIn > Indeed)
Priority applies to description and metadata. **The apply URL is chosen separately:** any direct ATS URL beats any aggregator URL, whatever the source.

1. **FreeHire** (`https://freehire.me/api/v1`; github.com/strelov1/freehire, MIT). No key. Limits: ~120 req/min, **2,000 req/day per IP**, `offset+limit ≤ 10,000`. Personal non-commercial use allowed.
   - Main query: `countries=ca&regions=none&category=software_engineering&category=fullstack&category=backend&category=frontend&category=ai_engineering&category=mobile&open_within_days=N`. (`category` values are separate facets; on 2026-10-31 backend/frontend/ai/mobile added ~29 Canada rows a day.) Title regexes still filter.
   - **Full descriptions** come from `/agent/jobs/search` pages (one request returns up to 100 full rows), not per-slug calls. `/jobs/search` returns only a ~1,000-char preview.
   - **Rolling local cache (for repost and identity lookups):** every FreeHire row ever fetched is kept in SQLite with its `created_at`, ATS source, and URL. A one-time backfill pulls the last 30 days in daily slices (stays under the paging cap); after that each run only fetches recent days. No per-row lookups.
   - Freshness from `created_at` / `open_within_days`, not `posted_at`.
   - `posting_language` and `seniority` are under `enrichment` (seniority is null on ~half of rows).
   - Check `meta.ignored_params` / `meta.ignored_values` on every response; any value = run error.
   - Track the per-day request count; on low `X-RateLimit-Remaining` skip non-essential calls.
2. **LinkedIn via JobSpy**, list only, terms "software engineer", "full stack engineer", location "Canada", `results_wanted` 100/term (builder may raise after a 429-free week).
3. **Indeed via JobSpy**, `country_indeed="Canada"`. Full description; direct URL on most rows.

Spike (2026-10-31): FreeHire 134 Canada + 31 no-location; 9/10 LinkedIn spot-checks found in FreeHire (8 with direct ATS URLs); only 10/74 LinkedIn jobs on Indeed.

### 5.2 LinkedIn-only rows
Not in the FreeHire cache → Indeed match → one LinkedIn guest detail fetch. JobSpy has no public per-URL fetch; verify on day one, else call LinkedIn's guest job-detail endpoint directly with the same throttling.

LinkedIn throttling: jittered 4–8 s between requests; stop all LinkedIn calls for the run on the first 429 (verify how JobSpy reports it, e.g. logger or `attrs`); no proxies; LinkedIn failure never fails a run.

### 5.3 Freshness window
- Filter: first seen within 24 h.
- Lookback per run: `max(3h, time since last successful discover run)`, **capped at 3 days** **(builder's call)**. Jobs older than 24 h found this way are flagged `Late`.
- LinkedIn dates are date-only UTC; "today" matching allows ±1 day.

### 5.4 Reposts (owner: off by default)
Runs after clustering and identity resolution, on the merged row.
- **Repost** = the row's posting identity (§6.1) matches a FreeHire cache entry with `created_at` older than 48 h.
- Company + title match only (no identity match) → **not** treated as a repost; flagged `Repost unknown` and kept. This protects genuinely new reqs at employers that post the same title weekly.
- `include_reposts` off: reposts dropped. On: kept and flagged.

### 5.5 Location
- `regions=none` rows are kept only if they match a LinkedIn/Indeed Canada row.
- Remote roles located in the US but tagged Canada are kept and applied to **(owner)**, flagged `Remote US`.

## 6. Normalization, identity, clustering, ranking

Order: title filters → cluster → **resolve identity** → location → repost → full description → rank.

### 6.1 Keys
1. **Source identity** `(source, source_id)`: don't ingest the same row twice.
2. **Posting identity** `(ats, ats_job_id)`; else the normalized direct ATS URL. **This is the applied guard.**
3. **Cluster key**: normalized company + normalized title (+ `canonical_key_version`). Display grouping only.

### 6.2 Identity resolution before display
Every row that passes the title filters gets its posting identity resolved **before it is shown**, so duplicates across sources collapse and the guard can see them:
- FreeHire ATS rows already have it. Indeed rows: parse the direct URL.
- LinkedIn rows: take identity from the matching FreeHire/Indeed row.
- Aggregator rows (whatjobs): their links are landing pages that do not redirect (verified). Prefer any other cluster member's direct URL; otherwise one best-effort parse of the landing page for an ATS link; otherwise flag `Aggregator link` (likely ends as `needs_you`).
- In the apply phase identity is re-derived after following the link, and the guard is checked again immediately before writing `submitting`.

### 6.3 Applied guard
- A posting identity that reached `submitting` or `submitted`, or was marked "Applied manually", is never prepared again automatically.
- The owner can override only with Decision = **Retry** (verified not submitted), which writes an audit note. The pipeline never clicks submit twice on its own.
- **Soft guard:** if the same company + normalized title was submitted in the last 30 days, a new posting is still shown (owner: new reqs with the same title should show again) but flagged `Applied to similar on <date>` and forced to review.

### 6.4 Clustering (owner: city preference)
- Rows with the same cluster key first seen within 7 days of each other form one Notion row. Representative city: **Toronto (GTA) > Vancouver > Montreal > remote-Canada > remote-US > other** (builder's call on the remote ranks). Others listed in `Alt locations`.
- If a cluster holds several posting identities (e.g. Revvity: 4 Workday reqs with the same title), the row lists them; the **apply target is fixed when the row is first shown** (preferred city) and is the URL in the Link column. Siblings are marked `sibling_of_applied` once the target is submitted.
- A same-title req appearing after the 7-day window is a new row **(owner)**.

### 6.5 Filters and rank
- **Include** (title must match): developer, engineer, programmer, software, full-?stack, back-?end, front-?end, SDE, mobile, développeur/développeuse, ingénieur(e) logiciel.
- **Exclude**: senior, sr, staff, principal, lead, manager, director, architect, head of. French `principal(e)` excluded (accepted miss).
- **Interns/co-ops (owner):** intern|co-op|coop|stagiaire|stage kept only if the company is on `config.yaml: notable_companies` (builder seeds big tech and top Canadian/US tech and finance employers; owner edits). Kept interns rank tier 1.
- **French postings (owner): kept and applied to.** Resume stays English; free-text answers are written in the question's language **(builder's call)**.
- Rank tiers **(owner)**: 1 New grad / SDE I / Junior / Associate / level "I" / notable intern; 2 requires < 3 years (from the full description); 3 everything else.
- Years: regex on the full description, then `enrichment.seniority`, then Haiku only when unclear.

## 7. Notion

One database "Jobs"; an integration token scoped to it.

| Property | Type | Written by |
|---|---|---|
| Title, Company, City, Alt locations | text | pipeline |
| Rank | select (1 New grad / 2 Under 3 yrs / 3 Other) | pipeline |
| ATS, Fill tier | select | pipeline |
| First seen, Posted | date | pipeline |
| Flags | multi-select (Repost, Repost unknown, Late, Remote US, French, Intern, Aggregator link, Applied to similar) | pipeline |
| Link | url — the exact posting that will be applied to | pipeline |
| Job ID | text (SQLite key) | pipeline |
| **Decision** | select: New / Approve / Approve + review / Skip / Submit / Retry / Applied manually | **owner** (pipeline resets to New, see below) |
| Status | select: New / Queued / Preparing / Awaiting review / Refilling / Submitting / Submitted / Needs you / Failed / Skipped / Closed / Duplicate | pipeline |
| LLM usage | number (equivalent $ for this job) | pipeline |
| Notes | text | either |

- Views: **Inbox** = Status ∈ {New, Queued, Awaiting review, Needs you, Failed}; **History** = all.
- Decision handling:
  - Skip → Status Skipped. Applied manually → Status Submitted and guard set. Retry → back to Queued (audit note).
  - When the pipeline writes Needs you or Failed it **resets Decision to New**, so re-approving is a visible change.
- **Sync keys on the Job ID property**, not stored page IDs. Two pages with the same Job ID → the newer is set to Status Duplicate. Create is preceded by a query by Job ID; page ID stored immediately.
- Row page body: resume PDF (≤5 MB upload), answers, filled-form screenshot, short log; text over 2,000 chars split across blocks.
- **Pipeline status page:** last successful discover/apply run, counts, errors, LinkedIn 429s, FreeHire requests used today, Notion requests, mail auth state, and the **usage section** (§10).
- Throttle to ~3 req/s. Never delete pages.

## 8. Application processing

### 8.1 States (SQLite authoritative)
```
new ─Approve────────► queued ─► preparing ─────────────────────────► submitting ─► submitted
new ─Approve+review─► queued ─► preparing ─► awaiting_review ─Submit─► refilling ─► submitting ─► submitted
new ─Skip──────────► skipped
preparing ─► closed | needs_you | failed | queued (infra error, attempts < 3)
refilling ─► awaiting_review (form changed since review)
refilling / submitting found at startup ─► needs_you (verify)
needs_you/failed ─Approve/Retry─► queued      any ─Applied manually─► submitted
```
- **Decision re-check:** right before tailoring and right before writing `submitting`, the row's Decision is re-read from Notion (one GET); if it is no longer Approve / Approve + review / Submit, abort and set the status accordingly.
- **Point of no return:** `submitting` is committed to SQLite immediately before the click. Any problem after it (crash, sleep, submit-time challenge, validation error) → `needs_you` with "verify"; the pipeline searches the mailbox (read-only) for a confirmation email to help. Only the owner moves it on (Submitted, Applied manually, or Retry).
- **Infra failures** (timeout, browser died, sleep, usage limit mid-call) return the job to `queued`, up to 3 attempts, then `failed`.
- **Auto-escalation to review:** a sensitive question with no deterministic answer in `profile.yaml`; no adapter and tier 3 used; required field couldn't be filled; a new job-specific question category; resume grounding check failed; `Applied to similar` flag.
- **Frozen review artifacts:** at `awaiting_review` store resume PDF, answers, field→value map, apply URL, and a hash of the form's question set. Submit pass (`refilling`) reuses them verbatim with no LLM calls; a different question set returns the row to `awaiting_review` with the diff.

### 8.2 Liveness
Per-adapter content check ("no longer accepting applications", redirect to board root, 404), since Greenhouse/Workday return 200 for closed jobs. FreeHire `closed_at` is a hint only. "You've already applied" page → `submitted`.

### 8.3 Resume tailoring (owner: LaTeX + Sonnet 5.5; rewording, adding, omitting allowed)
- `resume/master.tex` is the source of truth; each bullet/project/skill line carries a stable ID.
- Sonnet returns JSON (`--json-schema`): ordered items, each `{id}` (unchanged), `{id, text}` (reworded), or `{new: text, sources: [ids]}` (added).
- **Only master IDs may be sources.** `answers_bank.md` is for form answers, never resume content.
- **Grounding validator:** rejects any number, percentage, technology, employer, or title not in the cited master items, and scope-inflating verb swaps (deny-map: contributed/assisted/helped/supported → led/owned/architected/spearheaded/drove). Rejected items revert to master text and the row is escalated to review.
- One-page fit: if longer than one page, drop the lowest-ranked optional items and recompile; never drop below the master's required items (floor), never re-prompt.
- All LLM text is LaTeX-escaped; raw backslashes/braces rejected. Compile with `tectonic`; check pages with `pdfinfo`.
- Filename **(owner)**: `First_Last_Company_Role.pdf` (spaces → `_`, punctuation/parentheticals removed, role ≤ ~6 words, collision → short job ID). Final PDF uploaded to the Notion row **(owner)**.

### 8.4 Form filling — three tiers
Every tier first reads the **full list of fields** (label, type, required, options), then classifies each field into:
1. **Standard field** (name, email, phone, location, resume, LinkedIn/GitHub/website, EEO/self-ID, work authorization, sponsorship, "how did you hear", notice period, relocation, salary expectation) → value from `profile.yaml`; no LLM. `profile.yaml` holds deterministic answers for all of these, including "decline to answer" EEO defaults, so they don't force review.
2. **Known question** → matched by normalized text against the answers bank; reused with no LLM. **Only job-independent answers are reusable** (each saved answer is classified job-independent vs job-specific when written; job-specific ones like "Why this company?" are regenerated per job).
3. **New question** → one batched LLM call per job: Haiku for choice fields (must return one of the given options), Sonnet for free text (Opus medium for hard ones). Job-independent answers are saved to the bank.

| Tier | ATS | How fields are read | Who fills |
|---|---|---|---|
| **1 Scripted** | Greenhouse, Lever, Ashby | Greenhouse: public API `boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true`. Ashby: form definition JSON embedded in the job page. Lever: structured custom-question "cards" in the form markup. | Script |
| **2 LLM maps fields** | Workday, iCIMS, SmartRecruiters, Jobvite, unknown | Script extracts each page's fields from the DOM/accessibility tree as JSON | One LLM call per page returns field → value; script fills, clicks Next, repeats |
| **3 LLM drives the browser** | Fallback when tier 2 is stuck | `claude -p` with a Playwright MCP server attached, scoped to the one tab | LLM operates the page |

Rules for every tier:
- **The LLM never clicks submit.** The script does, after the screenshot, the Decision re-check, the guard check, and writing `submitting`. Tier 3 runs with an instruction and a tool-level block on submit-like controls; the script verifies the page is pre-submit when it returns.
- Values may only come from `profile.yaml`, the answers bank, or the answer step; tier 2/3 cannot invent values.
- **Tier 3 rows always stop for review** **(builder's call)** until the owner turns that off in config.
- Upload resume **first**, then fill/overwrite (ATS parsers overwrite name/email and prefill work history badly). Typeaheads/React selects: type, pick, verify the committed value.
- Resolve links first: Greenhouse embed URLs → direct board URL; aggregator → §6.2.
- Before the click, re-read every filled field and compare with the intended field→value map (guards against stray input).

**Browser (owner):** Playwright with real Chrome, headed, one long-lived dedicated profile (own `--user-data-dir`), submissions paced minutes apart (jittered). The window opens without taking focus where possible (builder verifies on macOS). The automation banner / `navigator.webdriver` stay as they are (no evasion); measure challenge rates on day one.

**Accounts (Workday etc.):** generate the password, **store it in Keychain first**, then create the account with the application email. "Email already registered" → try sign-in with any stored password; else `needs_you` (owner resets). Application email must be final before the first account (§8.6).

### 8.5 CAPTCHA (owner: no automated solving)
- Visible widget, owner active: pause with the window open, notify, Notion `needs_you`, poll ≤15 min, continue when solved.
- Owner away (no input >10 min via `ioreg` HIDIdleTime) **(owner: still attempt jobs)**: straight to `needs_you` with URL + PDF; close window; next job.
- Invisible challenges blocking submission → `needs_you` immediately.
- No automatic retries; the owner re-approves.

### 8.6 Email codes and links (owner: school mailbox, Gmail fallback)
- **Day one, before any application:** try delegated `Mail.Read` with device-code sign-in. If admin consent is required → switch to Gmail (IMAP app password in Keychain, `EXAMINE` read-only) **before** any ATS account exists.
- Poll messages from the adapter's known sender domains (`greenhouse-mail.io`, `myworkday.com`, `ashbyhq.com`, …) received after the action, every few seconds for ~3 min. Extract a 4–8 digit code near "code/verification" keywords, or a verification link on the ATS domain.
- Never modify, delete, or mark read. Token refresh failure → status page "mail auth expired" + notification; affected jobs `needs_you`.

## 9. LLM calls (owner: Claude Code subscription, headless)

| Task | Model |
|---|---|
| Resume tailoring | Sonnet 5.5 |
| Free-text answers | Sonnet 5.5 (Opus medium for hard ones) |
| Choice fields, field→profile mapping (tier 2), seniority when unclear | Haiku (owner says Haiku 5.5; confirm ID after the Claude Code upgrade) |
| Tier 3 browser driving | Sonnet 5.5 |

Invocation (non-tier-3):
```
/opt/homebrew/bin/claude -p --model <id> --output-format json --json-schema <file> \
  --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence
```
Tier 3 adds `--mcp-config <playwright-only.json>` and allows only that server's tools. Verify on day one whether `--bare` still authenticates from launchd; use it only if it does. Model IDs live in `config.yaml`. Stay on the CLI; don't extract the OAuth token.

## 10. Usage tracking and the daily cap

- Every `claude -p` result's JSON (model, input/output tokens, duration, `total_cost_usd`) is logged in SQLite with job ID and purpose (tailor / answer / field-map / tier3 / classify). On a subscription `total_cost_usd` is not billed; it is an **equivalent-cost gauge**.
- Notion: per-row **LLM usage**; status page shows today and the last 7 days by model and purpose, plus each time a plan limit was hit.
- **One overall daily cap (owner):** `config.yaml: daily_llm_cap_usd` (equivalent cost, all purposes together; builder picks a starting value). When reached, remaining jobs stay `queued` until the next day; the status page says so.
- Also on the status page: applications per day, FreeHire and Notion request counts.
- Claude Code does not expose remaining plan quota; a plan-limit error leaves jobs `queued`. Its exact form must be captured on day one.

## 11. Scheduling and runtime
- Three LaunchAgents (§4). Plists set `EnvironmentVariables` PATH and use absolute paths for `uv`, `claude`, `tectonic`.
- Locks via Python `fcntl.flock` (macOS has no `flock` command) with PID.
- `caffeinate -i` around runs; lid-close sleep can still interrupt, which the state rules (§8.1) make safe.
- Max applications per apply run (default 5) so a run stays well inside 2 h.
- Notifications via `osascript`/`terminal-notifier`; verify they appear from launchd on day one; Notion status page is the backup channel.

## 12. Security
- Keychain: Notion token, mail token / Gmail app password, ATS tenant passwords.
- Git-ignored: `profile.yaml`, `answers_bank.md`, `resume/`, `output/`, `data/`, `logs/`.
- Least privilege: Notion → one database; mail read-only; no listening ports; `claude -p` with no tools (tier 3: Playwright MCP only).
- Dedicated Chrome profile.

## 13. Repo layout (suggested)
```
JobApplier/
  config.yaml  profile.yaml*  answers_bank.md*
  prompts/ + schemas/
  resume/master.tex*
  jobapplier/
    sources/ freehire.py jobspy_sources.py
    identity.py dedupe.py rank.py db.py notion_sync.py usage.py
    tailor.py latex.py grounding.py
    apply/ base.py fields.py resolve.py tier1_greenhouse.py tier1_lever.py tier1_ashby.py tier2_generic.py tier3_agent.py
    mail/ graph.py imap.py
    llm.py notify.py discover.py apply_worker.py
  launchd/ discover.plist apply.plist watchdog.plist watchdog.sh
  data/* output/* logs/*      (* = git-ignored)
```

## 14. Cost
$0 recurring beyond the Claude subscription. Equivalent LLM cost is tracked and capped (§10).

## 15. Day-one verification checklist
1. School tenant allows `Mail.Read` consent (else switch to Gmail first).
2. `claude -p` from launchd: PATH, Keychain auth, `--bare` behavior, usage-limit error shape, `total_cost_usd` present.
3. Notifications from launchd processes.
4. FreeHire backfill in daily slices within the request budget.
5. LinkedIn: 429 behavior at 100/term from the home IP; how JobSpy reports it; detail-fetch path.
6. Chrome window can open without stealing focus; challenge rate on Greenhouse/Workday with the automation banner.
7. `tectonic` package cache primed.

## 16. Left to the builder
Normalization and regexes; delays, pacing, attempt counts; LaTeX ID mechanism; Notion setup script; `notable_companies` seed; starting value of `daily_llm_cap_usd`; job-independent vs job-specific answer classifier; tests (spike data in the session scratchpad as fixtures).

## 17. Owner answers and open question
Answered (2026-10-31): Workday required · interns only at notable companies · apply to US-remote roles tagged Canada · show new same-title reqs again · keep and apply to French postings · resume rewording/adding/omitting allowed, final PDF to Notion · attempt jobs while away · one overall daily usage cap.

**Open:** the owner's work-authorization answers for Canada and the US (citizen / PR / permit type, sponsorship needed now or later). These go into `profile.yaml`; without them every form escalates to review, and they decide whether US-remote roles are viable.
