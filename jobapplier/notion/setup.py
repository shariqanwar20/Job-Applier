from jobapplier.notion.client import Notion
from jobapplier.notion.push import SCHEMA


def create_database(n: Notion, parent_page_id: str) -> tuple[str, str]:
    r = n.request("POST", "/v1/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"text": {"content": "Jobs"}}],
        "initial_data_source": {"properties": SCHEMA},
    })
    return r["id"], r["data_sources"][0]["id"]
