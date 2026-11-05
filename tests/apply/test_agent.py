import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Error as PlaywrightError

from jobapplier.apply.agent import Application, resume_text
from jobapplier.apply.page import ClickRules
from jobapplier.apply.targets import Target
from jobapplier.llm import LlmError
from jobapplier.mail.gmail import MailError
from jobapplier.resume.master import parse
from tests.apply.fakes import (
    CAPTCHA,
    FakeGmail,
    FakeLlm,
    FakePage,
    answer,
    btn,
    el,
    link,
    owner_goes_to,
    state,
)

VALUES = {"name.first": "Alex", "email": "alex@example.com", "phone": "+1 416 555 0100"}
RESUME = "Experience\n- RESUME-LINE built a billing service"
WHY = "I build backend services, and Acme's billing work fits what I have done."

JOB_PAGE = state(
    link(1, "Back to jobs"), btn(2, "Apply now", goes=1), title="Software Developer", headings=["Developer"]
)
FORM = state(
    el(3, "Resume/CV", type="file", visible=False),
    el(4, "First name", required=True, form=True),
    el(5, "Why do you want to work here?", type="textarea", form=True),
    el(6, "Cover letter", required=True, form=True),
    btn(7, "Next", type="submit", form=True, goes=2),
)
REVIEW = state(btn(8, "Back"), btn(9, "Submit application", type="submit", form=True), title="Review")


@pytest.fixture
def job(config, tmp_path):
    pdf = tmp_path / "A_Acme.pdf"
    pdf.write_bytes(b"%PDF")
    target = Target("J7", "Acme", "Developer", "https://acme.example/jobs/7", pdf, "D" * 5000, {"id": "p7"})
    return target, config, tmp_path / "logs"


NOW = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)


def clock():
    """A minute later at every look: the start of each step."""
    times = (NOW + timedelta(minutes=i) for i in range(1000))
    return lambda: next(times)


def make(job, page, llm, gmail=None):
    target, cfg, logs = job
    rules = ClickRules.from_config(cfg["apply"])
    return Application(page, target, cfg, VALUES, RESUME, llm, rules, logs, gmail=gmail, now=clock())


def run(job, page, llm, gmail=None):
    """One loop to its first stop, with no waiting for the owner."""
    app = make(job, page, llm, gmail)
    app.advance()
    return app.finish()


def test_it_starts_the_application_fills_uploads_clicks_next_and_stops_at_ready_to_submit(job):
    target, _, _ = job
    page = FakePage([JOB_PAGE, FORM, REVIEW])
    llm = FakeLlm(
        [
            answer({"op": "click", "ref": 2}),
            answer({"op": "upload", "ref": 3, "value": "/etc/passwd"}),
            answer(
                {"op": "fill", "ref": 4, "value": "Alex"},
                {"op": "fill", "ref": 5, "value": WHY},
                {"op": "click", "ref": 7},
            ),
            answer({"op": "click", "ref": 9}),
            answer(status="ready_to_submit", note="Only the submit button is left."),
        ]
    )
    result = run(job, page, llm)
    assert page.url == target.url and page.clicks == ["Apply now", "Next"]
    assert page.done == [
        ("set_input_files", 3, str(target.pdf)),
        ("fill", 4, "Alex"),
        ("fill", 5, WHY),
    ]
    assert result["status"] == "ready_to_submit" and result["note"] == "Only the submit button is left."
    assert (
        result["filled"] == 3
        and result["steps"] == 5
        and result["answers"] == [("Why do you want to work here?", WHY)]
    )
    assert [c["model"] for c in llm.calls] == ["claude-haiku-5-5"] * 5
    assert not any(c["writing"] for c in llm.calls)
    last = llm.calls[-1]["prompt"]
    assert "REFUSED" in last and "Submit application" in last and "submit" in last.split("REFUSED")[1]
    first = llm.calls[0]["prompt"]
    assert "alex@example.com" in first and "RESUME-LINE" in first and "Developer at Acme" in first
    assert "D" * 3000 in first and "D" * 3001 not in first
    assert '"Apply now"' in first  # the snapshot


def test_the_banner_shows_the_status_note_filled_fields_answers_and_empty_required_fields(job):
    page = FakePage([FORM])
    llm = FakeLlm(
        [
            answer(
                {"op": "fill", "ref": 4, "value": "Alex"},
                {"op": "fill", "ref": 5, "value": WHY},
                status="needs_owner",
                note="I cannot write the cover letter truthfully.",
            )
        ]
    )
    result = run(job, page, llm)
    [(head, lines)] = page.banners
    assert "Check every page, then submit yourself" in head
    text = "\n".join(lines)
    assert "I cannot write the cover letter truthfully." in text
    assert "First name" in text and WHY in text  # every free-text answer, in full
    assert "Required and still empty: Cover letter" in text
    assert any("upload" in line and "A_Acme.pdf" in line for line in lines)  # not uploaded: say so
    assert result["missing"] == ["Cover letter"] and result["status"] == "needs_owner"


def test_apply_is_refused_after_a_fill_and_the_refusal_is_fed_back(job):
    page = FakePage([state(el(1, "First name", form=True), btn(2, "Apply", type="submit", form=True))])
    llm = FakeLlm(
        [
            answer({"op": "fill", "ref": 1, "value": "Alex"}, {"op": "click", "ref": 2}),
            answer(status="ready_to_submit"),
        ]
    )
    run(job, page, llm)
    assert page.clicks == [] and "REFUSED" in llm.calls[1]["prompt"]


def test_a_visible_password_field_stops_with_needs_account_before_asking_the_model(job):
    page = FakePage([state(el(1, "Email"), el(2, "Password", type="password"))])
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": "alex@example.com"})])
    result = run(job, page, llm)
    assert result["status"] == "needs_account" and llm.calls == [] and page.done == []
    assert page.banners[0][0].startswith("Sign in or create the account yourself")


def test_a_fill_in_a_password_form_is_refused_and_stops_with_needs_account(job):
    page = FakePage([state(el(1, "Email", pw_form=True), btn(2, "Next"))])
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": "alex@example.com"}, {"op": "click", "ref": 2})])
    result = run(job, page, llm)
    assert result["status"] == "needs_account" and page.done == [] and page.clicks == []


def test_max_steps_stops_the_loop(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["max_steps"] = 2
    page = FakePage([state(el(1, "Notes"))])
    values = iter(range(100))
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": str(next(values))}) for _ in range(5)])
    result = run(job, page, llm)
    assert result["status"] == "max_steps" and len(llm.calls) == 2


def test_an_unchanged_page_for_three_snapshots_stops_the_loop(job):
    page = FakePage([state(el(1, "Notes"), btn(2, "Next"))])
    llm = FakeLlm([answer({"op": "click", "ref": 2})])  # Next does nothing: a validation error
    result = run(job, page, llm)
    assert result["status"] == "stuck" and len(llm.calls) == 2


def test_an_llm_error_stops_the_loop_with_its_message(job):
    page = FakePage([state(el(1, "Notes"))])
    result = run(job, page, FakeLlm([LlmError("claude timed out after 300s")]))
    assert result["status"] == "llm_error" and "timed out" in result["note"]
    assert any("timed out" in line for line in page.banners[0][1])


def test_max_cost_stops_the_loop(job):
    page = FakePage([state(el(1, "Notes"))])
    values = iter(range(100))
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": str(next(values))}) for _ in range(5)], cost=0.3)
    result = run(job, page, llm)
    assert result["status"] == "max_cost" and len(llm.calls) == 2 and result["cost"] == pytest.approx(0.6)


def test_a_page_that_breaks_stops_the_loop(job):
    page = FakePage([JOB_PAGE, FORM])
    llm = FakeLlm([answer({"op": "click", "ref": 2})])

    def then_break(*a, **kw):
        page.broken = PlaywrightError("Execution context was destroyed")
        return llm(*a, **kw)

    result = run(job, page, then_break)
    assert result["status"] == "page_error" and "Execution context" in result["note"]
    assert page.clicks == ["Apply now"] and page.banners


def test_a_tab_opened_by_a_click_is_followed(job):
    form = FakePage([FORM], url="https://apply.acme.example/form")
    page = FakePage([state(btn(2, "Apply now", opens=form))])
    llm = FakeLlm([answer({"op": "click", "ref": 2}), answer(status="ready_to_submit")])
    run(job, page, llm)
    assert form.banners and not page.banners
    assert '"First name"' in llm.calls[1]["prompt"]
    assert "a new tab opened on apply.acme.example" in llm.calls[1]["prompt"]


def test_a_tab_this_step_did_not_open_is_not_followed(job):
    popup = FakePage([FORM])  # an ad or a chat window the page opened by itself
    page = FakePage([state(el(1, "City"), btn(2, "Next"))])
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": "Toronto"}), answer(status="ready_to_submit")])
    original = llm.__call__

    def opens_a_popup(*a, **kw):
        page.context.pages.append(popup)
        return original(*a, **kw)

    run(job, page, opens_a_popup)
    assert page.banners and not popup.banners


def test_a_failed_action_is_recorded_and_the_loop_goes_on(job):
    page = FakePage([state(el(1, "Notes", broken=True), el(2, "City"))])
    llm = FakeLlm(
        [
            answer({"op": "fill", "ref": 1, "value": "x"}, {"op": "fill", "ref": 2, "value": "Toronto"}),
            answer(status="ready_to_submit"),
        ]
    )
    result = run(job, page, llm)
    assert page.done == [("fill", 2, "Toronto")] and "FAILED" in llm.calls[1]["prompt"]
    assert result["filled"] == 1


def test_each_step_is_logged_with_its_cost_but_not_long_answers(job, caplog):
    caplog.set_level(logging.INFO)
    page = FakePage([FORM])
    llm = FakeLlm(
        [answer({"op": "fill", "ref": 5, "value": WHY + " " + WHY}), answer(status="ready_to_submit")]
    )
    run(job, page, llm)
    text = caplog.text
    assert "J7 step 1" in text and "$0.0100" in text and "total $0.0200" in text
    assert WHY not in text and "chars" in text


def test_the_trace_and_a_screenshot_are_saved_when_the_loop_stops(job):
    _, _, logs = job
    page = FakePage([JOB_PAGE, FORM])
    llm = FakeLlm(
        [
            answer({"op": "click", "ref": 2}),
            answer(
                {"op": "fill", "ref": 5, "value": WHY},
                {"op": "click", "ref": 9},
                status="needs_owner",
                note="n",
            ),
        ]
    )
    run(job, page, llm)
    assert page.shots == [str(logs / "apply-J7-2.png")] and (logs / "apply-J7-2.png").exists()
    trace = json.loads((logs / "apply-J7.json").read_text())
    assert trace["job"] == "J7" and trace["status"] == "needs_owner" and trace["note"] == "n"
    one, two = trace["steps"]
    assert {k: one["snapshot"][k] for k in ("url", "elements")} == {
        "url": "https://acme.example/jobs/7",
        "elements": 2,
    }
    assert one["snapshot"]["view"]["elements"][1]["text"] == "Apply now"
    assert one["asked"] == [{"op": "click", "ref": 2}] and one["done"] == ["click 2 (Apply now)"]
    assert two["refused"] == ["click 9: no element 9 on this page"]
    assert two["done"] == [f"fill 5 (Why do you want to work here?) = {WHY}"]  # answers belong in the trace
    assert two["status"] == "needs_owner" and two["cost"] == 0.01 and trace["cost"] == pytest.approx(0.02)
    raw = (logs / "apply-J7.json").read_text()
    assert "RESUME-LINE" not in raw and "+1 416 555 0100" not in raw  # no resume or profile text


def test_the_screenshot_is_taken_on_every_stop(job):
    _, _, logs = job
    page = FakePage([state(el(1, "Email"), el(2, "Password", type="password"))])
    run(job, page, FakeLlm([answer()]))
    assert page.shots == [str(logs / "apply-J7-1.png")]


def test_resume_text_lists_the_master_lines_by_section():
    tex = "\\section{Experience}\n\\item Built billing % id:a\n\\section{Skills}\nPython % id:b\n"
    assert resume_text(parse(tex)) == "Experience\n- Built billing\n\nSkills\n- Python"


# --- hand-off to the owner ---


def test_a_captcha_hands_off_without_a_model_call(job):
    page = FakePage([CAPTCHA])
    llm = FakeLlm([answer(status="ready_to_submit")])
    app = make(job, page, llm)
    app.advance()
    assert app.status == "captcha" and app.waiting and llm.calls == []
    result = app.finish()  # with no waiting, the captcha is where it ends
    assert result["status"] == "captcha" and "captcha" in page.banners[-1][0]


def test_the_owner_doing_the_step_resumes_the_same_loop(job):
    _, _, logs = job
    page = FakePage([CAPTCHA, FORM])
    owner_goes_to(page, 1)
    llm = FakeLlm([answer({"op": "fill", "ref": 4, "value": "Alex"}, status="ready_to_submit")])
    app = make(job, page, llm)
    app.advance()
    assert app.wait_for_owner()
    head, lines = page.banners[0]
    assert head.startswith("JobApplier needs you:")
    assert lines == ["Do it in this tab; JobApplier continues when the page changes."]
    assert page.fronted == 1
    app.advance()
    assert app.status == "ready_to_submit" and page.done == [("fill", 4, "Alex")]
    assert "step 1: the owner did the captcha step" in llm.calls[0]["prompt"]
    app.finish()
    trace = json.loads((logs / "apply-J7.json").read_text())
    [handoff] = trace["handoffs"]
    assert handoff["step"] == 1 and handoff["status"] == "captcha" and handoff["resumed"]
    assert handoff["waited_seconds"] == 9  # changed at the first poll, then the same for 3 polls
    assert page.cleared == 1  # the note asking for the owner is taken off on resume
    assert [s["step"] for s in trace["steps"]] == [2]  # the step budget carries over


def test_a_model_hand_off_waits_for_the_page_to_change_after_its_own_actions(job):
    page = FakePage([state(el(1, "Email"), el(2, "Code"))])
    llm = FakeLlm([answer({"op": "fill", "ref": 1, "value": "alex@example.com"}, status="needs_account")])
    app = make(job, page, llm)
    app.advance()
    assert app.status == "needs_account" and app.waiting
    job[1]["apply"]["agent"]["wait_for_owner_seconds"] = 6
    assert not app.wait_for_owner()  # its own fill is not the owner's change


def test_no_change_before_the_timeout_ends_the_loop_as_needs_owner(job):
    _, cfg, logs = job
    cfg["apply"]["agent"]["wait_for_owner_seconds"] = 9
    page = FakePage([CAPTCHA])
    app = make(job, page, FakeLlm([answer()]))
    app.advance()
    assert not app.wait_for_owner()
    assert app.status == "needs_owner" and not app.waiting and page.waits.count(3000) == 3
    result = app.finish()
    assert result["status"] == "needs_owner"
    [handoff] = json.loads((logs / "apply-J7.json").read_text())["handoffs"]
    assert handoff["waited_seconds"] == 9 and not handoff["resumed"]


def test_a_page_that_still_has_a_password_field_does_not_resume(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["wait_for_owner_seconds"] = 9
    page = FakePage([state(el(1, "Password", type="password")), state(el(2, "Password", type="password"))])
    owner_goes_to(page, 1)
    app = make(job, page, FakeLlm([answer()]))
    app.advance()
    assert app.status == "needs_account" and not app.wait_for_owner()


def test_max_handoffs_is_respected(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["max_handoffs"] = 2
    page = FakePage([state(el(1, "Why us?", required=True))])
    polls = []

    def owner_types(ms):  # one change, then the same page for 3 polls
        if ms == 3000:
            polls.append(ms)
            if len(polls) % 4 == 1:
                page.state["elements"][0]["value"] += "x"

    page.on_wait = owner_types
    llm = FakeLlm([answer(status="needs_owner", note="Why us? is not in my profile")])
    app = make(job, page, llm)
    app.advance()
    while app.waiting:
        assert app.wait_for_owner()
        app.advance()
    assert len(llm.calls) == 3 and app.status == "needs_owner" and len(app.handoffs) == 2


def test_apply_after_a_hand_off_is_refused_when_the_owner_filled_the_page(job):
    page = FakePage([CAPTCHA, state(el(1, "First name", value="Alex"), btn(2, "Apply"))])
    owner_goes_to(page, 1)
    llm = FakeLlm([answer({"op": "click", "ref": 2}), answer(status="ready_to_submit")])
    app = make(job, page, llm)
    app.advance()
    assert app.wait_for_owner()
    app.advance()
    assert page.clicks == [] and "REFUSED click 2 (Apply)" in llm.calls[1]["prompt"]


def test_a_headline_or_error_change_alone_does_not_resume(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["wait_for_owner_seconds"] = 15
    page = FakePage([CAPTCHA, {**CAPTCHA, "errors": ["Try again"], "headings": ["Checking your browser"]}])
    owner_goes_to(page, 1)
    app = make(job, page, FakeLlm([answer()]))
    app.advance()
    assert not app.wait_for_owner()


# --- verification codes from the owner's mail ---

CODE = "482913"
ORACLE = "https://fa-x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/apply"
EMAIL_PAGE = state(el(1, "Email"), btn(2, "Next", goes=1), url=ORACLE)
CODE_PAGE = state(
    *[el(10 + i, f"digit {i} of six", type="tel", maxlength=1) for i in range(1, 7)],
    btn(17, "Verify", goes=2),
    headings=["Confirm your identity", "Enter the verification code we sent to your email"],
    url=ORACLE,
)
AFTER_CODE = state(el(20, "City"), url=ORACLE)
MAIL = {"code": CODE, "sender": "Oracle <no-reply@oraclecloud.com>", "received": NOW + timedelta(minutes=1)}


def email_then_ready():
    return FakeLlm(
        [
            answer({"op": "fill", "ref": 1, "value": "alex@example.com"}, {"op": "click", "ref": 2}),
            answer(status="ready_to_submit"),
        ]
    )


def test_a_code_from_the_mail_is_filled_digit_by_digit_and_verified_without_a_model_call(job, caplog):
    _, _, logs = job
    caplog.set_level(logging.INFO)
    page = FakePage([EMAIL_PAGE, CODE_PAGE, AFTER_CODE])
    llm, gmail = email_then_ready(), FakeGmail(MAIL)
    result = run(job, page, llm, gmail)
    assert page.done[1:] == [("fill", 10 + i, d) for i, d in zip(range(1, 7), CODE, strict=True)]
    assert page.clicks == ["Next", "Verify"] and result["status"] == "ready_to_submit"
    assert len(llm.calls) == 2  # the code page took no model call
    domains, after = gmail.calls[0]
    assert domains == ["oraclecloud.com", "oracle.com"]  # apply.agent.code_senders for this host
    assert after == NOW - timedelta(seconds=60)  # the start of the step that sent the code, less a minute
    assert "code filled from Oracle <no-reply@oraclecloud.com>" in llm.calls[1]["prompt"]
    trace = (logs / "apply-J7.json").read_text()
    assert "code filled from Oracle <no-reply@oraclecloud.com>" in trace
    assert "Oracle <no-reply@oraclecloud.com>" in caplog.text and "19:01" in caplog.text
    assert CODE not in trace and CODE not in caplog.text and CODE not in llm.calls[1]["prompt"]


def test_a_single_code_field_is_filled(job):
    page = FakePage([state(el(1, "Verification code"), btn(2, "Verify", goes=1)), AFTER_CODE])
    result = run(job, page, FakeLlm([answer(status="ready_to_submit")]), FakeGmail(MAIL))
    assert (
        page.done == [("fill", 1, CODE)]
        and page.clicks == ["Verify"]
        and result["status"] == "ready_to_submit"
    )


def test_no_code_in_the_mail_hands_off_to_the_owner(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["code_wait_seconds"] = 15
    page = FakePage([CODE_PAGE])
    gmail = FakeGmail(None)
    app = make(job, page, FakeLlm([answer()]), gmail)
    app.advance()
    assert (
        app.status == "needs_account" and app.waiting and app.note.startswith("type the code from your email")
    )
    assert len(gmail.calls) == 4 and page.waits.count(5000) == 3 and page.done == []


@pytest.mark.parametrize("gmail", [None, FakeGmail(error=MailError("mail: sign in again"))])
def test_mail_off_or_failing_hands_off_to_the_owner(gmail, job):
    page = FakePage([CODE_PAGE])
    app = make(job, page, FakeLlm([answer()]), gmail)
    app.advance()
    assert app.status == "needs_account" and app.note.startswith("type the code from your email")
    assert 5000 not in page.waits and page.done == []


def test_a_code_is_filled_once_and_a_page_that_asks_again_goes_to_the_owner(job):
    _, cfg, _ = job
    cfg["apply"]["agent"]["code_wait_seconds"] = 5
    page = FakePage([CODE_PAGE])
    page.states[0]["elements"][-1].pop("goes")  # Verify does not get past the page: a wrong code
    app = make(job, page, FakeLlm([answer()]), FakeGmail(MAIL))
    app.advance()
    assert len([d for d in page.done if d[0] == "fill"]) == 6 and page.clicks == ["Verify"]
    assert app.status == "needs_account" and app.note.startswith("type the code from your email")


def test_the_model_saying_needs_code_on_a_page_without_code_words_hands_off(job):
    page = FakePage([state(el(1, "Pin"), btn(2, "Go"))])
    app = make(job, page, FakeLlm([answer(status="needs_code")]), FakeGmail(MAIL))
    app.advance()
    assert app.status == "needs_account" and page.done == [] and page.clicks == []


def test_a_page_off_the_known_hosts_reads_mail_only_from_its_own_domain(job):
    evil = state(
        *CODE_PAGE["elements"], headings=CODE_PAGE["headings"], url="https://login.evil.example/verify"
    )
    gmail = FakeGmail(None)
    job[1]["apply"]["agent"]["code_wait_seconds"] = 5
    app = make(job, FakePage([evil]), FakeLlm([answer()]), gmail)
    app.advance()
    assert {d for domains, _ in gmail.calls for d in domains} == {"evil.example"}


def test_a_code_shown_again_on_the_page_never_reaches_the_model_or_the_trace(job):
    _, _, logs = job
    echoed = state(
        el(20, "Reference number", value=CODE),  # the page shows the code again under another label
        errors=[f"The code {CODE} was accepted"],
        url=ORACLE,
    )
    page = FakePage([EMAIL_PAGE, CODE_PAGE, echoed])
    llm = email_then_ready()
    run(job, page, llm, FakeGmail(MAIL))
    assert CODE not in llm.calls[1]["prompt"] and "[code]" in llm.calls[1]["prompt"]
    assert CODE not in (logs / "apply-J7.json").read_text()


def test_the_screenshot_of_a_code_page_masks_the_code_fields(job):
    page = FakePage([CODE_PAGE])
    run(job, page, FakeLlm([answer()]), None)  # mail off: it stops on the code page
    assert page.masks[-1] == [11, 12, 13, 14, 15, 16]
