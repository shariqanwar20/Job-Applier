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

   It prints a `notion:` block. Paste it into a new file named `config.yaml` in the repo root (the file is git-ignored).
4. In Notion, add an **Inbox** view to the database with the filters `Status is New` and `Decision is not Skip`, sorted by Rank ascending, then First seen descending.

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
