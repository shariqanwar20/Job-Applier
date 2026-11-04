import logging
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from jobapplier import __main__ as app
from jobapplier.core import clusters
from jobapplier.mail.gmail import MailError
from jobapplier.notion.client import NotionError, call, make_client
from jobapplier.system import launchd
from jobapplier.system.lock import locked
from tests.conftest import AFFIRM_INDEED, FakeMail, FakeNotion, FakeSource


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


def test_mail_login_signs_in_and_prints_the_address(home, monkeypatch, capsys):
    root, _ = home
    calls = []
    monkeypatch.setattr(app, "sign_in", lambda cfg, r: calls.append((cfg["token"], r)))
    monkeypatch.setattr(
        app, "Gmail", lambda cfg, r: type("G", (), {"address": lambda self: "me@example.com"})()
    )
    assert app.main(["mail-login"]) == 0
    assert calls == [("data/gmail_token.json", root)]
    assert capsys.readouterr().out.strip() == "Gmail OK: me@example.com"


def test_mail_login_reports_failures_without_a_traceback_or_their_text(home, monkeypatch, capsys):
    def deny(cfg, r):
        raise RuntimeError("code=SECRET-AUTH-CODE")

    monkeypatch.setattr(app, "sign_in", deny)
    assert app.main(["mail-login"]) == 1
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "SECRET" not in out

    def missing(cfg, r):
        raise MailError("mail: put the OAuth client JSON at data/gmail_client.json (see README, Mail)")

    monkeypatch.setattr(app, "sign_in", missing)
    assert app.main(["mail-login"]) == 1
    assert capsys.readouterr().out.startswith("mail: put the OAuth client JSON")


def _one_posting(monkeypatch, n):
    monkeypatch.setattr(app, "make_client", lambda token: n)
    monkeypatch.setattr(app, "build_sources", lambda cfg: [FakeSource("indeed", ([AFFIRM_INDEED], None))])


def test_discover_leaves_mail_off_until_the_token_file_exists(home, monkeypatch):
    root, _ = home
    set_ids(root)
    _one_posting(monkeypatch, FakeNotion())
    monkeypatch.setattr(app, "Gmail", lambda *a, **kw: pytest.fail("mail must be off"))
    assert app.main(["discover"]) == 0


def test_discover_uses_mail_when_the_token_file_exists(home, monkeypatch):
    root, _ = home
    set_ids(root)
    (root / "data").mkdir()
    (root / "data" / "gmail_token.json").write_text("{}")
    n, mail, made = FakeNotion(), FakeMail({"affirm": "2026-05-02: Application received"}), []
    _one_posting(monkeypatch, n)
    monkeypatch.setattr(app, "Gmail", lambda cfg, r: made.append((cfg["token"], r)) or mail)
    assert app.main(["discover"]) == 0
    assert made == [("data/gmail_token.json", root)] and mail.calls == ["affirm"]
    assert n.rows["page-1"]["Flags"]["multi_select"] == [{"name": "Applied before"}]


FAKE_CLAUDE = """#!{python}
import json, sys
prompt = sys.stdin.read()
assert "humanizer" in prompt and "--json-schema" in sys.argv
items = [{{"id": "exp1.b1", "text": "Built a Go REST API that served 3 clinics"}}, {{"id": "exp1.b2"}}]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                  "structured_output": {{"sections": [{{"section": "Experience", "items": items}}]}}}}))
"""

FAKE_TECTONIC = """#!{python}
import pathlib, sys
from pypdf import PdfWriter
src = pathlib.Path(sys.argv[-1])
assert "Built a Go REST API" in src.read_text()
w = PdfWriter()
w.add_blank_page(612, 792)
with open(pathlib.Path(sys.argv[sys.argv.index("--outdir") + 1]) / (src.stem + ".pdf"), "wb") as f:
    w.write(f)
"""


def _script(path, body):
    path.write_text(body.format(python=sys.executable))
    path.chmod(0o755)
    return path


def test_tailor_one_job_end_to_end(home, monkeypatch, rules):
    root, notes = home
    claude = _script(root / "fake_claude", FAKE_CLAUDE)
    tectonic = _script(root / "fake_tectonic", FAKE_TECTONIC)
    (root / "config.yaml").write_text(
        "notion: {database_id: db1, data_source_id: ds1}\n"
        "profile: {name: Alex Example}\n"
        f"llm: {{claude: {claude}}}\n"
        f"tailor: {{tectonic: {tectonic}}}\n"
    )
    (root / "resume").mkdir()
    shutil.copy(
        Path(app.__file__).parents[1] / "resume" / "example.master.tex", root / "resume" / "master.tex"
    )
    (root / "data").mkdir()
    conn = clusters.connect(str(root / "data" / "jobs.db"))
    clusters.assign(conn, AFFIRM_INDEED, rules, datetime(2026, 11, 1, tzinfo=UTC), False)
    conn.commit()
    n = FakeNotion()
    n.rows = {
        "p1": {
            "Title": {"title": [{"text": {"content": "Software Engineer II, Full Stack"}}]},
            "Company": {"rich_text": [{"text": {"content": "Affirm"}}]},
            "Job ID": {"rich_text": [{"text": {"content": "J1"}}]},
            "Decision": {"select": {"name": "New"}},
            "Status": {"select": {"name": "New"}},
        }
    }
    monkeypatch.setattr(app, "make_client", lambda token: n)
    assert app.main(["tailor", "--job", "J1"]) == 0
    name = "Alex_Example_Affirm_Software_Engineer_II_Full_Stack.pdf"
    assert len(PdfReader(root / "output" / name).pages) == 1
    assert n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}
    assert n.rows["p1"]["Resume"]["files"][0]["name"] == name
    assert n.uploads["upload-1"][1].startswith(b"%PDF") and notes == []


def test_tailor_reports_row_errors(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "make_client", lambda token: FakeNotion())
    assert app.main(["tailor", "--job", "J1"]) == 1
    assert notes and "profile.name" in notes[0][1]


def test_tailor_returns_zero_when_locked(home, monkeypatch):
    root, notes = home
    set_ids(root)
    monkeypatch.setattr(app, "make_client", lambda token: pytest.fail("must not start work"))
    (root / "data").mkdir()
    with locked(str(root / "data" / "run.lock")):
        assert app.main(["tailor"]) == 0
    assert notes == []


def test_run_discovers_only_when_the_last_discover_is_old_then_tailors(monkeypatch, tmp_path):
    import os

    from jobapplier import __main__ as m

    calls = []
    monkeypatch.setattr(m, "_ROOT", tmp_path)
    monkeypatch.setattr(m, "load_config", lambda root: {"schedule": {"discover_every_minutes": 120}})
    monkeypatch.setattr(m, "_discover", lambda: calls.append("discover") or 0)
    monkeypatch.setattr(m, "_tailor", lambda job: calls.append("tailor") or 0)
    monkeypatch.setattr(m, "_notify_ready", lambda: 0)
    assert m._run() == 0 and calls == ["discover", "tailor"]
    assert m._run() == 0 and calls == ["discover", "tailor", "tailor"]  # discovered just now
    stamp = tmp_path / "data" / "last_discover"
    os.utime(stamp, (stamp.stat().st_mtime - 3 * 3600,) * 2)
    assert m._run() == 0 and calls[-2:] == ["discover", "tailor"]


def test_apply_without_a_profile_names_the_example(home, capsys):
    root, _ = home
    set_ids(root)
    assert app.main(["apply"]) == 1
    assert "profile.example.yaml" in capsys.readouterr().out


def _apply_home(root, rules, monkeypatch):
    """A fake root ready for `apply --job J1`: profile, master resume, a recorded PDF and a ready row."""
    set_ids(root)
    shutil.copy(Path(app.__file__).parents[1] / "profile.example.yaml", root / "profile.yaml")
    (root / "resume").mkdir()
    (root / "resume" / "master.tex").write_text("\\section{Experience}\nBuilt a billing service % id:a\n")
    (root / "data").mkdir()
    conn = clusters.connect(str(root / "data" / "jobs.db"))
    clusters.assign(conn, AFFIRM_INDEED, rules, datetime(2026, 11, 1, tzinfo=UTC), False)
    (root / "output").mkdir()
    (root / "output" / "A_J1.pdf").write_bytes(b"%PDF")
    clusters.record_resume(conn, 1, str(root / "output" / "A_J1.pdf"))
    conn.commit()
    n = FakeNotion()
    n.rows = {
        "p1": {
            "Company": {"rich_text": [{"text": {"content": "Affirm"}}]},
            "Job ID": {"rich_text": [{"text": {"content": "J1"}}]},
            "Status": {"select": {"name": "Tailored"}},
            "Resume": {"files": [{"name": "A_J1.pdf"}]},
        }
    }
    monkeypatch.setattr(app, "make_client", lambda token: n)
    return n


def test_apply_runs_the_agent_in_chrome_and_marks_it_submitted(home, monkeypatch, rules, capsys):
    from contextlib import contextmanager

    from tests.apply.fakes import FakeLlm, FakePage, answer, el, state

    root, _ = home
    n = _apply_home(root, rules, monkeypatch)
    page, profiles = FakePage([state(el(1, "First Name"))]), []
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": "Alex"}, status="ready_to_submit")])

    @contextmanager
    def fake_chrome(profile_dir):
        profiles.append(profile_dir)
        yield type("Ctx", (), {"pages": [page]})()

    monkeypatch.setattr(app, "chrome", fake_chrome)
    monkeypatch.setattr(
        app.llm,
        "run_with_cost",
        lambda prompt, schema, model, writing, **kw: llm(prompt, schema, model, writing),
    )
    answers = iter(["", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert app.main(["apply", "--job", "J1"]) == 0
    assert profiles == [root / "data" / "browser-profile"]
    assert page.url == "https://job-boards.greenhouse.io/affirm/jobs/8042399003"
    assert page.done == [("fill", 1, "Alex")]
    assert "Built a billing service" in llm.calls[0]["prompt"] and "Alex" in llm.calls[0]["prompt"]
    assert n.rows["p1"]["Status"] == {"select": {"name": "Submitted"}}
    assert "opened J1 (Affirm)" in capsys.readouterr().out
    assert (root / "logs" / "apply-J1.json").exists()


def test_apply_no_prompt_asks_nothing(home, monkeypatch, rules):
    from contextlib import contextmanager

    from tests.apply.fakes import FakeLlm, FakePage, answer, el, state

    root, _ = home
    n = _apply_home(root, rules, monkeypatch)
    page = FakePage([state(el(1, "First Name"))])

    @contextmanager
    def fake_chrome(profile_dir):
        yield type("Ctx", (), {"pages": [page]})()

    llm = FakeLlm([answer(status="ready_to_submit")])
    monkeypatch.setattr(app, "chrome", fake_chrome)
    monkeypatch.setattr(
        app.llm,
        "run_with_cost",
        lambda prompt, schema, model, writing, **kw: llm(prompt, schema, model, writing),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("--no-prompt must not ask"))
    assert app.main(["apply", "--job", "J1", "--no-prompt"]) == 0
    assert n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}


def test_apply_without_a_master_resume_says_so(home, monkeypatch, rules, capsys):
    root, _ = home
    _apply_home(root, rules, monkeypatch)
    (root / "resume" / "master.tex").unlink()
    assert app.main(["apply"]) == 1
    assert "example.master.tex" in capsys.readouterr().out


def _ready_row(job_id):
    return {
        "Job ID": {"rich_text": [{"text": {"content": job_id}}]},
        "Status": {"select": {"name": "Tailored"}},
        "Resume": {"files": [{"name": f"A_{job_id}.pdf"}]},
    }


def test_run_notifies_once_per_newly_ready_row_and_opens_no_browser(home, monkeypatch):
    root, notes = home
    set_ids(root)
    n = FakeNotion()
    n.rows = {"p1": _ready_row("J1"), "p2": _ready_row("J2")}
    monkeypatch.setattr(app, "make_client", lambda token: n)
    monkeypatch.setattr(app, "_discover", lambda: 0)
    monkeypatch.setattr(app, "_tailor", lambda job: 0)
    monkeypatch.setattr(app, "chrome", lambda *a: pytest.fail("run must not open Chrome"))
    run = "uv run python -m jobapplier apply"
    assert app.main(["run"]) == 0
    assert notes == [("JobApplier", f"2 applications ready: run `{run}`")]
    assert app.main(["run"]) == 0 and len(notes) == 1  # the same rows: no second notice
    n.rows["p3"] = _ready_row("J3")
    assert app.main(["run"]) == 0
    assert notes[-1] == ("JobApplier", f"1 application ready: run `{run}`")


def test_run_checks_ready_rows_even_when_tailor_fails(home, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_discover", lambda: 0)
    monkeypatch.setattr(app, "_tailor", lambda job: 1)
    monkeypatch.setattr(app, "_notify_ready", lambda: calls.append(1) or 0)
    assert app.main(["run"]) == 1 and calls == [1]
