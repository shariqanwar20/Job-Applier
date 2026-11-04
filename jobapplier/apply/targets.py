"""Which Notion rows to open for applying, at which application URL, with which resume PDF."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from jobapplier.core import clusters
from jobapplier.core.rules import Rules
from jobapplier.models import Posting
from jobapplier.notion.push import ready_rows

__all__ = ["Target", "apply_url", "find_targets"]

_ATS = ("greenhouse", "lever", "ashby", "workday")
# (host, job page path, what turns the job page into its application page). Greenhouse job pages hold
# the form already, so they are not here.
_APPLY_PAGES = (
    (re.compile(r"jobs\.(eu\.)?lever\.co"), re.compile(r"/[^/]+/[^/]+"), "/apply"),
    (re.compile(r"jobs\.ashbyhq\.com"), re.compile(r"/[^/]+/[^/]+"), "/application"),
    (re.compile(r"[^.]+\.breezy\.hr"), re.compile(r"/p/[^/]+"), "/apply"),
    (re.compile(r"jobs\.smartrecruiters\.com"), re.compile(r"/[^/]+/\d+-[^/]*"), "?oga=true"),
)
_BOARD_ONLY = "only a job-board link: find the employer's posting and put it in Link"
_NO_CLUSTER = "no cluster in data/jobs.db"
_NO_PDF = "no resume PDF in output/: run tailor first"


@dataclass(frozen=True)
class Target:
    job_id: str
    company: str
    title: str
    url: str
    pdf: Path
    description: str
    page: dict  # the Notion page, to set its Status later


def apply_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    for host, job_path, suffix in _APPLY_PAGES:
        if host.fullmatch(parts.hostname or "") and job_path.fullmatch(path):
            if suffix.startswith("?"):
                query = "&".join(q for q in (parts.query, suffix[1:]) if q)
                return urlunsplit(parts._replace(path=path, query=query))
            return urlunsplit(parts._replace(path=path + suffix))
    return url


def _text(prop: dict) -> str:
    return "".join(
        t.get("plain_text") or t["text"]["content"] for t in prop.get("title") or prop.get("rich_text") or []
    )


def _on_board(url: str, rules: Rules) -> bool:
    host = urlsplit(url).hostname or ""
    return not host or any(a in host for a in rules.aggregators)


def _job_url(members: list[Posting], link: str, rules: Rules) -> str | None:
    """An ATS member, else the row's Link (the owner may paste the employer's page there), else any
    member off the job boards."""
    ats = [m.url for m in members if (m.identity or "").split(":")[0] in _ATS]
    rest = [link] + [m.url for m in members]
    return next((u for u in ats + rest if u and not _on_board(u, rules)), None)


def _pdf(conn, cluster_id: int, job_id: str, page: dict, out_dir: Path) -> Path | None:
    recorded = clusters.resume(conn, cluster_id)
    names = [f.get("name", "") for f in (page["properties"].get("Resume") or {}).get("files", [])]
    found = [Path(recorded)] if recorded else []
    found += [out_dir / n for n in names if n]
    found += sorted(out_dir.glob(f"*_{job_id}.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in found if p.is_file()), None)


def find_targets(
    conn, client, cfg: dict, rules: Rules, root: Path, job_id: str | None = None
) -> tuple[list[Target], list[str]]:
    """Rows ready to apply (or only job_id's row), and a "J12 (Acme): why" line for each one skipped."""
    pages = ready_rows(client, cfg["notion"]["data_source_id"], job_id)
    if job_id and not pages:
        return [], [f"{job_id}: no Notion row with this Job ID"]
    targets, skipped = [], []
    for page in pages:
        props = page["properties"]
        jid, company = _text(props.get("Job ID", {})), _text(props.get("Company", {}))
        members = clusters.members(conn, int(jid[1:])) if re.fullmatch(r"J\d+", jid) else []
        url = _job_url(members, (props.get("Link") or {}).get("url") or "", rules)
        pdf = _pdf(conn, int(jid[1:]), jid, page, root / cfg["tailor"]["output_dir"]) if members else None
        why = next(
            (w for ok, w in ((members, _NO_CLUSTER), (url, _BOARD_ONLY), (pdf, _NO_PDF)) if not ok), None
        )
        if why:
            skipped.append(f"{jid} ({company}): {why}")
            continue
        description = max((m.description or "" for m in members), key=len)
        title = _text(props.get("Title", {}))
        targets.append(Target(jid, company, title, apply_url(url), pdf, description, page))
    return targets, skipped
