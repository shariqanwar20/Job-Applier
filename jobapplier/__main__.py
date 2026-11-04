"""The one entry point: `python -m jobapplier <discover | tailor | notion-setup | ...>`; see --help."""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from jobapplier import llm
from jobapplier.apply.browser import chrome
from jobapplier.apply.profile import ProfileError, load_profile, profile_values
from jobapplier.config import load_config, load_env
from jobapplier.core import clusters
from jobapplier.core.rules import Rules
from jobapplier.mail.gmail import Gmail, MailError, sign_in
from jobapplier.notion.client import NotionError, make_client
from jobapplier.notion.push import create_database, ready_rows, row_job_id, style_database
from jobapplier.pipelines.apply import run_apply
from jobapplier.pipelines.discover import run_discover
from jobapplier.pipelines.tailor import run_tailor
from jobapplier.sources import build_sources
from jobapplier.system import launchd
from jobapplier.system.lock import locked
from jobapplier.system.notify import notify

__all__ = ["main"]

_ROOT = Path(__file__).resolve().parents[1]
_log = logging.getLogger("jobapplier")
_NO_TOKEN = "Set NOTION_TOKEN in .env (see .env.example)"
_VIEWS_VERSION = "2026-03-11"  # the first Notion version with the views API
_NO_IDS = "Set notion IDs in config.yaml — run: python -m jobapplier notion-setup <page_id>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobapplier")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="fetch fresh jobs and add the new ones to Notion")
    tailor = commands.add_parser("tailor", help="make a tailored resume PDF for each approved row")
    tailor.add_argument("--job", help="tailor only this Job ID (e.g. J27), whatever its Decision")
    setup = commands.add_parser(
        "notion-setup", help="create the database under a Notion page and print the config snippet"
    )
    setup.add_argument("parent_page_id")
    commands.add_parser("mail-login", help="sign in to Gmail (read-only) once in the browser")
    commands.add_parser(
        "run", help="the scheduled step: discover when the last one is old enough, then tailor approved rows"
    )
    apply = commands.add_parser(
        "apply", help="open ready rows in Chrome, pre-filled; you submit, then answer which ones you did"
    )
    apply.add_argument("--job", help="open only this Job ID (e.g. J27), whatever its Status")
    commands.add_parser("install-agent", help="run `run` every 30 minutes via a launchd LaunchAgent")
    commands.add_parser("uninstall-agent", help="remove the LaunchAgent")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    # it logs the sign-in URL (printed anyway) and the redirect, which carries the one-time auth code
    logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
    load_env(_ROOT / ".env")
    if args.command == "notion-setup":
        return _notion_setup(args.parent_page_id)
    if args.command == "mail-login":
        return _mail_login()
    if args.command == "install-agent":
        return _install_agent()
    if args.command == "uninstall-agent":
        launchd.uninstall()
        return 0
    if args.command == "tailor":
        return _tailor(args.job)
    if args.command == "run":
        return _run()
    if args.command == "apply":
        return _apply(args.job)
    return _discover()


def _run() -> int:
    """Discover at most every schedule.discover_every_minutes, then tailor approved rows."""
    stamp = _ROOT / "data" / "last_discover"
    every = load_config(_ROOT)["schedule"]["discover_every_minutes"] * 60
    code = 0
    if not stamp.exists() or time.time() - stamp.stat().st_mtime >= every:
        code = _discover()
        if code == 0:
            stamp.parent.mkdir(exist_ok=True)
            stamp.touch()
    code = _tailor(None) or code
    return _notify_ready() or code


def _notify_ready() -> int:
    """One notice when rows became ready to apply since the last tick. Never opens a browser: the owner
    may be away."""
    try:
        cfg = load_config(_ROOT)
        token, ds_id = os.environ.get("NOTION_TOKEN"), cfg["notion"]["data_source_id"]
        if not (token and ds_id):
            return 0  # tailor already reported the missing setup
        (_ROOT / "data").mkdir(exist_ok=True)
        with locked(str(_ROOT / "data" / "run.lock")) as got:
            if not got:
                return 0
            conn = clusters.connect(str(_ROOT / "data" / "jobs.db"))
            new = clusters.newly_ready(conn, [row_job_id(p) for p in ready_rows(make_client(token), ds_id)])
            conn.commit()
        if new:
            what = f"{len(new)} application{'s' if len(new) > 1 else ''} ready"
            notify("JobApplier", f"{what}: run `uv run python -m jobapplier apply`")
        return 0
    except Exception:
        _log.exception("ready check failed")
        return 1


def _apply(job_id: str | None) -> int:
    """Interactive, so no lock: it only reads SQLite and sets Status in Notion."""
    cfg = load_config(_ROOT)
    token = _notion_token(cfg)
    if token is None:
        return 1
    try:
        values = profile_values(load_profile(_ROOT / "profile.yaml"))
    except ProfileError as exc:
        print(exc)
        return 1
    (_ROOT / "data").mkdir(exist_ok=True)
    conn = clusters.connect(str(_ROOT / "data" / "jobs.db"))
    browser = partial(chrome, _ROOT / cfg["apply"]["browser_profile"])
    try:
        rules = Rules.from_config(cfg)
        # input passed here, not as run_apply's default, so it is looked up when called
        result = run_apply(conn, cfg, make_client(token), rules, values, browser, job_id, ask=input)
    except NotionError as exc:
        print(f"notion: {exc}")
        return 1
    except PlaywrightError as exc:  # most often: Google Chrome is not installed
        print(f"Chrome: {exc}")
        return 1
    print(f"apply done: {result}")
    return 0


def _install_agent() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv not found on PATH; install it first: https://docs.astral.sh/uv/")
        return 1
    try:
        print(
            f"Installed {launchd.install(_ROOT, uv)}; it runs now and every 30 minutes (logs/jobapplier.log)."
        )
    except subprocess.CalledProcessError as exc:
        print(f"launchctl failed: {exc}")
        return 1
    return 0


def _gmail(cfg: dict) -> Gmail | None:
    """Mail is on once `mail-login` has saved a token; Gmail itself signs in lazily, on the first lookup."""
    return Gmail(cfg["mail"], _ROOT) if (_ROOT / cfg["mail"]["token"]).exists() else None


def _mail_login() -> int:
    mail_cfg = load_config(_ROOT)["mail"]
    try:
        sign_in(mail_cfg, _ROOT)
        print(f"Gmail OK: {Gmail(mail_cfg, _ROOT).address()}")
    except MailError as exc:
        print(exc)
        return 1
    except Exception as exc:  # a refused consent or a bad client file; the text can carry the auth code
        print(f"mail: sign-in failed ({type(exc).__name__}); see README, Mail")
        return 1
    return 0


def _notion_setup(parent_page_id: str) -> int:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print(_NO_TOKEN)
        return 1
    title = load_config(_ROOT)["notion"]["title"]
    client = make_client(token)
    database_id, ds_id = create_database(client, parent_page_id, title)
    try:
        style_database(client, make_client(token, notion_version=_VIEWS_VERSION), database_id, ds_id, title)
    except (NotionError, KeyError) as exc:  # KeyError: a reply without the expected fields
        print(f"Could not finish styling the database ({exc}); in Notion, group the view by Rank yourself.")
    print(f"Paste into your config.yaml:\nnotion:\n  database_id: {database_id}\n  data_source_id: {ds_id}")
    return 0


def _notion_token(cfg: dict) -> str | None:
    """The Notion token, or None after reporting what setup is missing."""
    token = os.environ.get("NOTION_TOKEN")
    if not (cfg["notion"]["data_source_id"] and cfg["notion"]["database_id"]):
        problem = _NO_IDS
    elif not token:
        problem = _NO_TOKEN
    else:
        return token
    _log.error(problem)
    notify("JobApplier", problem)
    return None


def _tailor(job_id: str | None) -> int:
    try:
        cfg = load_config(_ROOT)
        token = _notion_token(cfg)
        if token is None:
            return 1
        (_ROOT / "data").mkdir(exist_ok=True)
        with locked(str(_ROOT / "data" / "run.lock")) as got:
            if not got:
                _log.info("another run holds the lock; exiting")
                return 0
            conn = clusters.connect(str(_ROOT / "data" / "jobs.db"))
            llm_run = partial(llm.run, claude=cfg["llm"]["claude"], timeout=cfg["llm"]["timeout_seconds"])
            result = run_tailor(conn, cfg, make_client(token), llm_run, _ROOT, job_id)
        _log.info("tailor done: %s", result)
        if result["errors"]:
            notify("JobApplier: tailor errors", "; ".join(result["errors"]))
            return 1
        return 0
    except Exception as exc:
        _log.exception("tailor failed")
        notify("JobApplier: tailor crashed", str(exc))
        return 1


def _discover() -> int:
    try:
        cfg = load_config(_ROOT)
        token = _notion_token(cfg)
        if token is None:
            return 1
        (_ROOT / "data").mkdir(exist_ok=True)
        with locked(str(_ROOT / "data" / "run.lock")) as got:
            if not got:
                _log.info("another run holds the lock; exiting")
                return 0
            conn = clusters.connect(str(_ROOT / "data" / "jobs.db"))
            client = make_client(token)
            result = run_discover(
                conn, cfg, Rules.from_config(cfg), build_sources(cfg), client, datetime.now(UTC), _gmail(cfg)
            )
        _log.info("discover done: %s", result)
        if result["alert"]:
            notify("JobApplier: discover errors", "; ".join(result["errors"]))
        return 0
    except Exception as exc:
        _log.exception("discover failed")
        notify("JobApplier: discover crashed", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
