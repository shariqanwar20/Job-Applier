"""Ready Notion rows -> applications filled by one agent each in Chrome. The owner submits each by hand."""

import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from jobapplier.apply.agent import describe, run_application
from jobapplier.apply.page import ClickRules
from jobapplier.apply.targets import find_targets
from jobapplier.core.rules import Rules
from jobapplier.notion.client import NotionError
from jobapplier.notion.push import ensure_tailor_schema, mark_submitted

__all__ = ["run_apply"]

_log = logging.getLogger("jobapplier")


class _ClosingError(Exception):
    """The owner pressed Ctrl-C at a prompt and chose to close Chrome."""


def _answer(ask, prompt: str) -> str:
    """The lowercased answer; Ctrl-D is no. Ctrl-C asks whether to close Chrome rather than closing it
    with forms half done; no asks the prompt again."""
    while True:
        try:
            return ask(prompt).strip().lower()
        except EOFError:
            return ""
        except KeyboardInterrupt:
            try:
                close = ask("Close Chrome? [y/N] ").strip().lower() in ("y", "yes")
            except (KeyboardInterrupt, EOFError):
                close = True
            if close:
                raise _ClosingError from None


def _opened_line(t, r: dict, agent_cfg: dict) -> str:
    left = f", {len(r['missing'])} required still empty" if r["missing"] else ""
    how = f"{describe(r['status'], agent_cfg)}; filled {r['filled']} fields{left}, ${r['cost']:.2f}"
    return f"opened {t.job_id} ({t.company}) {t.url}: {how}"


def run_apply(
    conn,
    cfg: dict,
    client,
    rules: Rules,
    values: dict,
    resume: str,
    browser,
    llm_run,
    log_dir: Path,
    job_id: str | None = None,
    ask=input,
    say=print,
    today=date.today,
    prompt: bool = True,
) -> dict:
    """Runs the agent on up to apply.max_tabs ready rows in browser(), one after another, each in its own
    tab, then asks which ones the owner submitted. Without prompt it asks nothing, marks nothing and closes
    Chrome.

    browser is a context manager giving a browser context; it closes Chrome on exit. llm_run is
    llm.run_with_cost without claude and timeout; values is profile_values; resume is the master's text.
    """
    ensure_tailor_schema(client, cfg["notion"]["data_source_id"])
    targets, skipped = find_targets(conn, client, cfg, rules, job_id)
    cap = cfg["apply"]["max_tabs"]
    skipped += [
        f"{t.job_id} ({t.company}): over apply.max_tabs ({cap}); run apply again" for t in targets[cap:]
    ]
    targets = targets[:cap]
    for line in skipped:
        say(f"skipped {line}")
    result = {"opened": 0, "skipped": len(skipped), "submitted": 0}
    if not targets:
        say("Nothing ready to apply.")
        return result
    click_rules = ClickRules.from_config(cfg["apply"])
    with browser() as context:
        for n, t in enumerate(targets):
            # A persistent context opens with one blank tab; the first target goes there. A tab Chrome
            # restored from last time is the owner's: it gets a new tab instead.
            blank = n == 0 and context.pages and context.pages[0].url in ("", "about:blank")
            page = context.pages[0] if blank else context.new_page()
            result["opened"] += 1
            try:
                r = run_application(page, t, cfg, values, resume, llm_run, click_rules, log_dir)
                say(_opened_line(t, r, cfg["apply"]["agent"]))
            except Exception as exc:  # one page must not stop the others; the tab stays for the owner
                _log.warning("apply: %s: %s", t.job_id, exc)
                say(f"opened {t.job_id} ({t.company}) {t.url}: could not prepare it ({exc}); tab left open")
        if not prompt:
            say("--no-prompt: nothing marked Submitted; closing Chrome.")
            return result
        try:
            _answer(ask, "Submit the ones you want in Chrome, then press Enter here.")
            for t in targets:
                if _answer(ask, f"Submitted {t.job_id} ({t.company})? [y/N] ") not in ("y", "yes"):
                    continue
                try:
                    note = f"Submitted {today().isoformat()} via {urlsplit(t.url).hostname}"
                    mark_submitted(client, t.page["id"], note)
                    result["submitted"] += 1
                except NotionError as exc:
                    say(f"{t.job_id}: could not set Status in Notion ({exc}); set it to Submitted yourself")
        except _ClosingError:
            say("Closing Chrome; rows not answered keep their Status.")
    return result
