import re
import unicodedata
from dataclasses import dataclass

from jobapplier.models import Posting

__all__ = ["Rules", "is_intern", "is_notable", "keep", "norm"]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _words(words: list[str]) -> re.Pattern:
    if not words:  # an empty alternation would match every title
        return re.compile(r"(?!)")
    return re.compile(r"\b(" + "|".join(re.escape(norm(w)) for w in words) + r")\b")


@dataclass(frozen=True)
class Rules:
    include: re.Pattern
    exclude: re.Pattern
    intern: re.Pattern
    junior: re.Pattern
    cities: tuple[re.Pattern, ...]
    notable: re.Pattern
    aggregators: tuple[str, ...]
    source_order: tuple[str, ...]

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        r = cfg["rules"]
        return cls(
            include=_words(r["include"]),
            exclude=_words(r["exclude"]),
            intern=_words(r["intern"]),
            junior=_words(r["rank1_title"]),
            cities=tuple(_words(tier) for tier in r["cities"]),
            notable=_words(r["notable_companies"]),
            aggregators=tuple(r["aggregator_hosts"]),
            source_order=tuple(cfg["sources"]),
        )


def _is_target_title(title: str, rules: Rules) -> bool:
    t = norm(title)
    return bool(rules.include.search(t)) and not rules.exclude.search(t)


def is_intern(title: str, rules: Rules) -> bool:
    return bool(rules.intern.search(norm(title)))


def is_notable(company: str, rules: Rules) -> bool:
    return bool(rules.notable.search(norm(company)))


def keep(p: Posting, rules: Rules) -> bool:
    return _is_target_title(p.title, rules) and (
        not is_intern(p.title, rules) or is_notable(p.company, rules)
    )
