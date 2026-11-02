import re
import sqlite3
from datetime import datetime, timedelta

from jobapplier.core.rank import city_rank, is_remote_us, rank
from jobapplier.core.rules import Rules, is_intern, is_notable, norm
from jobapplier.models import Posting

_ATS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}
_COMPANY_NOISE = set("inc ltd llc corp corporation company co group plc limited sa the".split())
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
  PRIMARY KEY (source, source_id));
CREATE INDEX IF NOT EXISTS postings_identity ON postings(identity);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def cluster_key(company: str, title: str) -> str:
    words = [w for w in norm(company).split() if w not in _COMPANY_NOISE]
    return " ".join(words) + "|" + norm(re.sub(r"\([^)]*\)", " ", title))


def assign(conn: sqlite3.Connection, p: Posting, now: datetime, include_reposts: bool) -> int | None:
    """Store p in a cluster and return its id; None if this (source, source_id) is already stored."""
    if conn.execute("SELECT 1 FROM postings WHERE source = ? AND source_id = ?", (p.source, p.source_id)).fetchone():
        return None
    # Windows and the repost check anchor on the cluster's first_seen, not its latest member's, so a long-running job can't keep extending its own window.
    key, stamp, repost, cid = cluster_key(p.company, p.title), now.isoformat(), 0, None
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
            "SELECT id FROM clusters WHERE key = ? AND first_seen >= ? ORDER BY first_seen DESC, id DESC LIMIT 1",
            (key, (now - timedelta(days=7)).isoformat()),
        ).fetchone()
        cid = row["id"] if row else None
    if cid is None:
        cid = conn.execute("INSERT INTO clusters (key, first_seen, repost) VALUES (?, ?, ?)", (key, stamp, repost)).lastrowid
    conn.execute(
        "INSERT INTO postings (source, source_id, cluster_id, identity, title, company, location, url, posted,"
        " description, seniority, no_location, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.source, p.source_id, cid, p.identity, p.title, p.company, p.location, p.url, p.posted,
         p.description, p.seniority, int(p.no_location), stamp),
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
        " no_location, identity FROM postings WHERE cluster_id = ? ORDER BY rowid",
        (cluster_id,),
    )
    return [Posting(**{**r, "no_location": bool(r["no_location"])}) for r in rows]


def mark_pushed(conn: sqlite3.Connection, cluster_id: int, page_id: str, link: str, identity: str | None) -> None:
    conn.execute(
        "UPDATE clusters SET notion_page_id = ?, link = ?, target_identity = ? WHERE id = ?",
        (page_id, link, identity, cluster_id),
    )


def total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]


def summarize(members: list[Posting], rules: Rules, repost: bool) -> dict:
    ms = sorted(members, key=lambda m: (m.identity is None, city_rank(m.location, rules), rules.source_order.index(m.source)))
    first = ms[0]
    detailed = next((m for m in ms if m.description), first)
    # A no-location member can sort first (it has an identity); show a real city while link/identity stay with it.
    city = next((m.location for m in ms if m.location), "")
    alts = dict.fromkeys(m.location for m in ms if m.location and m.location != city)
    flags = [f for f, on in (("Remote US", is_remote_us(city)), ("Aggregator link", first.identity is None), ("Repost", repost)) if on]
    return {
        "title": first.title,
        "company": first.company,
        "city": city,
        "alt_locations": "; ".join(alts),
        "rank": rank(first.title, detailed.description, detailed.seniority, is_intern(first.title, rules) and is_notable(first.company, rules), rules),
        "ats": _ATS.get((first.identity or "").split(":")[0], "Other"),
        "posted": min((m.posted for m in ms if m.posted), default=""),
        "flags": flags,
        "link": first.url,
        "identity": first.identity,
    }
