"""The owner's Chrome, and the note JobApplier leaves on a page for the owner."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

__all__ = ["LOGIN_NOTE", "banner", "chrome"]

LOGIN_NOTE = "Sign in or create the account yourself; JobApplier does not handle passwords."
# Plain DOM, so it works on any page; clicking it removes it.
_BANNER = """([head, lines]) => {
  const old = document.getElementById('jobapplier-banner');  // a new note replaces the last one
  if (old) old.remove();
  const b = document.createElement('div');
  b.id = 'jobapplier-banner';
  b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#fff3cd;'
    + 'color:#222;border-bottom:2px solid #c90;padding:10px 16px;font:14px/1.4 system-ui,sans-serif;'
    + 'cursor:pointer;max-height:40vh;overflow:auto';
  for (const [i, line] of [head, ...lines, '(Click this note to close it.)'].entries()) {
    const d = document.createElement('div');
    d.textContent = line;
    if (i === 0) d.style.fontWeight = 'bold';
    b.append(d);
  }
  b.addEventListener('click', () => b.remove());
  document.body.append(b);
  window.scrollTo(0, 0);
}"""


@contextmanager
def chrome(profile_dir: Path) -> Iterator:
    """The installed Google Chrome (no browser download) with a profile kept between runs, so a sign-in
    the owner does by hand is still there next time."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            # Playwright turns extensions off by default; the owner's password manager must load. It also
            # adds --no-sandbox, which Chrome warns about in a bar on every launch.
            ignore_default_args=["--disable-extensions", "--no-sandbox"],
        )
        try:
            yield context
        finally:
            context.close()


def banner(page, head: str, lines: list[str]) -> None:
    page.evaluate(_BANNER, [head, lines])
