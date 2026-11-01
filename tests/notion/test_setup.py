from tests.conftest import FakeSession, Resp

from jobapplier.notion.client import Notion
from jobapplier.notion.push import SCHEMA
from jobapplier.notion.setup import create_database


def test_create_database_posts_schema_and_returns_ids():
    s = FakeSession([Resp(200, {"id": "db1", "data_sources": [{"id": "ds1"}]})])
    assert create_database(Notion("tok", session=s, sleep=lambda _: None), "parent1") == ("db1", "ds1")
    call = s.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/databases")
    assert call["json"]["parent"] == {"type": "page_id", "page_id": "parent1"}
    assert call["json"]["initial_data_source"] == {"properties": SCHEMA}
