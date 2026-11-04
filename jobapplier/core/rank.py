import re

from jobapplier.core.rules import Rules, norm

__all__ = ["city_rank", "is_remote_us", "rank"]

_LEVEL_I = re.compile(r"\b(engineer|developer|sde) (i|1)$")
# "experience" within 8 words after the count;
# a lookahead, so a stray "20 years ago" can't swallow a later match.
_YEARS = re.compile(r"\b(\d+)(?: (?:to )?\d+)? (?:years?|yrs?|ans?)\b(?=(?: \w+){0,7} experience)")

_US = re.compile(r"\b(united states|usa|us)\b")


def is_remote_us(location: str) -> bool:
    n = norm(location)
    words = n.split()
    return "remote" in words and bool(_US.search(n)) and "canada" not in words


def city_rank(location: str, rules: Rules) -> int:
    n = norm(location)
    for tier, cities in enumerate(rules.cities):
        if cities.search(n):
            return tier
    after = len(rules.cities)  # remote, remote US, anything else rank after the last city tier
    if "remote" in n.split():
        return after + 1 if is_remote_us(location) else after
    return after + 2


def _min_years(description: str) -> int | None:
    years = [int(y) for y in _YEARS.findall(norm(description))]
    return min(years, default=None)


def rank(title: str, description: str, seniority: str, notable_intern: bool, rules: Rules) -> int:
    t = norm(title)
    if notable_intern or rules.junior.search(t) or _LEVEL_I.search(t):
        return 1
    years = _min_years(description)
    if years is not None:
        return 2 if years < 3 else 3
    return 1 if seniority in ("junior", "intern") else 3
