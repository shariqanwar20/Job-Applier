import fcntl
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from jobapplier import db, dedupe, filters, notion
from jobapplier.config import load_config
from jobapplier.filters import Rules
from jobapplier.identity import posting_identity
from jobapplier.sources import build_sources

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("jobapplier")


def run_discover(conn, cfg: dict, rules: Rules, sources: list, n, now: datetime) -> dict:
    errors, skipped, first_skip = [], 0, None
    for src in sources:
        try:
            postings, err = src.fetch()
        except Exception as exc:  # Source.fetch shouldn't raise, but one bad source must not stop the others
            postings, err = [], f"{src.name}: {exc}"
        if err:
            errors.append(err)
        for p in postings:
            try:
                if filters.keep(p, rules):
                    p.identity = posting_identity(p.url, rules)
                    dedupe.assign(conn, p, now, cfg["include_reposts"])
            except Exception as exc:  # e.g. a malformed URL: skip that posting, keep the run
                log.warning("skipping %s %s: %s", p.source, p.source_id, exc)
                skipped, first_skip = skipped + 1, first_skip or exc
    if skipped:
        errors.append(f"skipped {skipped} postings: {first_skip}")
    conn.commit()  # before pushing: push commits per cluster and must not lose the assignments

    created, push_errors = notion.push(conn, n, cfg["notion"]["data_source_id"], rules)
    errors += push_errors
    total = conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
    text = f"Last discover {now.astimezone():%Y-%m-%d %H:%M} · {created} new · {total} total · errors: {'; '.join(errors) or 'none'}"
    try:
        notion.set_status(n, cfg["notion"]["database_id"], text)
    except notion.NotionError as exc:
        errors.append(f"notion: {exc}")
    return {"new_rows": created, "errors": errors, "alert": any(e != "linkedin: 429" for e in errors)}


@contextmanager
def locked(path: str):
    """Yields True holding an exclusive lock, False if another process has it."""
    with open(path, "a+") as f:  # append mode: a failed attempt must not wipe the holder's PID
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        f.truncate(0)
        f.write(str(os.getpid()))
        f.flush()
        yield True  # closing the file releases the lock


def notify(title: str, message: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"], check=False)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(ROOT)
        if not (cfg["notion"]["data_source_id"] and cfg["notion"]["database_id"]):
            msg = "Set notion IDs in config.yaml — run: python -m jobapplier.notion setup <page_id>"
            log.error(msg)
            notify("JobApplier", msg)
            return 1
        (ROOT / "data").mkdir(exist_ok=True)
        with locked(str(ROOT / "data" / "run.lock")) as got:
            if not got:
                log.info("another run holds the lock; exiting")
                return 0
            conn = db.connect(str(ROOT / "data" / "jobs.db"))
            n = notion.Notion(notion.keychain("jobapplier-notion"))
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
