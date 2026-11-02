import logging
from datetime import datetime

from jobapplier.core import clusters
from jobapplier.core.identity import posting_identity
from jobapplier.core.rules import Rules, keep
from jobapplier.notion.client import NotionError
from jobapplier.notion.push import push, set_status

__all__ = ["run_discover"]

_log = logging.getLogger("jobapplier")


def run_discover(conn, cfg: dict, rules: Rules, sources: list, client, now: datetime) -> dict:
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
                if keep(p, rules):
                    p.identity = posting_identity(p.url, rules)
                    clusters.assign(conn, p, now, cfg["include_reposts"])
            except Exception as exc:  # e.g. a malformed URL: skip that posting, keep the run
                _log.warning("skipping %s %s: %s", p.source, p.source_id, exc)
                skipped, first_skip = skipped + 1, first_skip or exc
    if skipped:
        errors.append(f"skipped {skipped} postings: {first_skip}")
    conn.commit()  # before pushing: push commits per cluster and must not lose the assignments

    created, push_errors = push(conn, client, cfg["notion"]["data_source_id"], rules)
    errors += push_errors
    text = (
        f"Last discover {now.astimezone():%Y-%m-%d %H:%M} · {created} new · {clusters.total(conn)} total"
        f" · errors: {'; '.join(errors) or 'none'}"
    )
    try:
        set_status(client, cfg["notion"]["database_id"], text)
    except NotionError as exc:
        errors.append(f"notion: {exc}")
    return {"new_rows": created, "errors": errors, "alert": any(e != "linkedin: 429" for e in errors)}
