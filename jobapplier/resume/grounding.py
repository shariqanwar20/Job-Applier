"""Pure checks on one LLM-written resume line (spec §8.3, §8.7). Each returns a list of reasons."""

import re

__all__ = ["escape", "grounding_problems", "style_problems"]

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")
# A hyphen or slash joins two words ("on-call", "CI/CD"), so each side is checked on its own.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*")
_TECH_MARK = re.compile(r"[A-Z0-9+#.]")
_DASHES = ("\u2014", "\u2013", "--")  # em dash, en dash, and LaTeX's en dash
_LATEX = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}"}


def _numbers(text: str) -> set[str]:
    found = set(_NUMBER.findall(text))
    return found | {n.rstrip("%") for n in found}  # "40%" in the source grounds a plain "40"


def _tokens(text: str) -> list[str]:
    return [t.rstrip(".") for t in _TOKEN.findall(text)]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def grounding_problems(text: str, source: str, cfg: dict) -> list[str]:
    """Reasons text says more than source, its master line (plain text)."""
    if any(ch in text for ch in "\\{}"):
        return ["has a raw \\, { or }"]
    out = [
        f"the number {n} is not in the master line"
        for n in _NUMBER.findall(text)
        if n not in _numbers(source)
    ]
    known = {t.lower() for t in _tokens(source)} | {w.lower() for w in cfg["allow_words"]}
    out += [
        f"{t} is not in the master line"
        for t in dict.fromkeys(_tokens(text))
        if _TECH_MARK.search(t) and t.lower() not in known
    ]
    src_words = _words(source)
    modest = src_words & set(cfg["modest_verbs"])
    out += [
        f"'{v}' overstates the master line"
        for v in cfg["inflating_verbs"]
        if v in _words(text) and (v not in src_words or modest)
    ]
    return out


def style_problems(text: str, cfg: dict) -> list[str]:
    out = ["has a dash" for d in _DASHES if d in text][:1]
    if (n := len(text.split())) > cfg["max_words"]:
        out.append(f"has {n} words (at most {cfg['max_words']})")
    out += [
        f"uses '{p}'"
        for p in cfg["banned_phrases"]
        if re.search(rf"(?<![\w-]){re.escape(p)}(?![\w-])", text, re.I)
    ]
    return out


def escape(text: str) -> str:
    """Plain text as LaTeX. Raw backslashes and braces never get here: grounding_problems rejects them."""
    return "".join(_LATEX.get(ch, ch) for ch in text).replace("^", r"\textasciicircum{}")
