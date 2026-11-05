import ast
import json
import re
from pathlib import Path

import pytest

from jobapplier.apply import page as page_mod
from jobapplier.apply.page import ClickRules, RefusedError, act, fill_code, read, verify
from tests.apply.fakes import SHOWN, FakePage, btn, el, link, state
from tests.conftest import ROOT

PDF = Path("/tmp/A_Acme.pdf")


@pytest.fixture
def agent_cfg(config):
    return config["apply"]["agent"]


@pytest.fixture
def rules(config):
    return ClickRules.from_config(config["apply"])


def snap(agent_cfg, *elements, **kw):
    return read(FakePage([state(*elements, **kw)]), agent_cfg)


def names(text="", **kw):
    """The name sources of a control as the page reports them right before a click."""
    return {
        "tag": "button",
        "type": "button",
        "role": "",
        "aria": "",
        "labelledby": "",
        "text": text,
        "value": "",
        "title": "",
        "form": True,
        "pw_form": False,
        "disabled": False,
        **kw,
    }


# --- the snapshot ---


def test_the_snapshot_lists_visible_elements_compactly_with_url_title_headings_and_errors(agent_cfg):
    s = snap(
        agent_cfg,
        el(1, "First name", required=True),
        el(2, "Country", type="select", options=["Select...", "Canada"], value=""),
        btn(3, "Next", type="submit", form=True),
        link(4, "Privacy policy"),
        title="Apply - Acme",
        url="https://acme.example/apply",
        headings=["Your details"],
        errors=["First name is required"],
        frames=["iframe https://captcha.example/x"],
    )
    v = s.view
    assert v["url"] == "https://acme.example/apply" and v["title"] == "Apply - Acme"
    assert v["headings"] == ["Your details"] and v["errors"] == ["First name is required"]
    assert v["frames"] == ["iframe https://captcha.example/x"]
    assert v["elements"] == [
        {"ref": 1, "type": "text", "label": "First name", "required": True},
        {"ref": 2, "type": "select", "label": "Country", "options": ["Select...", "Canada"]},
        {"ref": 3, "tag": "button", "text": "Next"},
        {"ref": 4, "tag": "link", "text": "Privacy policy"},
    ]
    assert json.loads(s.text) == v and sorted(s.refs) == [1, 2, 3, 4]
    assert s.missing == ["First name"] and not s.password


@pytest.mark.parametrize(
    "hidden",
    [
        {"rect": [-10000, 300, 200, 24]},  # moved off-screen
        {"rect": [40, 5000, 200, 24]},  # below the end of the document
        {"seen": False},  # an ancestor with opacity 0 (checkVisibility says so)
        {"clipped": True},  # inside a zero-height wrapper with overflow hidden
        {"aria_hidden": True},  # inside [aria-hidden=true]
        {"name": "hp_email"},  # a honeypot by name
        {"tabindex": "-1", "label": ""},  # out of the tab order with no label
    ],
)
def test_hidden_fields_and_honeypots_are_left_out(hidden, agent_cfg):
    s = snap(agent_cfg, {**el(1, "Website"), **hidden})
    assert s.view["elements"] == [] and s.refs == {}


def test_a_hidden_file_input_is_kept_since_styled_uploads_hide_the_native_one(agent_cfg):
    s = snap(agent_cfg, el(1, "Resume/CV", type="file", visible=False))
    assert s.view["elements"] == [{"ref": 1, "type": "file", "label": "Resume/CV"}]


def test_radios_are_one_question_with_options_and_a_hidden_radio_borrows_its_visible_label(agent_cfg):
    q = "Will you require sponsorship?"
    s = snap(
        agent_cfg,
        el(1, "Yes", type="radio", name="s", group=q, required=True),
        {**el(2, "No", type="radio", name="s", group=q), "seen": False, "labels": [SHOWN]},
        {**el(3, "Maybe", type="radio", name="s", group=q), "seen": False},
    )
    assert s.view["elements"] == [
        {
            "type": "radio",
            "label": q,
            "required": True,
            "options": [{"ref": 1, "label": "Yes"}, {"ref": 2, "label": "No"}],
        }
    ]
    assert s.refs[2]["via_label"] and not s.refs[1]["via_label"] and 3 not in s.refs
    assert s.missing == [q]


def test_the_label_falls_back_to_the_text_before_the_field_then_its_name(agent_cfg):
    s = snap(agent_cfg, el(1, before="Why us?", type="textarea"), el(2, name="linkedin_url"))
    assert [e["label"] for e in s.view["elements"]] == ["Why us?", "linkedin_url"]


def test_a_visible_password_field_marks_the_page_as_an_account_step(agent_cfg):
    assert snap(agent_cfg, el(1, "Email"), el(2, "Password", type="password")).password
    assert not snap(agent_cfg, el(1, "Email"), el(2, "Password", type="password", visible=False)).password


def test_the_snapshot_is_capped_and_says_how_many_elements_it_left_out(agent_cfg):
    agent_cfg["max_snapshot_chars"] = 600
    s = snap(agent_cfg, *[el(i, f"Question number {i} " + "x" * 30) for i in range(40)])
    assert len(s.text) <= 600 and s.view["more"].endswith("elements not shown")
    assert len(s.view["elements"]) + int(s.view["more"].split()[0]) == 40


def test_long_option_lists_and_labels_are_cut(agent_cfg):
    agent_cfg["max_options"] = 3
    s = snap(agent_cfg, el(1, "L" * 500, type="select", options=[f"o{i}" for i in range(10)]))
    [e] = s.view["elements"]
    assert e["options"] == ["o0", "o1", "o2", "(7 more: select one by its exact text)"]
    assert len(e["label"]) == 200


def test_required_fields_that_are_filled_are_not_missing(agent_cfg):
    s = snap(
        agent_cfg,
        el(1, "Email", required=True, value="a@b.c"),
        el(2, "Terms", type="checkbox", required=True, checked=True),
        el(3, "Country", type="select", required=True, value="Select..."),
        el(4, "Resume", type="file", required=True),
    )
    assert s.missing == ["Country"]


# --- the click guard ---

REFUSED = [
    names("Submit application"),
    names("Submit"),
    names("Continue and submit"),
    names("Continue to submission"),
    names("Continue and finalize"),
    names("Continue \u2014 final step"),
    names("Continue & post"),
    names("Next: send application"),
    names("Confirm"),
    names("Finish"),
    names("Done"),
    names("Next", aria="Submit application"),
    names("Next", title="Send application"),
    names("Next", labelledby="Finish and apply"),
    names("", tag="input", type="submit", value="Submit"),
    names("", type="submit"),  # an unnamed submit button: Chrome shows it as Submit
    names("Sign in"),
    names("Log in"),
    names("Create account"),
    names("Create an account"),
    names("Register"),
    names("Continue with Google"),
    names("Apply with LinkedIn"),
    names("Sub\u200bmit"),  # a zero-width space does not split the word
    names("Sub\u00admit application"),
    names("N\u0435xt"),  # a Cyrillic e
    names("Next", aria="\u0405ubmit"),
    names("Save", type="submit"),  # it submits the form and is not a next step
    names("Reset", type="reset"),
    names("Privacy policy", tag="a", type=""),  # a link away from the form
    names("Next month", tag="a", type=""),
    names("Next job", tag="a", type=""),  # another posting, not this form's next step
    names("Resume", tag="input", type="file"),  # opens a file chooser: upload only
    names("Email", tag="input", type="text"),
]


@pytest.mark.parametrize("live", REFUSED, ids=lambda n: n["text"] or n["value"] or n["type"])
def test_the_click_guard_refuses_submit_sign_in_and_leaving_controls(live, rules):
    assert rules.refuse(live, filled=0)


ALLOWED = [
    names("Next"),
    names("Next: Experience"),
    names("Continue to step 3"),
    names("Save & Continue", type="submit"),
    names("Continue application", type="submit"),
    names("", tag="input", type="submit", value="Continue"),
    names("Yes"),  # a custom yes/no button
    names("Add another"),
    names("Select...", role="combobox"),
    names("Canada", role="option"),
    names("Next", tag="a", type=""),
    names("Continu\u00e9"),  # an accented Latin letter is fine
]


@pytest.mark.parametrize("live", ALLOWED, ids=lambda n: n["text"] or n["value"])
def test_the_click_guard_allows_steps_and_form_widgets(live, rules):
    assert rules.refuse(live, filled=3) is None


@pytest.mark.parametrize(
    "live",
    [
        names("Apply now"),
        names("Apply"),
        names("Apply for this job", tag="a", type=""),
        names("Start application"),
        names("Begin application", type="submit"),
    ],
    ids=lambda n: n["text"],
)
def test_apply_is_allowed_on_a_fresh_job_page_and_refused_once_anything_is_filled(live, rules):
    assert rules.refuse(live, filled=0) is None
    assert "Apply" in rules.refuse(live, filled=1)


def test_the_click_binding_rejects_a_name_that_changed_after_the_check(rules):
    bound = rules.binding(names("Next: Experience"), filled=1)
    assert bound.search("Next: Experience")
    assert not bound.search("Submit")
    assert not bound.search("Next and submit")
    assert not bound.search("Next and apply")
    assert rules.binding(names("Apply now"), filled=0).search("Apply now")


# --- the actions ---


def run_act(agent_cfg, rules, page, action, filled=0):
    return act(page, read(page, agent_cfg), action, PDF, rules, filled, 1000)


def test_fill_select_check_uncheck_and_upload_work_by_ref(agent_cfg, rules):
    page = FakePage(
        [
            state(
                el(1, "First name"),
                el(2, "Country", type="select", options=["Canada"]),
                el(3, "Terms", type="checkbox"),
                el(4, "Alerts", type="checkbox", checked=True),
                el(5, "Resume", type="file", visible=False),
            )
        ]
    )
    for a in (
        {"op": "fill", "ref": 1, "value": "Alex"},
        {"op": "select", "ref": 2, "value": "Canada"},
        {"op": "check", "ref": 3},
        {"op": "uncheck", "ref": 4},
        {"op": "upload", "ref": 5},
    ):
        run_act(agent_cfg, rules, page, a)
    assert page.done == [
        ("fill", 1, "Alex"),
        ("select_option", 2, "Canada"),
        ("check", 3, None),
        ("uncheck", 4, None),
        ("set_input_files", 5, str(PDF)),
    ]


def test_an_upload_never_takes_a_path_from_the_model(agent_cfg, rules):
    page = FakePage([state(el(1, "Resume", type="file"))])
    run_act(agent_cfg, rules, page, {"op": "upload", "ref": 1, "value": "/Users/x/.ssh/id_rsa"})
    assert page.done == [("set_input_files", 1, str(PDF))]
    page = FakePage([state(el(1, "Website"))])
    with pytest.raises(RefusedError, match="does not fit"):
        run_act(agent_cfg, rules, page, {"op": "upload", "ref": 1})


def test_a_password_field_or_a_field_in_a_password_form_is_never_filled(agent_cfg, rules):
    page = FakePage([state(el(1, "Password", type="password", visible=False), el(2, "Email", pw_form=True))])
    s = read(page, agent_cfg)
    s.refs[1] = page.state["elements"][0] | {"via_label": False}  # even if the model names a hidden one
    for ref in (1, 2):
        with pytest.raises(RefusedError) as info:
            act(page, s, {"op": "fill", "ref": ref, "value": "x"}, PDF, rules, 0, 1000)
        assert info.value.stop == "needs_account"
    assert page.done == []


@pytest.mark.parametrize(
    ("element", "action"),
    [
        (el(1, "Terms", type="checkbox"), {"op": "fill", "ref": 1, "value": "x"}),
        (el(1, "Email"), {"op": "check", "ref": 1}),
        (el(1, "Email"), {"op": "select", "ref": 1, "value": "x"}),
        (el(1, "Yes", type="radio"), {"op": "uncheck", "ref": 1}),
        (el(1, "Email"), {"op": "fill", "ref": 1}),  # no value
        (el(1, "Email", disabled=True), {"op": "fill", "ref": 1, "value": "x"}),
        (el(1, "Email"), {"op": "press", "ref": 1, "value": "Enter"}),
        (el(1, "Email"), {"op": "fill", "ref": 9, "value": "x"}),  # not on the page
        (el(1, "Email"), {"op": "fill", "ref": "1", "value": "x"}),
    ],
)
def test_an_action_that_does_not_fit_the_element_is_refused(element, action, agent_cfg, rules):
    page = FakePage([state(element)])
    with pytest.raises(RefusedError):
        run_act(agent_cfg, rules, page, action)
    assert page.done == [] and page.clicks == []


@pytest.mark.parametrize(
    "element",
    [el(1, "Verification code"), el(1, "Enter the 6-digit code", auto="one-time-code"), el(1, "OTP")],
)
def test_a_verification_code_is_never_typed(element, agent_cfg, rules):
    page = FakePage([state(element)])
    with pytest.raises(RefusedError) as info:
        run_act(agent_cfg, rules, page, {"op": "fill", "ref": 1, "value": "123456"})
    assert info.value.stop == "needs_code" and page.done == []  # JobApplier reads it from the mail


def test_postal_code_is_not_a_verification_code(agent_cfg, rules):
    page = FakePage([state(el(1, "Postal code"))])
    run_act(agent_cfg, rules, page, {"op": "fill", "ref": 1, "value": "M5V 1A1"})
    assert page.done == [("fill", 1, "M5V 1A1")]


# --- verification codes: JobApplier fills a code read from the owner's mail ---

BOXES = [el(i, f"digit {i} of six", type="tel", maxlength=1) for i in range(1, 7)]
CODE_HEADINGS = ["Confirm your identity", "Enter the verification code we sent to your email"]


def code_page(*extra, button="Verify"):
    return FakePage([state(*BOXES, btn(7, button), *extra, headings=CODE_HEADINGS)])


def test_a_code_page_is_found_by_its_headings_or_labels(agent_cfg):
    assert snap(agent_cfg, el(1, "Code"), headings=["Enter the verification code"]).code_page
    assert snap(agent_cfg, el(1, "Security code")).code_page
    assert snap(agent_cfg, el(1, "Code", auto="one-time-code")).code_page
    assert not snap(agent_cfg, el(1, "Postal code"), headings=["Your details"]).code_page


def test_the_snapshot_never_shows_a_code_fields_value(agent_cfg):
    boxes = [{**b, "value": d} for b, d in zip(BOXES, "482913", strict=True)]
    s = snap(agent_cfg, *boxes, el(8, "Security code", value="482913"), headings=CODE_HEADINGS)
    assert "482913" not in s.text and '"value"' not in s.text


def test_a_six_box_code_is_filled_one_digit_per_box(agent_cfg, rules):
    page = code_page()
    fill_code(page, read(page, agent_cfg), "482913", rules, 1000)
    assert page.done == [("fill", i, d) for i, d in zip(range(1, 7), "482913", strict=True)]


def test_a_single_code_field_is_filled(agent_cfg, rules):
    page = FakePage([state(el(1, "Verification code"), btn(2, "Verify"))])
    fill_code(page, read(page, agent_cfg), "482913", rules, 1000)
    assert page.done == [("fill", 1, "482913")]


def test_a_code_is_not_filled_off_a_code_page_or_into_boxes_that_do_not_fit(agent_cfg, rules):
    page = code_page()
    with pytest.raises(RefusedError, match="fields"):
        fill_code(page, read(page, agent_cfg), "4829", rules, 1000)
    page = FakePage([state(el(1, "Email"))])
    with pytest.raises(RefusedError, match="code page"):
        fill_code(page, read(page, agent_cfg), "482913", rules, 1000)
    assert page.done == []


def test_verify_clicks_the_code_pages_verify_button(agent_cfg, rules):
    page = code_page(btn(8, "Resend code"))
    assert verify(page, read(page, agent_cfg), rules, 1000) == "Verify"
    assert page.clicks == ["Verify"]


@pytest.mark.parametrize("text", ["Verify", "Confirm", "Continue", "Next", "Submit code", "Verify email"])
def test_click_verify_clicks_these_names_on_a_code_page(text, agent_cfg, rules):
    page = code_page(button=text)
    page_mod._click_verify(page, read(page, agent_cfg), 7, rules, 1000)
    assert page.clicks == [text]


def test_click_verify_refuses_off_a_code_page(agent_cfg, rules):
    page = FakePage([state(el(1, "Email"), btn(2, "Verify"))])
    with pytest.raises(RefusedError, match="code page"):
        page_mod._click_verify(page, read(page, agent_cfg), 2, rules, 1000)
    assert page.clicks == []


def test_click_verify_refuses_on_a_page_with_too_many_other_fields(agent_cfg, rules):
    page = code_page(*[el(10 + i, f"Question {i}") for i in range(9)])
    with pytest.raises(RefusedError, match="fields"):
        page_mod._click_verify(page, read(page, agent_cfg), 7, rules, 1000)
    assert page.clicks == []


@pytest.mark.parametrize(
    "text", ["Submit application", "Confirm application", "Apply", "Sign in", "Confirm and apply now", "Back"]
)
def test_click_verify_refuses_anything_but_a_verify_button(text, agent_cfg, rules):
    page = code_page(button=text)
    with pytest.raises(RefusedError):
        page_mod._click_verify(page, read(page, agent_cfg), 7, rules, 1000)
    assert page.clicks == []


@pytest.mark.parametrize(
    "frame",
    [
        "iframe https://geo.captcha-delivery.com/captcha/?x=1",
        "iframe https://newassets.hcaptcha.com/captcha/v1/x",
        "iframe https://www.google.com/recaptcha/api2/anchor?k=x&size=normal",
        "iframe https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x",
        "iframe https://client-api.arkoselabs.com/fc/x",
    ],
)
def test_a_captcha_that_is_the_whole_page_is_found(frame, agent_cfg):
    assert snap(agent_cfg, btn(1, "Back"), frames=[frame]).captcha


def test_an_invisible_recaptcha_or_a_captcha_widget_beside_a_form_is_not_a_blocker(agent_cfg):
    invisible = "iframe https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"
    assert not snap(agent_cfg, frames=[invisible]).captcha
    widget = "iframe https://newassets.hcaptcha.com/captcha/v1/x"
    assert not snap(agent_cfg, el(1, "Email"), frames=[widget]).captcha  # the owner ticks it before submit
    assert not snap(agent_cfg, frames=["iframe https://www.youtube.com/embed/x"]).captcha


def test_a_hidden_radio_is_checked_through_its_visible_label(agent_cfg, rules):
    page = FakePage([state({**el(1, "Yes", type="radio", name="r"), "seen": False, "labels": [SHOWN]})])
    run_act(agent_cfg, rules, page, {"op": "check", "ref": 1})
    assert page.done == [("check", 1, "label")]


def test_a_click_is_bound_to_the_name_it_was_checked_with(agent_cfg, rules):
    page = FakePage([state(btn(1, "Next"), el(2, type="submit", tag="input", value="Continue", form=True))])
    run_act(agent_cfg, rules, page, {"op": "click", "ref": 1})
    run_act(agent_cfg, rules, page, {"op": "click", "ref": 2})
    assert page.clicks == ["Next", "Continue"]


def test_a_refused_click_never_reaches_the_page(agent_cfg, rules):
    page = FakePage([state(btn(1, "Submit application", type="submit", form=True))])
    with pytest.raises(RefusedError, match="submit"):
        run_act(agent_cfg, rules, page, {"op": "click", "ref": 1})
    assert page.clicks == []


def test_an_element_in_a_same_origin_frame_is_reached_through_its_frame(agent_cfg, rules):
    page = FakePage([state(el(1, "Email", frame=[0]))])
    run_act(agent_cfg, rules, page, {"op": "fill", "ref": 1, "value": "a@b.c"})
    assert page.frames == ['[data-jobapplier-frame="0"]'] and page.done == [("fill", 1, "a@b.c")]


# --- the code itself: no submit, no keys, no passwords ---

APPLY_CODE = [
    *sorted((ROOT / "jobapplier" / "apply").glob("*.py")),
    ROOT / "jobapplier" / "pipelines" / "apply.py",
]


@pytest.mark.parametrize("path", APPLY_CODE, ids=lambda p: p.name)
def test_the_apply_code_never_submits_presses_keys_or_types_passwords(path):
    src = path.read_text()
    forbidden = [
        r"\.dblclick\(",
        r"\.tap\(",
        r"\.press\(",
        r"press_sequentially",
        r"\.type\(",
        r"\.keyboard\b",  # page.keyboard; KeyboardInterrupt is fine
        r"\.mouse\b",
        r"submit\(",
        r"requestSubmit",
        r"dispatch_?[eE]vent",
        r"set_checked",
        r"\.focus\(",
        r"\.hover\(",
        r"getattr\(",  # a method picked by name could be any of these
        r"__getattribute__",
        r"\beval\(",
        r"methodcaller",
        r"prototype\.click",
        r"\[\s*['\"]click['\"]\s*\]",
        r"\.click\.(call|apply|bind)",
        r"\.checked\s*=",
        r"\.value\s*=[^=]",
        r"['\"]Enter['\"]",
    ]
    assert [p for p in forbidden if re.search(p, src, re.I)] == []
    called = set(re.findall(r"\.(fill|select_option|check|set_input_files|uncheck)\(", src))
    assert called <= {"fill", "select_option", "check", "uncheck", "set_input_files"}


def _calls(attrs: tuple[str, ...]) -> tuple[list, list]:
    """(every call to one of attrs in a function of the apply code, as (file, function, attr); the same
    calls found anywhere, so one in a lambda, class body or module level counts)."""
    found, everywhere = [], []
    for path in APPLY_CODE:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in attrs
            ):
                everywhere.append((path.name, node.func.attr, node.lineno))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in attrs
                ):
                    found.append((path.name, fn.name, node.func.attr))
    return found, everywhere


def test_the_only_clicks_are_the_guarded_click_and_the_code_pages_verify():
    found, everywhere = _calls(("click", "dblclick", "tap"))
    assert found == [("page.py", "_click", "click"), ("page.py", "_click_verify", "click")]
    assert len(everywhere) == 2
    # a click in a page script string counts too
    assert sum(len(re.findall(r"\.click\(", p.read_text(), re.I)) for p in APPLY_CODE) == 2


def test_field_changes_happen_only_in_act():
    found, everywhere = _calls(
        ("fill", "select_option", "check", "uncheck", "set_input_files", "set_checked")
    )
    assert {(f, fn) for f, fn, _ in found} == {("page.py", "act"), ("page.py", "fill_code")}
    assert len(everywhere) == 6


def test_the_snapshot_script_reads_frames_and_only_tags_elements():
    # The page script runs only in a browser; this pins what it may do there.
    script = page_mod._SNAPSHOT
    assert "contentDocument" in script and "data-jobapplier-frame" in script
    assert "['data-jobapplier', 'data-jobapplier-label', 'data-jobapplier-frame']" in script  # old tags go
    assert not re.search(r"\.(click|submit|focus|dispatchEvent)\b", script)
