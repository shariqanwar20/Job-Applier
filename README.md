# JobApplier

Every two hours, JobApplier pulls fresh software developer postings in Canada from FreeHire, Indeed and LinkedIn, drops duplicates and title-filtered noise, ranks what is left (new grad first, then by city), and adds new rows to a Notion database. You review the rows in Notion. Approving a row does nothing yet; the apply steps are not built.

Sources: the FreeHire public API, and LinkedIn and Indeed through [python-jobspy](https://github.com/speedyapply/JobSpy) (list pages only, throttled with a delay between requests).

## Requirements

- macOS (the token is kept in the Keychain and the scheduler is launchd)
- [uv](https://docs.astral.sh/uv/)
- A Notion account

## Setup

1. In Notion, create an internal integration (Settings, Connections, Develop or manage integrations) and copy its token. Create one empty page and share it with the integration (page menu, Connections).
2. Store the token in the Keychain. The command prompts for it, so the token never appears on the command line:

   ```
   security add-generic-password -a "$USER" -s jobapplier-notion -w
   ```

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

The template in `launchd/` is filled in with your repo path and the path to uv, then loaded as a LaunchAgent. Run this from the repo root:

```
mkdir -p logs && sed -e "s|__REPO__|$PWD|g" -e "s|__UV_DIR__|$(dirname "$(command -v uv)")|g" -e "s|__UV__|$(command -v uv)|g" launchd/jobapplier.plist.template > ~/Library/LaunchAgents/local.jobapplier.plist && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.jobapplier.plist
```

It runs once when loaded, then every two hours; a run missed while the Mac slept happens on wake. To remove it:

```
launchctl bootout gui/$(id -u)/local.jobapplier
```

## Logs

Output goes to `logs/jobapplier.log` in the repo.

## Tests

```
uv run pytest
```
