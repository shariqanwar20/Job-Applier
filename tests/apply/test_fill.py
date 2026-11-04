import re

import pytest

from jobapplier.apply.fill import (
    FieldRules,
    group_radios,
    is_login,
    missing_required,
    plan,
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
    "start_date": "Two weeks after an offer",
    "salary_expectation": "Open to a fair range",
    "salary_default": "85000",
    "voluntary_self_id": "decline",
}


def field(fid="1", label="", type="text", **kw):
    return {
        "id": fid,
        "type": type,
        "name": "",
        "label": label,
        "before": "",
        "group": "",
        "value": "",
        "checked": False,
        "options": [],
        "visible": True,
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
    assert plan(radios, VALUES, rules, "") == [("check", "9", "I prefer not to answer", radios[0]["label"])]


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
    assert [a[:2] for a in plan(radios, VALUES, rules, "")] == [("check", "2")]
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
    assert one(field(label="Email me about new jobs", type="checkbox"), rules) == []
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


@pytest.mark.parametrize("module", ["fill.py", "browser.py"])
def test_the_filler_never_clicks_submits_presses_keys_or_types_passwords(module):
    src = (ROOT / "jobapplier" / "apply" / module).read_text()
    forbidden = [
        r"\.click\(",
        r"\.dblclick\(",
        r"\.tap\(",
        r"\.press\(",
        r"press_sequentially",
        r"\.type\(",
        r"keyboard",
        r"mouse",
        r"submit\(",
        r"requestSubmit",
        r"dispatch_?[eE]vent",
        r"set_checked",
        r"\.focus\(",
    ]
    assert [p for p in forbidden if re.search(p, src)] == []
    called = set(re.findall(r"\.(fill|select_option|check|set_input_files|uncheck)\(", src))
    assert called <= {"fill", "select_option", "check", "set_input_files"}
