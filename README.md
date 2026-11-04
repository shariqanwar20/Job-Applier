# JobApplier

Every two hours, JobApplier pulls fresh software developer postings in Canada from FreeHire, Indeed and LinkedIn, drops duplicates and title-filtered noise, ranks what is left (new grad first, then by city), and adds new rows to a Notion database. You review the rows in Notion. Every 30 minutes it also makes a one-page tailored resume PDF for each row you approve and attaches it to the row; `apply` then opens each ready application in Chrome, where a Haiku agent fills it, and you submit it yourself.

Sources: the FreeHire public API, and LinkedIn and Indeed through [python-jobspy](https://github.com/speedyapply/JobSpy) (list pages only, throttled with a delay between requests).

## Requirements

- macOS (the scheduler is launchd)
- [uv](https://docs.astral.sh/uv/)
- A Notion account
- For tailoring: [Claude Code](https://claude.com/claude-code) signed in, and [tectonic](https://tectonic-typesetting.github.io/) (`brew install tectonic`)
- For applying: Google Chrome installed (Playwright drives it; no browser download)

## Setup

1. In Notion, create an internal integration (Settings, Connections, Develop or manage integrations) and copy its token. Create one empty page and share it with the integration (page menu, Connections).
2. Put the token in a `.env` file (git-ignored) in the repo root:

   ```
   cp .env.example .env && chmod 600 .env
   ```

   Then edit `.env` so it reads `NOTION_TOKEN=<your token>`. A `NOTION_TOKEN` already set in the environment takes precedence.
3. Create the database under that page. The page ID is the 32-character string at the end of the page URL:

   ```
   uv run python -m jobapplier notion-setup <parent_page_id>
   ```

   It creates an inline database titled "Full Time Role Search" (`notion.title`) directly on that page, with an **Inbox** view that hides rows whose Decision is Skip, sorts by First seen (newest first) and groups by Rank. It then prints a `notion:` block. Paste it into a new file named `config.yaml` in the repo root (the file is git-ignored). If the view cannot be set up, setup says so; group the view by Rank yourself.
4. Notion cannot pre-collapse a group. Click the arrow on the "3 Other" group once to collapse it; Notion remembers that, so ranks 1 and 2 stay in front.

## Configure

Defaults live in `config.default.yaml`. Your `config.yaml` is merged over it: dictionaries merge key by key, lists are replaced whole, and `null` values are ignored. You can change a source's settings but not remove a source. Only put what you want to change in `config.yaml`. For example, to look in different cities and search for different terms:

```yaml
notion:
  database_id: <from setup>
  data_source_id: <from setup>
lookback_days: 7
rules:
  cities:
    - [ottawa, kanata]
    - [calgary, edmonton]
sources:
  linkedin:
    terms: [backend developer, data engineer]
  indeed:
    terms: [backend developer, data engineer]
```

The city lists are tiers, best first; remote and everything else rank after the last tier. Because lists replace, a list you override (such as `cities` or `terms`) must contain every entry you want.

## What gets dropped

- Recruiter and staffing postings: the FreeHire query asks for `via_recruiter=false` (`sources.freehire.via_recruiter`), a posting whose FreeHire company type is in `rules.blocked_company_types` (default `outstaff`, `agency`) is dropped, and so is one whose company name contains a word or phrase from `rules.blocked_companies` (a seeded list of staffing firms; whole words, so `dice` does not match "Dicey Labs" but does match "Dice Health").
- Non-software "programmer" and "developer" roles: titles with a word in `rules.exclude` such as `cnc`, `welding`, `plc`, `cad`, `instructor` or `job developer`.
- Generic recruiter words in a company name (`recruiting`, `recruitment`, `staffing`, `consultancy`, `talent`) are in `rules.blocked_companies` too, so a small agency that is not on the list by name still goes.

Lists replace, so to block one more company, copy the whole `blocked_companies` list from `config.default.yaml` into your `config.yaml` and add to it. To let a company through, copy the list without it.

The same employer under different spellings ("TD", "TD Bank Group", "The Toronto-Dominion Bank (Canada)") is one row. Discover drops any `(...)` text and the words in `rules.company_noise` (`inc`, `group`, `bank`, `canada`, `the`, `du`, ...) from the company name, then looks what is left up in `rules.company_aliases` (normalized name to the name to compare as, e.g. `aws: amazon`). `company_noise` is a list, so it replaces like the others: copy it into your `config.yaml` to change it. `company_aliases` is a map, so your own entries are added to the defaults:

```yaml
rules:
  company_aliases:
    acme labs: acme
```

LinkedIn and Indeed answering 429 (rate limited) is retried once after `retry_after_429_seconds` (default 120); a second 429 stops that source for the run.

## Pruning

Each run also looks at rows it already pushed. A row that today's rules would filter, or that is a repeat of an earlier row for the same employer and title (first seen within 7 days of it), is set to Decision `Skip` with a Flags value of `Filtered` or `Duplicate`, but only while its Decision is still `New`. A row you have decided on is left alone. Skipped rows drop out of the Inbox view, and nothing is deleted.

## Mail

Optional. With Gmail connected, discover marks a new row **Applied before** when your mailbox holds an application confirmation from that company (subject or start of the message has a phrase from `mail.applied_phrases`, within `mail.applied_lookback_days`, default 365). Apply with this Gmail address from now on, so the confirmations land there.

The connection can only read. It asks Google for the single scope `gmail.readonly`, so the saved token cannot send, change or delete mail, whatever the code does. Gmail calls go to `gmail.googleapis.com` and are all GETs (only the token refresh posts, to Google's sign-in server).

1. At console.cloud.google.com, create a project and enable the Gmail API.
2. Under Google Auth Platform, set Audience to External and add yourself as a test user. Leave the app in Testing: Google then ends the sign-in after 7 days, and discover alerts "sign in again" (run `mail-login`, about 10 seconds). Publishing avoids that but needs a public homepage and privacy policy on a domain you own.
3. Under Data access, add the scope `https://www.googleapis.com/auth/gmail.readonly`.
4. Under Clients, create an OAuth client of type Desktop app and download its JSON to `data/gmail_client.json` (`mail.client_secrets`; `data/` is git-ignored).
5. Run `uv run python -m jobapplier mail-login`. It opens your browser for a one-time sign-in, with Google redirecting back to a temporary port on localhost, then saves the token to `data/gmail_token.json` (mode 600) and prints `Gmail OK: <your address>`.

Mail is on whenever the token file exists. If Google rejects the token (revoked, or expired), discover reports `sign in again with mail-login`, pushes the rows without the flag and carries on; run `mail-login` again.

## Tailoring

`tailor` writes a one-page resume for each approved row, from your master resume, and attaches it to the row.

1. Copy `resume/example.master.tex` to `resume/master.tex` and replace the content with your own. Everything in `resume/` except the example is git-ignored.
2. End each line the tailor may change with a LaTeX comment marker: `% id:<id>`, plus ` required` for a line that is always printed. Ids are unique and stable:

   ```latex
   \resumeItem{Built a REST API in Go that served 3 clinics} % id:exp1.b1
   \item \textbf{Languages:} Python, Go, TypeScript % id:skills.lang required
   ```

   The tailor changes the last `{...}` on the line, or the text after the last `}` (here, after the label). Lines without a marker stay as they are. Marked lines on consecutive lines form one list (one job's bullets); the tailor reorders lines only inside their list and keeps at least `tailor.min_lines_per_entry` (default 2) lines in each, putting omitted lines back in master order when needed.
3. Set your name in `config.yaml`; the file is named `First_Last_Company_Role.pdf` in `output/`:

   ```yaml
   profile:
     name: Alex Example
   ```

The flow: in Notion, set a row's Decision to **Approve** (or **Approve + review**), then run

```
uv run python -m jobapplier tailor
```

For each approved row still at Status New, Sonnet (`llm.models.tailor`, through `claude -p` on your Claude Code subscription) picks, orders and rewords your master lines for that job. It may add a line only when it comes from exactly one master line. The PDF is built with tectonic. The model is told to reword or replace a weak line rather than leave it out, and to leave out at most `tailor.max_omit` lines. If the PDF is longer than one page, optional lines are dropped (the last one of the section with the most), up to `tailor.max_drops` times; required lines are never dropped, and no job or project goes below `tailor.min_lines_per_entry` lines. The PDF goes into the row's **Resume** property and Status becomes **Tailored**. `tailor` adds the Resume and Notes properties and the new Status options to an older database the first time it runs; it never removes or renames anything.

Status **Needs review** means: look at the resume before applying. It is set when you chose Approve + review, or when a check failed. Each changed line is checked against its master line. A number (digits or words such as `five` or `thousands`), a capitalized word or technology, or a job-description word outside `tailor.common_words` that is not in the master line, a stronger verb (`led`, `owned`, ... when the master line says `helped`), or LaTeX in the text puts the master line back. The model is asked once more when a line breaks the style rules (no dashes, at most `tailor.max_words` words, none of `tailor.banned_phrases`); a second failure also puts the master line back. The row's **Notes** says which lines and why. A row that fails outright (Claude or tectonic error) stays at New and the next run retries it.

Writing style: the model must use two Claude Code skills that ship in the repo, `.claude/skills/humanizer` (removes AI-writing patterns) and `.claude/skills/asd-ste100` (short, plain sentences in the style of Simplified Technical English). The call loads project settings and may use only the Skill tool.

To try one row, whatever its Decision: `uv run python -m jobapplier tailor --job J27`.

## Apply

`apply` opens each ready row's application page in Chrome and lets one Haiku agent fill it, page by page. **It never clicks Submit, never creates an account and never types a password.** These rules are in code, not only in the prompt. You check the form and submit it.

1. Copy `profile.example.yaml` to `profile.yaml` (git-ignored) and put your answers in: name, email, phone, location, links, work authorization, start date, relocation, salary and `voluntary_self_id`.
2. In Notion, approve a row. The PDF appears and Status becomes **Tailored** or **Needs review**.
3. For Needs review, read the PDF, then set Status to **Reviewed**.
4. Run it, check each tab, submit the ones you want, then answer `y` for each one you submitted:

   ```
   uv run python -m jobapplier apply
   ```

   `y` sets Status to **Submitted** and writes "Submitted <date> via <host>" in Notes; `N` leaves the row as it was. Chrome closes after the last answer. Ctrl-C at a prompt asks "Close Chrome? [y/N]"; N goes back to the prompt. `--job J27` opens one row whatever its Status.

Rows with Status Tailored or Reviewed open, at most `apply.max_tabs` (8) per run, one after another. The command prints what it opened and what it skipped, and why: a row whose only links are job boards (`rules.aggregator_hosts`; paste the employer's posting into the row's Link and run again), or a row with no tailored PDF on record (the PDF `tailor` made, as recorded in `data/jobs.db`; a row tailored before `apply` existed needs `tailor --job J27` once more). A Lever, Ashby, Breezy or SmartRecruiters job page is turned into its application page; a Greenhouse page holds the form already.

### How a tab is filled

Each application runs its own loop in its own tab, with no shared conversation. Each step:

1. Python reads the page into a compact JSON snapshot: URL, title, headings, error messages, and every visible field, button and link with a ref. Fields from same-origin frames are included; a cross-origin frame is only named. The snapshot is cut at `apply.agent.max_snapshot_chars`. Hidden fields and honeypots are left out: a field hidden by CSS (also through a parent), moved off the page, clipped to nothing, or inside `aria-hidden`.
2. It asks `llm.models.apply` (Haiku) through `claude -p`. The call loads no tools or skills. The prompt holds your `profile.yaml`, the text of your `resume/master.tex` lines, the job (title, company, the first `apply.agent.description_chars` characters of the description), the steps so far with every refusal, and the snapshot. The model returns actions (`fill`, `select`, `check`, `uncheck`, `upload`, `click` on a ref), a status and a note. The prompt tells it to:
   - start the application on a job page;
   - upload the resume first;
   - answer free-text questions briefly from your resume and profile, never inventing experience;
   - decline the voluntary self-identification questions;
   - tick required consent boxes but never opt-ins;
   - click Next;
   - stop when only submitting is left, and at a sign-in, account or email-verification step.
3. Python does each action after reading the element again. Code refuses:
   - any typing or ticking in a password field or in a form that has one;
   - an upload of anything but the tailored PDF (the model never names a file);
   - a click on a control when any source of its name (aria-label, labelled-by text, text, value, title) has a word starting with one of `apply.submit_stems` (submit, send, finish, final, complete, confirm, done, post), a word from `apply.sign_in_words` (sign in, create account, register, Continue with Google, Apply with LinkedIn), or a non-Latin look-alike letter;
   - an unnamed control;
   - an `apply.apply_words` control ("Apply", "Apply now", "Start application") once anything is filled, so the job page's start button works but a form's final Apply never does;
   - a link, or a button that submits its form, unless its text starts with an `apply.next_words` phrase (Next, Continue, Save & Continue) with at most `apply.next_max_extra_words` more words, none of them in `apply.next_deny_words`.

   The click is bound to the text it was checked with, so Chrome checks the name once more. There is no Enter key, no form submit and no keyboard or mouse call in the apply code; a test checks the source for them. A refusal goes back to the model in the next step.
4. It waits for the page to settle (`apply.agent.settle_seconds`, plus `apply.parser_wait_seconds` after the upload) and takes the next snapshot.

The loop stops when:

- the model says the form is ready to submit, needs an account, or needs you;
- a password field appears;
- it reaches `apply.agent.max_steps` (25) steps;
- the page is the same for `apply.agent.unchanged_steps` (3) snapshots, usually a validation error;
- its calls cost `apply.agent.max_cost_usd` ($0.50);
- the model call fails;
- the page breaks.

A note at the top of the page then gives:

- why it stopped and the model's note;
- what it filled, and whether the resume was uploaded;
- **every free-text answer the model wrote**, for you to check;
- the required fields still empty.

Click the note to close it. Use the form's Back button to check the earlier pages. The terminal line for each row says the same in short, with the cost.

### Debugging

Each application leaves two files in `logs/`:

- `apply-<JobID>.json`, a trace. For every step it holds the snapshot's URL and element count, the actions the model asked for, what was done, refused or failed, the status, the note and the cost. It has the model's answers but never your profile or resume.
- `apply-<JobID>-<step>.png`, a full-page screenshot of the page where the loop stopped.

The log also has one line per step, with its actions and cost; answers longer than 80 characters are shown by length only. `apply --no-prompt` asks nothing and marks nothing Submitted, and it closes Chrome once every loop has finished. Use it to try the agent on a row.

The scheduled `run` never opens a browser. When rows become ready, it sends one notification: "N applications ready: run `uv run python -m jobapplier apply`".

### Logins

At a sign-in or account step (a password field, or the model says so), the loop stops and its note says to sign in yourself. Chrome uses its own profile in `data/browser-profile`, kept between runs, so a sign-in you do there stays. Run `apply` again after signing in.

- To install the 1Password extension in that profile once, run `apply` with any row and open the Chrome Web Store in a tab.
- On Workday-style sign-in or sign-up pages, use 1Password's autofill or password generator yourself.

## Run once by hand

```
uv run python -m jobapplier discover
```

Run it twice to check de-duplication: the second run should report no new rows. The database description in Notion shows the time and errors of the last run.

## Run on a schedule

Run this from the repo root. It writes `~/Library/LaunchAgents/local.jobapplier.plist` with your repo path and the path to uv, and loads it as a LaunchAgent:

```
uv run python -m jobapplier install-agent
```

The agent runs `python -m jobapplier run` once when loaded, then every 30 minutes: discover when the last discover is older than `schedule.discover_every_minutes` (120), then tailor approved rows, then notify you when rows became ready to apply. A run missed while the Mac slept happens on wake. Logs go to `logs/jobapplier.log`. Running it again reinstalls the agent. To remove it:

```
uv run python -m jobapplier uninstall-agent
```

`uv` must be on your PATH when you run `install-agent` (the agent remembers its location). The first scheduled run needs network access, because uv syncs the dependencies before it starts.

## Logs

Output goes to `logs/jobapplier.log` in the repo.

## Tests

```
uv run pytest
```

## Conventions

- PEP 8 naming, checked by ruff: `uv run ruff check` and `uv run ruff format` must pass.
- A module's `__all__` is its public API: the names other modules import.
- Every other module-level name starts with `_` and is private to its module.
- Tests mirror the package folders (`jobapplier/core/rules.py` is tested in `tests/core/test_rules.py`).
