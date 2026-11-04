"""Opens an application in the owner's Chrome and pre-fills it. The owner submits; this code never does."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from jobapplier.apply.fill import (
    FieldRules,
    button_name,
    fill_page,
    is_login,
    missing_required,
    next_button,
    plan,
    read_buttons,
    read_fields,
    resume_input,
    upload_file,
)
from jobapplier.apply.targets import Target

__all__ = ["LOGIN_NOTE", "ClickRefusedError", "chrome", "prepare"]

LOGIN_NOTE = "Sign in or create the account yourself; JobApplier does not handle passwords."
# Plain DOM, so it works on any page; clicking it removes it.
_BANNER = """([head, lines]) => {
  const b = document.createElement('div');
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
            # Playwright turns extensions off by default; the owner's password manager must load.
            ignore_default_args=["--disable-extensions"],
        )
        try:
            yield context
        finally:
            context.close()


class ClickRefusedError(Exception):
    pass


def _click_next(button, rules: FieldRules, timeout_ms: float) -> None:
    """The only click in the apply code. The name is read again right before the click, so a button that
    has turned into Submit (or a sign-in) is refused, never clicked."""
    name = button_name(button)
    if not rules.is_next(name):
        raise ClickRefusedError(f"not a next-step button: {name!r}")
    button.click(timeout=timeout_ms)


def _banner(page, head: str, lines: list[str]) -> None:
    page.evaluate(_BANNER, [head, lines])


def _settle(page, seconds: float) -> None:
    with suppress(PlaywrightError):  # pages with live chat or analytics never go idle; the form is there
        page.wait_for_load_state("networkidle", timeout=seconds * 1000)


def _signature(page, fields: list[dict], buttons: list[dict]) -> tuple:
    """What the page shows; the same before and after a next click means the form refused the step."""
    return page.url, [(f["type"], f["name"], f["label"]) for f in fields], [b["name"] for b in buttons]


def prepare(page, target: Target, values: dict, cfg: dict, rules: FieldRules) -> dict:
    """Opens target.url in page, uploads the resume and fills each page of the form, clicking next between
    pages, then shows a banner where it stopped. Never on a submit button: the owner submits.

    Returns {"filled": n, "missing": [required labels still empty], "login": bool, "pages": pages seen}.
    """
    a = cfg["apply"]
    timeout = a["field_timeout_seconds"] * 1000
    page.goto(target.url, wait_until="domcontentloaded", timeout=a["load_timeout_seconds"] * 1000)
    _settle(page, a["load_timeout_seconds"])
    filled, uploaded, upload_error, notes = 0, False, None, []
    for number in range(1, a["max_pages"] + 1):
        fields = read_fields(page)
        if is_login(page.url, page.title(), fields, rules):
            _banner(page, LOGIN_NOTE, [])
            return {"filled": filled, "missing": [], "login": True, "pages": number}
        upload = None if uploaded else resume_input(fields)
        # Upload first: a resume parser overwrites fields filled before it runs.
        if upload:
            upload_error = upload_file(page, upload, str(target.pdf), timeout)
            uploaded = upload_error is None
        if uploaded and upload:
            notes.append(f"Uploaded {target.pdf.name}.")
            page.wait_for_timeout(a["parser_wait_seconds"] * 1000)
            fields = read_fields(page)
        filled += fill_page(page, plan(fields, values, rules, target.description), timeout)
        after, buttons = read_fields(page), read_buttons(page)
        missing = missing_required(after)
        button, stop = next_button(buttons, rules)
        if missing:
            stop = f"Required and still empty: {', '.join(missing)}"
        elif number == a["max_pages"] and button:
            stop = f"Stopped after apply.max_pages ({number}) pages: continue yourself."
        if stop:
            break
        before = _signature(page, after, buttons)
        try:
            _click_next(page.locator(f'[data-jobapplier-button="{button}"]'), rules, timeout)
        except ClickRefusedError as exc:
            stop = f"{exc}; continue yourself."
            break
        _settle(page, a["load_timeout_seconds"])
        page.wait_for_timeout(a["next_wait_seconds"] * 1000)
        if _signature(page, read_fields(page), read_buttons(page)) == before:
            stop = "The page did not change after Next: look for an error message on it."
            break
    if upload_error:
        notes.append(f"Resume upload failed ({upload_error}): upload {target.pdf} yourself.")
    elif not uploaded:
        notes.append(f"No resume upload field found: upload {target.pdf} yourself.")
    if not any(f["type"] not in ("file", "password") and f["visible"] for f in after):
        notes.append("No application form on this page: open the application yourself.")
    notes.append(stop if missing else f"Stopped on page {number}: {stop}")
    head = f"JobApplier filled {filled} fields. Check every section, then submit yourself."
    _banner(page, head, notes)
    return {"filled": filled, "missing": missing, "login": False, "pages": number}
