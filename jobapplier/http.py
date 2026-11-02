from time import monotonic, sleep

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__all__ = ["ApiClient", "ApiError"]


class ApiError(Exception):
    pass


class ApiClient:
    """A JSON API over one requests.Session: retries, an optional pause between calls, one error type."""

    def __init__(
        self,
        base_url: str,
        headers: dict | None = None,
        timeout: float = 30,
        min_interval: float = 0,
        session: requests.Session | None = None,
    ):
        self.base_url, self.timeout, self.min_interval = base_url, timeout, min_interval
        self.session = session or requests.Session()
        self.session.headers.update(headers or {})
        # raise_on_status=False: after the last retry the response comes back, so the error names its status.
        retry = Retry(
            total=3,
            status_forcelist={429, 500, 502, 503, 504},
            allowed_methods={"GET"},
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._last_call = float("-inf")

    def get_json(self, path: str, params=None) -> dict:
        wait = self._last_call + self.min_interval - monotonic()
        if wait > 0:
            sleep(wait)
        self._last_call = monotonic()
        try:
            resp = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"network: {type(e).__name__}") from None  # the exception text can carry the URL
        if not 200 <= resp.status_code < 300:
            raise ApiError(f"HTTP {resp.status_code}")
        return resp.json()
