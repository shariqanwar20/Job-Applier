import pytest
from jobapplier.core.rank import city_rank, is_remote_us, min_years, rank

@pytest.mark.parametrize("loc,r", [
    ("Toronto, ON", 0), ("Mississauga Ontario Canada", 0), ("Canada, BC, Vancouver", 1),
    ("Montréal, Canada", 2), ("CAD Remote – ON", 3), ("Remote Canada", 3),
    ("Remote, United States", 4), ("Ottawa, ON, ca", 5), ("", 5),
])
def test_city_rank(loc, r, rules):
    assert city_rank(loc, rules) == r

def test_is_remote_us():
    assert is_remote_us("Remote - USA") and not is_remote_us("Remote Canada") and not is_remote_us("Toronto")

@pytest.mark.parametrize("text,y", [
    ("You have 3+ years of experience with Python.", 3),
    ("2-4 years of professional experience", 2),
    ("Founded 20 years ago. 1 year of experience required.", 1),
    ("Au moins 3 ans d'expérience en développement", 3),
    ("Great culture.", None),
])
def test_min_years(text, y):
    assert min_years(text) == y

def test_rank(rules):
    assert rank("Associate Software Engineer", "", "", False, rules) == 1
    assert rank("Software Engineer I", "5 years of experience", "", False, rules) == 1
    assert rank("Software Engineer II", "2+ years of experience", "middle", False, rules) == 2
    assert rank("Software Engineer II", "5+ years of experience", "junior", False, rules) == 3
    assert rank("Software Developer", "", "junior", False, rules) == 1
    assert rank("Software Developer", "", "", False, rules) == 3
    assert rank("Software Engineer Intern", "", "intern", True, rules) == 1
