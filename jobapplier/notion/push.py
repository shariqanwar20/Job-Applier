import logging
from datetime import datetime
from pathlib import Path

from notion_client import Client

from jobapplier.core import clusters
from jobapplier.mail.gmail import MailError
from jobapplier.notion.client import NotionError, call

__all__ = [
    "approved_rows",
    "attach_resume",
    "create_database",
    "ensure_tailor_schema",
    "prune",
    "push",
    "set_status",
    "style_database",
]

_log = logging.getLogger("jobapplier")


def _options(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


_RANKS = ("1 New grad", "2 Under 3 yrs", "3 Other")
_SCHEMA = {
    "Title": {"title": {}},
    "Company": {"rich_text": {}},
    "City": {"rich_text": {}},
    "Alt locations": {"rich_text": {}},
    "Rank": _options(*_RANKS),
    "ATS": _options("Greenhouse", "Lever", "Ashby", "Workday", "Other"),
    "First seen": {"date": {}},
    "Posted": {"date": {}},
    "Flags": {
        "multi_select": {
            "options": [
                {"name": n}
                for n in ("Remote US", "Aggregator link", "Repost", "Filtered", "Duplicate", "Applied before")
            ]
        }
    },
    "Link": {"url": {}},
    "Job ID": {"rich_text": {}},
    "Decision": _options("New", "Approve", "Approve + review", "Skip"),
    "Status": _options("New", "Tailored", "Needs review"),
    "Resume": {"files": {}},
    "Notes": {"rich_text": {}},
}
_APPROVED = ("Approve", "Approve + review")


def _title(text: str) -> list[dict]:
    return [{"text": {"content": text}}]


def create_database(client: Client, parent_page_id: str, title: str) -> tuple[str, str]:
    r = call(
        client.databases.create,
        parent={"type": "page_id", "page_id": parent_page_id},
        title=_title(title),
        is_inline=True,  # sits directly on the parent page instead of opening as a sub-page
        initial_data_source={"properties": _SCHEMA},
    )
    return r["id"], r["data_sources"][0]["id"]


def style_database(client: Client, views_client: Client, database_id: str, ds_id: str, title: str) -> None:
    """Titles the data source and turns the default view into an Inbox grouped by Rank.

    Best effort for the caller: the database already works without it. The views API needs a newer
    Notion version than the rest of the code, hence views_client.
    """
    call(client.data_sources.update, data_source_id=ds_id, title=_title(title))  # the view header shows it
    rank_id = call(client.data_sources.retrieve, data_source_id=ds_id)["properties"]["Rank"]["id"]
    views = call(views_client.views.list, database_id=database_id)["results"]
    if not views:
        raise NotionError("the database has no view")
    call(
        views_client.views.update,
        view_id=views[0]["id"],
        name="Inbox",
        filter={"property": "Decision", "select": {"does_not_equal": "Skip"}},
        sorts=[{"property": "First seen", "direction": "descending"}],
        configuration={
            "type": "table",
            "group_by": {
                "type": "select",
                "property_id": rank_id,
                "sort": {"type": "manual"},
                "hide_empty_groups": True,
            },
        },
    )


def _text(value: str) -> list[dict]:
    return [{"text": {"content": value[:2000]}}] if value else []  # Notion rejects longer rich text


def _page_properties(job_id: str, s: dict, first_seen: str) -> dict:
    props = {
        "Title": {"title": _text(s["title"])},
        "Company": {"rich_text": _text(s["company"])},
        "City": {"rich_text": _text(s["city"])},
        "Alt locations": {"rich_text": _text(s["alt_locations"])},
        "Rank": {"select": {"name": _RANKS[s["rank"] - 1]}},
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


def _find_page(client: Client, ds_id: str, job_id: str) -> str | None:
    r = call(
        client.data_sources.query,
        data_source_id=ds_id,
        filter={"property": "Job ID", "rich_text": {"equals": job_id}},
        page_size=1,
    )
    return r["results"][0]["id"] if r["results"] else None


def push(conn, client: Client, ds_id: str, rules, mail=None) -> tuple[int, list[str]]:
    """Adds the new clusters as rows. With mail, flags a company applied to before.

    A mail failure is recorded once and mail is not asked again this run; the rows still go in.
    """
    created, errors, applied = 0, [], {}
    for row in clusters.pushable(conn):
        job_id = f"J{row['id']}"
        s = clusters.summarize(clusters.members(conn, row["id"]), rules, bool(row["repost"]))
        try:
            # Look up first: a crash after the create but before the commit must not duplicate the row.
            page_id = _find_page(client, ds_id, job_id)
            if page_id is None:
                name = clusters.employer(s["company"], rules)  # mail says "Acme", not "Acme Inc."
                if mail is not None and name not in applied:
                    try:
                        applied[name] = mail.applied_before(name)
                    except MailError as exc:
                        errors.append(str(exc))
                        mail = None
                if hit := applied.get(name):
                    s["flags"].append("Applied before")
                    _log.info("applied before: %s (%s)", s["company"], hit)
                page_id = call(
                    client.pages.create,
                    parent={"type": "data_source_id", "data_source_id": ds_id},
                    properties=_page_properties(job_id, s, row["first_seen"]),
                )["id"]
                created += 1
        except NotionError as exc:
            errors.append(f"notion: {job_id}: {exc}")
            continue
        clusters.mark_pushed(conn, row["id"], page_id, s["link"], s["identity"])
        conn.commit()
    return created, errors


def prune(conn, client: Client, rules, now: datetime) -> tuple[int, list[str]]:
    """Sets rows the current rules would not push to Skip, flagged Filtered or Duplicate. Never deletes.

    now is unused: the duplicate window runs on the clusters' own first_seen.
    """
    skipped, errors = 0, []
    for cluster_id, page_id, reason in clusters.prunable(conn, rules):
        try:
            props = call(client.pages.retrieve, page_id=page_id)["properties"]
            # A renamed or missing Decision means the owner has reworked the row: leave it alone.
            if (props.get("Decision", {}).get("select") or {}).get("name") == "New":
                flags = [
                    f["name"] for f in props.get("Flags", {}).get("multi_select", [])
                ]  # an update replaces the whole list
                call(
                    client.pages.update,
                    page_id=page_id,
                    properties={
                        "Decision": {"select": {"name": "Skip"}},
                        "Flags": {"multi_select": [{"name": f} for f in [*flags, reason.capitalize()]]},
                    },
                )
                skipped += 1
            else:
                reason = "kept: owner decided"  # they already acted on the row, so leave it as they set it
        except NotionError as exc:
            errors.append(f"notion: J{cluster_id}: {exc}")
            continue
        clusters.mark_pruned(conn, cluster_id, reason)
        conn.commit()
    return skipped, errors


def set_status(client: Client, database_id: str, text: str) -> None:
    call(client.databases.update, database_id=database_id, description=[{"text": {"content": text[:2000]}}])


def ensure_tailor_schema(client: Client, ds_id: str) -> None:
    """Adds Resume, Notes and the tailor Status options to a database made before them. Never removes."""
    props = call(client.data_sources.retrieve, data_source_id=ds_id)["properties"]
    add = {name: _SCHEMA[name] for name in ("Resume", "Notes") if name not in props}
    status = props.get("Status", {"select": {"options": []}})
    if "select" not in status:  # rewriting it as a select would change the owner's property type
        raise NotionError("the Status property must be a select")
    # An update replaces the whole option list, so the existing options go back in unchanged.
    options = [{k: o[k] for k in ("id", "name", "color") if k in o} for o in status["select"]["options"]]
    missing = [
        o for o in _SCHEMA["Status"]["select"]["options"] if o["name"] not in {x["name"] for x in options}
    ]
    if missing:
        add["Status"] = {"select": {"options": options + missing}}
    if add:
        call(client.data_sources.update, data_source_id=ds_id, properties=add)


def approved_rows(client: Client, ds_id: str, job_id: str | None = None) -> list[dict]:
    """Pages with Decision Approve or Approve + review and Status New; with job_id, that one row as it is."""
    if job_id:
        where = {"property": "Job ID", "rich_text": {"equals": job_id}}
    else:
        decided = [{"property": "Decision", "select": {"equals": d}} for d in _APPROVED]
        where = {"and": [{"or": decided}, {"property": "Status", "select": {"equals": "New"}}]}
    pages, cursor = [], None
    while True:
        more = {"start_cursor": cursor} if cursor else {}
        r = call(client.data_sources.query, data_source_id=ds_id, filter=where, page_size=100, **more)
        pages += r["results"]
        if not r.get("has_more"):
            return pages
        cursor = r["next_cursor"]


def attach_resume(client: Client, page_id: str, pdf: Path, status: str, notes: str) -> None:
    """Uploads pdf to the row's Resume property and sets Status and Notes."""
    up = call(
        client.file_uploads.create, mode="single_part", filename=pdf.name, content_type="application/pdf"
    )
    call(
        client.file_uploads.send,
        file_upload_id=up["id"],
        file=(pdf.name, pdf.read_bytes(), "application/pdf"),
    )
    props = {
        "Resume": {"files": [{"type": "file_upload", "file_upload": {"id": up["id"]}, "name": pdf.name}]},
        "Status": {"select": {"name": status}},
        "Notes": {"rich_text": _text(notes)},  # empty clears an earlier run's problems
    }
    call(client.pages.update, page_id=page_id, properties=props)
