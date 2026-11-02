import logging
from time import sleep

import httpx
from notion_client import Client, RetryOptions
from notion_client.errors import HTTPResponseError, NotionClientErrorBase

__all__ = ["NotionError", "call", "make_client"]


class NotionError(Exception):
    pass


def make_client(token: str, http: httpx.Client | None = None) -> Client:
    # The SDK retries 429 on every method but 5xx only on GET/DELETE, so a page create is never sent twice.
    # Our own logger: without one the SDK adds a handler that, with propagation, prints each warning twice.
    return Client(
        client=http,
        auth=token,
        retry=RetryOptions(max_retries=5),
        logger=logging.getLogger("jobapplier.notion"),
    )


def call(endpoint, **kwargs) -> dict:
    """Runs one SDK call, paced, with every SDK or transport failure raised as NotionError."""
    sleep(0.35)  # the SDK does not pace; this stays under Notion's ~3 requests/second
    try:
        return endpoint(**kwargs)
    except HTTPResponseError as e:
        raise NotionError(f"{e.status} {e}") from None
    except NotionClientErrorBase as e:
        raise NotionError(str(e)) from None
    except httpx.HTTPError as e:
        raise NotionError(f"network: {type(e).__name__}") from None  # the exception text can carry the URL
    except ValueError:  # a 2xx whose body is not JSON
        raise NotionError("malformed response") from None
