"""A fake browser tab for the apply tests: states of a page, each a snapshot as the page script reports it.
Fills and ticks change the state, so the next snapshot shows them; a click moves to the element's `goes`."""

import copy
import re
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import Error as PlaywrightError

SHOWN = {
    "seen": True,
    "rect": [40, 300, 200, 24],
    "doc": [1200, 3000],
    "aria_hidden": False,
    "clipped": False,
}
_INPUTS = {"text", "email", "tel", "url", "number", "date", "password", "file", "checkbox", "radio", "search"}


def el(ref, label="", type="text", tag=None, visible=True, **kw):
    """An element as the snapshot script reports it: its name sources, its state and its visibility facts."""
    if tag is None:
        tag = "input" if type in _INPUTS else type if type in ("select", "textarea") else "button"
    return {
        **SHOWN,
        "seen": visible,
        "labels": [],
        "ref": ref,
        "frame": [],
        "tag": tag,
        "type": type,
        "role": "",
        "label": label,
        "before": "",
        "name": "",
        "group": "",
        "value": "",
        "checked": False,
        "options": [],
        "required": False,
        "disabled": False,
        "tabindex": None,
        "aria": "",
        "labelledby": "",
        "text": "",
        "title": "",
        "form": False,
        "pw_form": False,
        "auto": "",
        **kw,
    }


def btn(ref, text, type="button", **kw):
    return el(ref, type=type, tag="button", text=text, **kw)


def link(ref, text, **kw):
    return el(ref, type="", tag="a", text=text, **kw)


def state(*elements, title="Apply", url=None, headings=(), errors=(), frames=()):
    return {
        "title": title,
        "url": url,
        "headings": list(headings),
        "errors": list(errors),
        "frames": list(frames),
        "elements": list(elements),
    }


class FakeLocator:
    def __init__(self, page, ref, label=False, value=None, aria=None, has_text=None):
        self.page, self.ref, self.label = page, ref, label
        self.value, self.aria, self.has_text = value, aria, has_text

    @property
    def first(self):
        return self

    def _el(self):
        found = [e for e in self.page.state["elements"] if e["ref"] == self.ref]
        if not found:
            raise PlaywrightError(f"Timeout: no element {self.ref}")
        return found[0]

    def evaluate(self, script):
        e = self._el()
        keys = (
            "tag",
            "type",
            "role",
            "aria",
            "labelledby",
            "text",
            "value",
            "title",
            "form",
            "pw_form",
            "auto",
        )
        return {k: e[k] for k in keys} | {"disabled": e["disabled"]}

    def filter(self, has_text):
        return FakeLocator(self.page, self.ref, has_text=has_text)

    def _do(self, method, arg=None):
        e = self._el()
        if e.get("broken"):
            raise PlaywrightError("Timeout 3000ms exceeded")
        self.page.done.append((method, self.ref, arg))
        return e

    def fill(self, value, timeout):
        self._do("fill", value)["value"] = value

    def select_option(self, label, timeout):
        self._do("select_option", label)["value"] = label

    def check(self, timeout):
        self._do("check", "label" if self.label else None)["checked"] = True

    def uncheck(self, timeout):
        self._do("uncheck")["checked"] = False

    def set_input_files(self, path, timeout):
        self._do("set_input_files", path)["value"] = Path(path).name

    def click(self, timeout):
        e = self._el()
        assert self.has_text or self.value or self.aria, "an unbound click"
        if self.has_text and not self.has_text.search(e["text"]):
            raise PlaywrightError(f"Timeout {timeout}ms exceeded: no element matches")
        if self.value and e["value"].lower() != self.value.lower():
            raise PlaywrightError(f"Timeout {timeout}ms exceeded: no element matches")
        self.page.clicks.append(e["text"] or e["value"] or e["aria"])
        if "opens" in e:
            self.page.context.pages.append(e["opens"])
        if "goes" in e:
            self.page.at = e["goes"]


class FakePage:
    def __init__(self, states, url="about:blank"):
        self.states = copy.deepcopy(list(states))
        self.at, self.url = 0, url
        self.done, self.clicks, self.banners, self.waits, self.shots, self.frames = [], [], [], [], [], []
        self.broken = None  # an exception the next snapshot raises
        self.on_wait = None  # called with each wait's ms: the owner acting while JobApplier waits
        self.fronted = 0
        self.context = SimpleNamespace(pages=[self])

    def __deepcopy__(self, memo):
        return self  # a tab a state opens is the same tab, not a copy

    @property
    def state(self):
        return self.states[self.at]

    def goto(self, url, wait_until, timeout):
        self.url = url

    def wait_for_load_state(self, state, timeout):
        pass

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if self.on_wait:
            self.on_wait(ms)

    def bring_to_front(self):
        self.fronted += 1

    def screenshot(self, path, full_page):
        assert full_page
        self.shots.append(path)
        Path(path).write_bytes(b"PNG")

    def evaluate(self, script, arg=None):
        if arg is not None:
            self.banners.append(arg)
            return None
        if self.broken:
            raise self.broken
        s = copy.deepcopy(self.state)
        return {**s, "url": s["url"] or self.url}

    def frame_locator(self, selector):
        self.frames.append(selector)
        return self

    def locator(self, selector):
        ref = int(re.search(r'data-jobapplier(?:-label)?="(\d+)"', selector)[1])
        value = re.search(r'\[value="(.*?)" i\]', selector)
        aria = re.search(r'\[aria-label="(.*?)" i\]', selector)
        return FakeLocator(
            self, ref, label="-label" in selector, value=value and value[1], aria=aria and aria[1]
        )


class FakeLlm:
    """Answers each call with the next scripted answer (a dict, or an exception to raise) and its cost."""

    def __init__(self, answers, cost=0.01):
        self.answers, self.cost, self.calls = list(answers), cost, []

    def __call__(self, prompt, schema, model, writing):
        self.calls.append({"prompt": prompt, "schema": schema, "model": model, "writing": writing})
        got = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(got, Exception):
            raise got
        return got, self.cost


CAPTCHA = state(
    frames=["iframe https://geo.captcha-delivery.com/captcha/?initialCid=x"], title="acme.example"
)


def owner_goes_to(page, at, poll_ms=3000):
    """The owner does the step during the first poll: the page moves to state at."""

    def on_wait(ms):
        if ms == poll_ms:
            page.at = at

    page.on_wait = on_wait


def answer(*actions, status="continue", note=""):
    return {"actions": [dict(a) for a in actions], "status": status, "note": note}
