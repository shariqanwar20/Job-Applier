import pytest
from jobapplier.filters import norm, is_target_title, is_intern, is_notable, keep, cluster_key
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
def test_is_target_title(title, ok):
    assert is_target_title(title) is ok

def test_intern_kept_only_at_notable_company():
    notable = ["disney", "google"]
    intern = Posting("freehire", "1", "Software Engineer Intern, Pipeline Infrastructure", "Disney Company", "Vancouver, Canada", "u")
    other = Posting("freehire", "2", "Full-stack AI Developer Intern", "Revvity", "CAD Remote – ON", "u")
    coop = Posting("freehire", "3", "Software Developer Co-op", "Google", "Toronto", "u")
    assert is_intern(intern.title) and is_intern(coop.title)
    assert keep(intern, notable) and keep(coop, notable)
    assert not keep(other, notable)
    assert not is_notable("Tdx Labs", ["td"])

def test_cluster_key_ignores_suffixes_and_parentheticals():
    assert cluster_key("Affirm, Inc.", "Software Engineer II, Full Stack (Card Pre-Swipe Experience)") == \
           cluster_key("Affirm", "Software Engineer II, Full Stack")
