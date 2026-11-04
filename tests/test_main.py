import logging
import shutil
from pathlib import Path

import httpx
import pytest

from jobapplier import __main__ as app
from jobapplier.notion.client import NotionError, call, make_client
from jobapplier.system import launchd
from jobapplier.system.lock import locked
from tests.conftest import AFFIRM_INDEED, FakeNotion, FakeSource


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake repo root with the shipped defaults, a token in the environment, and notify recorded."""
    shutil.copy(app._ROOT / "config.default.yaml", tmp_path / "config.default.yaml")
    monkeypatch.setattr(app, "_ROOT", tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    notes = []
    monkeypatch.setattr(app, "notify", lambda title, message: notes.append((title, message)))
    return tmp_path, notes


def set_ids(root):
    (root / "config.yaml").write_text("notion: {database_id: db1, data_source_id: ds1}\n")


def test_root_is_the_repo_root():
    assert (app._ROOT / "config.default.yaml").exists()


def test_main_without_notion_ids_notifies_and_fails(home):
    _, notes = home
    assert app.main(["discover"]) == 1
    assert len(notes) == 1 and "python -m jobapplier notion-setup" in notes[0][1]


def test_main_returns_zero_when_locked(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "make_client", lambda token: pytest.fail("must not start work"))
    (root / "data").mkdir()
    with locked(str(root / "data" / "run.lock")):
        assert app.main(["discover"]) == 0
    assert notes == []


def test_main_runs_and_notifies_on_alert(home, monkeypatch):
    root, notes = home
    set_ids(root)
    n = FakeNotion()
    monkeypatch.setattr(app, "make_client", lambda token: n)
    monkeypatch.setattr(
        app,
        "build_sources",
        lambda cfg: [
            FakeSource("indeed", ([AFFIRM_INDEED], None)),
            FakeSource("freehire", ([], "freehire: HTTP 500")),
        ],
    )
    assert app.main(["discover"]) == 0
    assert len(n.rows) == 1 and (root / "data" / "jobs.db").exists()
    assert len(notes) == 1 and "freehire: HTTP 500" in notes[0][1]


def test_main_unexpected_exception_notifies_and_returns_one(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "make_client", lambda token: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app.main(["discover"]) == 1
    assert len(notes) == 1 and "boom" in notes[0][1]


def test_main_reads_token_from_env_file(home, monkeypatch):
    root, _ = home
    set_ids(root)
    monkeypatch.delenv("NOTION_TOKEN")
    (root / ".env").write_text("NOTION_TOKEN=from-file\n")
    tokens = []
    monkeypatch.setattr(app, "make_client", lambda token: tokens.append(token) or FakeNotion())
    monkeypatch.setattr(app, "build_sources", lambda cfg: [])
    assert app.main(["discover"]) == 0
    assert tokens == ["from-file"]


def test_discover_without_token_notifies_and_fails(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.delenv("NOTION_TOKEN")
    assert app.main(["discover"]) == 1
    assert len(notes) == 1 and "NOTION_TOKEN" in notes[0][1] and ".env" in notes[0][1]


def test_notion_setup_without_token_prints_message_and_fails(home, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN")
    assert app.main(["notion-setup", "page1"]) == 1
    assert "NOTION_TOKEN" in capsys.readouterr().out


def test_install_agent_dispatches_to_launchd(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(app.shutil, "which", lambda name: "/opt/bin/uv")
    monkeypatch.setattr(
        launchd, "install", lambda repo, uv: calls.append((repo, uv)) or Path("/p/agent.plist")
    )
    assert app.main(["install-agent"]) == 0
    assert calls == [(app._ROOT, "/opt/bin/uv")]
    assert "/p/agent.plist" in capsys.readouterr().out


def test_install_agent_without_uv_fails(monkeypatch, capsys):
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(launchd, "install", lambda repo, uv: pytest.fail("must not install"))
    assert app.main(["install-agent"]) == 1
    assert "uv" in capsys.readouterr().out


def test_uninstall_agent_dispatches_to_launchd(monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "uninstall", lambda: calls.append(1))
    assert app.main(["uninstall-agent"]) == 0
    assert calls == [1]


def test_notion_setup_creates_database_styles_it_and_prints_snippet(home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        app, "make_client", lambda token, notion_version=None: f"client-{token}-{notion_version}"
    )
    monkeypatch.setattr(
        app, "create_database", lambda n, page, title: calls.append((n, page, title)) or ("db9", "ds9")
    )
    monkeypatch.setattr(app, "style_database", lambda *args: calls.append(args))
    assert app.main(["notion-setup", "page1"]) == 0
    assert calls == [
        ("client-tok-None", "page1", "Full Time Role Search"),
        ("client-tok-None", "client-tok-2026-03-11", "db9", "ds9", "Full Time Role Search"),
    ]
    out = capsys.readouterr().out
    assert "config.yaml" in out and "database_id: db9" in out and "data_source_id: ds9" in out


def test_notion_setup_survives_a_failing_view_setup(home, monkeypatch, capsys):
    def fail(*args):
        raise NotionError("400 unsupported")

    monkeypatch.setattr(app, "make_client", lambda token, notion_version=None: None)
    monkeypatch.setattr(app, "create_database", lambda *args: ("db9", "ds9"))
    monkeypatch.setattr(app, "style_database", fail)
    assert app.main(["notion-setup", "page1"]) == 0
    out = capsys.readouterr().out
    assert "group the view by Rank yourself" in out and "400 unsupported" in out and "database_id: db9" in out


def test_main_keeps_httpx_request_logs_out(monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)  # restored after the test
    monkeypatch.setattr(launchd, "uninstall", lambda: None)
    app.main(["uninstall-agent"])
    caplog.set_level(logging.INFO)
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    call(make_client("tok", http).pages.create, parent={}, properties={})
    assert not [r for r in caplog.records if r.name == "httpx"]
