import subprocess
import sys
import time

import requests

from jobapplier import db, dedupe

BASE = "https://api.notion.com"


class NotionError(Exception):
    pass


class Notion:
    def __init__(self, token: str, session=None, sleep=time.sleep):
        self.session, self.sleep = session or requests.Session(), sleep
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03", "Content-Type": "application/json"}

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(6):
            self.sleep(0.35)  # stays under Notion's ~3 requests/second
            resp = self.session.request(method, BASE + path, headers=self.headers, json=body, timeout=30)
            if resp.status_code in (429, 529) and attempt < 5:
                self.sleep(int(resp.headers.get("Retry-After", 1)))
                continue
            break
        if not 200 <= resp.status_code < 300:
            raise NotionError(f"{resp.status_code} {resp.text[:300]}")
        return resp.json()


def _options(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


SCHEMA = {
    "Title": {"title": {}},
    "Company": {"rich_text": {}},
    "City": {"rich_text": {}},
    "Alt locations": {"rich_text": {}},
    "Rank": _options("1 New grad", "2 Under 3 yrs", "3 Other"),
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
    return [{"text": {"content": value[:2000]}}]  # Notion rejects longer rich text


def page_properties(job_id: str, s: dict, first_seen: str) -> dict:
    rank = next(o["name"] for o in SCHEMA["Rank"]["select"]["options"] if o["name"].startswith(f"{s['rank']} "))
    props = {
        "Title": {"title": _text(s["title"])},
        "Company": {"rich_text": _text(s["company"])},
        "City": {"rich_text": _text(s["city"])},
        "Alt locations": {"rich_text": _text(s["alt_locations"])},
        "Rank": {"select": {"name": rank}},
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
    for row in dedupe.pushable(conn):
        job_id = f"J{row['id']}"
        s = dedupe.summarize(db.members(conn, row["id"]), rules, bool(row["repost"]))
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
        db.mark_pushed(conn, row["id"], page_id, s["link"], s["identity"])
        conn.commit()
    return created, errors


def set_status(n: Notion, database_id: str, text: str) -> None:
    n.request("PATCH", f"/v1/databases/{database_id}", {"description": [{"text": {"content": text[:2000]}}]})


def keychain(service: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[0] != "setup":
        sys.exit("usage: python -m jobapplier.notion setup <parent_page_id>")
    database_id, ds_id = create_database(Notion(keychain("jobapplier-notion")), argv[1])
    print(f"Paste into your config.yaml:\nnotion:\n  database_id: {database_id}\n  data_source_id: {ds_id}")


if __name__ == "__main__":
    main(sys.argv[1:])
