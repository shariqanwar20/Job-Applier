# JobApplier — Design

Date: 2026-10-31
Status: Approved v7 (three design reviews + one simplicity review; owner answers in §17). **§18 overrides earlier sections where they disagree.**

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

apply agent (every 30 min, exits immediately if the queue is empty; discover also triggers it via `launchctl kickstart`)
  0. recover()                        `preparing` → queued (attempt+1) · `refilling` → awaiting_review · `submitting` → needs_you(verify)
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
- In the apply phase identity is re-derived after following the link and persisted. If it collides with an identity that already reached `submitting`/`submitted`, the row is set to `Duplicate` and stops. The guard is checked again immediately before writing `submitting`.

### 6.3 Applied guard
- A posting identity that reached `submitting` or `submitted`, or was marked "Applied manually", is never prepared again automatically.
- For an identity that reached `submitting`, plain **Approve is rejected** (row stays `needs_you`, note "use Retry (verified)").
- The only override is Decision = **Retry (verified)**. It is accepted only after the pipeline has written the mailbox confirmation-search result into the row's Notes, and it runs the "already applied" liveness check before anything else. An audit note is written. The pipeline never clicks submit twice on its own.
- **Soft guard:** if the same company + normalized title was submitted in the last 30 days, a new posting is still shown (owner: new reqs with the same title should show again) but flagged `Applied to similar on <date>` and forced to review.

### 6.4 Clustering (owner: city preference)
- Rows with the same cluster key first seen within 7 days of each other form one Notion row. Representative city: **Toronto (GTA) > Vancouver > Montreal > remote-Canada > remote-US > other** (builder's call on the remote ranks). Others listed in `Alt locations`.
- If a cluster holds several posting identities (e.g. Revvity: 4 Workday reqs with the same title), the row lists them; the **apply target is set when the row is first shown** (preferred city) and is the URL in the Link column. While Status is New or Queued, the target may be **upgraded** from an aggregator link to a direct ATS link for the same posting found in a later run; never after preparation starts. Siblings are marked `sibling_of_applied` once the target is submitted.
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
| **Decision** | select: New / Approve / Approve + review / Skip / Submit / Retry (verified) / Applied manually | **owner** (pipeline resets to New, see below) |
| Status | select: New / Queued / Preparing / Awaiting review / Refilling / Submitting / Submitted / Needs you / Failed / Skipped / Closed / Duplicate | pipeline |
| LLM usage | number (equivalent $ for this job) | pipeline |
| Notes | text | either |

- Views: **Inbox** = Status ∈ {New, Queued, Awaiting review, Needs you, Failed}; **History** = all.
- Decision handling:
  - Skip → Status Skipped. Applied manually → Status Submitted and guard set. Retry (verified) → back to Queued under the rules in §6.3.
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
preparing ─► closed | needs_you | failed | queued (infra error, attempts < 3) | submitted ("already applied" page)
refilling ─► awaiting_review (form changed since review)
startup: preparing ─► queued · refilling ─► awaiting_review · submitting ─► needs_you (verify)
needs_you/failed (never clicked) ─Approve─► queued
needs_you (after click) ─Retry (verified)─► queued      any ─Applied manually─► submitted
apply-phase identity collides with an applied posting ─► duplicate
```
- **Decision re-check:** right before tailoring and right before writing `submitting`, the row's Decision is re-read from Notion (one GET); if it is no longer Approve / Approve + review / Submit, abort and set the status accordingly.
- **Point of no return:** `submitting` is committed to SQLite immediately before the click. **Submit-time challenges** (common: Lever loads hCaptcha/reCAPTCHA, Greenhouse uses invisible reCAPTCHA on submit) follow §8.5: if the owner is active, keep the window open, notify, and wait ≤15 min for the owner to solve it, then capture the confirmation. Any other problem after the click (crash, sleep, owner away, validation error) → `needs_you` with "verify"; the pipeline searches the mailbox (read-only) for a confirmation email to help. Only the owner moves it on (Submitted, Applied manually, or Retry).
- **Infra failures** (timeout, browser died, sleep, usage limit mid-call) return the job to `queued`, up to 3 attempts, then `failed`.
- **Auto-escalation to review:** a sensitive question with no deterministic answer in `profile.yaml`; no adapter and tier 3 used; required field couldn't be filled; a new job-specific question category; resume grounding check failed; `Applied to similar` flag.
- **Frozen review artifacts:** at `awaiting_review` store resume PDF, answers, field→value map, apply URL, and a hash of the form's question set. Submit pass (`refilling`) reuses them verbatim with no LLM calls; a different question set returns the row to `awaiting_review` with the diff.

### 8.2 Liveness
Per-adapter content check ("no longer accepting applications", redirect to board root, 404), since Greenhouse/Workday return 200 for closed jobs. FreeHire `closed_at` is a hint only. "You've already applied" page → `submitted`.

### 8.3 Resume tailoring (owner: LaTeX + Sonnet 5.5; rewording, adding, omitting allowed)
- `resume/master.tex` is the source of truth; each bullet/project/skill line carries a stable ID.
- Sonnet returns JSON (`--json-schema`): ordered items, each `{id}` (unchanged), `{id, text}` (reworded), or `{new: text, source: id}` (added). **An added item derives from exactly one master item** (a split or expansion of that bullet), never a combination of several, so it can't fabricate by recombining facts **(builder's call)**.
- **Writing style (owner): Humanizer + ASD-STE100.** See §8.7.
- **Only master IDs may be sources.** `answers_bank.md` is for form answers, never resume content.
- **Grounding validator:** rejects any number, percentage, technology, employer, or title not in the cited master items, and scope-inflating verb swaps (deny-map: contributed/assisted/helped/supported → led/owned/architected/spearheaded/drove). The validator applies equally to reworded and added items. Rejected items revert to master text and the row is escalated to review.
- One-page fit: if longer than one page, drop the lowest-ranked optional items and recompile; never drop below the master's required items (floor), never re-prompt.
- All LLM text is LaTeX-escaped; raw backslashes/braces rejected. Compile with `tectonic`; check pages with `pdfinfo`.
- Filename **(owner)**: `First_Last_Company_Role.pdf` (spaces → `_`, punctuation/parentheticals removed, role ≤ ~6 words, collision → short job ID). Final PDF uploaded to the Notion row **(owner)**.

### 8.4 Form filling — three tiers
Every tier first reads the **full list of fields** (label, type, required, options), then classifies each field into:
1. **Standard field** (name, preferred name, pronouns, email, phone, location/province, resume (always the file field, never pasted text), LinkedIn/GitHub/website, EEO/self-ID, work authorization, sponsorship now/future, previously employed here, "how did you hear", notice period, relocation, salary expectation, data/GDPR consent checkboxes, **structured work history and education**) → value from `profile.yaml`; no LLM. Work history and education are generated once from `master.tex` into `profile.yaml` and reviewed by the owner (Workday's "My Experience" page needs them) **(builder's call)**. `profile.yaml` holds deterministic answers for all of these, including "decline to answer" EEO defaults, so they don't force review.
2. **Known question** → matched by normalized text against the answers bank; reused with no LLM. **Only job-independent answers are reusable** (each saved answer is classified job-independent vs job-specific when written; job-specific ones like "Why this company?" are regenerated per job).
3. **New question** → one batched LLM call per job: Haiku for choice fields (must return one of the given options), Sonnet for free text (Opus medium for hard ones). Job-independent answers are saved to the bank.

| Tier | ATS | How fields are read | Who fills |
|---|---|---|---|
| **1 Scripted** | Greenhouse, Lever, Ashby | Greenhouse: public API `boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true` (verified: returns custom `questions`, `demographic_questions`, `data_compliance`, `location_questions`). Ashby: `POST jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting` → `jobPosting.applicationForm.sections[].fieldEntries[]` (verified; the page itself is an SPA without the form), or intercept that request in Playwright; `api.ashbyhq.com/posting-api/job-board/{org}` for listing/liveness. Lever: `cards[<uuid>][fieldN]` inputs, each card with a hidden `baseTemplate` JSON describing its questions (verified). | Script |
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
- Visible widget or submit-time challenge, owner active: pause with the window open, notify, Notion `needs_you`, poll ≤15 min, continue when solved (before or after the submit click).
- Owner away (no input >10 min via `ioreg` HIDIdleTime) **(owner: still attempt jobs)**: straight to `needs_you` with URL + PDF; close window; next job.
- Invisible challenges blocking submission → `needs_you` immediately.
- No automatic retries; the owner re-approves.

### 8.6 Email codes and links (owner: school mailbox, Gmail fallback)
- **Day one, before any application:** try delegated `Mail.Read` with device-code sign-in. If admin consent is required → switch to Gmail (IMAP app password in Keychain, `EXAMINE` read-only) **before** any ATS account exists.
- Poll messages from the adapter's known sender domains (`greenhouse-mail.io`, `myworkday.com`, `ashbyhq.com`, …) received after the action, every few seconds for ~3 min. Extract a 4–8 digit code near "code/verification" keywords, or a verification link on the ATS domain.
- Never modify, delete, or mark read. Token refresh failure → status page "mail auth expired" + notification; affected jobs `needs_you`.

### 8.7 Writing style for bullets and answers (owner)
- Every resume rewording/addition and every free-text answer follows two rule sets: **Humanizer** (remove AI-writing patterns: filler, inflated vocabulary, "not X but Y", rule-of-three padding, em-dash asides, stock openers/closers) and **ASD-STE100 Simplified Technical English** writing rules (short sentences — about 20 words max, active voice, one idea per sentence, plain verbs, no ambiguous pronouns, consistent terms). The STE dictionary is not enforced (it would remove normal technical vocabulary) **(builder's call)**.
- **Used as real Claude Code skills (owner).** Both skills live in the repo at `.claude/skills/humanizer/` and `.claude/skills/asd-ste100/` (project-scoped, versioned with the code). Writing calls (tailoring, free-text answers) load project settings and may use only the Skill tool (§9). Source: the owner's copies if provided; otherwise Humanizer from its public repo and a builder-written STE100 skill summarising the writing rules (not copied from the standard).
- A cheap code check after generation: sentence length, banned-phrase list, no em-dashes. A failure triggers one regeneration; a second failure escalates the row to review.

## 9. LLM calls (owner: Claude Code subscription, headless)

| Task | Model |
|---|---|
| Resume tailoring | Sonnet 5.5 |
| Free-text answers | Sonnet 5.5 (Opus medium for hard ones) |
| Choice fields, field→profile mapping (tier 2), seniority when unclear | Haiku (owner says Haiku 5.5; confirm ID after the Claude Code upgrade) |
| Tier 3 browser driving | Sonnet 5.5 |

Invocation, run from the repo root:
```
# writing calls (tailoring, free-text answers): skills on
/opt/homebrew/bin/claude -p --model <id> --output-format json --json-schema <file> \
  --setting-sources project --tools "Skill" --strict-mcp-config --no-session-persistence

# classification calls (choice fields, field mapping, seniority): nothing loaded
/opt/homebrew/bin/claude -p --model <id> --output-format json --json-schema <file> \
  --setting-sources "" --tools "" --strict-mcp-config --no-session-persistence
```
- Writing prompts name the two skills explicitly. `.claude/settings.json` in the repo stays minimal (no hooks, no MCP servers), so loading project settings brings in the skills and nothing else. The project `CLAUDE.md`, if any, is kept short because it also loads.
- Skill use adds a tool turn per call (more tokens). Day one: confirm both skills are invoked in `-p` (check the JSON transcript) and measure the extra usage.
Tier 3 adds `--mcp-config <playwright-only.json>` and allows only that server's tools. Verify on day one whether `--bare` still authenticates from launchd; use it only if it does. Model IDs live in `config.yaml`. Stay on the CLI; don't extract the OAuth token.

## 10. Usage tracking and the daily cap

- Every `claude -p` result's JSON (model, input/output tokens, duration, `total_cost_usd`) is logged in SQLite with job ID and purpose (tailor / answer / field-map / tier3 / classify). On a subscription `total_cost_usd` is not billed; it is an **equivalent-cost gauge**.
- Notion: per-row **LLM usage**; status page shows today and the last 7 days by model and purpose, plus each time a plan limit was hit.
- **One overall daily cap (owner):** `config.yaml: daily_llm_cap_usd` (equivalent cost, all purposes together). The owner is on the $100/month plan; the builder sets a conservative starting cap and tunes it from the first week's usage data. Opus is used only for answers the Sonnet draft fails the style/grounding checks on twice. When reached, remaining jobs stay `queued` until the next day; the status page says so.
- **Pacing (builder's call, within the one cap):** at most `max_llm_jobs_per_hour` jobs (default 4) start LLM work per hour, so the pipeline doesn't use up the plan's 5-hour window in one burst; optional `quiet_hours` window preferred for apply runs.
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
- Least privilege: Notion → one database; mail read-only; no listening ports; `claude -p` writing calls allow only the Skill tool, classification calls no tools, tier 3 the Playwright MCP only.
- Dedicated Chrome profile.

## 12a. Engineering principles (owner: simplicity)
- **Python 3.12**, light runtime: stdlib `sqlite3`, `dataclasses`, `logging`, `subprocess`; third-party only `python-jobspy`, `requests`, `playwright`, `pyyaml`, `msal` (Graph). Notion via plain HTTP, no SDK needed. No ORM, web framework, task queue, or async unless a step proves it needs it.
- **Few, small patterns:** one `Source` interface (fetch → rows) and one `AtsAdapter` interface (read_fields, fill, submit) with a class per source/ATS; the job state machine in one module (a transition table, no framework); everything else plain functions. No plugin system, no DI container.
- **Test-driven** (pytest): tests first for the pure core — normalization, identity, clustering, ranking, state transitions, grounding validator, field classification — using the spike data as fixtures. Notion, `claude -p`, mail, and the browser sit behind thin wrappers with fakes in tests.
- **Build in thin, usable slices**, each useful on its own:
  1. Discover → dedupe → rank → Notion inbox (no applying).
  2. Resume tailoring + PDF in Notion for approved rows.
  3. Greenhouse tier 1 end to end, including the state machine, guard, email codes, CAPTCHA handoff.
  4. Lever and Ashby tier 1.
  5. Workday via tier 2.
  6. Tier 3, only if tier 2's failure rate shows it is needed.
- When a design detail turns out more complex than its value in practice, the builder simplifies and records why.

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
2. `claude -p` from launchd: PATH, Keychain auth, `--bare` behavior, usage-limit error shape, `total_cost_usd` present, **both project skills invoked** with `--setting-sources project --tools Skill`.
3. Notifications from launchd processes.
4. FreeHire backfill in daily slices within the request budget.
5. LinkedIn: 429 behavior at 100/term from the home IP; how JobSpy reports it; detail-fetch path.
6. Chrome window can open without stealing focus; submit-time challenge rate on Lever/Greenhouse/Workday with the automation banner (decides how often unattended submits end in `needs_you`).
7. `tectonic` package cache primed.

## 16. Left to the builder
Normalization and regexes; delays, pacing, attempt counts; LaTeX ID mechanism; Notion setup script; `notable_companies` seed; starting value of `daily_llm_cap_usd`; job-independent vs job-specific answer classifier; tests (spike data in the session scratchpad as fixtures).

## 17. Owner answers
Answered (2026-10-31): Workday required · interns only at notable companies · apply to US-remote roles tagged Canada · show new same-title reqs again · keep and apply to French postings · resume rewording/adding/omitting allowed, final PDF to Notion · attempt jobs while away · one overall daily usage cap.

Work authorization (owner): **Canada — work permit, no sponsorship needed. US — sponsorship required.** Stored in `profile.yaml` as deterministic answers. US-remote rows still get applied to (owner) but are flagged `Remote US` so the owner knows sponsorship will be needed.

Plan (owner): $100/month Claude plan.

Writing style (owner): Humanizer + ASD-STE100 as real Claude Code skills, loaded in headless writing calls (§8.7, §9).

Engineering (owner): Python, light runtime, simple design patterns, test-driven, simplicity first (§12a).

## 18. Simplifications (simplicity review, 2026-10-31)

A ponytail-style review cut mechanisms that real usage has not yet shown we need. Each line overrides the section it names. Revisit an item only when the "until" condition is observed.

1. §5.1 No 30-day backfill and no rolling cache of every FreeHire row. Only rows that pass the title filter are stored. Removes §15 item 4.
2. §5.1 No per-day request counter or `X-RateLimit-Remaining` logic (a run makes a few requests). On a 429, stop FreeHire for that run and record the error.
3. §5.3 Fixed lookback `lookback_days` (default 3) for every source; no "time since last run" and no `Late` flag. Source-identity dedupe drops the overlap; the Posted column shows age.
4. §5.4 Repost = the row's posting identity is already in SQLite on a cluster first seen more than 48 h earlier. Off: the row joins that cluster silently. On: a new row flagged `Repost`. No `Repost unknown` flag. FreeHire's `reality.repost_count` is not used (it counts cross-board copies).
5. §5.1 One FreeHire request type: `/agent/jobs/search` with `countries=ca&regions=none` (regions joins the country OR-group, so one query returns Canada and no-location rows, with full descriptions).
6. §6.2 No aggregator landing-page parse and §6.4 no target upgrade: the Link is fixed when the row is first shown; rows without a direct ATS link get `Aggregator link`. Until aggregator rows prove common.
7. §6.4 No `sibling_of_applied` mark. §6.1 no `canonical_key_version`.
8. §7 Notion sync keeps only query-by-Job-ID before create. No duplicate-page detection (two pages for one Job ID map to one SQLite row; the guard lives in SQLite).
9. §6.5 Unclear years → rank 3 (no Haiku call). Slice 1 has no `claude -p` at all.
10. §4/§11 **One LaunchAgent** every 30 min (plus RunAtLoad; launchd coalesces intervals missed during sleep into one run on wake). Each run: discover if the last discover is older than 2 h, then up to `max_jobs_per_run` (default 2) queued jobs. One plist, one lock. No `kickstart`. Until slice 3 the run is discover-only.
11. §10 No `max_llm_jobs_per_hour` or `quiet_hours`: `max_jobs_per_run` × the 30-min schedule limits the rate, plus the one daily cap.
12. §8.1 States: `failed` merges into `needs_you`; `skipped`, `closed` and `duplicate` merge into `closed` (reason in Notes); `refilling` merges into `preparing` (recovery: a preparing row with frozen review artifacts → `awaiting_review`, else → `queued`). Status values: New, Queued, Preparing, Awaiting review, Submitting, Submitted, Needs you, Closed.
13. §8.7 vs §10: a second style/grounding failure goes to review. Free-text answers use Sonnet; `answer_model` in config can be switched to Opus by the owner. No automatic Opus escalation.
14. §6.3 Soft guard keeps the `Applied to similar` flag (set when the row is shown) but no forced review; built in slice 3.
15. §4 Watchdog stays (only thing that notices Python/uv failing to start) but without the "Mac awake" check; built in slice 3.
16. §7/§10 Status: the database description shows last run, last error, row counts, usage today, plan-limit hits. Request counters and per-model tables stay in SQLite.
17. §9/§15 No `--bare` check.
18. §7 Notion views are set up by hand once (the API does not manage views). Notion API version `2025-09-03` (data-source endpoints).

Slice 1 builds: FreeHire + Indeed + LinkedIn (list only, 4–8 s jitter, stop on first 429) → title/intern filters → identity → 7-day clustering with city preference → `regions=none` rule → repost rule → rank (regex, then `enrichment.seniority`, else 3) → create-only Notion push → status in the database description → osascript notification on error → one LaunchAgent with an `fcntl` lock and `caffeinate -i`. Not in slice 1: Notion→SQLite pull sync, any state past `new`, `claude -p`, usage tracking, LinkedIn detail fetch, page-body uploads, mail, watchdog.

## 19. Decisions made while building slice 1 (owner, 2026-11-01)
These override §3, §12, §12a and §13 where they disagree.
- **Config:** tunables (title words, intern words, rank-1 words, city tiers, notable companies, aggregator hosts, each source's terms/location/country/categories/delays, lookback) live in `config.default.yaml`. Users override in a git-ignored `config.yaml`, deep-merged: dicts merge, lists replace, nulls are ignored. The project will be released publicly, so no personal values are committed.
- **Secrets:** `.env` (git-ignored; `.env.example` committed) instead of macOS Keychain, for the Notion token and later tokens.
- **Layout:** one entry point, `python -m jobapplier <discover|notion-setup|install-agent|uninstall-agent>` (`jobapplier/__main__.py`). The package folders are:
  - `core/rules.py`, `core/identity.py`, `core/rank.py`, and `core/clusters.py`, which holds all SQL.
  - `sources/`: one `Source` ABC and a class per source.
  - `notion/`: client and push.
  - `system/`: lock, notify, launchd.
  - `pipelines/discover.py`.
  - `http.py`.
- **API callers:** Notion uses the `notion-client` SDK (3.1.0, API `2025-09-03`). Plain HTTP sources share `http.ApiClient` (requests + urllib3 `Retry`).
- **Standard:** PEP 8 naming enforced with ruff. Each module's public API is in `__all__`, and module-private names start with `_`. Tests mirror the package folders.
- **Scheduling:** `install-agent` writes the LaunchAgent with `plistlib` (every 2 h + RunAtLoad). There is no hand-edited plist.
- **Schema migrations:** not adopted yet (owner: skip).
