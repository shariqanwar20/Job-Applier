from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest

from jobapplier.apply import browser
from jobapplier.core.clusters import assign, connect, record_resume
from jobapplier.core.identity import posting_identity
from jobapplier.models import Posting
from jobapplier.pipelines import apply as pipeline
from jobapplier.pipelines.apply import run_apply
from tests.apply.test_browser import FakePage
from tests.apply.test_fill import VALUES, field
from tests.conftest import FakeNotion

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def text(value):
    return [{"text": {"content": value}}]


def row(job_id, company, status="Tailored"):
    return {
        "Title": {"title": text("Developer")},
        "Company": {"rich_text": text(company)},
        "Job ID": {"rich_text": text(job_id)},
        "Status": {"select": {"name": status}},
        "Resume": {"files": [{"name": f"A_{job_id}.pdf"}]},
        "Notes": {"rich_text": []},
    }


class FakeContext:
    """A persistent Chrome context: it starts with one blank tab (pages); new_page hands out the rest."""

    def __init__(self, pages):
        self.queue, self.opened, self.closed = list(pages), [], False
        self.pages = [self.queue.pop(0)] if self.queue else []
        self.opened += self.pages
        self.made = 0

    def new_page(self):
        self.made += 1
        page = self.queue.pop(0)
        self.opened.append(page)
        return page


@pytest.fixture
def home(tmp_path, config, rules):
    config["notion"]["data_source_id"] = "ds1"
    (tmp_path / "output").mkdir()
    conn = connect(":memory:")
    for n, url in (
        (1, "https://jobs.lever.co/pri/1"),
        (2, "https://ca.linkedin.com/jobs/view/2"),
        (3, "https://job-boards.greenhouse.io/acme/jobs/3"),
    ):
        p = Posting("indeed", f"in-{n}", f"Developer {n}", f"Co{n}", "Toronto, ON", url)
        p.identity = posting_identity(url, rules)
        assign(conn, p, rules, NOW, False)
        pdf = tmp_path / "output" / f"A_J{n}.pdf"
        pdf.write_bytes(b"%PDF")
        record_resume(conn, n, str(pdf))
    n = FakeNotion()
    n.rows = {
        "p1": row("J1", "PRI"),
        "p2": row("J2", "Board Co", status="Reviewed"),
        "p3": row("J3", "Acme", status="Reviewed"),
        "p4": row("J4", "Gone", status="Needs review"),
    }
    return tmp_path, config, rules, conn, n


def run(home, answers, pages=None, job_id=None):
    _, cfg, rules, conn, n = home
    ctx = FakeContext(pages or [FakePage([[field("1", "Email")]]) for _ in range(3)])

    @contextmanager
    def fake_browser():
        yield ctx
        ctx.closed = True

    asked, said = [], []

    def ask(prompt):
        asked.append(prompt)
        return answers.pop(0)

    result = run_apply(
        conn,
        cfg,
        n,
        rules,
        VALUES,
        fake_browser,
        job_id,
        ask=ask,
        say=said.append,
        today=lambda: date(2026, 11, 4),
    )
    return result, ctx, asked, said


def test_apply_opens_ready_rows_asks_and_marks_the_submitted_ones(home):
    *_, n = home
    result, ctx, asked, said = run(home, ["", "y", "n"])
    assert [p.url for p in ctx.opened] == [
        "https://jobs.lever.co/pri/1/apply",
        "https://job-boards.greenhouse.io/acme/jobs/3",
    ]
    assert asked == [
        "Submit the ones you want in Chrome, then press Enter here.",
        "Submitted J1 (PRI)? [y/N] ",
        "Submitted J3 (Acme)? [y/N] ",
    ]
    assert n.rows["p1"]["Status"] == {"select": {"name": "Submitted"}}
    assert (
        n.rows["p1"]["Notes"]["rich_text"][0]["text"]["content"] == "Submitted 2026-11-04 via jobs.lever.co"
    )
    assert n.rows["p3"]["Status"] == {"select": {"name": "Reviewed"}}  # N leaves it as it was
    assert ctx.closed and result == {"opened": 2, "skipped": 1, "submitted": 1}
    assert any(line.startswith("skipped J2 (Board Co): only a job-board link") for line in said)
    assert any(line.startswith("opened J1 (PRI)") and "filled 1 fields" in line for line in said)


def test_apply_with_job_opens_that_row_whatever_its_status(home):
    *_, n = home
    n.rows["p4"] = {**row("J1", "PRI", status="Submitted")}
    del n.rows["p1"]
    result, ctx, _, _ = run(home, ["", ""], job_id="J1")
    assert len(ctx.opened) == 1 and result["submitted"] == 0


def test_apply_caps_tabs_and_says_so(home):
    _, cfg, *_ = home
    cfg["apply"]["max_tabs"] = 1
    result, ctx, _, said = run(home, ["", ""])
    assert len(ctx.opened) == 1 and result["skipped"] == 2
    assert any("J3 (Acme): over apply.max_tabs (1)" in line for line in said)


def test_nothing_ready_opens_no_browser(home):
    *_, n = home
    n.rows = {}

    @contextmanager
    def no_browser():
        pytest.fail("must not open Chrome")
        yield

    _, cfg, rules, conn, _ = home
    said = []
    result = run_apply(conn, cfg, n, rules, VALUES, no_browser, ask=pytest.fail, say=said.append)
    assert result == {"opened": 0, "skipped": 0, "submitted": 0} and said == ["Nothing ready to apply."]


def test_a_tab_that_fails_to_load_is_reported_and_the_rest_still_open(home, monkeypatch):
    calls = []

    def prepare(page, target, values, cfg, rules):
        calls.append(target.job_id)
        if target.job_id == "J1":
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        return {"filled": 0, "missing": [], "login": True, "pages": 1}

    monkeypatch.setattr(pipeline, "prepare", prepare)
    _, _, asked, said = run(home, ["", "n", "n"])
    assert calls == ["J1", "J3"] and len(asked) == 3
    assert any("J1 (PRI)" in line and "ERR_NAME_NOT_RESOLVED" in line for line in said)
    assert any(line.startswith("opened J3 (Acme)") and "sign in yourself" in line for line in said)


def test_end_of_input_counts_as_no(home):
    def eof(prompt):
        raise EOFError

    _, cfg, rules, conn, n = home
    ctx = FakeContext([FakePage([[]]), FakePage([[]])])

    @contextmanager
    def fake_browser():
        yield ctx

    result = run_apply(conn, cfg, n, rules, VALUES, fake_browser, ask=eof, say=lambda line: None)
    assert result["submitted"] == 0 and n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}


def test_the_pipeline_uses_the_real_prepare():
    assert pipeline.prepare is browser.prepare


def test_the_opened_line_counts_pages(home, monkeypatch):
    monkeypatch.setattr(
        pipeline, "prepare", lambda *a: {"filled": 7, "missing": [], "login": False, "pages": 3}
    )
    _, _, _, said = run(home, ["", "n", "n"])
    assert any(
        line.startswith("opened J1 (PRI)") and line.endswith("filled 7 fields over 3 pages") for line in said
    )


def test_the_first_target_uses_chromes_blank_tab(home):
    _, ctx, _, _ = run(home, ["", "n", "n"])
    assert len(ctx.opened) == 2 and ctx.made == 1  # two targets, one new tab


def asker(answers, asked):
    def ask(prompt):
        asked.append(prompt)
        got = answers.pop(0)
        if got is KeyboardInterrupt:
            raise KeyboardInterrupt
        return got

    return ask


def test_ctrl_c_at_a_prompt_asks_before_closing_chrome(home):
    _, cfg, rules, conn, n = home
    asked = []

    @contextmanager
    def fake_browser():
        yield FakeContext([FakePage([[]]), FakePage([[]])])

    answers = [KeyboardInterrupt, "n", "", "y", "n"]  # ^C, keep Chrome, then answer as usual
    ask = asker(answers, asked)
    result = run_apply(conn, cfg, n, rules, VALUES, fake_browser, ask=ask, say=lambda line: None)
    assert asked[:3] == [
        "Submit the ones you want in Chrome, then press Enter here.",
        "Close Chrome? [y/N] ",
        "Submit the ones you want in Chrome, then press Enter here.",
    ]
    assert result["submitted"] == 1 and n.rows["p1"]["Status"] == {"select": {"name": "Submitted"}}

    n.rows["p1"]["Status"] = {"select": {"name": "Tailored"}}
    asked.clear()
    said = []
    ask = asker(["", KeyboardInterrupt, "y"], asked)  # ^C at the first y/N, then close
    result = run_apply(conn, cfg, n, rules, VALUES, fake_browser, ask=ask, say=said.append)
    assert asked[-1] == "Close Chrome? [y/N] " and len(asked) == 3
    assert result["submitted"] == 0 and n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}
    assert said[-1] == "Closing Chrome; rows not answered keep their Status."


def test_a_restored_tab_is_left_alone_and_a_new_one_opened(home):
    restored = FakePage([[]])
    restored.url = "https://mail.example.com/inbox"
    pages = [restored, FakePage([[field("1", "Email")]]), FakePage([[field("1", "Email")]])]
    _, ctx, _, _ = run(home, ["", "n", "n"], pages=pages)
    assert restored.url == "https://mail.example.com/inbox" and ctx.made == 2
