import re
import unicodedata
from dataclasses import dataclass

from jobapplier.models import Posting

__all__ = ["Rules", "is_intern", "is_notable", "keep", "norm", "words"]


def norm(text: str) -> str:
    # Format characters (zero-width space, soft hyphen) would otherwise split a word in two.
    text = unicodedata.normalize("NFKD", "".join(c for c in text if unicodedata.category(c) != "Cf"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def words(phrases: list[str], prefix: bool = False) -> re.Pattern:
    """Matches any of phrases as whole words in normalized text; with prefix, as word starts ("referr")."""
    normed = [
        n for n in map(norm, phrases) if n
    ]  # a punctuation-only word would be an empty, match-all branch
    if not normed:  # an empty alternation would match every title
        return re.compile(r"(?!)")
    return re.compile(r"\b(" + "|".join(map(re.escape, normed)) + (r")" if prefix else r")\b"))


@dataclass(frozen=True)
class Rules:
    include: re.Pattern
    exclude: re.Pattern
    intern: re.Pattern
    junior: re.Pattern
    cities: tuple[re.Pattern, ...]
    notable: re.Pattern
    aggregators: tuple[str, ...]
    blocked_types: frozenset[str]
    blocked_companies: re.Pattern
    company_noise: frozenset[str]
    company_aliases: dict[str, str]
    source_order: tuple[str, ...]

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        r = cfg["rules"]
        return cls(
            include=words(r["include"]),
            exclude=words(r["exclude"]),
            intern=words(r["intern"]),
            junior=words(r["rank1_title"]),
            cities=tuple(words(tier) for tier in r["cities"]),
            notable=words(r["notable_companies"]),
            aggregators=tuple(r["aggregator_hosts"]),
            blocked_types=frozenset(map(norm, r["blocked_company_types"])),
            blocked_companies=words(r["blocked_companies"]),
            company_noise=frozenset(w for word in r["company_noise"] for w in norm(word).split()),
            company_aliases={norm(k): norm(v) for k, v in r["company_aliases"].items()},
            source_order=tuple(cfg["sources"]),
        )


def _is_target_title(title: str, rules: Rules) -> bool:
    t = norm(title)
    return bool(rules.include.search(t)) and not rules.exclude.search(t)


def is_intern(title: str, rules: Rules) -> bool:
    return bool(rules.intern.search(norm(title)))


def is_notable(company: str, rules: Rules) -> bool:
    return bool(rules.notable.search(norm(company)))


def _is_blocked(p: Posting, rules: Rules) -> bool:
    return norm(p.company_type) in rules.blocked_types or bool(
        rules.blocked_companies.search(norm(p.company))
    )


def keep(p: Posting, rules: Rules) -> bool:
    return (
        _is_target_title(p.title, rules)
        and not _is_blocked(p, rules)
        and (not is_intern(p.title, rules) or is_notable(p.company, rules))
    )
