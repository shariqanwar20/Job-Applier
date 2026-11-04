import re
import sqlite3
from datetime import datetime, timedelta

from jobapplier.core.rank import city_rank, is_remote_us, rank
from jobapplier.core.rules import Rules, is_intern, is_notable, keep, norm
from jobapplier.models import Posting

__all__ = [
    "assign",
    "connect",
    "employer",
    "mark_pruned",
    "mark_pushed",
    "members",
    "newly_ready",
    "prunable",
    "pushable",
    "record_resume",
    "resume",
    "summarize",
    "total",
]

_ATS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}
_PARENS = re.compile(r"\([^)]*\)")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, first_seen TEXT NOT NULL,
  repost INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT, link TEXT, target_identity TEXT);
CREATE INDEX IF NOT EXISTS clusters_key ON clusters(key);
CREATE TABLE IF NOT EXISTS postings (
  source TEXT NOT NULL, source_id TEXT NOT NULL, cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  identity TEXT, title TEXT, company TEXT, location TEXT, url TEXT, posted TEXT, description TEXT,
  seniority TEXT, no_location INTEGER NOT NULL, first_seen TEXT NOT NULL,
  company_type TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source, source_id));
CREATE INDEX IF NOT EXISTS postings_identity ON postings(identity);
CREATE TABLE IF NOT EXISTS pruned (cluster_id INTEGER PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resumes (cluster_id INTEGER PRIMARY KEY, pdf TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ready (job_id TEXT PRIMARY KEY);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # CREATE TABLE IF NOT EXISTS leaves a database from before this column unchanged.
    return conn


def employer(company: str, rules: Rules) -> str:
    """The company name without noise words or parentheticals: "Acme Inc. (Canada)" -> "acme"."""
    # A parenthetical is a country or an abbreviation of the name ("(AWS)"): the same employer varies there.
    return " ".join(w for w in norm(_PARENS.sub(" ", company)).split() if w not in rules.company_noise)


def _cluster_key(company: str, title: str, rules: Rules) -> str:
    name = employer(company, rules)
    return rules.company_aliases.get(name, name) + "|" + norm(_PARENS.sub(" ", title))


def assign(
    conn: sqlite3.Connection, p: Posting, rules: Rules, now: datetime, include_reposts: bool
) -> int | None:
    """Store p in a cluster and return its id; None if this (source, source_id) is already stored."""
    if conn.execute(
        "SELECT 1 FROM postings WHERE source = ? AND source_id = ?", (p.source, p.source_id)
    ).fetchone():
        return None
    # Windows and the repost check anchor on the cluster's first_seen, not its latest member's,
    # so a long-running job can't keep extending its own window.
    key, stamp, repost, cid = _cluster_key(p.company, p.title, rules), now.isoformat(), 0, None
    if p.identity:
        row = conn.execute(
            "SELECT c.id, c.first_seen FROM postings x JOIN clusters c ON c.id = x.cluster_id"
            " WHERE x.identity = ? ORDER BY c.first_seen DESC, c.id DESC LIMIT 1",
            (p.identity,),
        ).fetchone()
        if row:
            if include_reposts and now - datetime.fromisoformat(row["first_seen"]) > timedelta(hours=48):
                repost = 1
            else:
                cid = row["id"]
    if cid is None and not repost:
        row = conn.execute(
            "SELECT id FROM clusters WHERE key = ? AND first_seen >= ?"
            " ORDER BY first_seen DESC, id DESC LIMIT 1",
            (key, (now - timedelta(days=7)).isoformat()),
        ).fetchone()
        cid = row["id"] if row else None
    if cid is None:
        cid = conn.execute(
            "INSERT INTO clusters (key, first_seen, repost) VALUES (?, ?, ?)", (key, stamp, repost)
        ).lastrowid
    conn.execute(
        "INSERT INTO postings (source, source_id, cluster_id, identity, title, company, location, url,"
        " posted, description, seniority, no_location, first_seen, company_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            p.source,
            p.source_id,
            cid,
            p.identity,
            p.title,
            p.company,
            p.location,
            p.url,
            p.posted,
            p.description,
            p.seniority,
            int(p.no_location),
            stamp,
            p.company_type,
        ),
    )
    return cid


def pushable(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clusters c WHERE notion_page_id IS NULL"
        " AND EXISTS (SELECT 1 FROM postings WHERE cluster_id = c.id AND no_location = 0) ORDER BY id"
    ).fetchall()


def members(conn: sqlite3.Connection, cluster_id: int) -> list[Posting]:
    rows = conn.execute(
        "SELECT source, source_id, title, company, location, url, posted, description, seniority,"
        " no_location, identity, company_type FROM postings WHERE cluster_id = ? ORDER BY rowid",
        (cluster_id,),
    )
    return [Posting(**{**r, "no_location": bool(r["no_location"])}) for r in rows]


def mark_pushed(
    conn: sqlite3.Connection, cluster_id: int, page_id: str, link: str, identity: str | None
) -> None:
    conn.execute(
        "UPDATE clusters SET notion_page_id = ?, link = ?, target_identity = ? WHERE id = ?",
        (page_id, link, identity, cluster_id),
    )


def prunable(conn: sqlite3.Connection, rules: Rules) -> list[tuple[int, str, str]]:
    """Pushed clusters today's rules would not push, as (cluster_id, page_id, "filtered" | "duplicate").

    A duplicate has the first member's key (recomputed: the stored key predates the current rules)
    of an earlier surviving cluster first seen within 7 days of it.
    """
    done = dict(conn.execute("SELECT cluster_id, reason FROM pruned").fetchall())
    survivors: list[tuple[str, datetime]] = []
    out = []
    for row in conn.execute("SELECT * FROM clusters WHERE notion_page_id IS NOT NULL ORDER BY id").fetchall():
        previous = done.get(row["id"])
        if previous in ("filtered", "duplicate"):
            continue
        ms = members(conn, row["id"])
        reason = "filtered"
        if any(keep(m, rules) for m in ms):
            key = _cluster_key(ms[0].company, ms[0].title, rules)
            seen = datetime.fromisoformat(row["first_seen"])
            # A deliberate repost shares its original's key on purpose, so it is never a duplicate.
            near = not row["repost"] and any(
                k == key and abs(seen - t) <= timedelta(days=7) for k, t in survivors
            )
            reason = "duplicate" if near else None
            if not near and not row["repost"]:
                survivors.append((key, seen))
        if reason and previous is None:  # a row the owner already decided on was settled in an earlier run
            out.append((row["id"], row["notion_page_id"], reason))
    return out


def mark_pruned(conn: sqlite3.Connection, cluster_id: int, reason: str) -> None:
    conn.execute("INSERT INTO pruned (cluster_id, reason) VALUES (?, ?)", (cluster_id, reason))


def record_resume(conn: sqlite3.Connection, cluster_id: int, pdf: str) -> None:
    conn.execute("INSERT OR REPLACE INTO resumes (cluster_id, pdf) VALUES (?, ?)", (cluster_id, pdf))


def resume(conn: sqlite3.Connection, cluster_id: int) -> str | None:
    row = conn.execute("SELECT pdf FROM resumes WHERE cluster_id = ?", (cluster_id,)).fetchone()
    return row["pdf"] if row else None


def newly_ready(conn: sqlite3.Connection, job_ids: list[str]) -> list[str]:
    """The job_ids not ready on the previous call, in order; then job_ids becomes the ready set."""
    before = {r["job_id"] for r in conn.execute("SELECT job_id FROM ready")}
    conn.execute("DELETE FROM ready")
    conn.executemany("INSERT OR IGNORE INTO ready (job_id) VALUES (?)", [(j,) for j in job_ids])
    return [j for j in dict.fromkeys(job_ids) if j not in before]


def total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]


def summarize(members: list[Posting], rules: Rules, repost: bool) -> dict:
    ms = sorted(
        members,
        key=lambda m: (m.identity is None, city_rank(m.location, rules), rules.source_order.index(m.source)),
    )
    first = ms[0]
    detailed = next((m for m in ms if m.description), first)
    # A no-location member can sort first (it has an identity);
    # show a real city while link and identity stay with it.
    city = next((m.location for m in ms if m.location), "")
    alts = dict.fromkeys(m.location for m in ms if m.location and m.location != city)
    flags = [
        f
        for f, on in (
            ("Remote US", is_remote_us(city)),
            ("Aggregator link", first.identity is None),
            ("Repost", repost),
        )
        if on
    ]
    return {
        "title": first.title,
        "company": first.company,
        "city": city,
        "alt_locations": "; ".join(alts),
        "rank": rank(
            first.title,
            detailed.description,
            detailed.seniority,
            is_intern(first.title, rules) and is_notable(first.company, rules),
            rules,
        ),
        "ats": _ATS.get((first.identity or "").split(":")[0], "Other"),
        "posted": min((m.posted for m in ms if m.posted), default=""),
        "flags": flags,
        "link": first.url,
        "identity": first.identity,
    }
