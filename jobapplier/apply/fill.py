"""Pre-fills an application form from profile.yaml. It never submits: see the module test.

The logic works on plain field dicts (read_fields builds them from the page), so it is tested without a
browser. On the page it only calls fill, select_option, check (radios and consent boxes) and
set_input_files.
"""

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from jobapplier.core.rules import norm

__all__ = [
    "FieldRules",
    "fill_page",
    "group_radios",
    "is_login",
    "missing_required",
    "plan",
    "read_fields",
    "resume_input",
    "salary_range",
]

_OWNED = frozenset({"name.first", "name.last", "name.full", "email", "phone"})  # must equal the profile
_TEXT = frozenset({"text", "textarea", "email", "tel", "url", "number", "search", "date"})
_HONEYPOT = re.compile(r"^hp[_-]|honeypot", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^(select|choose|please|none selected)\b|^$")
_RESUME = re.compile(r"\b(resume|cv)\b")
_AMOUNT = r"\d[\d,]*(?:\.\d+)?\s?[kK]?"
_SALARY = re.compile(rf"\$\s?{_AMOUNT}\s*(?:-|\u2013|\u2014|to)\s*\$?\s?{_AMOUNT}")  # an en or em dash too

# Marks every field with data-jobapplier so it is filled through a locator, never by position.
_READ = """() => {
  const shown = el => {
    if (!el) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0
      && r.width > 1 && r.height > 1;
  };
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '';
  const own = el => {
    const parts = [...(el.labels || [])].map(text);
    for (const id of (el.getAttribute('aria-labelledby') || '').split(/\\s+/))
      if (id) parts.push(text(document.getElementById(id)));
    parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '');
    return parts.filter(Boolean).join(' ');
  };
  const before = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement)
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
        const t = text(s);
        if (t) return t.slice(-200);
      }
    return '';
  };
  const out = [];
  document.querySelectorAll('input, textarea, select').forEach((el, i) => {
    const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : el.tagName.toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    el.setAttribute('data-jobapplier', String(i));
    const legend = el.closest('fieldset') && el.closest('fieldset').querySelector('legend');
    const picked = type === 'select' && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
    out.push({
      id: String(i), type, name: el.name || el.id || '', label: own(el), before: before(el),
      group: type === 'radio' ? (text(legend) || before(el.closest('label') || el)) : '',
      value: type === 'select' ? (picked && el.value !== '' ? text(picked) : '') : (el.value || ''),
      checked: !!el.checked, options: type === 'select' ? [...el.options].map(text) : [],
      visible: shown(el) || [...(el.labels || [])].some(shown),
      tabindex: el.getAttribute('tabindex'),
      required: el.required || el.getAttribute('aria-required') === 'true',
    });
  });
  return out;
}"""


def _words(phrases: list[str]) -> re.Pattern:
    normed = [n for n in map(norm, phrases) if n]
    return re.compile(r"\b(" + "|".join(map(re.escape, normed)) + r")\b") if normed else re.compile(r"(?!)")


@dataclass(frozen=True)
class FieldRules:
    fields: tuple[tuple[re.Pattern, tuple[str, ...]], ...]  # (label pattern, profile keys to try)
    decline: re.Pattern
    login: re.Pattern
    consent: re.Pattern
    opt_in: re.Pattern
    tick_opt_ins: bool

    @classmethod
    def from_config(cls, apply_cfg: dict) -> "FieldRules":
        fields = tuple(
            (_words(r["patterns"]), tuple(r["key"]) if isinstance(r["key"], list) else (r["key"],))
            for r in apply_cfg["fields"]
        )
        return cls(
            fields,
            _words(apply_cfg["decline_words"]),
            _words(apply_cfg["login_words"]),
            _words(apply_cfg["consent_words"]),
            _words(apply_cfg["opt_in_words"]),
            bool(apply_cfg["tick_opt_ins"]),
        )


def group_radios(raw: list[dict]) -> list[dict]:
    """One field per radio group: label is the question, choices its options, value the checked one."""
    out, groups = [], {}
    for f in raw:
        if f["type"] != "radio":
            out.append(f)
            continue
        g = groups.get(f["name"] or f["id"])
        if g is None:
            g = groups[f["name"] or f["id"]] = {**f, "label": f["group"], "value": "", "choices": []}
            out.append(g)
        choice = f["label"] or f["value"]
        g["choices"].append({"id": f["id"], "label": choice})
        g["value"] = choice if f["checked"] else g["value"]
        g["visible"], g["required"] = g["visible"] or f["visible"], g["required"] or f["required"]
    return out


def read_fields(page) -> list[dict]:
    return group_radios(page.evaluate(_READ))


def _label(f: dict) -> str:
    return (f["label"] or f["before"] or f["name"]).strip()


def _display(f: dict) -> str:
    return _label(f)[:120]


def _is_honeypot(f: dict) -> bool:
    return bool(_HONEYPOT.search(f["name"])) or (f["tabindex"] == "-1" and not f["label"])


def _is_empty(f: dict) -> bool:
    if f["type"] == "checkbox":
        return not f["checked"]
    if f["type"] == "select":
        return bool(_PLACEHOLDER.search(norm(f["value"])))
    return not f["value"].strip()


def salary_range(description: str) -> str | None:
    m = _SALARY.search(description)
    return re.sub(r"\s+", " ", m[0]).strip() if m else None


def _value(keys: tuple[str, ...], f: dict, values: dict, description: str) -> str:
    if keys == ("salary",):
        posted = salary_range(description)
        if posted:
            return posted
        keys = ("salary_expectation", "salary_default") if f["type"] == "textarea" else ("salary_default",)
    return next((values[k] for k in keys if values.get(k)), "")


def _choose(options: list[str], value: str, rules: FieldRules) -> str | None:
    """The option for value: the decline option, a Yes/No answer, or the option naming value."""
    normed = [(o, norm(o)) for o in options if not _PLACEHOLDER.search(norm(o))]
    want = norm(value)
    if value == "decline":
        return next((o for o, n in normed if rules.decline.search(n)), None)
    if want in ("yes", "no"):
        return next((o for o, n in normed if re.match(rf"{want}\b", n)), None)
    return next((o for o, n in normed if n == want), None) or next(
        (o for o, n in normed if want and re.search(rf"\b{re.escape(want)}\b", n)), None
    )


def plan(fields: list[dict], values: dict, rules: FieldRules, description: str) -> list[tuple]:
    """(method, field id, argument, label) for each field to fill. Only empty fields are filled, except
    name, email and phone, which are set to the profile's. Consent checkboxes are ticked."""
    actions = []
    for f in fields:
        if f["type"] in ("file", "password") or not f["visible"] or _is_honeypot(f):
            continue
        text = norm(f"{_label(f)} {f['name']}")
        if f["type"] == "checkbox":
            # an opt-in wins over consent words: "I agree to receive marketing emails" stays unticked
            opt_in = bool(rules.opt_in.search(text))
            if not f["checked"] and (rules.tick_opt_ins if opt_in else rules.consent.search(text)):
                actions.append(("check", f["id"], "", _display(f)))
            continue
        keys = next((k for pattern, k in rules.fields if pattern.search(text)), ("skip",))
        if keys == ("skip",):
            continue
        value = _value(keys, f, values, description)
        owned = keys[0] in _OWNED and f["type"] in _TEXT
        if not value or not (_is_empty(f) or (owned and f["value"] != value)):
            continue
        if f["type"] == "radio":
            label = _choose([c["label"] for c in f["choices"]], value, rules)
            ids = [c["id"] for c in f["choices"] if c["label"] == label]
            if ids:
                actions.append(("check", ids[0], label, _display(f)))
        elif f["type"] == "select":
            if option := _choose(f["options"], value, rules):
                actions.append(("select_option", f["id"], option, _display(f)))
        elif f["type"] in _TEXT and value != "decline":
            actions.append(("fill", f["id"], value, _display(f)))
    return actions


def fill_page(page, actions: list[tuple], timeout_ms: float) -> int:
    """Does each action through the field's locator; returns how many worked. A failure leaves the field
    to the owner."""
    done = 0
    for method, fid, arg, _ in actions:
        el = page.locator(f'[data-jobapplier="{fid}"]')
        try:
            if method == "check":
                el.check(timeout=timeout_ms)
            elif method == "select_option":
                el.select_option(label=arg, timeout=timeout_ms)
            elif method == "set_input_files":
                el.set_input_files(arg, timeout=timeout_ms)
            else:
                el.fill(arg, timeout=timeout_ms)
        except PlaywrightError:
            continue
        done += 1
    return done


def resume_input(fields: list[dict]) -> str | None:
    """The file input to upload the resume to: one labelled or named resume or CV, else the only one."""
    files = [f for f in fields if f["type"] == "file"]
    named = [f for f in files if _RESUME.search(norm(f"{_display(f)} {f['name']}"))]
    pick = named[:1] or (files if len(files) == 1 else [])
    return pick[0]["id"] if pick else None


def missing_required(fields: list[dict]) -> list[str]:
    return [
        _display(f)
        for f in fields
        if f["required"] and f["visible"] and f["type"] != "file" and not _is_honeypot(f) and _is_empty(f)
    ]


def is_login(url: str, title: str, fields: list[dict], rules: FieldRules) -> bool:
    """A sign-in or sign-up page: a visible password field, or a login word in the URL or title."""
    shown = any(f["type"] == "password" and f["visible"] for f in fields)
    return shown or bool(rules.login.search(norm(f"{url} {title}")))
