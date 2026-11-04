"""Pure checks on one LLM-written resume line (spec §8.3, §8.7). Each returns a list of reasons."""

import re

__all__ = ["escape", "grounding_problems", "style_problems"]

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*%?")
# A hyphen or slash joins two words ("on-call", "CI/CD"), so each side is checked on its own.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*")
_TECH_MARK = re.compile(r"[A-Z0-9+#.]")
_DASHES = ("\u2014", "\u2013", "--")  # em dash, en dash, and LaTeX's en dash
_LATEX = {
    **{ch: "\\" + ch for ch in "&%$#_"},
    "~": r"\textasciitilde{}",
    # In the default OT1 font encoding these print as other glyphs (¡ ¿ —).
    "<": r"\textless{}",
    ">": r"\textgreater{}",
    "|": r"\textbar{}",
}


def _numbers(text: str) -> set[str]:
    found = set(_NUMBER.findall(text))
    return found | {n.rstrip("%") for n in found}  # "40%" in the source grounds a plain "40"


def _tokens(text: str) -> list[str]:
    return [t.rstrip(".") for t in _TOKEN.findall(text)]


def _stem(word: str) -> str:
    """A light stem, so "parallelized" matches "parallelizing" and "leads" matches "lead"."""
    w = word.lower()
    if w.endswith("ing") and len(w) >= 6:
        w = w[:-3]
    elif w.endswith("ed") and not w.endswith("eed") and len(w) >= 4:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) >= 4:
        w = w[:-1]
    # A final e goes too ("serve" -> "serv" = "served"). No -d rule: it would split "build" from "builds".
    return w[:-1] if w.endswith("e") and len(w) >= 3 else w


def _stems(words) -> set[str]:
    return {_stem(w) for w in words}


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+", text)


def grounding_problems(text: str, source: str, cfg: dict, job_text: str = "") -> list[str]:
    """Reasons text says more than source, its master line (plain text).

    job_text is the job description: its words may appear only where the master line has them.
    """
    if any(ch in text for ch in "\\{}"):
        return ["has a raw \\, { or }"]
    out = [
        f"the number {n} is not in the master line"
        for n in _NUMBER.findall(text)
        if n not in _numbers(source)
    ]
    src = _stems(_words(source)) | _stems(_tokens(source))
    # common words too: a second sentence may start with "It" or "They"
    known = src | _stems(cfg["allow_words"]) | _stems(cfg["common_words"])
    flagged = set()
    for t in dict.fromkeys(_tokens(text)):
        if _TECH_MARK.search(t) and _stem(t) not in known:
            out.append(f"{t} is not in the master line")
            flagged.add(_stem(t))
    # Number words match exactly (the list has the plurals): the stem of "one" is "on".
    numbers, job = {w.lower() for w in cfg["number_words"]}, _stems(_words(job_text))
    common = known | _stems(cfg["common_words"])
    exact = {w.lower() for w in _words(source)}
    for w in dict.fromkeys(_words(text)):
        s = _stem(w)
        if w.lower() in numbers:  # before the stem test below, which would let "one" through on "on"
            if w.lower() not in exact:
                out.append(f"the number {w} is not in the master line")
            continue
        if s in flagged or s in src:
            continue
        if s in job and s not in common:
            out.append(f"'{w}' is from the job description, not the master line")
    modest = src & _stems(cfg["modest_verbs"])
    out += [
        f"'{w}' overstates the master line"
        for w in dict.fromkeys(_words(text))
        if _stem(w) in _stems(cfg["inflating_verbs"]) and (_stem(w) not in src or modest)
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
