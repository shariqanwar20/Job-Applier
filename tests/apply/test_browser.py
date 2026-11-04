from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from jobapplier.apply import browser
from jobapplier.apply.browser import chrome, prepare
from jobapplier.apply.fill import FieldRules
from jobapplier.apply.targets import Target
from tests.apply.test_fill import VALUES, field


class FakeLocator:
    def __init__(self, page, fid):
        self.page, self.fid = page, fid

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


class FakePage:
    """reads: what each successive read of the form returns (the last one repeats). No click method: the
    code must never need one."""

    def __init__(self, reads, title="Software Developer", idle=True, broken=()):
        self.reads, self._title, self.idle, self.broken = list(reads), title, idle, set(broken)
        self.url, self.done, self.banners, self.waits = "", [], [], []

    def goto(self, url, wait_until, timeout):
        self.url = url

    def wait_for_load_state(self, state, timeout):
        if not self.idle:
            raise PlaywrightError("Timeout 30000ms exceeded")

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def title(self):
        return self._title

    def evaluate(self, script, arg=None):
        if arg is not None:
            self.banners.append(arg)
            return None
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def locator(self, selector):
        return FakeLocator(self, selector.split('"')[1])


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
    assert result == {"filled": 2, "missing": ["LinkedIn", "Why us?"], "login": False}
    [(head, lines)] = page.banners
    assert head == "JobApplier filled 2 fields. Check every section, then submit yourself."
    assert "Required and still empty: LinkedIn, Why us?" in lines
    assert any("A_Acme.pdf" in line for line in lines)


def test_a_login_page_is_left_alone_with_a_note(setup):
    target, cfg, rules = setup
    page = FakePage([[field("1", "Email"), field("2", "Password", type="password")]], title="Sign in")
    result = prepare(page, target, VALUES, cfg, rules)
    assert page.done == [] and result == {"filled": 0, "missing": [], "login": True}
    [(head, _)] = page.banners
    assert head == "Sign in or create the account yourself; JobApplier does not handle passwords."


def test_a_slow_page_a_failing_field_and_no_upload_field_still_get_a_banner(setup):
    target, cfg, rules = setup
    page = FakePage([[field("1", "Email"), field("2", "Phone")]], idle=False, broken={"2"})
    result = prepare(page, target, VALUES, cfg, rules)
    assert result["filled"] == 1 and page.waits == []
    [(_, lines)] = page.banners
    assert any("upload" in line and "yourself" in line for line in lines)


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
