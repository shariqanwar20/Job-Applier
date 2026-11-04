import pytest

from jobapplier.resume.grounding import escape, grounding_problems, style_problems

SOURCE = "Built a REST API in Go that served 3 clinics and cut load time by 40%"


@pytest.fixture
def t(config):
    return config["tailor"]


@pytest.mark.parametrize(
    "text",
    [
        "Built a Go REST API that served 3 clinics",
        "Cut load time by 40% with a REST API in Go",
        "Cut load time by 40 percent",  # the bare number is in the source
        "Wrote a Go-based REST API for clinics",  # a hyphen part and an allow-listed first word
        "Developed the clinic API in GO",  # case-insensitive
    ],
)
def test_grounded_text_passes(text, t):
    assert grounding_problems(text, SOURCE, t) == []


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("Built a REST API that served 5 clinics", "5"),
        ("Cut load time by 45%", "45%"),
        ("Cut load time by 3%", "3%"),  # a number in the source is not the same percentage
        ("Built a REST API in Go and Kubernetes", "Kubernetes"),
        ("Built a REST API in Go at Google", "Google"),
        ("Built a gRPC API in Go", "gRPC"),
        ("Built a REST API in Node.js", "Node.js"),
        ("Built a REST API with CI/CD", "CI"),
        ("Built a REST API in C++", "C++"),
        ("Built an API with \\textbf{Go}", "\\"),
        ("Built an API {in Go}", "{"),
    ],
)
def test_ungrounded_text_is_reported(text, why, t):
    problems = grounding_problems(text, SOURCE, t)
    assert problems and why in " ".join(problems)


def test_inflating_verbs_need_the_source_to_use_them(t):
    assert grounding_problems("Led the REST API work in Go", SOURCE, t)
    assert grounding_problems("Led the move to PostgreSQL", "Helped and led the move to PostgreSQL", t)
    assert grounding_problems("Owned the move to PostgreSQL", "Helped move the service to PostgreSQL", t)
    assert grounding_problems("Led the move to PostgreSQL", "Led the move to PostgreSQL", t) == []


def test_allow_words_come_from_config(t):
    assert grounding_problems("Shipped a REST API in Go", SOURCE, t)
    assert grounding_problems("Shipped a REST API in Go", SOURCE, {**t, "allow_words": ["shipped"]}) == []


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("Built a REST API \u2014 in Go", "dash"),
        ("Built a REST API \u2013 in Go", "dash"),
        ("Built a REST API -- in Go", "dash"),
        (" ".join(["word"] * 26), "26 words"),
        ("Built a robust REST API", "robust"),
        ("Leveraged Go to build an API", "leveraged"),
        ("Built a cutting-edge API", "cutting-edge"),
    ],
)
def test_style_problems(text, why, t):
    problems = style_problems(text, t)
    assert problems and why in " ".join(problems)


def test_clean_style_passes(t):
    assert style_problems(" ".join(["word"] * 25), t) == []
    assert style_problems("Built an on-call tool in Go", t) == []


def test_escape_makes_text_safe_latex():
    assert escape("R&D 40% $5 #1 a_b ~x ^y") == (
        r"R\&D 40\% \$5 \#1 a\_b \textasciitilde{}x \textasciicircum{}y"
    )


JD = "We run Kubernetes and terraform for our users. You will lead teams and build services."
CLINICS = "Built a REST API in Go that served 3 clinics"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("Built a REST API in Go that served five clinics", "five"),
        ("Built a REST API in Go that served thousands of users at 3 clinics", "thousands"),
        ("Built a REST API in Go that served twice the clinics", "twice"),
    ],
)
def test_spelled_out_numbers_must_be_in_the_source(text, why, t):
    problems = grounding_problems(text, CLINICS, t)
    assert problems and why in " ".join(problems)


def test_a_spelled_out_number_in_the_source_passes(t):
    assert grounding_problems("Served five clinics", "Built an API that served five clinics", t) == []


def test_job_description_terms_need_the_source(t):
    problems = " ".join(
        grounding_problems("Built a REST API in Go with kubernetes and terraform", CLINICS, t, JD)
    )
    assert "kubernetes" in problems and "terraform" in problems


def test_common_words_from_the_job_description_pass(t):
    assert grounding_problems("Built Go services for the clinic users", CLINICS, t, JD) == []


def test_words_match_with_a_light_stem(t):
    src = "Cut test time in half by parallelizing the test runs"
    assert grounding_problems("Parallelized the test runs and cut test time in half", src, t, JD) == []
    assert grounding_problems("Serving 3 clinics with a REST API in Go", CLINICS, t, JD) == []


@pytest.mark.parametrize(
    "verb", ["Leading", "Leads", "Owning", "Drives", "Driving", "Architecting", "Spearheading"]
)
def test_inflected_inflating_verbs_are_caught(verb, t):
    problems = grounding_problems(
        f"Built and {verb.lower()} the REST API in Go", "Built the REST API in Go", t
    )
    assert problems and "overstates" in " ".join(problems)


def test_escape_maps_characters_plain_latex_prints_wrongly():
    assert escape("a<b>c|d") == r"a\textless{}b\textgreater{}c\textbar{}d"
