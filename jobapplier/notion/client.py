import time

import requests

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
            try:
                resp = self.session.request(method, BASE + path, headers=self.headers, json=body, timeout=30)
            except requests.RequestException as e:
                raise NotionError(f"network: {type(e).__name__}") from None  # the exception text can carry the URL
            if resp.status_code in (429, 529) and attempt < 5:
                self.sleep(float(resp.headers.get("Retry-After", 1)))
                continue
            break
        if not 200 <= resp.status_code < 300:
            raise NotionError(f"{resp.status_code} {resp.text[:300]}")
        return resp.json()
