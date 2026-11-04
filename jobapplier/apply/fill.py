"""Pre-fills an application form from profile.yaml. It never submits: see the module test.

The logic works on plain field dicts (read_fields builds them from the page), so it is tested without a
browser. On the page it only calls fill, select_option, check (radios and consent boxes the plan chose)
and set_input_files (the resume).
"""

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from jobapplier.core.rules import norm, words

__all__ = [
    "FieldRules",
    "button_name",
    "fill_page",
    "group_radios",
    "is_login",
    "missing_required",
    "next_button",
    "plan",
    "read_buttons",
    "read_fields",
    "resume_input",
    "salary_range",
    "upload_file",
]

_OWNED = frozenset({"name.first", "name.last", "name.full", "email", "phone"})  # must equal the profile
_TEXT = frozenset({"text", "textarea", "email", "tel", "url", "number", "date"})
_TICKS = {"check_radio": "radio", "tick_consent": "checkbox"}  # plan action -> the only type it may tick
_ACTIONS = frozenset({"fill", "select_option", *_TICKS})
_HONEYPOT = re.compile(r"^hp[_-]|honeypot", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^(select|choose|please|none selected)\b|^$")
_RESUME = re.compile(r"\b(resume|cv)\b")
_WHERE = re.compile(r"\b(which|what|where|cities|city|locations?)\b")
_AMOUNT = r"\d[\d,]*(?:\.\d+)?\s?[kK]?"
_SALARY = re.compile(rf"\$\s?{_AMOUNT}\s*(?:-|\u2013|\u2014|to)\s*\$?\s?{_AMOUNT}")  # an en or em dash too

# What the page says about whether a person can see el; _visible decides from it. checkVisibility covers
# display, visibility and opacity on el and its ancestors; the rest catches off-screen and clipped
# honeypots.
_FACTS = """
  const facts = el => {
    const r = el.getBoundingClientRect();
    let clipped = false;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.overflow !== 'visible' && (a.clientHeight === 0 || a.clientWidth === 0)) {
        clipped = true;
        break;
      }
    }
    const d = document.documentElement;
    return {
      seen: el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}),
      rect: [r.left + scrollX, r.top + scrollY, r.width, r.height],
      doc: [d.scrollWidth, d.scrollHeight],
      aria_hidden: !!el.closest('[aria-hidden="true"]'),
      clipped,
    };
  };
"""
_NAME = "el => (el.getAttribute('aria-label') || el.innerText || el.value || '').replace(/\\s+/g, ' ').trim()"
# Marks every field with data-jobapplier so it is filled through a locator, never by position.
_READ = (
    "() => {"
    + _FACTS
    + """
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '';
  const own = el => {
    const parts = [...(el.labels || [])].map(text);
    for (const id of (el.getAttribute('aria-labelledby') || '').split(/\\s+/))
      if (id) parts.push(text(document.getElementById(id)));
    parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '');
    return parts.filter(Boolean).join(' ');
  };
  const before = el => {  // stops at the field's own form or fieldset: text past it is another question
    const box = el.closest('fieldset, form');
    for (let n = el; n && n !== box && n !== document.body; n = n.parentElement)
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
      ...facts(el), labels: type === 'radio' ? [...(el.labels || [])].map(facts) : [],
      tabindex: el.getAttribute('tabindex'),
      required: el.required || el.getAttribute('aria-required') === 'true',
    });
  });
  return out;
}"""
)


# Links are left out: following one leaves the form.
_BUTTONS = (
    "() => {"
    + _FACTS
    + f"""
  const name = {_NAME};
  const out = [];
  document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')
    .forEach((el, i) => {{
      if (el.tagName === 'A') return;
      el.setAttribute('data-jobapplier-button', String(i));
      out.push({{id: String(i), name: name(el), ...facts(el)}});
    }});
  return out;
}}"""
)


@dataclass(frozen=True)
class FieldRules:
    fields: tuple[tuple[re.Pattern, tuple[str, ...], bool], ...]  # (label pattern, keys to try, whole)
    skip: re.Pattern
    decline: re.Pattern
    login: re.Pattern
    consent: re.Pattern
    opt_in: re.Pattern
    tick_opt_ins: bool
    next_step: re.Pattern
    submit: re.Pattern

    @classmethod
    def from_config(cls, apply_cfg: dict) -> "FieldRules":
        fields = tuple(
            (
                words(r["patterns"]),
                tuple(r["key"]) if isinstance(r["key"], list) else (r["key"],),
                bool(r.get("whole")),
            )
            for r in apply_cfg["fields"]
        )
        return cls(
            fields,
            words(apply_cfg["skip_words"], prefix=True),
            words(apply_cfg["decline_words"]),
            words(apply_cfg["login_words"]),
            words(apply_cfg["consent_words"]),
            words(apply_cfg["opt_in_words"]),
            bool(apply_cfg["tick_opt_ins"]),
            words(apply_cfg["next_words"]),
            words(apply_cfg["submit_words"]),
        )

    def is_next(self, name: str) -> bool:
        """A next-step button: a next word, and no submit or sign-in word."""
        n = norm(name)
        return bool(self.next_step.search(n)) and not (self.submit.search(n) or self.login.search(n))


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


def _visible(facts: dict) -> bool:
    x, y, w, h = facts["rect"]
    page_w, page_h = facts["doc"]
    on_page = x + w > 0 and y + h > 0 and x < page_w and y < page_h
    return facts["seen"] and w > 1 and h > 1 and on_page and not (facts["aria_hidden"] or facts["clipped"])


def _shown(f: dict) -> bool:
    # A styled radio often hides its native input behind a visible label; no other field may borrow one.
    return _visible(f) or (f["type"] == "radio" and any(map(_visible, f["labels"])))


def read_fields(page) -> list[dict]:
    return group_radios([{**f, "visible": _shown(f)} for f in page.evaluate(_READ)])


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
    if keys == ("relocation",):
        where = f["type"] in _TEXT and _WHERE.search(norm(_label(f)))
        keys = ("relocation.cities",) if where else ("relocation.willing",)
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


def _hits(pattern: re.Pattern, whole: bool, text: str, label: str, f: dict) -> bool:
    if whole:
        return bool(pattern.fullmatch(label) or pattern.fullmatch(norm(f["name"])))
    return bool(pattern.search(text))


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
                actions.append(("tick_consent", f["id"], "", _display(f)))
            continue
        own = norm(_label(f))
        keys = next(
            (k for pattern, k, whole in rules.fields if _hits(pattern, whole, text, own, f)), ("skip",)
        )
        if keys == ("skip",) or rules.skip.search(own):
            continue
        value = _value(keys, f, values, description)
        owned = keys[0] in _OWNED and f["type"] in _TEXT
        if not value or not (_is_empty(f) or (owned and f["value"] != value)):
            continue
        if f["type"] == "radio":
            label = _choose([c["label"] for c in f["choices"]], value, rules)
            ids = [c["id"] for c in f["choices"] if c["label"] == label]
            if ids:
                actions.append(("check_radio", ids[0], label, _display(f)))
        elif f["type"] == "select":
            if option := _choose(f["options"], value, rules):
                actions.append(("select_option", f["id"], option, _display(f)))
        elif f["type"] in _TEXT and value != "decline":
            actions.append(("fill", f["id"], value, _display(f)))
    return actions


def fill_page(page, actions: list[tuple], timeout_ms: float) -> int:
    """Does each planned action through the field's locator; returns how many worked. A failure leaves the
    field to the owner. Anything but a planned action is refused."""
    done = 0
    for method, fid, arg, _ in actions:
        if method not in _ACTIONS:
            raise ValueError(f"not a planned fill action: {method}")
        el = page.locator(f'[data-jobapplier="{fid}"]')
        try:
            if method in _TICKS:
                # The type is read again now: the page may have redrawn since the plan was made.
                if el.evaluate("el => el.type") != _TICKS[method]:
                    continue
                el.check(timeout=timeout_ms)
            elif method == "select_option":
                el.select_option(label=arg, timeout=timeout_ms)
            else:
                el.fill(arg, timeout=timeout_ms)
        except PlaywrightError:
            continue
        done += 1
    return done


def upload_file(page, fid: str, path: str, timeout_ms: float) -> str | None:
    """Puts the file in the file input; None when it worked, else the error's type."""
    try:
        page.locator(f'[data-jobapplier="{fid}"]').set_input_files(path, timeout=timeout_ms)
    except PlaywrightError as exc:
        return type(exc).__name__
    return None


def read_buttons(page) -> list[dict]:
    return [{**b, "visible": _visible(b)} for b in page.evaluate(_BUTTONS)]


def button_name(button) -> str:
    """The name of the button a locator points at, read now."""
    return button.evaluate(_NAME)


def next_button(buttons: list[dict], rules: FieldRules) -> tuple[str | None, str]:
    """(id of the visible next-step button, "") or (None, why the owner takes over here)."""
    shown = [b for b in buttons if b["visible"] and norm(b["name"])]
    if nxt := [b for b in shown if rules.is_next(b["name"])]:
        return nxt[0]["id"], ""
    if any(rules.submit.search(norm(b["name"])) for b in shown):
        return None, "This is the last step: check every page, then submit yourself."
    return None, "No Next button found: continue yourself."


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
