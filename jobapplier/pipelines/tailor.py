"""Approved Notion rows -> a tailored one-page resume PDF on each row (spec §8.3)."""

import logging
import re
import subprocess
from pathlib import Path

from jobapplier.core import clusters
from jobapplier.notion.push import approved_rows, attach_resume, ensure_tailor_schema, prop_text
from jobapplier.resume.build import fit, pdf_path
from jobapplier.resume.master import Item, MasterError, parse
from jobapplier.resume.tailor import tailor

__all__ = ["run_tailor"]

_log = logging.getLogger("jobapplier")


def _one(
    page: dict, client, conn, cfg: dict, items: list[Item], tex: str, llm_run, root: Path, runner
) -> str:
    """Tailors one row and returns the Status it got."""
    props, t = page["properties"], cfg["tailor"]
    job_id = prop_text(props["Job ID"])
    members = clusters.members(conn, int(job_id[1:])) if re.fullmatch(r"J\d+", job_id) else []
    if not members:
        raise ValueError(f"no cluster for {job_id} in data/jobs.db")
    job = {
        "company": prop_text(props["Company"]),
        "title": prop_text(props["Title"]),
        "description": max((m.description or "" for m in members), key=len),
    }
    plan, problems = tailor(items, job, cfg, llm_run)
    out = pdf_path(root / t["output_dir"], cfg["profile"]["name"], job["company"], job["title"], job_id)
    problems += fit(tex, items, plan, out, t, (root / t["master"]).parent, runner)
    decision = (props.get("Decision", {}).get("select") or {}).get("name")
    status = "Needs review" if problems or decision == "Approve + review" else "Tailored"
    attach_resume(client, page["id"], out, status, "; ".join(problems))
    clusters.record_resume(conn, int(job_id[1:]), str(out))  # apply uploads this exact file
    conn.commit()
    _log.info("%s: %s, %s", job_id, status, out.name)
    return status


def run_tailor(
    conn, cfg: dict, client, llm_run, root: Path, job_id: str | None = None, runner=subprocess.run
) -> dict:
    """Tailors approved rows still at Status New, or only job_id's row whatever its Decision.

    A failing row keeps Status New, so the next run retries it.
    """
    result = {"tailored": 0, "review": 0, "errors": []}
    master = root / cfg["tailor"]["master"]
    if not cfg["profile"]["name"].strip():
        result["errors"].append("set profile.name in config.yaml (it names the resume file)")
    elif not master.exists():
        result["errors"].append(
            f"no {cfg['tailor']['master']}: copy resume/example.master.tex there and edit it"
        )
    if result["errors"]:
        return result
    tex = master.read_text()
    try:
        items = parse(tex)
    except MasterError as exc:
        result["errors"].append(str(exc))
        return result
    ds_id = cfg["notion"]["data_source_id"]
    ensure_tailor_schema(client, ds_id)
    pages = approved_rows(client, ds_id, job_id)
    if job_id and not pages:
        result["errors"].append(f"no Notion row with Job ID {job_id}")
    for page in pages:
        try:
            status = _one(page, client, conn, cfg, items, tex, llm_run, root, runner)
        except Exception as exc:  # one bad row must not stop the others
            _log.exception("tailor failed for %s", page["id"])
            result["errors"].append(f"{prop_text(page['properties'].get('Job ID', {})) or page['id']}: {exc}")
            continue
        result["tailored" if status == "Tailored" else "review"] += 1
    return result
