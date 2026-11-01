import shutil

import pytest
from tests.conftest import AFFIRM_INDEED, FakeNotion, FakeSource

from jobapplier import __main__ as app
from jobapplier.system.lock import locked


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake repo root with the shipped defaults, and notify recorded instead of shown."""
    shutil.copy(app.ROOT / "config.default.yaml", tmp_path / "config.default.yaml")
    monkeypatch.setattr(app, "ROOT", tmp_path)
    notes = []
    monkeypatch.setattr(app, "notify", lambda title, message: notes.append((title, message)))
    return tmp_path, notes


def set_ids(root):
    (root / "config.yaml").write_text("notion: {database_id: db1, data_source_id: ds1}\n")


def test_root_is_the_repo_root():
    assert (app.ROOT / "config.default.yaml").exists()


def test_main_without_notion_ids_notifies_and_fails(home):
    root, notes = home
    assert app.main(["discover"]) == 1
    assert len(notes) == 1 and "python -m jobapplier notion-setup" in notes[0][1]


def test_main_returns_zero_when_locked(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "keychain", lambda s: pytest.fail("must not start work"))
    (root / "data").mkdir()
    with locked(str(root / "data" / "run.lock")):
        assert app.main(["discover"]) == 0
    assert notes == []


def test_main_runs_and_notifies_on_alert(home, monkeypatch):
    root, notes = home
    set_ids(root)
    n = FakeNotion()
    monkeypatch.setattr(app, "keychain", lambda s: "tok")
    monkeypatch.setattr(app, "Notion", lambda token: n)
    monkeypatch.setattr(app, "build_sources", lambda cfg: [FakeSource("indeed", ([AFFIRM_INDEED], None)), FakeSource("freehire", ([], "freehire: HTTP 500"))])
    assert app.main(["discover"]) == 0
    assert len(n.pages) == 1 and (root / "data" / "jobs.db").exists()
    assert len(notes) == 1 and "freehire: HTTP 500" in notes[0][1]


def test_main_unexpected_exception_notifies_and_returns_one(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "keychain", lambda s: (_ for _ in ()).throw(RuntimeError("no keychain")))
    assert app.main(["discover"]) == 1
    assert len(notes) == 1 and "no keychain" in notes[0][1]


def test_notion_setup_creates_database_and_prints_snippet(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(app, "keychain", lambda s: "tok")
    monkeypatch.setattr(app, "Notion", lambda token: f"client-{token}")
    monkeypatch.setattr(app, "create_database", lambda n, page: calls.append((n, page)) or ("db9", "ds9"))
    assert app.main(["notion-setup", "page1"]) == 0
    assert calls == [("client-tok", "page1")]
    assert capsys.readouterr().out == "Paste into your config.yaml:\nnotion:\n  database_id: db9\n  data_source_id: ds9\n"
