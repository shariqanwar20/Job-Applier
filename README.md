# JobApplier

Every two hours, JobApplier pulls fresh software developer postings in Canada from FreeHire, Indeed and LinkedIn, drops duplicates and title-filtered noise, ranks what is left (new grad first, then by city), and adds new rows to a Notion database. You review the rows in Notion. Approving a row does nothing yet; the apply steps are not built.

Sources: the FreeHire public API, and LinkedIn and Indeed through [python-jobspy](https://github.com/speedyapply/JobSpy) (list pages only, throttled with a delay between requests).

## Requirements

- macOS (the scheduler is launchd)
- [uv](https://docs.astral.sh/uv/)
- A Notion account

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

- Recruiter and staffing postings: the FreeHire query asks for `via_recruiter=false` (`sources.freehire.via_recruiter`), a posting whose FreeHire company type is in `rules.blocked_company_types` (default `outstaff`, `agency`) is dropped, and so is one whose company name contains a word or phrase from `rules.blocked_companies` (a seeded list of staffing firms; whole words, so `dice` does not match "Dicey Labs").
- Non-software "programmer" roles: titles with a word in `rules.exclude` such as `cnc`, `welding` or `plc`.

Lists replace, so to block one more company, copy the whole `blocked_companies` list from `config.default.yaml` into your `config.yaml` and add to it. To let a company through, copy the list without it.

LinkedIn and Indeed answering 429 (rate limited) is retried once after `retry_after_429_seconds` (default 120); a second 429 stops that source for the run.

## Run once by hand

```
uv run python -m jobapplier discover
```

Run it twice to check de-duplication: the second run should report no new rows. The database description in Notion shows the time and errors of the last run.

## Run every two hours

Run this from the repo root. It writes `~/Library/LaunchAgents/local.jobapplier.plist` with your repo path and the path to uv, and loads it as a LaunchAgent:

```
uv run python -m jobapplier install-agent
```

It runs once when loaded, then every two hours; a run missed while the Mac slept happens on wake. Running it again reinstalls the agent. To remove it:

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
