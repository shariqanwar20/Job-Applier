import re

from jobapplier.filters import norm

_GTA = ["toronto", "mississauga", "markham", "brampton", "vaughan", "oakville", "richmond hill", "north york", "etobicoke", "scarborough"]
_JUNIOR_TITLE = re.compile(r"\b(new grad|graduate|entry level|junior|jr|associate)\b|\b(engineer|developer|sde) (i|1)$")
# "experience" within 8 words after the count; lookahead so a stray "20 years ago" can't swallow a later match.
_YEARS = re.compile(r"\b(\d+)(?: (?:to )?\d+)? (?:years?|yrs?|ans?)\b(?=(?: \w+){0,7} experience)")


def _has(text: str, phrases: list[str]) -> bool:
    padded = f" {text} "
    return any(f" {p} " in padded for p in phrases)


def is_remote_us(location: str) -> bool:
    n = norm(location)
    words = n.split()
    return "remote" in words and _has(n, ["united states", "usa", "us"]) and "canada" not in words


def city_rank(location: str) -> int:
    n = norm(location)
    if _has(n, _GTA):
        return 0
    if _has(n, ["vancouver", "burnaby"]):
        return 1
    if _has(n, ["montreal"]):
        return 2
    if "remote" in n.split():
        return 4 if is_remote_us(location) else 3
    return 5


def min_years(description: str) -> int | None:
    years = [int(y) for y in _YEARS.findall(norm(description))]
    return min(years, default=None)


def rank(title: str, description: str, seniority: str, notable_intern: bool) -> int:
    if notable_intern or _JUNIOR_TITLE.search(norm(title)):
        return 1
    years = min_years(description)
    if years is not None:
        return 2 if years < 3 else 3
    return 1 if seniority in ("junior", "intern") else 3
