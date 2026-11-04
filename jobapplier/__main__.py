"""The one entry point: `python -m jobapplier <discover | notion-setup | mail-login | ...>`; see --help."""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from jobapplier.config import load_config, load_env
from jobapplier.core import clusters
from jobapplier.core.rules import Rules
from jobapplier.mail.gmail import Gmail, MailError, sign_in
from jobapplier.notion.client import NotionError, make_client
from jobapplier.notion.push import create_database, style_database
from jobapplier.pipelines.discover import run_discover
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
    setup = commands.add_parser(
        "notion-setup", help="create the database under a Notion page and print the config snippet"
    )
    setup.add_argument("parent_page_id")
    commands.add_parser("mail-login", help="sign in to Gmail (read-only) once in the browser")
    commands.add_parser("install-agent", help="run discover every two hours via a launchd LaunchAgent")
    commands.add_parser("uninstall-agent", help="remove the LaunchAgent")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
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
    return _discover()


def _install_agent() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv not found on PATH; install it first: https://docs.astral.sh/uv/")
        return 1
    try:
        print(f"Installed {launchd.install(_ROOT, uv)}; discover runs now and every two hours.")
    except subprocess.CalledProcessError as exc:
        print(f"launchctl failed: {exc}")
        return 1
    return 0


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


def _discover() -> int:
    try:
        cfg = load_config(_ROOT)
        token = os.environ.get("NOTION_TOKEN")
        if not (cfg["notion"]["data_source_id"] and cfg["notion"]["database_id"]):
            problem = _NO_IDS
        elif not token:
            problem = _NO_TOKEN
        else:
            problem = None
        if problem:
            _log.error(problem)
            notify("JobApplier", problem)
            return 1
        (_ROOT / "data").mkdir(exist_ok=True)
        with locked(str(_ROOT / "data" / "run.lock")) as got:
            if not got:
                _log.info("another run holds the lock; exiting")
                return 0
            conn = clusters.connect(str(_ROOT / "data" / "jobs.db"))
            client = make_client(token)
            result = run_discover(
                conn, cfg, Rules.from_config(cfg), build_sources(cfg), client, datetime.now(UTC)
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
