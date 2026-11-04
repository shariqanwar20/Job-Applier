import html
import os
import re
from datetime import UTC, datetime
from email.header import decode_header, make_header
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from jobapplier.core.rules import norm
from jobapplier.http import ApiClient, ApiError

__all__ = ["SCOPES", "Gmail", "MailError", "extract_code", "sign_in"]

# The only scope this project ever asks for: the token it yields cannot send, change or delete mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SIGN_IN_AGAIN = "mail: sign in again with `python -m jobapplier mail-login`"
_METADATA = [
    ("format", "metadata"),
    ("metadataHeaders", "Subject"),
    ("metadataHeaders", "From"),
    ("metadataHeaders", "Date"),
]
_AFTER = re.compile(r"\b(?:code|verification)\b\D{0,40}?(?<!\d)(\d{4,8})(?!\d)", re.IGNORECASE)
_BEFORE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)\D{0,40}?\b(?:code|verification)\b", re.IGNORECASE)


class MailError(Exception):
    pass


def extract_code(text: str) -> str | None:
    """A 4-8 digit code within 40 characters of "code" or "verification"; the form after it wins.

    After first so that a footer year ("(c) 2026 ... code: 7731") is not read as the code.
    """
    m = _AFTER.search(text) or _BEFORE.search(text)
    return m.group(1) if m else None


def _load_credentials(path: Path) -> Credentials:
    return Credentials.from_authorized_user_file(str(path), SCOPES)


def _save_token(path: Path, creds) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(creds.to_json())
    path.chmod(0o600)  # O_CREAT's mode does not apply to a file that already exists


def sign_in(cfg: dict, root: Path, flow_factory=InstalledAppFlow.from_client_secrets_file) -> None:
    """One-time browser consent on a loopback port; saves the token (read-only scope) with mode 0600."""
    secrets = root / cfg["client_secrets"]
    if not secrets.exists():
        raise MailError(f"mail: put the OAuth client JSON at {cfg['client_secrets']} (see README, Mail)")
    creds = flow_factory(str(secrets), SCOPES).run_local_server(port=0, open_browser=True)
    _save_token(root / cfg["token"], creds)


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, LookupError, ValueError):  # a malformed encoded word: show it as it came
        return value


class Gmail:
    """Read-only Gmail over its REST API. cfg is the `mail` config section; root the repo root."""

    def __init__(
        self, cfg: dict, root: Path, api: ApiClient | None = None, credentials_loader=_load_credentials
    ):
        self._cfg, self._token_path = cfg, root / cfg["token"]
        self._api, self._loader, self._creds = api or ApiClient(_BASE), credentials_loader, None

    def _headers(self) -> dict:
        if self._creds is None:
            try:
                self._creds = self._loader(self._token_path)
            except (OSError, ValueError):  # no token file, or one that is not a token
                raise MailError(_SIGN_IN_AGAIN) from None
        if not self._creds.valid:
            try:
                self._creds.refresh(Request())
                _save_token(self._token_path, self._creds)
            except RefreshError:
                raise MailError(_SIGN_IN_AGAIN) from None
            except (GoogleAuthError, OSError) as exc:
                raise MailError(f"mail: {type(exc).__name__}") from None
        return {"Authorization": f"Bearer {self._creds.token}"}

    def _get(self, path: str, params=None) -> dict:
        headers = self._headers()
        try:
            return self._api.get_json(path, params, headers=headers)
        except ApiError as exc:  # its text is only "HTTP <status>" or "network: <type>"
            raise MailError(_SIGN_IN_AGAIN if str(exc) == "HTTP 401" else f"mail: {exc}") from None
        except ValueError:  # a 2xx whose body is not JSON
            raise MailError("mail: malformed response") from None

    def address(self) -> str:
        return self._get("/profile")["emailAddress"]

    def search(self, raw_query: str, limit: int = 25) -> list[dict]:
        """Gmail search syntax; the newest `limit` messages as subject, sender, received and preview."""
        listed = self._get("/messages", [("q", raw_query), ("maxResults", limit)])
        out = []
        for ref in listed.get("messages", []):
            m = self._get(f"/messages/{ref['id']}", _METADATA)
            headers = {h["name"].lower(): h["value"] for h in m["payload"]["headers"]}
            out.append(
                {
                    "subject": _decode(headers.get("subject", "")),
                    "sender": _decode(headers.get("from", "")),
                    "received": datetime.fromtimestamp(int(m["internalDate"]) / 1000, UTC),
                    "preview": " ".join(html.unescape(m.get("snippet", "")).split())[
                        :500
                    ],  # snippets are HTML-escaped
                }
            )
        return sorted(out, key=lambda m: m["received"], reverse=True)

    def applied_before(self, company: str) -> str | None:
        """ "<date>: <subject>" of the newest application confirmation that names the company, else None."""
        name = norm(company)
        if not name:
            return None
        days = self._cfg["applied_lookback_days"]
        phrases = [norm(p) for p in self._cfg["applied_phrases"]]
        for m in self.search(f'"{name}" newer_than:{days}d'):
            said = norm(f"{m['subject']} {m['preview']}")
            if any(_has(said, p) for p in phrases) and _has(norm(f"{said} {m['sender']}"), name):
                return f"{m['received']:%Y-%m-%d}: {m['subject']}"
        return None

    def find_code(self, sender_domains: list[str], after: datetime) -> str | None:
        """The newest code mailed at or after `after` from one of the domains. Polling is the caller's job."""
        if not sender_domains:
            return None
        for m in self.search(f"from:({' OR '.join(sender_domains)}) newer_than:1d"):
            if m["received"] >= after and (code := extract_code(f"{m['subject']} {m['preview']}")):
                return code
        return None


def _has(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "  # whole words: "acme" must not match "acmeco"
