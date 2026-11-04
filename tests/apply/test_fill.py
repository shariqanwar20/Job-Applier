import ast
import re

import pytest

from jobapplier.apply.fill import (
    FieldRules,
    group_radios,
    is_login,
    missing_required,
    next_button,
    plan,
    read_fields,
    resume_input,
    salary_range,
)
from tests.conftest import ROOT

VALUES = {
    "name.first": "Alex",
    "name.last": "Example",
    "name.full": "Alex Example",
    "email": "alex@example.com",
    "phone": "+1 416 555 0100",
    "location.city": "Toronto",
    "location.province": "Ontario",
    "location.country": "Canada",
    "location.full": "Toronto, Ontario",
    "links.linkedin": "https://linkedin.com/in/alex",
    "links.github": "https://github.com/alex",
    "links.website": "",
    "work_authorization.canada": "Yes",
    "work_authorization.sponsorship_now": "No",
    "work_authorization.sponsorship_future": "No",
    "relocation.willing": "Yes",
    "relocation.cities": "Vancouver, Montreal",
    "start_date": "Two weeks after an offer",
    "salary_expectation": "Open to a fair range",
    "salary_default": "85000",
    "voluntary_self_id": "decline",
}


SHOWN = {
    "seen": True,
    "rect": [40, 300, 200, 24],
    "doc": [1200, 3000],
    "aria_hidden": False,
    "clipped": False,
}


def field(fid="1", label="", type="text", visible=True, **kw):
    """A field as read from the page: the visibility facts the page reports, and visible as the code
    decides it from them."""
    return {
        **SHOWN,
        "seen": visible,
        "labels": [],
        "id": fid,
        "type": type,
        "name": "",
        "label": label,
        "before": "",
        "group": "",
        "value": "",
        "checked": False,
        "options": [],
        "visible": visible,
        "tabindex": None,
        "required": False,
        **kw,
    }


@pytest.fixture
def rules(config):
    return FieldRules.from_config(config["apply"])


def one(f, rules, description=""):
    return [(m, v) for m, _, v, _ in plan([f], VALUES, rules, description)]


@pytest.mark.parametrize(
    ("label", "want"),
    [
        ("First Name", "Alex"),
        ("Last Name", "Example"),
        ("Full name", "Alex Example"),
        ("Name", "Alex Example"),
        ("Email", "alex@example.com"),
        ("Phone", "+1 416 555 0100"),
        ("City", "Toronto"),
        ("Location (City)", "Toronto"),
        ("Province", "Ontario"),
        ("Country", "Canada"),
        ("Where are you based?", "Toronto, Ontario"),
        ("LinkedIn Profile", "https://linkedin.com/in/alex"),
        ("GitHub URL", "https://github.com/alex"),
        ("Portfolio URL", "https://github.com/alex"),  # no website in the profile, so github
        ("Are you legally authorized to work in Canada?", "Yes"),
        ("Do you require visa sponsorship?", "No"),
        ("Will you now or in the future require sponsorship for employment?", "No"),
        ("Are you willing to relocate?", "Yes"),
        ("Will you require employer sponsorship to work for us?", "No"),
        ("When can you start?", "Two weeks after an offer"),
        ("Desired salary", "85000"),
    ],
)
def test_each_rule_maps_a_label_to_the_profile_value(label, want, rules):
    assert one(field(label=label), rules) == [("fill", want)]


def test_the_label_falls_back_to_the_text_before_the_field_and_the_name(rules):
    assert one(field(before="Email address"), rules) == [("fill", "alex@example.com")]
    assert one(field(name="job_application[first_name]"), rules) == [("fill", "Alex")]


def test_salary_is_the_posted_range_else_the_text_answer_or_the_default(rules):
    desc = "Base pay: $90,000 - $110,000 CAD per year."
    assert salary_range(desc) == "$90,000 - $110,000"
    assert salary_range("The range is $95K to $120K.") == "$95K to $120K"
    assert salary_range("$80,000\u2013$95,000") == "$80,000\u2013$95,000"
    assert salary_range("Competitive pay.") is None
    assert one(field(label="Salary expectations"), rules, desc) == [("fill", "$90,000 - $110,000")]
    box = field(label="Salary expectations", type="textarea")
    assert one(box, rules) == [("fill", "Open to a fair range")]


def test_skip_rules_leave_company_and_referral_fields_alone(rules):
    assert one(field(label="Current company name"), rules) == []
    assert one(field(label="Who referred you? (name)"), rules) == []
    assert one(field(label="Favourite colour"), rules) == []


def test_demographic_questions_take_the_decline_option_or_stay_empty(rules):
    gender = field(
        label="Gender", type="select", options=["Select...", "Male", "Female", "Decline to self-identify"]
    )
    assert one(gender, rules) == [("select_option", "Decline to self-identify")]
    race = field(label="Race", type="select", options=["Select...", "White", "I don't wish to answer"])
    assert one(race, rules) == [("select_option", "I don't wish to answer")]
    assert one(field(label="Veteran status", type="select", options=["Yes", "No"]), rules) == []
    assert one(field(label="Pronouns"), rules) == []  # a text box: never typed
    radios = group_radios(
        [
            field("7", "Yes", type="radio", name="dis", group="Do you have a disability?"),
            field("8", "No", type="radio", name="dis", group="Do you have a disability?"),
            field("9", "I prefer not to answer", type="radio", name="dis", group="Do you have a disability?"),
        ]
    )
    assert plan(radios, VALUES, rules, "") == [
        ("check_radio", "9", "I prefer not to answer", radios[0]["label"])
    ]


def test_yes_no_selects_and_radios_choose_the_matching_option(rules):
    auth = field(
        label="Are you legally authorized to work in Canada?",
        type="select",
        options=["--", "Yes, I am", "No, I am not"],
    )
    assert one(auth, rules) == [("select_option", "Yes, I am")]
    q = "Will you require sponsorship?"
    radios = group_radios(
        [
            field("1", "Yes", type="radio", name="s", group=q),
            field("2", "No", type="radio", name="s", group=q),
        ]
    )
    assert [a[:2] for a in plan(radios, VALUES, rules, "")] == [("check_radio", "2")]
    country = field(label="Country", type="select", options=["United States", "Canada (+1)"])
    assert one(country, rules) == [("select_option", "Canada (+1)")]
    assert one(field(label="Province", type="select", options=["Quebec", "Alberta"]), rules) == []


def test_parser_filled_fields_are_kept_except_name_email_and_phone(rules):
    assert one(field(label="City", value="Mississauga"), rules) == []
    assert one(field(label="LinkedIn", value="https://linkedin.com/in/other"), rules) == []
    assert one(field(label="Email", value="old@example.com"), rules) == [("fill", "alex@example.com")]
    assert one(field(label="First Name", value="ALEX"), rules) == [("fill", "Alex")]
    assert one(field(label="Email", value="alex@example.com"), rules) == []  # already right
    picked = field(label="Country", type="select", value="United States", options=["United States", "Canada"])
    assert one(picked, rules) == []
    placeholder = field(label="Country", type="select", value="Select...", options=["Select...", "Canada"])
    assert one(placeholder, rules) == [("select_option", "Canada")]
    q = "Are you willing to relocate?"
    answered = group_radios(
        [
            field("1", "Yes", type="radio", name="r", group=q),
            field("2", "No", type="radio", name="r", group=q, checked=True),
        ]
    )
    assert plan(answered, VALUES, rules, "") == []


def test_hidden_fields_honeypots_checkboxes_and_passwords_are_never_touched(rules):
    assert one(field(label="Email", visible=False), rules) == []
    assert one(field(name="hp_email", label="Email"), rules) == []
    assert one(field(name="website_honeypot", label="Website"), rules) == []
    assert one(field(name="email2", tabindex="-1", before="Email"), rules) == []
    assert one(field(label="Email", type="password"), rules) == []
    assert one(field(label="Resume", type="file"), rules) == []


def test_resume_input_prefers_a_resume_or_cv_field_else_the_only_file_input():
    cover = field("1", "Cover letter", type="file", visible=False)
    cv = field("2", "Resume/CV", type="file", visible=False)
    assert resume_input([cover, cv]) == "2"
    assert resume_input([field("3", "Attach", type="file", name="cv_upload"), cover]) == "3"
    assert resume_input([field("4", "Attachment", type="file")]) == "4"
    assert resume_input([cover, field("5", "Other", type="file")]) is None
    assert resume_input([field("6", "Email")]) is None


def test_missing_required_lists_visible_empty_required_fields_by_label():
    fields = [
        field("1", "Email", required=True),
        field("2", "Phone", required=True, value="1"),
        field("3", "", before="Why us?", type="textarea", required=True),
        field("4", "Hidden", required=True, visible=False),
        field("5", "Notes"),
        *group_radios([field("6", "Yes", type="radio", name="r", group="Relocate?", required=True)]),
    ]
    assert missing_required(fields) == ["Email", "Why us?", "Relocate?"]


def test_login_pages_are_found_by_password_field_url_or_title(rules):
    words = rules
    assert is_login("https://x.example/jobs/1", "Developer", [field(type="password")], words)
    assert not is_login(
        "https://x.example/jobs/1", "Developer", [field(type="password", visible=False)], words
    )
    assert is_login("https://acme.wd3.myworkdayjobs.com/en-US/login", "Workday", [], words)
    assert is_login("https://x.example/a", "Create Account | Acme", [], words)
    assert not is_login("https://jobs.lever.co/acme/1/apply", "Acme - Software Engineer", [field()], words)
    assert not is_login("https://x.example/a", "Continue with your application", [field()], words)


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
        r"getattr\(",  # a method picked by name could be any of these
        r"__getattribute__",
        r"\beval\(",
        r"methodcaller",
        r"prototype\.click",
        r"\[\s*['\"]click['\"]\s*\]",
        r"\.click\.(call|apply|bind)",
        r"\.checked\s*=",
    ]
    assert [p for p in forbidden if re.search(p, src, re.I)] == []
    called = set(re.findall(r"\.(fill|select_option|check|set_input_files|uncheck)\(", src))
    assert called <= {"fill", "select_option", "check", "set_input_files"}


def _calls(attrs: tuple[str, ...]) -> tuple[list, list]:
    """(every call to one of attrs anywhere in the apply code, as (file, function or None, attr);
    the same list built from every AST node, so a call in a lambda, class body or module level counts)."""
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


def test_the_only_click_is_in_click_next():
    found, everywhere = _calls(("click", "dblclick", "tap"))
    assert found == [("browser.py", "_click_next", "click")] and len(everywhere) == 1
    # a click in a page script string counts too
    assert sum(len(re.findall(r"\.click\(", p.read_text(), re.I)) for p in APPLY_CODE) == 1


CONSENT = "I agree to the privacy policy and certify that my answers are accurate"
OPT_IN = "I agree to receive marketing emails and job alerts"


def test_consent_boxes_are_ticked_and_opt_ins_are_left_unless_configured(config):
    boxes = [
        field("1", CONSENT, type="checkbox", value="on"),
        field("2", OPT_IN, type="checkbox", value="on"),
        field("3", "Toronto", type="checkbox", value="on", before="Which offices?"),
        field("4", "I acknowledge the terms", type="checkbox", value="on", checked=True),
        field("5", CONSENT, type="checkbox", value="on", visible=False),
    ]
    rules = FieldRules.from_config(config["apply"])
    assert [a[:2] for a in plan(boxes, VALUES, rules, "")] == [("tick_consent", "1")]
    config["apply"]["tick_opt_ins"] = True
    rules = FieldRules.from_config(config["apply"])
    assert [a[:2] for a in plan(boxes, VALUES, rules, "")] == [("tick_consent", "1"), ("tick_consent", "2")]


def test_an_unticked_required_checkbox_is_missing():
    box = field("1", "I agree to the terms", type="checkbox", value="on", required=True)
    assert missing_required([box]) == ["I agree to the terms"]
    assert missing_required([{**box, "checked": True}]) == []


def button(bid, text, visible=True, in_form=True, **names):
    """A button as the page reports it: each source of its name apart (aria-label, aria-labelledby text,
    visible text, value, title)."""
    return {
        "id": bid,
        "tag": "button",
        "aria": "",
        "labelledby": "",
        "text": text,
        "value": "",
        "title": "",
        "in_form": in_form,
        "visible": visible,
        **names,
    }


def test_next_button_is_a_next_word_without_a_submit_word(rules):
    assert next_button([button("1", "Back"), button("2", "Save and Continue")], rules) == ("2", "")
    assert next_button([button("1", "Next", visible=False), button("2", "Next")], rules)[0] == "2"
    bid, why = next_button([button("1", "Back"), button("2", "Submit application")], rules)
    assert bid is None and "submit yourself" in why
    assert next_button([button("1", "Continue and submit")], rules)[0] is None  # both lists: never
    assert next_button([button("1", "Continue with Google")], rules)[0] is None  # a sign-in, not a step
    bid, why = next_button([button("1", "Add another")], rules)
    assert bid is None and "No Next button" in why


class ReadPage:
    def __init__(self, raw):
        self.raw = raw

    def evaluate(self, script):
        return self.raw


@pytest.mark.parametrize(
    "hidden",
    [
        {"rect": [-10000, 300, 200, 24]},  # moved off-screen
        {"rect": [40, 5000, 200, 24]},  # below the end of the document
        {"seen": False},  # an ancestor with opacity 0 (checkVisibility says so)
        {"clipped": True},  # inside a zero-height wrapper with overflow hidden
        {"aria_hidden": True},  # inside [aria-hidden=true]
        {"seen": False, "labels": [SHOWN]},  # a hidden input does not borrow its visible label
    ],
)
def test_a_css_hidden_honeypot_is_never_filled(hidden, rules):
    page = ReadPage([{**field("1", "Website"), **hidden}])
    [f] = read_fields(page)
    assert not f["visible"] and plan([f], VALUES, rules, "") == []


def test_a_visible_field_and_a_radio_with_a_styled_label_are_filled(rules):
    q = "Are you willing to relocate?"
    radios = [
        {**field("2", "Yes", type="radio", name="r", group=q), "seen": False, "labels": [SHOWN]},
        {**field("3", "No", type="radio", name="r", group=q), "seen": False, "labels": [SHOWN]},
    ]
    fields = read_fields(ReadPage([field("1", "Website"), *radios]))
    assert [a[:3] for a in plan(fields, VALUES, rules, "")] == [
        ("fill", "1", "https://github.com/alex"),
        ("check_radio", "2", "Yes"),
    ]


@pytest.mark.parametrize(
    "label",
    [
        "Emergency contact name",
        "Name of your current manager",
        "Phone extension",
        "Reference full name",
        "Who referred you?",
        "Company",
        "Preferred first name",
    ],
)
def test_contact_manager_and_extension_fields_are_never_filled_or_overwritten(label, rules):
    assert one(field(label=label), rules) == []
    assert one(field(label=label, value="Someone"), rules) == []


@pytest.mark.parametrize("label", ["Name", "Full name", "Your name", "Name*"])
def test_bare_name_is_the_full_name_only_as_the_whole_label(label, rules):
    assert one(field(label=label, name="_systemfield_name"), rules) == [("fill", "Alex Example")]


def test_a_free_text_relocation_question_gets_the_cities(rules):
    assert one(field(label="Which cities would you relocate to?"), rules) == [("fill", "Vancouver, Montreal")]
    assert one(field(label="Are you open to relocation?"), rules) == [("fill", "Yes")]
    yes_no = field(label="Which cities would you relocate to?", type="select", options=["Yes", "No"])
    assert one(yes_no, rules) == [("select_option", "Yes")]


def test_a_search_box_is_not_a_form_field(rules):
    assert one(field(label="City", type="search"), rules) == []


def test_the_text_before_a_field_is_looked_for_only_inside_its_form_or_fieldset():
    # The page script runs only in a browser; this pins the bound it walks to.
    from jobapplier.apply import fill

    assert "el.closest('fieldset, form')" in fill._READ and "n !== box" in fill._READ


def test_the_only_check_is_in_fill_page_for_a_planned_radio_or_consent_box():
    found, everywhere = _calls(("check", "set_input_files", "set_checked", "uncheck"))
    assert sorted(found) == [("fill.py", "fill_page", "check"), ("fill.py", "upload_file", "set_input_files")]
    assert len(everywhere) == 2


@pytest.mark.parametrize(
    "names",
    [
        {"text": "Next month"},
        {"text": "Next slide"},
        {"text": "Show next 10 jobs"},
        {"text": "Continue reading"},
        {"text": "Proceed to payment"},
        {"text": "", "aria": "Next"},  # the next word must be in the visible text
        {"text": "Submit application", "aria": "Next"},
        {"tag": "input", "text": "", "aria": "Next", "value": "Submit"},
        {"text": "Next", "value": "submit"},
        {"text": "Next", "title": "Send application"},
        {"text": "Next", "labelledby": "Finish and apply"},
        {"text": "Continue with Google"},
        {"text": "Next one two three four five six"},  # more than apply.next_max_extra_words
        {"text": "Continue", "aria": "Continue to the photo gallery"},  # a deny word in any source
    ],
)
def test_is_next_refuses_other_next_buttons_and_any_submit_or_sign_in_word_anywhere(names, rules):
    assert not rules.is_next(button("1", **{"text": "Next", **names}))


@pytest.mark.parametrize(
    "names",
    [
        {"text": "Next"},
        {"text": "Next: Experience"},
        {"text": "Continue to step 3"},
        {"text": "Save & Continue"},
        {"text": "  Save and Continue "},
        {"text": "Next step", "aria": "Go to the next step"},
        {"text": "Continue application"},
        {"text": "Proceed to questions"},
        {"tag": "input", "text": "", "value": "Continue"},
    ],
)
def test_is_next_accepts_a_next_button_with_a_few_more_words(names, rules):
    assert rules.is_next(button("1", **names))


def test_any_visible_submit_button_on_the_page_stops_the_walk(rules):
    bid, why = next_button([button("1", "Next"), button("2", "Submit application")], rules)
    assert bid is None and "submit yourself" in why
    bid, _ = next_button([button("1", "Next"), button("2", "Apply now", in_form=False)], rules)
    assert bid is None  # outside the form, but on the page
    assert next_button([button("1", "Next"), button("2", "Submit", visible=False)], rules) == ("1", "")


def test_only_a_button_in_the_form_that_holds_the_fields_is_a_next_button(rules):
    bid, why = next_button([button("1", "Next", in_form=False)], rules)
    assert bid is None and "No Next button" in why


@pytest.mark.parametrize(
    "label",
    [
        "I agree to receive emails about jobs at Acme",
        "I consent to being contacted about future opportunities",
        "I agree to receive communications from Acme and its partners",
        "I consent to Acme retaining my data for future openings",
        "I agree to join the Acme talent community",
        "I agree to SMS and WhatsApp updates",
    ],
)
def test_consent_words_never_tick_a_marketing_or_contact_opt_in(label, rules):
    assert plan([field("1", label, type="checkbox", value="on")], VALUES, rules, "") == []


@pytest.mark.parametrize(
    ("label", "want"),
    [
        ("Contact number", "+1 416 555 0100"),
        ("Contact email", "alex@example.com"),
        ("Preferred contact phone", "+1 416 555 0100"),
        ("Preferred contact email", "alex@example.com"),
    ],
)
def test_the_owners_own_contact_fields_are_filled(label, want, rules):
    assert one(field(label=label), rules) == [("fill", want)]


@pytest.mark.parametrize(
    "label", ["Contact name", "Contact person", "Emergency contact phone", "Relationship to contact"]
)
def test_another_persons_contact_fields_are_skipped(label, rules):
    assert one(field(label=label), rules) == []


SUBMIT_LIKE = [
    "Continue to submission",
    "Continue to confirmation",
    "Continue to completion",
    "Next - submitting",
    "Continue and finalize",
    "Continue — final step",
    "Continue & post",
    "Next: send application",
    "Apply",
    "Apply now",
]


@pytest.mark.parametrize("text", SUBMIT_LIKE)
def test_submit_word_starts_refuse_a_next_button_and_stop_the_walk(text, rules):
    assert not rules.is_next(button("1", text))
    assert rules.is_submit_button(button("1", text))


def test_continue_application_is_still_a_next_button(rules):
    assert rules.is_next(button("1", "Continue application"))
    assert not rules.is_submit_button(button("1", "Continue application"))
