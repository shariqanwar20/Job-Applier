import pathlib

import pytest
import yaml

from jobapplier.core.filters import Rules
from jobapplier.models import Posting

ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / "config.default.yaml").read_text())


@pytest.fixture
def rules(config):
    return Rules.from_config(config)


AFFIRM_INDEED = Posting(
    "indeed", "in-1", "Software Engineer II, Full Stack", "Affirm", "Toronto, ON",
    "https://job-boards.greenhouse.io/affirm/jobs/8042399003", "2026-10-30",
)


class FakeSource:
    def __init__(self, name, result=None, exc=None):
        self.name, self.result, self.exc = name, result, exc

    def fetch(self):
        if self.exc:
            raise self.exc
        return self.result


class Resp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code, self._body, self.headers = status_code, body or {}, headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Serves queued responses and records every call."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeNotion:
    """In-memory stand-in for jobapplier.notion.client.Notion: pages by id, create fails on the Nth attempt in fail_on_create."""

    def __init__(self, fail_on_create=()):
        from jobapplier.notion.client import NotionError

        self.error = NotionError
        self.pages, self.fail_on_create, self.creates, self.description = {}, set(fail_on_create), 0, None

    def request(self, method, path, body=None):
        if path.endswith("/query"):
            want = body["filter"]["rich_text"]["equals"]
            hits = [{"id": pid} for pid, p in self.pages.items() if p["Job ID"]["rich_text"][0]["text"]["content"] == want]
            return {"results": hits[: body["page_size"]]}
        if method == "POST" and path == "/v1/pages":
            self.creates += 1
            if self.creates in self.fail_on_create:
                raise self.error("400 validation_error")
            pid = f"page-{len(self.pages) + 1}"
            self.pages[pid] = body["properties"]
            return {"id": pid}
        if method == "PATCH" and path.startswith("/v1/databases/"):
            self.description = body["description"][0]["text"]["content"]
            return {}
        raise AssertionError(f"unexpected {method} {path}")
