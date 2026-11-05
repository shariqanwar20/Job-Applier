from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest

from jobapplier.apply import agent
from jobapplier.core.clusters import assign, connect, record_resume
from jobapplier.core.identity import posting_identity
from jobapplier.models import Posting
from jobapplier.pipelines import apply as pipeline
from jobapplier.pipelines.apply import run_apply
from tests.apply.fakes import CAPTCHA, FakeLlm, FakePage, answer, el, owner_goes_to, state
from tests.conftest import FakeNotion

VALUES = {"name.first": "Alex", "email": "alex@example.com"}

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


def email_page():
    return FakePage([state(el(1, "Email"))])


def llm():
    return FakeLlm(
        [answer({"op": "fill", "ref": 1, "value": "alex@example.com"}), answer(status="ready_to_submit")]
    )


def run(home, answers, pages=None, job_id=None, prompt=True, wait=True, model=None):
    tmp, cfg, rules, conn, n = home
    ctx = FakeContext(pages or [email_page() for _ in range(3)])

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
        "RESUME",
        fake_browser,
        model or llm(),
        tmp / "logs",
        job_id,
        ask=ask,
        say=said.append,
        today=lambda: date(2026, 11, 4),
        prompt=prompt,
        wait=wait,
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
    assert any(
        line.startswith("opened J1 (PRI)") and "ready for you to submit" in line and "filled 1 fields" in line
        for line in said
    )


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

    tmp, cfg, rules, conn, _ = home
    said = []
    result = run_apply(
        conn, cfg, n, rules, VALUES, "", no_browser, llm(), tmp, ask=pytest.fail, say=said.append
    )
    assert result == {"opened": 0, "skipped": 0, "submitted": 0} and said == ["Nothing ready to apply."]


def test_a_tab_that_fails_to_load_is_reported_and_the_rest_still_open(home):
    failing = email_page()

    def goto(url, wait_until, timeout):
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    failing.goto = goto
    login = FakePage([state(el(1, "Password", type="password"))])
    _, _, asked, said = run(home, ["", "n", "n"], pages=[failing, login], wait=False)
    assert len(asked) == 3
    assert any("J1 (PRI)" in line and "ERR_NAME_NOT_RESOLVED" in line for line in said)
    assert any(
        line.startswith("opened J3 (Acme)") and "sign in or create the account yourself" in line
        for line in said
    )


def test_end_of_input_counts_as_no(home):
    def eof(prompt):
        raise EOFError

    tmp, cfg, rules, conn, n = home
    ctx = FakeContext([email_page(), email_page()])

    @contextmanager
    def fake_browser():
        yield ctx

    result = run_apply(
        conn, cfg, n, rules, VALUES, "", fake_browser, llm(), tmp, ask=eof, say=lambda line: None
    )
    assert result["submitted"] == 0 and n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}


def test_the_pipeline_uses_the_real_loop():
    assert pipeline.Application is agent.Application


def test_the_opened_line_says_why_it_stopped_what_is_left_and_the_cost(home, monkeypatch):
    result = {"status": "stuck", "note": "", "filled": 7, "missing": ["Why us?"], "cost": 0.1234}
    monkeypatch.setattr(agent.Application, "finish", lambda self: result)
    _, _, _, said = run(home, ["", "n", "n"])
    assert any(
        line.startswith("opened J1 (PRI)")
        and line.endswith(
            "the page did not change for 3 steps; filled 7 fields, 1 required still empty, $0.12"
        )
        for line in said
    )


def test_no_prompt_asks_nothing_marks_nothing_and_closes_chrome(home):
    *_, n = home
    result, ctx, asked, said = run(home, [], prompt=False)
    assert asked == [] and result["submitted"] == 0 and ctx.closed
    assert n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}
    assert said[-1] == "--no-prompt: nothing marked Submitted; closing Chrome."


def test_each_application_writes_its_trace(home):
    tmp, *_ = home
    run(home, ["", "n", "n"])
    assert (tmp / "logs" / "apply-J1.json").exists() and (tmp / "logs" / "apply-J3.json").exists()


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
    tmp, cfg, rules, conn, n = home
    asked = []

    @contextmanager
    def fake_browser():
        yield FakeContext([email_page(), email_page()])

    def go(ask, say):
        return run_apply(conn, cfg, n, rules, VALUES, "", fake_browser, llm(), tmp, ask=ask, say=say)

    answers = [KeyboardInterrupt, "n", "", "y", "n"]  # ^C, keep Chrome, then answer as usual
    ask = asker(answers, asked)
    result = go(ask, lambda line: None)
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
    result = go(ask, said.append)
    assert asked[-1] == "Close Chrome? [y/N] " and len(asked) == 3
    assert result["submitted"] == 0 and n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}
    assert said[-1] == "Closing Chrome; rows not answered keep their Status."


def test_a_restored_tab_is_left_alone_and_a_new_one_opened(home):
    restored = FakePage([state()], url="https://mail.example.com/inbox")
    pages = [restored, email_page(), email_page()]
    _, ctx, _, _ = run(home, ["", "n", "n"], pages=pages)
    assert restored.url == "https://mail.example.com/inbox" and ctx.made == 2


def by_company(model):
    return ["PRI" if "at PRI" in c["prompt"] else "Acme" for c in model.calls]


def test_applications_waiting_for_the_owner_get_a_second_pass_after_the_others_are_filled(home):
    captcha = FakePage([CAPTCHA, state(el(1, "Email"))])
    owner_goes_to(captcha, 1)
    model = llm()
    model.answers = [answer({"op": "fill", "ref": 1, "value": "alex@example.com"}, status="ready_to_submit")]
    _, _, _, said = run(home, ["", "n", "n"], pages=[captcha, email_page()], model=model)
    assert by_company(model) == ["Acme", "PRI"]  # J1 (PRI) waited until J3 (Acme) was filled
    assert captcha.fronted >= 1 and captcha.done == [("fill", 1, "alex@example.com")]
    assert any(line.startswith("J1 (PRI) needs you in its tab") for line in said)
    assert any(line.startswith("opened J1 (PRI)") and "ready for you to submit" in line for line in said)


def test_no_wait_finishes_a_waiting_application_without_polling(home):
    captcha = FakePage([CAPTCHA])
    _, _, _, said = run(home, ["", "n", "n"], pages=[captcha, email_page()], wait=False)
    assert 3000 not in captcha.waits and captcha.fronted == 0
    assert any(line.startswith("opened J1 (PRI)") and "captcha" in line for line in said)
