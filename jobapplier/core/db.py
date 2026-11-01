import sqlite3

from jobapplier.models import Posting

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, first_seen TEXT NOT NULL,
  repost INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT, link TEXT, target_identity TEXT);
CREATE INDEX IF NOT EXISTS clusters_key ON clusters(key);
CREATE TABLE IF NOT EXISTS postings (
  source TEXT NOT NULL, source_id TEXT NOT NULL, cluster_id INTEGER NOT NULL REFERENCES clusters(id),
  identity TEXT, title TEXT, company TEXT, location TEXT, url TEXT, posted TEXT, description TEXT,
  language TEXT, seniority TEXT, no_location INTEGER NOT NULL, first_seen TEXT NOT NULL,
  PRIMARY KEY (source, source_id));
CREATE INDEX IF NOT EXISTS postings_identity ON postings(identity);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def members(conn: sqlite3.Connection, cluster_id: int) -> list[Posting]:
    rows = conn.execute(
        "SELECT source, source_id, title, company, location, url, posted, description, language, seniority,"
        " no_location, identity FROM postings WHERE cluster_id = ? ORDER BY rowid",
        (cluster_id,),
    )
    return [Posting(**{**r, "no_location": bool(r["no_location"])}) for r in rows]


def mark_pushed(conn: sqlite3.Connection, cluster_id: int, page_id: str, link: str, identity: str | None) -> None:
    conn.execute(
        "UPDATE clusters SET notion_page_id = ?, link = ?, target_identity = ? WHERE id = ?",
        (page_id, link, identity, cluster_id),
    )
