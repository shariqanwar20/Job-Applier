import sqlite3
from datetime import datetime, timedelta

from jobapplier.core.filters import Rules, cluster_key, is_intern, is_notable
from jobapplier.core.rank import city_rank, is_remote_us, rank
from jobapplier.models import Posting

_ATS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}


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
        " description, language, seniority, no_location, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.source, p.source_id, cid, p.identity, p.title, p.company, p.location, p.url, p.posted,
         p.description, p.language, p.seniority, int(p.no_location), stamp),
    )
    return cid


def pushable(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clusters c WHERE notion_page_id IS NULL"
        " AND EXISTS (SELECT 1 FROM postings WHERE cluster_id = c.id AND no_location = 0) ORDER BY id"
    ).fetchall()


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
