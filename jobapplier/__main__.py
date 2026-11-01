"""The one entry point: `python -m jobapplier discover` or `python -m jobapplier notion-setup <parent_page_id>`."""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from jobapplier.config import load_config
from jobapplier.core import db
from jobapplier.core.filters import Rules
from jobapplier.notion.client import Notion
from jobapplier.notion.setup import create_database
from jobapplier.pipelines.discover import run_discover
from jobapplier.sources import build_sources
from jobapplier.system.keychain import keychain
from jobapplier.system.lock import locked
from jobapplier.system.notify import notify

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("jobapplier")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobapplier")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="fetch fresh jobs and add the new ones to Notion")
    setup = commands.add_parser("notion-setup", help="create the Jobs database under a Notion page and print the config snippet")
    setup.add_argument("parent_page_id")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "notion-setup":
        return notion_setup(args.parent_page_id)
    return discover()


def notion_setup(parent_page_id: str) -> int:
    database_id, ds_id = create_database(Notion(keychain("jobapplier-notion")), parent_page_id)
    print(f"Paste into your config.yaml:\nnotion:\n  database_id: {database_id}\n  data_source_id: {ds_id}")
    return 0


def discover() -> int:
    try:
        cfg = load_config(ROOT)
        if not (cfg["notion"]["data_source_id"] and cfg["notion"]["database_id"]):
            msg = "Set notion IDs in config.yaml — run: python -m jobapplier notion-setup <page_id>"
            log.error(msg)
            notify("JobApplier", msg)
            return 1
        (ROOT / "data").mkdir(exist_ok=True)
        with locked(str(ROOT / "data" / "run.lock")) as got:
            if not got:
                log.info("another run holds the lock; exiting")
                return 0
            conn = db.connect(str(ROOT / "data" / "jobs.db"))
            n = Notion(keychain("jobapplier-notion"))
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
