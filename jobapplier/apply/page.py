"""The page as the apply agent sees it, and the only actions it may take there. It never submits: see the
module test.

read() turns the page into a compact JSON snapshot and tags each element with data-jobapplier="<ref>".
act() does one model action on a ref, after reading the element again and checking it in code: no click on
a submit, sign-in or account control, no typing in a password form, and an upload only of the tailored PDF.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from jobapplier.core.rules import norm, words

__all__ = ["ClickRules", "RefusedError", "Snapshot", "act", "code_masks", "fill_code", "read", "verify"]

_TEXT = frozenset(
    {
        "text",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "month",
        "week",
        "time",
        "datetime-local",
        "search",
        "textarea",
    }
)
_BUTTON_INPUTS = frozenset({"button", "submit", "image", "reset"})
_FITS = {  # op -> the element types and roles it may act on
    "fill": (_TEXT, ()),
    "select": ({"select"}, ()),
    "check": ({"checkbox", "radio"}, ("checkbox", "radio", "switch")),
    "uncheck": ({"checkbox"}, ("checkbox", "switch")),
    "upload": ({"file"}, ()),
}
_SOURCES = ("aria", "labelledby", "text", "value", "title")
_HONEYPOT = re.compile(r"^hp[_-]|honeypot", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^(select|choose|please|none selected)\b|^$")
_LIST_MAX = 20  # headings and error messages kept per snapshot

# The kind of control: an input's type, a button's type (submit when the page sets none), else the tag.
_KIND = """const kind = el => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') return (el.type || 'text').toLowerCase();
    if (tag === 'button') return (el.type || 'submit').toLowerCase();
    return ['select', 'textarea'].includes(tag) ? tag : '';
  };
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  const text = el => el ? t(el.innerText || el.textContent) : '';
  const by = el => (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)
    .map(id => el.ownerDocument.getElementById(id)).filter(Boolean).map(text).join(' ');"""

# Every element a person could use, in the page and its same-origin frames; cross-origin frames are only
# named. Visibility facts go to Python, which decides (a honeypot is often hidden by CSS).
_SNAPSHOT = (
    "() => {\n  "
    + _KIND
    + """
  const shown = el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const facts = el => {
    const doc = el.ownerDocument, win = doc.defaultView, r = el.getBoundingClientRect();
    let clipped = false;
    for (let a = el.parentElement; a && a !== doc.body; a = a.parentElement) {
      const s = win.getComputedStyle(a);
      if (s.overflow !== 'visible' && (a.clientHeight === 0 || a.clientWidth === 0)) {
        clipped = true;
        break;
      }
    }
    const d = doc.documentElement;
    return {
      seen: shown(el), rect: [r.left + win.scrollX, r.top + win.scrollY, r.width, r.height],
      doc: [d.scrollWidth, d.scrollHeight], aria_hidden: !!el.closest('[aria-hidden="true"]'), clipped,
    };
  };
  const own = el => {
    const parts = [...(el.labels || [])].map(text);
    parts.push(by(el), el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '');
    return t(parts.filter(Boolean).join(' '));
  };
  const before = el => {  // stops at the field's own form or fieldset: text past it is another question
    const box = el.closest('fieldset, form');
    for (let n = el; n && n !== box && n !== el.ownerDocument.body; n = n.parentElement)
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
        const got = text(s);
        if (got) return got.slice(-200);
      }
    return '';
  };
  const out = {url: location.href, title: document.title, headings: [], errors: [], frames: [], elements: []};
  const seen = new Set();
  let ref = 0, frames = 0;
  const walk = (doc, path) => {
    for (const a of ['data-jobapplier', 'data-jobapplier-label', 'data-jobapplier-frame'])
      doc.querySelectorAll(`[${a}]`).forEach(e => e.removeAttribute(a));
    doc.querySelectorAll('h1, h2, h3, legend, [role=heading]').forEach(h => {
      if (shown(h) && text(h)) out.headings.push(text(h).slice(0, 200));
    });
    doc.querySelectorAll('[role=alert], [aria-live=assertive], [class*=error i], [class*=invalid i]')
      .forEach(e => {
        const msg = text(e).slice(0, 200);
        if (msg && shown(e) && !seen.has(msg)) { seen.add(msg); out.errors.push(msg); }
      });
    const sel = 'input, textarea, select, button, a[href], [role=button], [role=link], [role=option], '
      + '[role=radio], [role=checkbox], [role=switch], [role=combobox], [role=menuitem], [role=tab]';
    doc.querySelectorAll(sel).forEach(el => {
      const tag = el.tagName.toLowerCase(), type = kind(el);
      if (type === 'hidden') return;
      const n = ref++;
      el.setAttribute('data-jobapplier', String(n));
      const labels = [...(el.labels || [])];
      if (type === 'radio' || type === 'checkbox')  // a styled one hides its input behind the label
        labels.forEach(l => l.setAttribute('data-jobapplier-label', String(n)));
      const legend = el.closest('fieldset') && el.closest('fieldset').querySelector('legend');
      const picked = tag === 'select' && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      const field = ['input', 'textarea', 'select'].includes(tag);
      out.elements.push({
        ref: n, frame: path, tag, type, role: el.getAttribute('role') || '',
        label: own(el).slice(0, 300), before: field ? before(el) : '', name: el.name || el.id || '',
        group: type === 'radio' ? (text(legend) || before(el.closest('label') || el)) : '',
        // never a password's value: the owner's password manager may have filled it
        value: tag === 'select' ? (picked && el.value !== '' ? text(picked) : '')
          : field && type !== 'password' ? t(el.value).slice(0, 500) : '',
        checked: !!el.checked || el.getAttribute('aria-checked') === 'true'
          || el.getAttribute('aria-selected') === 'true'  // a picked option of a custom dropdown
          // a picked value shown as a removable pill (Workday: "Canada (+1), press delete to clear value")
          || el.getAttribute('data-automation-id') === 'selectedItem'
          || /\b(clear|remove) (this )?(value|selection)\b/i.test(el.getAttribute('aria-label') || ''),
        options: tag === 'select' ? [...el.options].map(text) : [],
        required: !!el.required || el.getAttribute('aria-required') === 'true',
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        tabindex: el.getAttribute('tabindex'),
        aria: t(el.getAttribute('aria-label')), labelledby: by(el), text: field ? '' : text(el).slice(0, 200),
        title: t(el.getAttribute('title')), auto: (el.getAttribute('autocomplete') || '').toLowerCase(),
        maxlength: el.maxLength > 0 ? el.maxLength : null, ...facts(el), labels: labels.map(facts),
      });
    });
    doc.querySelectorAll('iframe, frame').forEach(f => {
      let inner = null;
      try { inner = f.contentDocument; } catch (e) { inner = null; }
      if (inner && inner.body) {
        const k = frames++;
        f.setAttribute('data-jobapplier-frame', String(k));
        walk(inner, [...path, k]);
      } else if (shown(f)) out.frames.push('iframe ' + (f.src || '').slice(0, 200));
    });
  };
  walk(document, []);
  return out;
}"""
)

# The element as it is right before an action, so a page that changed since the snapshot is judged anew.
_LIVE = (
    "el => {\n  "
    + _KIND
    + """
  const tag = el.tagName.toLowerCase(), type = kind(el), form = el.form || el.closest('form');
  return {
    tag, type, role: el.getAttribute('role') || '',
    aria: t(el.getAttribute('aria-label')), labelledby: by(el),
    text: ['input', 'textarea', 'select'].includes(tag) ? '' : text(el),
    value: tag === 'input' && ['button', 'submit', 'image', 'reset'].includes(type) ? t(el.value) : '',
    title: t(el.getAttribute('title')), form: !!el.form,
    pw_form: !!(form && form.querySelector('input[type=password]')),
    auto: (el.getAttribute('autocomplete') || '').toLowerCase(),
    form_fields: el.form ? [...el.form.elements].filter(f => f.tagName !== 'BUTTON'
      && !['hidden', 'submit', 'button', 'image', 'reset'].includes((f.type || '').toLowerCase())).length : 0,
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
  };
}"""
)


class RefusedError(Exception):
    """An action the code will not do; stop, when set, is the status the loop stops with."""

    def __init__(self, why: str, stop: str | None = None):
        super().__init__(why)
        self.stop = stop


def _foreign(text: str) -> bool:
    """Has a letter outside Latin after NFKD (Cyrillic \u0405 looks like S); accents are fine."""
    text = unicodedata.normalize("NFKD", "".join(c for c in text if unicodedata.category(c) != "Cf"))
    return any(c.isalpha() and not c.isascii() for c in text if not unicodedata.combining(c))


def _raw(phrases: list[str]) -> str:
    """An alternation matching phrases in raw page text: "save & continue" also matches "Save & Continue"."""
    return "|".join(r"\W+".join(map(re.escape, norm(p).split())) for p in phrases if norm(p))


@dataclass(frozen=True)
class ClickRules:
    submit_stem: re.Pattern
    sign_in: re.Pattern
    apply: re.Pattern
    next_start: re.Pattern  # a next phrase at the start of a normalized visible name
    next_max_extra: int
    next_deny: frozenset[str]
    raw_stems: str
    raw_sign_in: str
    raw_apply: str
    code: (
        re.Pattern
    )  # a verification code page or field: the code from the mail goes there, never the model's
    verify_start: re.Pattern  # the code page's verify button
    code_page_max_fields: int
    one_click: re.Pattern  # an Apply that sends a stored profile at once

    @classmethod
    def from_config(cls, apply_cfg: dict) -> "ClickRules":
        nexts = sorted({n for n in map(norm, apply_cfg["next_words"]) if n}, key=len, reverse=True)
        return cls(
            words(apply_cfg["submit_stems"], prefix=True),
            words(apply_cfg["sign_in_words"]),
            words(apply_cfg["apply_words"]),
            re.compile(r"^(" + "|".join(map(re.escape, nexts)) + r")\b"),
            int(apply_cfg["next_max_extra_words"]),
            frozenset(map(norm, apply_cfg["next_deny_words"])),
            _raw(apply_cfg["submit_stems"]),
            _raw(apply_cfg["sign_in_words"]),
            _raw(apply_cfg["apply_words"]),
            words(apply_cfg["agent"]["code_words"]),
            re.compile(r"^(" + _raw(apply_cfg["agent"]["verify_words"]) + r")\b"),
            int(apply_cfg["agent"]["code_page_max_fields"]),
            words(apply_cfg["one_click_words"]),
        )

    @staticmethod
    def _visible(live: dict) -> str:
        return norm(live["value"]) if live["tag"] == "input" else norm(live["text"]) or norm(live["aria"])

    def _is_next(self, live: dict, names: list[str]) -> bool:
        visible = self._visible(live)
        start = self.next_start.match(visible)
        if not start or len(visible[start.end() :].split()) > self.next_max_extra:
            return False
        return not any(w in self.next_deny for n in names for w in n.split())

    def name_refusal(self, live: dict) -> str | None:
        """A look-alike letter, a submit word or a sign-in word in any source of the control's name."""
        if any(_foreign(live[k]) for k in _SOURCES):  # a look-alike letter could hide a submit word
            return "a look-alike letter in its name"
        names = [norm(live[k]) for k in _SOURCES]
        if any(self.submit_stem.search(n) for n in names):
            return "a submit word in its name: the owner submits"
        if any(self.sign_in.search(n) for n in names):
            return "a sign-in or account word in its name: the owner signs in"
        return None

    def refuse(self, live: dict, fresh: bool) -> str | None:
        """Why the control must not be clicked, or None. fresh: nothing on the page is answered or
        uploaded and the agent has filled nothing, as on a job description page."""
        names = [norm(live[k]) for k in _SOURCES]
        tag, kind = live["tag"], live["type"]
        if tag in ("select", "textarea") or (tag == "input" and kind not in _BUTTON_INPUTS):
            return "not a button: use fill, select, check or upload"
        if not self._visible(live):  # Chrome shows an unnamed submit button as "Submit"
            return "it has no visible name"
        if kind == "reset":
            return "a reset button clears the form"
        if why := self.name_refusal(live):
            return why
        if any(self.apply.search(n) for n in names):
            if any(self.one_click.search(n) for n in names):
                return "a one-click Apply: it may send a stored profile at once"
            # A form's own submit, or any page with answers on it: its Apply may send the application.
            own_submit = live["form"] and kind in ("submit", "image") and live["form_fields"] > 0
            if own_submit or not fresh:
                return "an Apply button on a form or a page with answers: it may submit the application"
            return None
        if self._is_next(live, names):
            return None
        if tag == "a" or live["role"] == "link":
            return "a link that is not Next or Apply: it would leave the form"
        if live["form"] and kind in ("submit", "image"):
            return "it submits the form and is not a Next button"
        return None

    def binding(self, live: dict, fresh: bool) -> re.Pattern:
        """The text the click is bound to, which Playwright checks again on every retry: the text that was
        checked, with no submit, sign-in or (once anything is filled) apply word added since."""
        never = [rf"(?!.*\b({self.raw_stems}))", rf"(?!.*\b({self.raw_sign_in})\b)"]
        if not fresh:
            never.append(rf"(?!.*\b({self.raw_apply})\b)")
        text = r"\W+".join(map(re.escape, live["text"].split()))
        return re.compile("^" + "".join(never) + r"\W*" + text, re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Snapshot:
    view: dict  # what the model sees
    text: str  # view as JSON
    refs: dict[int, dict]  # ref -> the element as read, for the refs in view
    password: bool  # a visible password field: an account step
    captcha: bool  # a captcha that is the whole page: only the owner can get past it
    code_page: bool  # it asks for a verification code
    fresh: bool  # no answer and no file on the page: a job description, not a form in progress
    missing: list[str]  # required fields still empty

    def label(self, ref: int) -> str:
        e = self.refs[ref]
        if e["type"] == "radio":
            return f"{e['group'][:200]}: {e['label'] or e['value']}".lstrip(": ")
        return _label(e) if _is_field(e) else _name(e)


def _visible(facts: dict) -> bool:
    x, y, w, h = facts["rect"]
    page_w, page_h = facts["doc"]
    on_page = x + w > 0 and y + h > 0 and x < page_w and y < page_h
    return facts["seen"] and w > 1 and h > 1 and on_page and not (facts["aria_hidden"] or facts["clipped"])


def _is_honeypot(e: dict) -> bool:
    return bool(_HONEYPOT.search(e["name"])) or (e["tabindex"] == "-1" and not e["label"])


def _label(e: dict) -> str:
    return (e["label"] or e["before"] or e["name"]).strip()[:200]


def _name(e: dict) -> str:
    return next((e[k] for k in ("text", "aria", "labelledby", "value", "title") if e[k]), "")[:200]


def _is_field(e: dict) -> bool:
    return e["tag"] in ("select", "textarea") or (e["tag"] == "input" and e["type"] not in _BUTTON_INPUTS)


def _is_empty(e: dict) -> bool:
    if e["type"] in ("checkbox", "radio"):
        return not e["checked"]
    if e["type"] == "select":
        return bool(_PLACEHOLDER.search(norm(e["value"])))
    return not e["value"].strip()


def _options(options: list[str], cap: int) -> list[str]:
    if len(options) <= cap:
        return options
    return [*options[:cap], f"({len(options) - cap} more: select one by its exact text)"]


_DIGIT = re.compile(r"\bdigit\b")
_CODE_TYPES = frozenset({"text", "tel", "number", "search"})


def _is_box(e: dict) -> bool:
    return e["tag"] == "input" and e["type"] in _CODE_TYPES and e.get("maxlength") == 1


def _mark_codes(kept: list[dict], code: re.Pattern) -> None:
    """Sets e["code"]: a one-time-code field, one labelled with a code word or "digit 1 of six", or a
    one-character box in a run of four or more (one alone is a middle initial)."""
    run = sum(map(_is_box, kept)) >= 4
    for e in kept:
        text_box = e["tag"] == "input" and e["type"] in _CODE_TYPES
        named = e.get("auto") == "one-time-code" or code.search(norm(f"{_label(e)} {e['name']}"))
        e["code"] = text_box and bool(named or _DIGIT.search(norm(_label(e))) or (run and _is_box(e)))


def _is_answered(e: dict) -> bool:
    """A field the owner, a parser or the agent has answered, or a file input (an upload step)."""
    if e["type"] == "file":
        return True
    if e["type"] in ("checkbox", "radio"):
        return e["checked"]
    return _is_field(e) and e["type"] not in ("select", "search") and bool(e["value"].strip())


def _entry(e: dict, cap: int) -> dict:
    if not _is_field(e):
        tag = "link" if e["tag"] == "a" or e["role"] == "link" else e["role"] or "button"
        out = {"ref": e["ref"], "tag": tag, "text": _name(e)}
    else:
        out = {"ref": e["ref"], "type": e["type"], "label": _label(e)}
        if e["role"]:
            out["role"] = e["role"]
        # never a code's value: neither the model nor the trace may see it
        if e["value"] and e["type"] not in ("checkbox", "radio", "password") and not e["code"]:
            out["value"] = e["value"]
        if e["options"]:
            out["options"] = _options(e["options"], cap)
    for key in ("checked", "required", "disabled"):
        if e[key]:
            out[key] = True
    return out


def _entries(kept: list[dict], cap: int) -> list[tuple[dict, list[int]]]:
    """(view entry, its refs) in page order; the radios of one question are one entry with options."""
    out, groups = [], {}
    for e in kept:
        if e["type"] != "radio":
            out.append((_entry(e, cap), [e["ref"]]))
            continue
        key = (tuple(e["frame"]), e["name"] or e["group"] or e["ref"])
        if key not in groups:
            groups[key] = ({"type": "radio", "label": e["group"][:200], "options": []}, [])
            out.append(groups[key])
        entry, refs = groups[key]
        option = {"ref": e["ref"], "label": (e["label"] or e["value"])[:200]}
        if e["checked"]:
            option["checked"] = True
        entry["options"].append(option)
        if e["required"]:
            entry["required"] = True
        refs.append(e["ref"])
    return out


def _missing(kept: list[dict]) -> list[str]:
    out, answered = [], set()
    radios = [e for e in kept if e["type"] == "radio"]
    for e in radios:
        if e["checked"]:
            answered.add(e["name"] or e["group"])
    for e in kept:
        if not (_is_field(e) and e["required"]) or e["disabled"] or e["type"] in ("file", "password"):
            continue
        if e["type"] == "radio":
            key = e["name"] or e["group"]
            if key not in answered:
                answered.add(key)  # once per question
                out.append(e["group"][:200] or _label(e))
        elif _is_empty(e):
            out.append(_label(e))
    return out


def read(page, agent_cfg: dict) -> Snapshot:
    raw = page.evaluate(_SNAPSHOT)
    kept, password = [], False
    for e in raw["elements"]:
        shown = _visible(e)
        password = password or (shown and e["type"] == "password")
        via_label = not shown and e["type"] in ("radio", "checkbox") and any(map(_visible, e["labels"]))
        if (shown or via_label or e["type"] == "file") and not _is_honeypot(e):
            kept.append({**e, "via_label": via_label})
    code = words(agent_cfg["code_words"])
    _mark_codes(kept, code)
    entries = _entries(kept, agent_cfg["max_options"])
    view = {
        "url": raw["url"],
        "title": raw["title"],
        "headings": raw["headings"][:_LIST_MAX],
        "errors": raw["errors"][:_LIST_MAX],
        "frames": raw["frames"][:_LIST_MAX],
        "elements": [entry for entry, _ in entries],
    }
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    while len(text) > agent_cfg["max_snapshot_chars"] and view["elements"]:
        view["elements"].pop()
        view["more"] = f"{len(entries) - len(view['elements'])} elements not shown"
        text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    shown_refs = {r for _, refs in entries[: len(view["elements"])] for r in refs}
    refs = {e["ref"]: e for e in kept if e["ref"] in shown_refs}
    said = [*raw["headings"], *(f"{_label(e)} {e['name']}" for e in kept if _is_field(e))]
    code_page = any(code.search(norm(t)) for t in said) or any(e.get("auto") == "one-time-code" for e in kept)
    fresh = not any(map(_is_answered, kept))
    captcha = _captcha(raw, kept, agent_cfg)
    return Snapshot(view, text, refs, password, captcha, code_page, fresh, _missing(kept))


def _captcha(raw: dict, kept: list[dict], agent_cfg: dict) -> bool:
    """A captcha frame with no form field beside it: it blocks the page. A widget inside a form (or an
    invisible reCAPTCHA, which shows a badge) does not stop the filling; the owner ticks it before submit."""
    hosts = [h.lower() for h in agent_cfg["captcha_hosts"]]
    seen = [raw["url"].lower()] + [f.lower() for f in raw["frames"] if "size=invisible" not in f.lower()]
    found = any(h in src for src in seen for h in hosts)
    return found and not any(_is_field(e) and e["type"] != "file" for e in kept)


def _click(root, selector: str, live: dict, rules: ClickRules, fresh: bool, timeout_ms: float) -> str:
    """The only click in the apply code. Returns "apply" for an Apply click, else "click"."""
    if why := rules.refuse(live, fresh):
        raise RefusedError(why)
    if live["tag"] == "input":  # its name is its value
        value = live["value"].replace("\\", "\\\\").replace('"', '\\"')
        target = root.locator(f'{selector}[value="{value}" i]')
    elif live["text"]:
        target = root.locator(selector).filter(has_text=rules.binding(live, fresh))
    else:
        aria = live["aria"].replace("\\", "\\\\").replace('"', '\\"')
        target = root.locator(f'{selector}[aria-label="{aria}" i]')
    if live["role"] in ("option", "menuitem", "treeitem") and live["text"].strip():
        # a custom dropdown (Workday) acts on its option's inner text, not on the row's padded middle
        inner = target.get_by_text(live["text"].strip(), exact=True)
        if inner.count():
            target = inner.first
    target.click(timeout=timeout_ms)
    return "apply" if any(rules.apply.search(norm(live[k])) for k in _SOURCES) else "click"


def _where(page, e: dict):
    """(the frame that holds e, its selector)."""
    root = page
    for k in e["frame"]:
        root = root.frame_locator(f'[data-jobapplier-frame="{k}"]')
    return root, f'[data-jobapplier="{e["ref"]}"]'


def act(
    page, snap: Snapshot, action: dict, pdf: Path, rules: ClickRules, apply_open: bool, timeout_ms: float
):
    """Does one model action ({"op", "ref", "value"?}) or raises RefusedError; a click returns "apply" or
    "click". An upload always puts pdf in: the model never names a file. apply_open: the application has
    not started yet (no fill, no click past the job page's Apply, no hand-off)."""
    ref = action.get("ref")
    e = snap.refs.get(ref) if isinstance(ref, int) and not isinstance(ref, bool) else None
    if e is None:
        raise RefusedError(f"no element {ref!r} on this page")
    root, selector = _where(page, e)
    live = root.locator(selector).evaluate(_LIVE)
    op = action.get("op")
    if live["disabled"]:
        raise RefusedError("it is disabled")
    if op == "click":
        return _click(root, selector, live, rules, snap.fresh and apply_open, timeout_ms)
    if op not in _FITS:
        raise RefusedError(f"not an action: {op!r}")
    if live["type"] == "password" or live["pw_form"]:
        raise RefusedError("a password field or a form with one: the owner signs in", stop="needs_account")
    if op == "fill" and (
        live["auto"] == "one-time-code" or rules.code.search(norm(f"{_label(e)} {e['name']}"))
    ):
        raise RefusedError("a verification code: JobApplier reads it from the mail", stop="needs_code")
    if op in ("check", "uncheck") and live["tag"] != "input" and (why := rules.name_refusal(live)):
        raise RefusedError(why)  # a role toggle is clicked by Playwright: the same name check as a click
    types, roles = _FITS[op]
    if live["type"] not in types and live["role"] not in roles:
        raise RefusedError(f"{op} does not fit a {live['type'] or live['role'] or live['tag']} element")
    value = action.get("value")
    if op in ("fill", "select") and not isinstance(value, str):
        raise RefusedError(f"{op} needs a value")
    if e["via_label"] and op in ("check", "uncheck"):
        target = root.locator(f'[data-jobapplier-label="{ref}"]').first  # Playwright checks its input
    else:
        target = root.locator(selector)
    if op == "fill":
        target.fill(value, timeout=timeout_ms)
    elif op == "select":
        target.select_option(label=value, timeout=timeout_ms)
    elif op == "check":
        target.check(timeout=timeout_ms)
    elif op == "uncheck":
        target.uncheck(timeout=timeout_ms)
    else:
        target.set_input_files(str(pdf), timeout=timeout_ms)


def _code_fields(snap: Snapshot, rules: ClickRules) -> list[dict]:
    """The code inputs in page order: the boxes of a code typed one digit per box, else the one field."""
    fields = [e for e in snap.refs.values() if e["code"] and not e["disabled"]]
    boxes = [e for e in fields if _is_box(e) or _DIGIT.search(norm(_label(e)))]
    return boxes if len(boxes) > 1 else fields[:1]


def code_masks(page, snap: Snapshot, codes: set[str]) -> list:
    """Locators to black out in a screenshot: the code fields, and any field that shows a used code."""
    shown = [
        e
        for e in snap.refs.values()
        if e.get("code") or any(c in re.sub(r"[\s-]", "", e["value"]) for c in codes)
    ]
    return [(lambda root, sel: root.locator(sel))(*_where(page, e)) for e in shown]


def _check_code_page(snap: Snapshot, rules: ClickRules) -> None:
    """Refuses unless the page asks for a code and has few other fields: never an application form."""
    if not snap.code_page:
        raise RefusedError("not a verification code page")
    codes = {e["ref"] for e in _code_fields(snap, rules)}
    others = [e for e in snap.refs.values() if _is_field(e) and e["ref"] not in codes and e["type"] != "file"]
    if len(others) > rules.code_page_max_fields:
        raise RefusedError(f"{len(others)} other fields: not a verification code page")


def fill_code(page, snap: Snapshot, code: str, rules: ClickRules, timeout_ms: float) -> None:
    """Types a code read from the owner's mail: one digit per box, or the whole code in its one field.
    The model never gives the code; this is the only fill outside act."""
    _check_code_page(snap, rules)
    fields = _code_fields(snap, rules)
    parts = list(code) if len(fields) > 1 else [code]
    if not fields or len(parts) != len(fields):
        raise RefusedError(f"{len(fields)} code fields for a {len(code)}-character code")
    for e, part in zip(fields, parts, strict=True):
        root, selector = _where(page, e)
        live = root.locator(selector).evaluate(_LIVE)
        if live["type"] not in _CODE_TYPES or live["pw_form"]:
            raise RefusedError("a code field that is not a plain text box", stop="needs_account")
        root.locator(selector).fill(part, timeout=timeout_ms)


def _click_verify(page, snap: Snapshot, ref: int, rules: ClickRules, timeout_ms: float) -> None:
    """The one other click: the verify button of a code page, whose name often has a submit word. Only on a
    page that asks for a code and has few other fields, so it can never be an application's submit."""
    _check_code_page(snap, rules)
    e = snap.refs.get(ref)
    if e is None or _is_field(e):
        raise RefusedError("not a button on this page")
    root, selector = _where(page, e)
    live = root.locator(selector).evaluate(_LIVE)
    names = [norm(live[k]) for k in _SOURCES]
    visible = ClickRules._visible(live)
    start = rules.verify_start.match(visible)
    # past the verify word, a submit word ("Verify and submit") may send more than the code
    rests = [re.sub(r"\bsubmit code\b", "", rules.verify_start.sub("", n, count=1)) for n in names]
    if (
        any(_foreign(live[k]) for k in _SOURCES)
        or not start
        or len(visible[start.end() :].split()) > 2
        or any(rules.submit_stem.search(r) for r in rests)
        or any(rules.apply.search(n) or rules.sign_in.search(n) or "application" in n.split() for n in names)
        or live["disabled"]
    ):
        raise RefusedError(f"not a verify button: {visible!r}")
    if live["tag"] == "input":
        value = live["value"].replace("\\", "\\\\").replace('"', '\\"')
        target = root.locator(f'{selector}[value="{value}" i]')
    else:  # bound to the checked text, which Playwright checks again on every retry
        text = r"\W+".join(map(re.escape, live["text"].split()))
        target = root.locator(selector).filter(has_text=re.compile(rf"^\W*{text}\W*$", re.IGNORECASE))
    target.click(timeout=timeout_ms)


def verify(page, snap: Snapshot, rules: ClickRules, timeout_ms: float) -> str:
    """Clicks the code page's verify button; returns its name."""
    for ref, e in snap.refs.items():
        if not _is_field(e) and rules.verify_start.match(norm(_name(e))):
            _click_verify(page, snap, ref, rules, timeout_ms)
            return _name(e)
    raise RefusedError("no verify button on the code page")
