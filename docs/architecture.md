# JobApplier — Architecture (visual)

Draft v6 · 2026-10-31 · Full written spec: [`superpowers/specs/2026-10-31-job-applier-design.md`](superpowers/specs/2026-10-31-job-applier-design.md) (the spec wins if anything here disagrees; its §18 simplifications — one LaunchAgent, fewer states, no backfill — are not yet redrawn here)

A background job on your Mac finds fresh Canadian software postings every 2 hours, de-duplicates and ranks them into Notion, and applies to the ones you approve. Scraping is plain code. Forms are scripted for Greenhouse, Lever and Ashby, and LLM-assisted for Workday and everything else. Claude tailors the resume and writes free-text answers, and usage is tracked in Notion against one daily cap.

---

## 1. Three background jobs

```mermaid
flowchart LR
  subgraph D["Discover · every 2h, at login, on wake"]
    D0["Read your decisions<br/>Notion → SQLite"] --> D1["FreeHire → LinkedIn → Indeed"]
    D1 --> D2["Filter · cluster · resolve ATS ID<br/>location · repost · rank"]
    D2 --> D3["Push new rows to Notion"]
  end
  subgraph A["Apply · every 30 min, kicked by discover, exits if queue empty"]
    A0["Recover: preparing → queued<br/>refilling → awaiting review<br/>submitting → needs you"] --> A1["Up to 5 approved jobs"]
    A1 --> A2["Re-check your decision<br/>tailor · fill · review gate"]
    A2 --> A3["Re-check decision + applied guard<br/>save Submitting · click"]
  end
  W["Watchdog · hourly<br/>no heartbeat in 6h → notify"]
  N[("Notion Jobs DB")]
  S[("SQLite<br/>source of truth")]
  D3 --> N
  N -. "you approve" .-> D0
  A3 --> S
  D2 --> S
  W -. watches .-> D
  W -. watches .-> A
```

| | |
|---|---|
| **SQLite is the source of truth** | Notion is the screen. A Notion glitch can't cause a double application |
| **Separate apply job** | A slow application session never blocks discovery |
| **Watchdog** | A tiny shell script with no Python, so it still alerts you if the main setup breaks |

---

## 2. Where jobs come from

```mermaid
flowchart TD
  F["FreeHire API<br/>Canada + no-location<br/>software · fullstack · backend · frontend · AI · mobile"] --> M{"Match"}
  FC[("FreeHire local cache<br/>rolling 30 days")] -. "repost + ATS ID lookup" .-> M
  LI["LinkedIn via JobSpy<br/>list only"] --> M
  IN["Indeed via JobSpy<br/>description + direct URL"] --> M
  M --> R["Resolve ATS job ID BEFORE showing<br/>direct ATS link beats aggregator link"]
  R -->|"LinkedIn-only, not on Indeed"| D["1 LinkedIn detail fetch<br/>stop on first 429"]
  R --> FD["Full descriptions<br/>FreeHire agent endpoint · 100 per request"]
```

- **Priority FreeHire › LinkedIn › Indeed** for descriptions and details. The **apply link** is chosen separately: a direct Greenhouse/Workday/… link always wins over a whatjobs-style redirect link.
- **Budgets:** FreeHire allows 2,000 requests a day. Matching uses the local cache rather than one call per job.

---

## 3. Filtering, clustering, ranking (in this order)

```mermaid
flowchart TD
  A["New rows"] --> T1{"Title has developer, engineer,<br/>programmer, software, full-stack,<br/>back-end, front-end, SDE, mobile, développeur?"}
  T1 -- no --> Z["drop"]
  T1 -- yes --> T2{"Title has senior, sr, staff, principal,<br/>lead, manager, director, architect, head of?"}
  T2 -- yes --> Z
  T2 -- no --> IT{"Intern or co-op?"}
  IT -- "yes, not a notable company" --> Z
  IT -- "no, or notable company" --> CL["Cluster same company + title within 7 days<br/>keep Toronto, then Vancouver, Montreal,<br/>remote Canada, remote US, other"]
  CL --> ID["Resolve ATS job ID"]
  ID --> LOC{"Location"}
  LOC -- "no location and no Canada match" --> Z
  LOC -- "Remote, United States" --> FL["keep, flag Remote US"]
  LOC -- "Canada" --> RP
  FL --> RP{"Same ATS job ID first seen<br/>by FreeHire over 2 days ago?"}
  RP -- "yes and reposts off" --> Z
  RP -- "no, unknown, or reposts on" --> FD["Fetch full description"]
  FD --> RK["Rank"]
  RK --> R1["1 · New grad, SDE I, Junior, Associate,<br/>level I, notable intern"]
  RK --> R2["2 · Requires under 3 years"]
  RK --> R3["3 · Everything else"]
```

- A company + title match without a matching job ID counts as **Repost unknown** and is kept, so a genuinely new posting at a company that reposts the same title weekly isn't lost.
- **French postings are kept.** The resume stays in English; answers are written in the question's language.
- After downtime, the first-seen window stretches to cover the gap, up to 3 days. Jobs picked up late get a **Late** flag.

---

## 4. Keys, so nothing is applied twice

```mermaid
flowchart LR
  S["Source ID<br/>don't ingest twice"] --> P["ATS + ATS job ID<br/>APPLIED GUARD<br/>resolved before display,<br/>re-checked before the click"]
  P --> K["Company + title<br/>display grouping only"]
  K -. "submitted in last 30 days" .-> SG["Soft guard: still shown,<br/>flagged, forced to review"]
```

---

## 5. What you see in Notion

**Decision** (you): New · Approve · Approve + review · Skip · Submit · Retry (verified) · Applied manually
**Inbox** shows New, Queued, Awaiting review, Needs you and Failed. **History** shows everything. Nothing is deleted.

| Decision | Status | Rank | Title | Company | City | ATS · Tier | Flags | LLM usage |
|---|---|---|---|---|---|---|---|---|
| Approve | ✅ Submitted | 1 | Associate Software Engineer | BlackLine | Toronto | Workday · 2 | – | $0.21 |
| Approve + review | 🟡 Awaiting review | 2 | Software Engineer II, Full Stack | Affirm | Toronto | Greenhouse · 1 | – | $0.08 |
| New | 🔴 Needs you | 2 | Software Engineer, Identity | Lyft | Toronto | Greenhouse · 1 | – | $0.06 |
| New | ⚪ New | 1 | Software Developer Embedded Systems I | Cisco | Kanata | – | Repost unknown | – |

*Example rows with made-up values, to show the shape.* "Needs you" resets Decision to New, so re-approving is a visible change. Each row's page has the final resume PDF, answers, and a screenshot.

**Pipeline status page:** last runs · errors · LinkedIn blocks · FreeHire/Notion requests · mail login state · **LLM usage today and last 7 days by model and purpose · daily cap**.

---

## 6. Life of one job

```mermaid
stateDiagram-v2
  [*] --> New
  New --> Skipped: Skip
  New --> Queued: Approve or Approve + review
  Queued --> Preparing
  Preparing --> Queued: infra error or crash, under 3 tries
  Preparing --> Submitted: already applied page
  Preparing --> Closed: posting gone
  Preparing --> NeedsYou: CAPTCHA, unresolvable link
  Preparing --> AwaitingReview: review row or auto-flagged
  Preparing --> Submitting: approved, decision re-checked
  AwaitingReview --> Refilling: you pick Submit
  Refilling --> AwaitingReview: form changed, or crash before click
  Refilling --> Submitting: frozen answers, no LLM
  Submitting --> Submitted: confirmation seen, or you solved the submit CAPTCHA
  Submitting --> NeedsYou: anything else goes wrong after the click
  NeedsYou --> Queued: Approve if never clicked, else only Retry verified
  Preparing --> Duplicate: real ATS ID already applied
  NeedsYou --> Submitted: you mark Applied manually
  Preparing --> Failed: 3 failed tries
  Failed --> Queued: you Approve
```

- **Point of no return:** *Submitting* is saved before the click and never retried automatically. If something goes wrong after the click, plain Approve is refused. Only **Retry (verified)** works, and only after the pipeline has posted its confirmation-email search result in the row.
- **CAPTCHAs at submit time are common** (Lever, Greenhouse). If you're at the Mac, the window stays open for up to 15 minutes so you can solve it.
- **Your decision is re-read from Notion** right before tailoring and right before submitting, so if you switch a job to Skip mid-run, it isn't sent.
- **Switched to review automatically when:**
  - a sensitive question has no set answer in your profile
  - tier 3 was used
  - a required field couldn't be filled
  - a new question type specific to this job comes up
  - a resume change failed the grounding check
  - you applied to a similar role in the last 30 days

---

## 7. Filling the form: three tiers

```mermaid
flowchart TD
  U["Apply link"] --> W{"Which ATS?"}
  W -- "Greenhouse, Lever, Ashby" --> T1["Tier 1 · Scripted<br/>read full question list<br/>GH questions API / Ashby GraphQL / Lever cards JSON"]
  W -- "Workday, iCIMS, unknown" --> T2["Tier 2 · LLM maps fields<br/>script reads page fields as JSON<br/>1 LLM call per page, script fills"]
  T2 -- stuck --> T3["Tier 3 · LLM drives browser<br/>claude -p + Playwright MCP<br/>always goes to review"]
  T1 --> C{"Each field"}
  T2 --> C
  T3 --> C
  C -- "standard field" --> P1["profile.yaml · no LLM<br/>incl. work history + education"]
  C -- "seen before, job-independent" --> P2["answers bank · no LLM"]
  C -- "new question" --> P3["one batched call per job<br/>Haiku for choices · Sonnet for text"]
  P1 --> SUB["Script verifies fields, screenshots,<br/>re-checks decision + guard,<br/>saves Submitting, clicks submit"]
  P2 --> SUB
  P3 --> SUB
```

- **The LLM never clicks submit.** The script does, after its checks.
- Custom questions are fine: tier 1 reads the company's full question list first, so nothing on the form is unexpected. This was checked against live Greenhouse, Ashby and Lever jobs.
- Your work history and education are generated once from `master.tex` into `profile.yaml` for you to check. Workday's "My Experience" page needs them.
- Answers like "Why this company?" are never reused across companies.
- An **added resume bullet** may only expand or split **one** master bullet, never combine several.

---

## 8. Applying to one job

```mermaid
sequenceDiagram
  participant P as Pipeline
  participant N as Notion
  participant C as Claude (claude -p)
  participant B as Chrome (own profile)
  participant M as Mail (read-only)
  participant Y as You
  P->>N: Decision still Approve?
  P->>B: Resolve link and check posting is open
  P->>C: Master bullet IDs + full description
  C-->>P: Keep / reword / add (cites sources) / omit
  P->>P: Grounding check, render, 1 page
  opt Workday-style account needed
    P->>P: Password saved to the secrets store first
    P->>B: Create account
    P->>M: Wait for verification email
    M-->>P: Code or link
  end
  P->>B: Upload resume first, then fill (tier 1, 2 or 3)
  P->>B: Re-read fields, screenshot
  alt review row
    P->>N: Freeze PDF + answers, Awaiting review
  else approved row
    P->>N: Decision still Approve?
    P->>P: Guard check, save Submitting
    P->>B: Click submit
    alt CAPTCHA and you are active
      P->>Y: Notification, window stays open 15 min
      Y->>B: Solve it
      P->>N: Submitted
    else CAPTCHA and you are away, or any error
      P->>N: Needs you (verify)
    else no challenge
      P->>N: Submitted, confirmation captured
    end
  end
```

---

## 9. Models and usage

| Job | Model |
|---|---|
| Resume tailoring (reword, add, omit; grounded in master bullets) | Sonnet 5.5 |
| Free-text answers | Sonnet 5.5 · Opus medium for hard ones |
| Choice fields, tier 2 field mapping, unclear seniority | Haiku (confirm ID after upgrade) |
| Tier 3 browser driving | Sonnet 5.5 |

**Writing style:** every bullet and answer follows **Humanizer** (no AI-sounding phrasing) and **ASD-STE100** writing rules (short sentences, active voice, one idea per sentence). Both are real Claude Code skills stored in the repo (`.claude/skills/`). Writing calls load them in `claude -p` with only the Skill tool allowed. A quick code check catches long sentences and banned phrases; on failure it retries once, then the row goes to review.

At most 4 jobs start LLM work per hour (adjustable), so the pipeline doesn't use up your plan's 5-hour window in one burst. Every call records its tokens and **equivalent cost** (`total_cost_usd`). That figure isn't billed on your subscription but works as a usage gauge. There's **one overall daily cap**; once it's hit, remaining jobs wait until the next day.

---

## 10. Runtime and security

| | |
|---|---|
| **Locks** | Python `fcntl.flock` + PID. A crashed run can't block later runs |
| **Paths** | launchd has a bare PATH, so the scripts use full paths to `uv`, `claude` and `tectonic` |
| **Secrets** | `.env` (git-ignored): Notion token, mail token. A password per Workday-style account is saved to a secure store before the account is created (store not chosen yet) |
| **Least access** | Notion sees one database · mail is read-only · writing calls: Skill tool only · classification calls: no tools · tier 3: browser only · no ports opened |
| **Chrome** | Dedicated profile. Opens without taking focus where possible. The automation banner is left alone (no evasion) |

**Day-one checks:** school mailbox allows app access (otherwise switch to Gmail before creating any accounts) · `claude -p` from launchd (login, usage-limit error, cost field) · notifications · FreeHire backfill budget · LinkedIn blocking rate · Chrome focus and CAPTCHA rate · tectonic cache.

---

## 11. How it gets built (simplicity first)

Python 3.12 with standard-library `sqlite3` and dataclasses, and a few packages (JobSpy, requests, Playwright, PyYAML, msal). No ORM, no web framework, no task queue. Two small interfaces, **Source** and **ATS adapter**, plus a state machine in one module. Everything else is plain functions. Tests come first for the core logic, using today's spike data.

```mermaid
flowchart LR
  P1["1 · Discover → Notion inbox"] --> P2["2 · Tailored resume PDF"]
  P2 --> P3["3 · Greenhouse end to end"]
  P3 --> P4["4 · Lever + Ashby"]
  P4 --> P5["5 · Workday via tier 2"]
  P5 --> P6["6 · Tier 3, only if needed"]
```

Each slice is usable on its own. Slice 1 already gives you the daily ranked inbox.
