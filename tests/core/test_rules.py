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
        ("Junior CNC Programmer", False),
        ("(CAN) Front End Checkout Team Associate", False),
        ("Cashier / Front End Checkout Team Associate", False),
        ("Welding Robot Programmer", False),
        ("PLC Programmer", False),
        ("Machinist Programmer", False),
        ("Junior Java Software Developer", True),
        ("Application Developer, Junior", True),
        ("Job Developer", False),
        ("CAD Programmer I", False),
        ("Full Stack Developer Course Instructor", False),
        ("Programmer Analyst C++", True),
        ("Online Game Programmer", True),
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


def test_punctuation_only_word_matches_nothing(config):
    r = Rules.from_config({**config, "rules": {**config["rules"], "include": ["developer", "-"]}})
    assert not keep(job("Pastry Chef"), r) and keep(job("Software Developer"), r)


def test_blocked_company_type_is_dropped(rules):
    for company_type in ("outstaff", "agency"):
        assert not keep(
            Posting("freehire", "1", "Software Developer", "Acme", "Toronto", "u", company_type=company_type),
            rules,
        )
    for company_type in ("inhouse", "product", ""):
        assert keep(
            Posting("freehire", "1", "Software Developer", "Acme", "Toronto", "u", company_type=company_type),
            rules,
        )


@pytest.mark.parametrize(
    "company",
    [
        "Jobgether",
        "Turing",
        "Robert Half",
        "Randstad Canada Inc.",
        "S.I. Systems",
        "TEKsystems",
        "Cornerbridge Talent Partners",
        "TLNT Bridge Recruiting",
        "Apex Systems",
        "Confidential",
    ],
)
def test_blocked_company_is_dropped(company, rules):
    assert not keep(Posting("linkedin", "1", "Software Developer", company, "Toronto", "u"), rules)


def test_blocked_company_matches_whole_words_only(rules):
    for company in ("Acme", "Turingly Labs", "Hayseed Software", "Predice", "Kaseya Careers"):
        assert keep(Posting("linkedin", "1", "Software Developer", company, "Toronto", "u"), rules)


def test_empty_blocklists_block_nothing(config):
    r = Rules.from_config(
        {**config, "rules": {**config["rules"], "blocked_companies": [], "blocked_company_types": []}}
    )
    assert keep(
        Posting("freehire", "1", "Software Developer", "Jobgether", "Toronto", "u", company_type="agency"), r
    )


def test_norm_drops_zero_width_characters_and_soft_hyphens():
    from jobapplier.core.rules import norm

    assert (
        norm("Sub\u200bmit") == "submit" and norm("Sub\u00admit") == "submit" and norm("Ne\u2060xt") == "next"
    )
