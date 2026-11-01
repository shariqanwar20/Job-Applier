import re
import unicodedata

from jobapplier.models import Posting

_INCLUDE = re.compile(r"\b(developer|engineer|programmer|software|full ?stack|back ?end|front ?end|sde|mobile|developpeur|developpeuse|ingenieure? logiciel)\b")
_EXCLUDE = re.compile(r"\b(senior|sr|staff|principal|lead|manager|director|architect|head of)\b")
_INTERN = re.compile(r"\b(intern|internship|co ?op|stagiaire|stage)\b")
_COMPANY_NOISE = set("inc ltd llc corp corporation company co group plc limited sa the".split())


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_target_title(title: str) -> bool:
    t = norm(title)
    return bool(_INCLUDE.search(t)) and not _EXCLUDE.search(t)


def is_intern(title: str) -> bool:
    return bool(_INTERN.search(norm(title)))


def is_notable(company: str, notable: list[str]) -> bool:
    c = f" {norm(company)} "
    return any(f" {norm(n)} " in c for n in notable)


def keep(p: Posting, notable: list[str]) -> bool:
    return is_target_title(p.title) and (not is_intern(p.title) or is_notable(p.company, notable))


def cluster_key(company: str, title: str) -> str:
    words = [w for w in norm(company).split() if w not in _COMPANY_NOISE]
    return " ".join(words) + "|" + norm(re.sub(r"\([^)]*\)", " ", title))
