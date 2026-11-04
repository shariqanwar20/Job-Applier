import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError, TransportError

import jobapplier
from jobapplier.http import ApiError
from jobapplier.mail import gmail
from jobapplier.mail.gmail import SCOPES, Gmail, MailError, extract_code, sign_in

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
SIGN_IN_AGAIN = "mail: sign in again with `python -m jobapplier mail-login`"
METADATA = [
    ("format", "metadata"),
    ("metadataHeaders", "Subject"),
    ("metadataHeaders", "From"),
    ("metadataHeaders", "Date"),
]


def message(subject, sender, received, snippet=""):
    return {
        "snippet": snippet,
        "internalDate": str(int(received.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "ignored: internalDate wins"},
            ]
        },
    }


class FakeApi:
    """Answers the three GETs we make; records (path, params, headers). Any query lists all messages."""

    def __init__(self, messages=None, error=None):
        self.messages, self.error, self.calls = dict(messages or {}), error, []

    def get_json(self, path, params=None, headers=None):
        self.calls.append((path, params, headers))
        if self.error:
            raise self.error
        if path == "/profile":
            return {"emailAddress": "me@example.com"}
        if path == "/messages":
            limit = dict(params)["maxResults"]
            newest_first = sorted(self.messages, key=lambda i: self.messages[i]["internalDate"], reverse=True)
            return {"messages": [{"id": i} for i in newest_first[:limit]]} if self.messages else {}
        return self.messages[path.removeprefix("/messages/")]


class FakeCreds:
    def __init__(self, valid=True, refresh_error=None):
        self.valid, self.refresh_error, self.refreshes, self.token = valid, refresh_error, 0, "tok-secret"

    def refresh(self, request):
        if self.refresh_error:
            raise self.refresh_error
        self.valid, self.token, self.refreshes = True, "fresh-secret", self.refreshes + 1

    def to_json(self):
        return json.dumps({"token": self.token})


@pytest.fixture
def mail_cfg(config):
    return config["mail"]


def open_gmail(mail_cfg, tmp_path, api, creds=None):
    creds = creds or FakeCreds()
    return Gmail(mail_cfg, tmp_path, api=api, credentials_loader=lambda path: creds)


def test_extract_code():
    assert extract_code("Your verification code is 482913") == "482913"
    assert extract_code("482913 is your code") == "482913"
    assert extract_code("© 2026 Acme. Your security code: 7731") == "7731"
    assert extract_code("Welcome to Acme, no digits here") is None
    assert extract_code("Order 123456789012 shipped; code of conduct") is None  # too long, not a code


def test_nothing_is_read_or_called_until_first_use(mail_cfg, tmp_path):
    def loader(path):
        raise AssertionError("must not load")

    Gmail(mail_cfg, tmp_path, api=FakeApi(), credentials_loader=loader)


def test_search_gets_metadata_with_a_bearer_and_parses_messages(mail_cfg, tmp_path):
    api = FakeApi(
        {
            "a1": message("old one", "a@x.com", NOW - timedelta(days=2)),
            "b2": message(
                "=?utf-8?q?Caf=C3=A9_applied?=", "Jobs <jobs@acme.com>", NOW, "It&#39;s   done " + "x" * 600
            ),
        }
    )
    found = open_gmail(mail_cfg, tmp_path, api).search('"Acme" newer_than:365d', limit=5)
    bearer = {"Authorization": "Bearer tok-secret"}
    assert api.calls[0] == ("/messages", [("q", '"Acme" newer_than:365d'), ("maxResults", 5)], bearer)
    assert api.calls[1:] == [("/messages/b2", METADATA, bearer), ("/messages/a1", METADATA, bearer)]
    assert [m["subject"] for m in found] == ["Café applied", "old one"]  # newest first
    assert found[0]["sender"] == "Jobs <jobs@acme.com>" and found[0]["received"] == NOW
    assert found[0]["preview"].startswith("It's done x") and len(found[0]["preview"]) == 500
    assert found[1]["preview"] == ""


def test_search_with_no_hits_is_empty(mail_cfg, tmp_path):
    api = FakeApi()
    assert open_gmail(mail_cfg, tmp_path, api).search("x") == [] and len(api.calls) == 1


def test_address_reads_the_profile(mail_cfg, tmp_path):
    assert open_gmail(mail_cfg, tmp_path, FakeApi()).address() == "me@example.com"


def test_an_expired_token_is_refreshed_once_and_saved_private(mail_cfg, tmp_path):
    creds, api = FakeCreds(valid=False), FakeApi()
    g = open_gmail(mail_cfg, tmp_path, api, creds)
    g.search("x")
    g.search("y")
    token = tmp_path / mail_cfg["token"]
    assert creds.refreshes == 1 and json.loads(token.read_text()) == {"token": "fresh-secret"}
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert api.calls[0][2] == {"Authorization": "Bearer fresh-secret"}


def test_a_token_that_cannot_be_used_asks_for_a_new_sign_in(mail_cfg, tmp_path):
    def missing(path):
        raise FileNotFoundError(path)

    def corrupt(path):
        raise ValueError("bad json")

    for loader in (missing, corrupt):
        with pytest.raises(MailError) as info:
            Gmail(mail_cfg, tmp_path, api=FakeApi(), credentials_loader=loader).search("x")
        assert str(info.value) == SIGN_IN_AGAIN
    revoked = FakeCreds(valid=False, refresh_error=RefreshError("invalid_grant"))
    with pytest.raises(MailError) as info:
        open_gmail(mail_cfg, tmp_path, FakeApi(), revoked).search("x")
    assert str(info.value) == SIGN_IN_AGAIN


def test_a_network_failure_while_refreshing_is_a_mail_error(mail_cfg, tmp_path):
    down = FakeCreds(valid=False, refresh_error=TransportError("https://oauth2.googleapis.com boom"))
    with pytest.raises(MailError) as info:
        open_gmail(mail_cfg, tmp_path, FakeApi(), down).search("x")
    assert str(info.value) == "mail: TransportError"


def test_api_errors_become_mail_errors_without_the_token(mail_cfg, tmp_path):
    with pytest.raises(MailError) as info:
        open_gmail(mail_cfg, tmp_path, FakeApi(error=ApiError("HTTP 500"))).search("x")
    assert str(info.value) == "mail: HTTP 500" and "tok-secret" not in str(info.value)
    with pytest.raises(MailError) as info:
        open_gmail(mail_cfg, tmp_path, FakeApi(error=ApiError("HTTP 401"))).search("x")
    assert str(info.value) == SIGN_IN_AGAIN


def applied(subject, sender="Careers <no-reply@acme.com>", snippet="", days=1):
    return message(subject, sender, NOW - timedelta(days=days), snippet)


def test_applied_before_hit_returns_date_and_subject(mail_cfg, tmp_path):
    api = FakeApi({"m1": applied("Thank you for applying to Acme", days=3)})
    assert open_gmail(mail_cfg, tmp_path, api).applied_before("Acme") == (
        "2026-10-28: Thank you for applying to Acme"
    )
    assert api.calls[0][1][0] == ("q", '"acme" newer_than:365d')


def test_applied_before_matches_company_in_sender_or_preview_and_newest_wins(mail_cfg, tmp_path):
    api = FakeApi(
        {
            "m1": applied("Application received", sender="Acme Talent <x@y.com>", days=9),
            "m2": applied("Re: your note", snippet="We received your application for Acme", days=2),
            "m3": applied("Application received", sender="Acme <a@acme.com>", days=5),
        }
    )
    assert open_gmail(mail_cfg, tmp_path, api).applied_before("Acme") == "2026-10-29: Re: your note"


def test_applied_before_misses(mail_cfg, tmp_path):
    api = FakeApi(
        {
            "m1": applied(
                "Thank you for applying to Acmeco", sender="Acmeco <a@acmeco.com>"
            ),  # other company
            "m2": applied("Acme newsletter: new roles"),  # no applied phrase
        }
    )
    g = open_gmail(mail_cfg, tmp_path, api)
    assert g.applied_before("Acme") is None
    assert g.applied_before("???") is None  # nothing to search for


def test_find_code_returns_the_newest_code_after_the_cutoff(mail_cfg, tmp_path):
    api = FakeApi(
        {
            "m1": message("Your code", "a@x.com", NOW - timedelta(minutes=30), "code: 111111"),
            "m2": message("Your code", "a@x.com", NOW - timedelta(minutes=2), "Use code: 222222 to sign in"),
            "m3": message("Hello", "a@x.com", NOW - timedelta(minutes=1), "no code here"),
        }
    )
    g = open_gmail(mail_cfg, tmp_path, api)
    assert g.find_code(["greenhouse.io", "lever.co"], NOW - timedelta(minutes=10)) == "222222"
    assert api.calls[0][1][0] == ("q", "from:(greenhouse.io OR lever.co) newer_than:1d")
    assert g.find_code(["greenhouse.io"], NOW) is None
    assert g.find_code([], NOW - timedelta(hours=1)) is None


def test_sign_in_runs_the_loopback_flow_and_saves_a_private_token(mail_cfg, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / mail_cfg["client_secrets"]).write_text("{}")
    seen = {}

    class Flow:
        def run_local_server(self, **kw):
            seen["server"] = kw
            return FakeCreds()

    def factory(path, scopes):
        seen["factory"] = (path, scopes)
        return Flow()

    sign_in(mail_cfg, tmp_path, flow_factory=factory)
    token = tmp_path / mail_cfg["token"]
    assert seen["factory"] == (str(tmp_path / mail_cfg["client_secrets"]), SCOPES)
    assert seen["server"] == {"port": 0, "open_browser": True}
    assert stat.S_IMODE(token.stat().st_mode) == 0o600 and json.loads(token.read_text())["token"]


def test_sign_in_without_the_client_file_says_where_to_put_it(mail_cfg, tmp_path):
    with pytest.raises(MailError, match=r"data/gmail_client\.json"):
        sign_in(mail_cfg, tmp_path, flow_factory=lambda *a: pytest.fail("must not start"))


def test_the_only_scope_is_read_only_and_only_gets_go_to_gmail():
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    package = Path(jobapplier.__file__).parent
    scopes = {
        s for f in package.rglob("*.py") for s in re.findall(r"googleapis\.com/auth/[\w.]+", f.read_text())
    }
    assert scopes == {"googleapis.com/auth/gmail.readonly"}
    src = Path(gmail.__file__).read_text()
    assert gmail._BASE == "https://gmail.googleapis.com/gmail/v1/users/me"
    assert set(re.findall(r"https?://([\w.]+)", src)) == {"gmail.googleapis.com", "www.googleapis.com"}
    assert not re.search(r"\.(post|put|patch|delete|request|send|modify|trash)\(|requests\.|urllib", src)
