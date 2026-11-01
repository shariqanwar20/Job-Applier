import pytest
from jobapplier.core.filters import Rules, norm, is_target_title, is_intern, is_notable, keep, cluster_key
from jobapplier.models import Posting

def test_norm():
    assert norm("Développeur·euse  Full-Stack!") == "developpeur euse full stack"

@pytest.mark.parametrize("title,ok", [
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
])
def test_is_target_title(title, ok, rules):
    assert is_target_title(title, rules) is ok

def test_intern_kept_only_at_notable_company(rules):
    intern = Posting("freehire", "1", "Software Engineer Intern, Pipeline Infrastructure", "Disney Company", "Vancouver, Canada", "u")
    other = Posting("freehire", "2", "Full-stack AI Developer Intern", "Revvity", "CAD Remote – ON", "u")
    coop = Posting("freehire", "3", "Software Developer Co-op", "Google", "Toronto", "u")
    assert is_intern(intern.title, rules) and is_intern(coop.title, rules)
    assert keep(intern, rules) and keep(coop, rules)
    assert not keep(other, rules)
    assert is_notable("TD Bank", rules) and not is_notable("Tdx Labs", rules)

def test_cluster_key_ignores_suffixes_and_parentheticals():
    assert cluster_key("Affirm, Inc.", "Software Engineer II, Full Stack (Card Pre-Swipe Experience)") == \
           cluster_key("Affirm", "Software Engineer II, Full Stack")


def test_shipped_config_gives_source_order(rules):
    assert rules.source_order == ("freehire", "indeed", "linkedin")


def test_empty_lists_match_nothing(config):
    r = Rules.from_config({**config, "rules": {**config["rules"], "exclude": [], "intern": []}})
    assert is_target_title("Senior Software Engineer", r) and not is_intern("Software Engineer Intern", r)
