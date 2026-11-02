import pytest

from jobapplier.core.rank import city_rank
from jobapplier.core.rules import Rules, is_intern, is_notable, keep, norm
from jobapplier.models import Posting


def job(title):
    return Posting("freehire", "1", title, "Acme", "Toronto", "u")


def test_norm():
    assert norm("Développeur·euse  Full-Stack!") == "developpeur euse full stack"


@pytest.mark.parametrize(
    "title,ok",
    [
        ("Software Engineer II, Full Stack (Card Pre-Swipe Experience)", True),
        ("Associate Software Engineer", True),
        ("Software Development Engineer in Test, Evisor", True),
        ("Développeur·euse généraliste intermédiaire (Développement)", True),
        ("Fullstack Developer - Node.js", True),
        ("Sr. Software Engineer", False),
        ("Engineering Manager, Conversational AI", False),
        ("Développeur·euse généraliste principal·e", False),
        ("Staff Fullstack Developer- Simulation", False),
        ("Lead Full Stack Developer", False),
        ("Salesforce Administrator", False),
    ],
)
def test_keep_by_title(title, ok, rules):
    assert keep(job(title), rules) is ok


def test_intern_kept_only_at_notable_company(rules):
    intern = Posting(
        "freehire",
        "1",
        "Software Engineer Intern, Pipeline Infrastructure",
        "Disney Company",
        "Vancouver, Canada",
        "u",
    )
    other = Posting("freehire", "2", "Full-stack AI Developer Intern", "Revvity", "CAD Remote \u2013 ON", "u")
    coop = Posting("freehire", "3", "Software Developer Co-op", "Google", "Toronto", "u")
    assert is_intern(intern.title, rules) and is_intern(coop.title, rules)
    assert keep(intern, rules) and keep(coop, rules)
    assert not keep(other, rules)
    assert is_notable("TD Bank", rules) and not is_notable("Tdx Labs", rules)


def test_shipped_config_gives_source_order(rules):
    assert rules.source_order == ("freehire", "indeed", "linkedin")


def test_empty_lists_match_nothing(config):
    r = Rules.from_config({**config, "rules": {**config["rules"], "exclude": [], "intern": []}})
    assert keep(job("Senior Software Engineer"), r) and not is_intern("Software Engineer Intern", r)


def test_config_words_are_normalized(config):
    r = Rules.from_config(
        {**config, "rules": {**config["rules"], "cities": [["Montréal"]], "include": ["Developer"]}}
    )
    assert keep(job("Software Developer"), r) and city_rank("Montreal, QC", r) == 0
