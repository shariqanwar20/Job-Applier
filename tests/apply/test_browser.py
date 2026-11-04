import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from jobapplier.apply import browser
from jobapplier.apply.browser import ClickRefusedError, chrome, prepare
from jobapplier.apply.fill import FieldRules
from jobapplier.apply.targets import Target
from tests.apply.test_fill import SHOWN, VALUES, button, field


class FakeLocator:
    def __init__(self, page, fid):
        self.page, self.fid = page, fid

    def evaluate(self, script):
        return self.page.types.get(self.fid, "text")  # the element's type, read at fill time

    def _do(self, method, arg):
        if self.fid in self.page.broken:
            raise PlaywrightError("element is not visible")
        self.page.done.append((method, self.fid, arg))

    def fill(self, value, timeout):
        self._do("fill", value)

    def select_option(self, label, timeout):
        self._do("select_option", label)

    def check(self, timeout):
        self._do("check", None)

    def set_input_files(self, path, timeout):
        self._do("set_input_files", path)


class FakeNext:
    """A next button as a locator sees it, read from the current screen. A click must be bound to the
    name (filter has_text, or a value selector for an input), as the real code does."""

    def __init__(self, page, bid, has_text=None, value=None):
        self.page, self.bid, self.has_text, self.value = page, bid, has_text, value

    def _now(self):
        b = next(b for b in self.page.screen["buttons"] if b["id"] == self.bid)
        return {**b, **self.page.renamed.get(self.bid, {})}

    def evaluate(self, script):
        return {k: self._now()[k] for k in ("tag", "aria", "labelledby", "text", "value", "title")}

    def filter(self, has_text):
        return FakeNext(self.page, self.bid, has_text=has_text)

    def click(self, timeout):
        now = self._now()
        assert self.has_text or self.value, "an unbound click"
        bound = (
            self.has_text.search(now["text"]) if self.has_text else now["value"].lower() == self.value.lower()
        )
        if not bound:
            raise PlaywrightError(f"Timeout {timeout}ms exceeded: no element matches")
        self.page.clicks.append(now["text"] or now["value"])
        if not self.page.stuck:
            self.page.at += 1


class FakePage:
    """screens: the form's pages, each {"reads": successive reads of its fields (the last one repeats),
    "buttons": [...], "title": ...}; a next click moves to the following screen unless stuck. reads alone
    is a one-page form without buttons."""

    def __init__(self, reads=(), title="Software Developer", idle=True, broken=(), screens=None, stuck=False):
        self.screens = [dict(s) for s in screens or [{"reads": list(reads), "buttons": []}]]
        for s in self.screens:
            s["reads"] = list(s["reads"])
        self._title, self.idle, self.broken, self.stuck = title, idle, set(broken), stuck
        self.url, self.done, self.banners, self.waits, self.clicks, self.at = "", [], [], [], [], 0
        self.renamed = {}  # button id -> name sources it has by the time it would be clicked
        self.types = {}  # field id -> its type on the page, as the last read saw it

    @property
    def screen(self):
        return self.screens[self.at]

    def goto(self, url, wait_until, timeout):
        self.url = url

    def wait_for_load_state(self, state, timeout):
        if not self.idle:
            raise PlaywrightError("Timeout 30000ms exceeded")

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def title(self):
        return self.screen.get("title", self._title)

    def evaluate(self, script, arg=None):
        if arg is not None:
            self.banners.append(arg)
            return None
        if "data-jobapplier-button" in script:
            return self.screen["buttons"]
        reads = self.screen["reads"]
        got = reads.pop(0) if len(reads) > 1 else reads[0]
        self.types |= {f["id"]: f["type"] for f in got}
        return got

    def locator(self, selector):
        bid = selector.split('"')[1]
        if "data-jobapplier-button" not in selector:
            return FakeLocator(self, bid)
        value = re.search(r'\[value="(.*?)" i\]', selector)
        return FakeNext(self, bid, value=value[1] if value else None)


@pytest.fixture
def setup(config, tmp_path):
    pdf = tmp_path / "A_Acme.pdf"
    pdf.write_bytes(b"%PDF")
    target = Target("J1", "Acme", "Developer", "https://jobs.lever.co/acme/1/apply", pdf, "", {"id": "p1"})
    return target, config, FieldRules.from_config(config["apply"])


def test_prepare_uploads_the_resume_first_waits_for_the_parser_then_fills_and_shows_a_banner(setup):
    target, cfg, rules = setup
    resume = field("0", "Resume/CV", type="file", visible=False)
    parsed = [resume, field("1", "Email", value="parser@example.com"), field("2", "City", value="Ottawa")]
    after = [*parsed, field("3", "LinkedIn", required=True), field("4", "Why us?", required=True)]
    page = FakePage([[resume], after, after])
    page.done.clear()
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.url == target.url
    assert page.done[0] == ("set_input_files", "0", str(target.pdf))
    assert page.waits == [3000]  # parser_wait_seconds
    assert page.done[1:] == [("fill", "1", "alex@example.com"), ("fill", "3", "https://linkedin.com/in/alex")]
    assert result == {"filled": 2, "missing": ["LinkedIn", "Why us?"], "login": False, "pages": 1}
    [(head, lines)] = page.banners
    assert head == "JobApplier filled 2 fields. Check every section, then submit yourself."
    assert "Required and still empty: LinkedIn, Why us?" in lines
    assert any("A_Acme.pdf" in line for line in lines)


def test_a_login_page_is_left_alone_with_a_note(setup):
    target, cfg, rules = setup
    page = FakePage([[field("1", "Email"), field("2", "Password", type="password")]], title="Sign in")
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.done == [] and result == {"filled": 0, "missing": [], "login": True, "pages": 1}
    [(head, _)] = page.banners
    assert head == "Sign in or create the account yourself; JobApplier does not handle passwords."


def test_a_slow_page_a_failing_field_and_no_upload_field_still_get_a_banner(setup):
    target, cfg, rules = setup
    page = FakePage([[field("1", "Email"), field("2", "Phone")]], idle=False, broken={"2"})
    result = prepare(page, target, VALUES, cfg, rules)
    assert result["filled"] == 1 and page.waits == []
    [(_, lines)] = page.banners
    assert any(line.startswith("No resume upload field found") for line in lines)


def test_a_page_without_a_form_says_so(setup):
    target, cfg, rules = setup
    page = FakePage([[]])
    prepare(page, target, VALUES, cfg, rules)
    [(_, lines)] = page.banners
    assert any("No application form" in line for line in lines)


def test_chrome_opens_the_installed_chrome_with_a_kept_profile(monkeypatch, tmp_path):
    launched, closed = [], []
    context = SimpleNamespace(close=lambda: closed.append(1))

    def launch(**kw):
        launched.append(kw)
        return context

    @contextmanager
    def fake_sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    monkeypatch.setattr(browser, "sync_playwright", fake_sync_playwright)
    with chrome(tmp_path / "profile") as ctx:
        assert ctx is context and closed == []
    assert launched == [
        {
            "user_data_dir": str(tmp_path / "profile"),
            "channel": "chrome",
            "headless": False,
            "ignore_default_args": ["--disable-extensions"],  # so the owner's 1Password extension loads
        }
    ]
    assert closed == [1] and Path(tmp_path / "profile").is_dir()


@pytest.fixture
def field_rules(config):
    return FieldRules.from_config(config["apply"])


def one_button(**names):
    return FakePage(screens=[screen([], btn("b1", **names)), screen([])])


@pytest.mark.parametrize(
    "names",
    [
        {"text": "Submit application"},
        {"text": "Continue and submit"},
        {"text": "Continue with Google"},
        {"text": "Back"},
        {"text": "Submit application", "aria": "Next"},
        {"tag": "input", "text": "", "aria": "Next", "value": "Submit"},
    ],
)
def test_click_next_refuses_anything_but_a_next_button(names, field_rules):
    page = one_button(**names)
    with pytest.raises(ClickRefusedError):
        browser._click_next(page, "b1", field_rules, 1000)
    assert page.clicks == []


def test_click_next_clicks_a_next_button_bound_to_its_name(field_rules):
    page = one_button(text="Save and Continue")
    browser._click_next(page, "b1", field_rules, 1000)
    assert page.clicks == ["Save and Continue"]
    page = one_button(tag="input", text="", value="Next")
    browser._click_next(page, "b1", field_rules, 1000)
    assert page.clicks == ["Next"]


def btn(bid, text="", **names):
    return {**button(bid, text, **names), **SHOWN}


def screen(fields, *buttons, title="Apply"):
    return {"reads": [fields], "buttons": list(buttons), "title": title}


THREE_PAGES = [
    screen([field("1", "Resume/CV", type="file"), field("2", "First Name")], btn("b1", "Next")),
    screen(
        [field("3", "Email"), field("4", "I agree to the terms", type="checkbox")],
        btn("b1", "Back"),
        btn("b2", "Continue"),
    ),
    screen([field("5", "Notes")], btn("b1", "Back"), btn("b2", "Submit application")),
]


def test_a_three_page_form_is_filled_through_to_the_review_page_and_stops_before_submit(setup):
    target, cfg, rules = setup
    page = FakePage(screens=THREE_PAGES)
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.clicks == ["Next", "Continue"] and page.at == 2
    assert page.done == [
        ("set_input_files", "1", str(target.pdf)),
        ("fill", "2", "Alex"),
        ("fill", "3", "alex@example.com"),
        ("check", "4", None),
    ]
    assert result == {"filled": 3, "missing": [], "login": False, "pages": 3}
    assert page.waits == [3000, 2000, 2000]  # the parser, then each next
    [(head, lines)] = page.banners  # only on the page where it stopped
    assert "filled 3 fields" in head
    assert "Stopped on page 3: This is the last step: check every page, then submit yourself." in lines


def test_a_required_empty_field_stops_on_that_page(setup):
    target, cfg, rules = setup
    first = screen([field("1", "First Name"), field("2", "Why us?", required=True)], btn("b1", "Next"))
    page = FakePage(screens=[first, THREE_PAGES[1]])
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.clicks == [] and result["missing"] == ["Why us?"] and result["pages"] == 1
    [(_, lines)] = page.banners
    assert "Required and still empty: Why us?" in lines


def test_a_page_that_does_not_change_after_next_stops(setup):
    target, cfg, rules = setup
    page = FakePage(screens=[THREE_PAGES[0], THREE_PAGES[2]], stuck=True)
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.clicks == ["Next"] and result["pages"] == 1
    [(_, lines)] = page.banners
    assert any("did not change" in line and "error" in line for line in lines)


def test_a_sign_in_page_after_next_stops_without_filling(setup):
    target, cfg, rules = setup
    login = screen([field("9", "Email"), field("10", "Password", type="password")], btn("b1", "Next"))
    page = FakePage(screens=[THREE_PAGES[0], login])
    result = prepare(page, target, VALUES, cfg, rules)
    assert result["login"] and result["pages"] == 2 and ("fill", "9", "alex@example.com") not in page.done
    assert page.banners[-1][0].startswith("Sign in or create the account yourself")


def test_max_pages_stops_the_walk(setup):
    target, cfg, rules = setup
    cfg["apply"]["max_pages"] = 2
    page = FakePage(screens=THREE_PAGES)
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.clicks == ["Next"] and result["pages"] == 2
    assert any("apply.max_pages (2)" in line for line in page.banners[0][1])


def test_a_next_button_that_turned_into_submit_is_not_clicked(setup):
    target, cfg, rules = setup
    page = FakePage(screens=THREE_PAGES)
    page.renamed = {"b1": {"text": "Submit"}}
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.clicks == [] and result["pages"] == 1
    assert any("not a next-step button" in line for line in page.banners[0][1])


def test_fill_page_does_only_planned_actions_and_checks_only_the_planned_kind():
    from jobapplier.apply.fill import fill_page

    page = FakePage([[field("1", "Yes", type="radio"), field("2", "Agree", type="text")]])
    page.evaluate("read")
    with pytest.raises(ValueError, match="check"):
        fill_page(page, [("check", "1", "", "q")], 1000)
    with pytest.raises(ValueError, match="set_input_files"):
        fill_page(page, [("set_input_files", "1", "/x.pdf", "q")], 1000)
    assert fill_page(page, [("check_radio", "1", "Yes", "q"), ("tick_consent", "2", "", "Agree")], 1000) == 1
    assert page.done == [("check", "1", None)]  # field 2 is no longer a checkbox: not ticked


def test_the_upload_note_tells_not_found_from_failed(setup):
    target, cfg, rules = setup
    page = FakePage([[field("0", "Resume/CV", type="file"), field("1", "Email")]], broken={"0"})
    prepare(page, target, VALUES, cfg, rules)
    assert any(line.startswith("Resume upload failed (Error)") for line in page.banners[0][1])
    page = FakePage([[field("1", "Email")]])
    prepare(page, target, VALUES, cfg, rules)
    assert any(line.startswith("No resume upload field found") for line in page.banners[0][1])
