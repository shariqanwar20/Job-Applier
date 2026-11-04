import pathlib
from types import SimpleNamespace

import httpx
import pytest
import yaml
from notion_client import APIResponseError

from jobapplier.core.rules import Rules
from jobapplier.models import Posting
from jobapplier.notion import client as notion_client_mod

ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / "config.default.yaml").read_text())


@pytest.fixture
def rules(config):
    return Rules.from_config(config)


AFFIRM_INDEED = Posting(
    "indeed",
    "in-1",
    "Software Engineer II, Full Stack",
    "Affirm",
    "Toronto, ON",
    "https://job-boards.greenhouse.io/affirm/jobs/8042399003",
    "2026-10-30",
)


class FakeSource:
    def __init__(self, name, result=None, exc=None):
        self.name, self.result, self.exc = name, result, exc

    def fetch(self):
        if self.exc:
            raise self.exc
        return self.result


def api_error(status, code, message="boom"):
    """An SDK error as notion_client raises it for an HTTP error response."""
    return APIResponseError(
        code=code, status=status, message=message, headers=httpx.Headers(), raw_body_text=""
    )


class FakeNotion:
    """In-memory stand-in for notion_client.Client with the methods we call.

    rows holds created pages by id; fail_on_create maps the Nth create attempt to the exception it raises;
    fail_on_update maps a page id to the exception updating it raises. ds_properties is the data source's
    schema, which data_sources.update adds to; uploads maps a file upload id to the file sent.
    """

    def __init__(self, fail_on_create=None, fail_on_update=None):
        self.rows, self.fail_on_create, self.creates, self.description, self.calls = (
            {},
            dict(fail_on_create or {}),
            0,
            None,
            [],
        )
        self.fail_on_update = dict(fail_on_update or {})
        self.databases = SimpleNamespace(create=self._create_database, update=self._update_database)
        self.data_sources = SimpleNamespace(
            query=self._query, update=self._update_data_source, retrieve=self._retrieve_data_source
        )
        self.pages = SimpleNamespace(
            create=self._create_page, retrieve=self._retrieve_page, update=self._update_page
        )
        self.ds_properties = {"Rank": {"id": "rank%3Aid", "type": "select"}}
        self.uploads = {}
        self.file_uploads = SimpleNamespace(create=self._create_upload, send=self._send_upload)

    def _matches(self, props, f):
        if "and" in f:
            return all(self._matches(props, g) for g in f["and"])
        if "or" in f:
            return any(self._matches(props, g) for g in f["or"])
        p = props.get(f["property"], {})
        if "files" in f:
            return bool(p.get("files"))
        if "select" in f:
            return (p.get("select") or {}).get("name") == f["select"]["equals"]
        return "".join(t["text"]["content"] for t in p.get("rich_text", [])) == f["rich_text"]["equals"]

    def _query(self, data_source_id, **kw):
        self.calls.append(("data_sources.query", {"data_source_id": data_source_id, **kw}))
        hits = [
            {"id": pid, "properties": p} for pid, p in self.rows.items() if self._matches(p, kw["filter"])
        ]
        return {"results": hits[: kw["page_size"]], "has_more": False, "next_cursor": None}

    def _update_data_source(self, data_source_id, **kw):
        self.calls.append(("data_sources.update", {"data_source_id": data_source_id, **kw}))
        self.ds_properties.update(kw.get("properties", {}))
        return {}

    def _retrieve_data_source(self, data_source_id):
        self.calls.append(("data_sources.retrieve", {"data_source_id": data_source_id}))
        return {"properties": self.ds_properties}

    def _create_upload(self, **kw):
        self.calls.append(("file_uploads.create", kw))
        return {"id": f"upload-{len(self.uploads) + 1}", "status": "pending"}

    def _send_upload(self, file_upload_id, **kw):
        self.calls.append(("file_uploads.send", {"file_upload_id": file_upload_id}))
        self.uploads[file_upload_id] = kw["file"]
        return {"id": file_upload_id, "status": "uploaded"}

    def _create_page(self, **kw):
        self.calls.append(("pages.create", kw))
        self.creates += 1
        if self.creates in self.fail_on_create:
            raise self.fail_on_create[self.creates]
        pid = f"page-{len(self.rows) + 1}"
        self.rows[pid] = kw["properties"]
        return {"id": pid}

    def _retrieve_page(self, page_id):
        self.calls.append(("pages.retrieve", {"page_id": page_id}))
        return {"id": page_id, "properties": self.rows[page_id]}

    def _update_page(self, page_id, **kw):
        self.calls.append(("pages.update", {"page_id": page_id, **kw}))
        if page_id in self.fail_on_update:
            raise self.fail_on_update[page_id]
        self.rows[page_id].update(kw["properties"])
        return {"id": page_id}

    def _update_database(self, database_id, **kw):
        self.calls.append(("databases.update", {"database_id": database_id, **kw}))
        self.description = kw["description"][0]["text"]["content"]
        return {}

    def _create_database(self, **kw):
        self.calls.append(("databases.create", kw))
        return {"id": "db1", "data_sources": [{"id": "ds1"}]}


class FakeMail:
    """Stands in for Gmail: hits maps a company to its hit text; error is raised on every call."""

    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.calls = dict(hits or {}), error, []

    def applied_before(self, company):
        self.calls.append(company)
        if self.error:
            raise self.error
        return self.hits.get(company)


class FakeViewsNotion:
    """The Notion client built for the views API (a newer Notion version than FakeNotion's)."""

    def __init__(self, views=({"object": "view", "id": "view1"},), fail=None):
        self.views_listed, self.fail, self.calls = list(views), fail, []
        self.views = SimpleNamespace(list=self._list, update=self._update)

    def _list(self, **kw):
        self.calls.append(("views.list", kw))
        if self.fail:
            raise self.fail
        return {"results": self.views_listed}

    def _update(self, view_id, **kw):
        self.calls.append(("views.update", {"view_id": view_id, **kw}))
        return {}


@pytest.fixture(autouse=True)
def no_notion_pacing(monkeypatch):
    monkeypatch.setattr(notion_client_mod, "sleep", lambda seconds: None)
