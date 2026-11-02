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

    rows holds created pages by id; fail_on_create maps the Nth create attempt to the exception it raises.
    """

    def __init__(self, fail_on_create=None):
        self.rows, self.fail_on_create, self.creates, self.description, self.calls = (
            {},
            dict(fail_on_create or {}),
            0,
            None,
            [],
        )
        self.databases = SimpleNamespace(create=self._create_database, update=self._update_database)
        self.data_sources = SimpleNamespace(query=self._query)
        self.pages = SimpleNamespace(create=self._create_page)

    def _query(self, data_source_id, **kw):
        self.calls.append(("data_sources.query", {"data_source_id": data_source_id, **kw}))
        want = kw["filter"]["rich_text"]["equals"]
        hits = [
            {"id": pid}
            for pid, p in self.rows.items()
            if p["Job ID"]["rich_text"][0]["text"]["content"] == want
        ]
        return {"results": hits[: kw["page_size"]]}

    def _create_page(self, **kw):
        self.calls.append(("pages.create", kw))
        self.creates += 1
        if self.creates in self.fail_on_create:
            raise self.fail_on_create[self.creates]
        pid = f"page-{len(self.rows) + 1}"
        self.rows[pid] = kw["properties"]
        return {"id": pid}

    def _update_database(self, database_id, **kw):
        self.calls.append(("databases.update", {"database_id": database_id, **kw}))
        self.description = kw["description"][0]["text"]["content"]
        return {}

    def _create_database(self, **kw):
        self.calls.append(("databases.create", kw))
        return {"id": "db1", "data_sources": [{"id": "ds1"}]}


@pytest.fixture(autouse=True)
def no_notion_pacing(monkeypatch):
    monkeypatch.setattr(notion_client_mod, "sleep", lambda seconds: None)
