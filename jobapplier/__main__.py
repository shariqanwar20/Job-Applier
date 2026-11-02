"""The one entry point: `python -m jobapplier <discover | notion-setup | install-agent | uninstall-agent>`."""
import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from jobapplier.config import load_config, load_env
from jobapplier.core import clusters
from jobapplier.core.rules import Rules
from jobapplier.notion.client import Notion
from jobapplier.notion.push import create_database
from jobapplier.pipelines.discover import run_discover
from jobapplier.sources import build_sources
from jobapplier.system import launchd
from jobapplier.system.lock import locked
from jobapplier.system.notify import notify

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("jobapplier")
NO_TOKEN = "Set NOTION_TOKEN in .env (see .env.example)"
NO_IDS = "Set notion IDs in config.yaml — run: python -m jobapplier notion-setup <page_id>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobapplier")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="fetch fresh jobs and add the new ones to Notion")
    setup = commands.add_parser("notion-setup", help="create the Jobs database under a Notion page and print the config snippet")
    setup.add_argument("parent_page_id")
    commands.add_parser("install-agent", help="run discover every two hours via a launchd LaunchAgent")
    commands.add_parser("uninstall-agent", help="remove the LaunchAgent")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(ROOT / ".env")
    if args.command == "notion-setup":
        return notion_setup(args.parent_page_id)
    if args.command == "install-agent":
        return install_agent()
    if args.command == "uninstall-agent":
        launchd.uninstall()
        return 0
    return discover()


def install_agent() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv not found on PATH; install it first: https://docs.astral.sh/uv/")
        return 1
    try:
        print(f"Installed {launchd.install(ROOT, uv)}; discover runs now and every two hours.")
    except subprocess.CalledProcessError as exc:
        print(f"launchctl failed: {exc}")
        return 1
    return 0


def notion_setup(parent_page_id: str) -> int:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print(NO_TOKEN)
        return 1
    database_id, ds_id = create_database(Notion(token), parent_page_id)
    print(f"Paste into your config.yaml:\nnotion:\n  database_id: {database_id}\n  data_source_id: {ds_id}")
    return 0


def discover() -> int:
    try:
        cfg = load_config(ROOT)
        token = os.environ.get("NOTION_TOKEN")
        if not (cfg["notion"]["data_source_id"] and cfg["notion"]["database_id"]):
            problem = NO_IDS
        elif not token:
            problem = NO_TOKEN
        else:
            problem = None
        if problem:
            log.error(problem)
            notify("JobApplier", problem)
            return 1
        (ROOT / "data").mkdir(exist_ok=True)
        with locked(str(ROOT / "data" / "run.lock")) as got:
            if not got:
                log.info("another run holds the lock; exiting")
                return 0
            conn = clusters.connect(str(ROOT / "data" / "jobs.db"))
            n = Notion(token)
            result = run_discover(conn, cfg, Rules.from_config(cfg), build_sources(cfg), n, datetime.now(timezone.utc))
        log.info("discover done: %s", result)
        if result["alert"]:
            notify("JobApplier: discover errors", "; ".join(result["errors"]))
        return 0
    except Exception as exc:
        log.exception("discover failed")
        notify("JobApplier: discover crashed", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
