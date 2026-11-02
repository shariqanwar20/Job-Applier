from jobapplier.core import clusters
from jobapplier.notion.client import Notion, NotionError


def _options(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


RANKS = ("1 New grad", "2 Under 3 yrs", "3 Other")
SCHEMA = {
    "Title": {"title": {}},
    "Company": {"rich_text": {}},
    "City": {"rich_text": {}},
    "Alt locations": {"rich_text": {}},
    "Rank": _options(*RANKS),
    "ATS": _options("Greenhouse", "Lever", "Ashby", "Workday", "Other"),
    "First seen": {"date": {}},
    "Posted": {"date": {}},
    "Flags": {"multi_select": {"options": [{"name": n} for n in ("Remote US", "Aggregator link", "Repost")]}},
    "Link": {"url": {}},
    "Job ID": {"rich_text": {}},
    "Decision": _options("New", "Approve", "Approve + review", "Skip"),
    "Status": _options("New"),
}


def create_database(n: Notion, parent_page_id: str) -> tuple[str, str]:
    r = n.request("POST", "/v1/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"text": {"content": "Jobs"}}],
        "initial_data_source": {"properties": SCHEMA},
    })
    return r["id"], r["data_sources"][0]["id"]


def _text(value: str) -> list[dict]:
    return [{"text": {"content": value[:2000]}}] if value else []  # Notion rejects longer rich text


def page_properties(job_id: str, s: dict, first_seen: str) -> dict:
    props = {
        "Title": {"title": _text(s["title"])},
        "Company": {"rich_text": _text(s["company"])},
        "City": {"rich_text": _text(s["city"])},
        "Alt locations": {"rich_text": _text(s["alt_locations"])},
        "Rank": {"select": {"name": RANKS[s["rank"] - 1]}},
        "ATS": {"select": {"name": s["ats"]}},
        "First seen": {"date": {"start": first_seen}},
        "Flags": {"multi_select": [{"name": f} for f in s["flags"]]},
        "Link": {"url": s["link"] or None},
        "Job ID": {"rich_text": _text(job_id)},
        "Decision": {"select": {"name": "New"}},
        "Status": {"select": {"name": "New"}},
    }
    if s["posted"]:  # Notion rejects an empty date
        props["Posted"] = {"date": {"start": s["posted"]}}
    return props


def find_page(n: Notion, ds_id: str, job_id: str) -> str | None:
    r = n.request("POST", f"/v1/data_sources/{ds_id}/query", {
        "filter": {"property": "Job ID", "rich_text": {"equals": job_id}}, "page_size": 1})
    return r["results"][0]["id"] if r["results"] else None


def push(conn, n: Notion, ds_id: str, rules) -> tuple[int, list[str]]:
    created, errors = 0, []
    for row in clusters.pushable(conn):
        job_id = f"J{row['id']}"
        s = clusters.summarize(clusters.members(conn, row["id"]), rules, bool(row["repost"]))
        try:
            # Look up first: a crash after the create but before the commit must not duplicate the row.
            page_id = find_page(n, ds_id, job_id)
            if page_id is None:
                page_id = n.request("POST", "/v1/pages", {
                    "parent": {"type": "data_source_id", "data_source_id": ds_id},
                    "properties": page_properties(job_id, s, row["first_seen"]),
                })["id"]
                created += 1
        except NotionError as exc:
            errors.append(f"notion: {job_id}: {exc}")
            continue
        clusters.mark_pushed(conn, row["id"], page_id, s["link"], s["identity"])
        conn.commit()
    return created, errors


def set_status(n: Notion, database_id: str, text: str) -> None:
    n.request("PATCH", f"/v1/databases/{database_id}", {"description": [{"text": {"content": text[:2000]}}]})
