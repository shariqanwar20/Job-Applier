from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from jobapplier.apply import browser
from jobapplier.apply.browser import banner, chrome
from tests.apply.fakes import FakePage, state


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
            # extensions so the owner's 1Password loads; no --no-sandbox, which Chrome warns about
            "ignore_default_args": ["--disable-extensions", "--no-sandbox"],
        }
    ]
    assert closed == [1] and Path(tmp_path / "profile").is_dir()


def test_the_banner_is_drawn_with_its_head_and_lines():
    page = FakePage([state()])
    banner(page, "Head", ["one", "two"])
    assert page.banners == [["Head", ["one", "two"]]]
    assert "getElementById('jobapplier-banner')" in browser._BANNER  # a new note replaces the old one
