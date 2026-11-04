import pathlib

from jobapplier.resume.master import parse
from jobapplier.resume.tailor import tailor

ITEMS = parse((pathlib.Path(__file__).parents[2] / "resume" / "example.master.tex").read_text())
JOB = {"company": "Acme", "title": "Backend Developer", "description": "We use Go and PostgreSQL."}


class FakeLlm:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, schema, model, writing):
        self.calls.append({"prompt": prompt, "schema": schema, "model": model, "writing": writing})
        return self.answers.pop(0)


def answer(*items):
    return {"sections": [{"section": "Experience", "items": list(items)}]}


def test_prompt_names_the_skills_job_and_master_lines(config):
    llm = FakeLlm(answer({"id": "exp1.b1"}))
    tailor(ITEMS, JOB, config, llm)
    [c] = llm.calls
    assert c["writing"] is True and c["model"] == "claude-sonnet-5-5"
    p = c["prompt"]
    for part in (
        "humanizer",
        "asd-ste100",
        "at most 3 lines",
        "Backend Developer",
        "Acme",
        "We use Go and PostgreSQL.",
    ):
        assert part in p
    assert "exp1.b1 (required): Built a REST API in Go that served 3 clinics" in p
    assert "exp1.b2: Cut page load time by 40% with Redis caching" in p  # plain text, not LaTeX
    assert "## Skills" in p
    assert c["schema"]["required"] == ["sections"]


def test_description_is_cut_to_the_configured_length(config):
    llm = FakeLlm(answer({"id": "exp1.b1"}))
    config["tailor"]["max_description_chars"] = 10
    tailor(ITEMS, {**JOB, "description": "0123456789TAIL"}, config, llm)
    assert "0123456789" in llm.calls[0]["prompt"] and "TAIL" not in llm.calls[0]["prompt"]


def test_unchanged_reworded_and_added_items_are_kept_and_escaped(config):
    llm = FakeLlm(
        answer(
            {"id": "exp1.b2"},
            {"id": "exp1.b1", "text": "Built a Go REST API that served 3 clinics & staff"},
            {"new": "Wrote the REST API in Go", "source": "exp1.b1"},
        )
    )
    plan, problems = tailor(ITEMS, JOB, config, llm)
    assert plan == [
        {"id": "exp1.b2"},
        {"id": "exp1.b1", "text": r"Built a Go REST API that served 3 clinics \& staff"},
        {"new": "Wrote the REST API in Go", "source": "exp1.b1"},
    ]
    assert problems == []


def test_ungrounded_items_revert_to_the_master_line(config):
    llm = FakeLlm(
        answer(
            {"id": "exp1.b1", "text": "Built a REST API in Go that served 30 clinics"},
            {"new": "Ran Kubernetes for the order service", "source": "exp2.b1"},
            {"new": "Built a Go API at Google", "source": "exp1.b1"},
        )
    )
    plan, problems = tailor(ITEMS, JOB, config, llm)
    assert plan == [{"id": "exp1.b1"}, {"id": "exp2.b1"}]  # the second revert of exp1.b1 is a repeat
    assert len(problems) == 3 and problems[0].startswith("exp1.b1:") and "30" in problems[0]
    assert "Kubernetes" in problems[1]


def test_unknown_ids_are_dropped_and_reported(config):
    llm = FakeLlm(answer({"id": "nope"}, {"new": "Built a thing", "source": "gone"}, {"id": "exp1.b2"}))
    plan, problems = tailor(ITEMS, JOB, config, llm)
    assert plan == [{"id": "exp1.b2"}]
    assert any("nope" in p for p in problems) and any("gone" in p for p in problems)


def test_a_style_failure_regenerates_once_with_the_problems(config):
    bad = answer({"id": "exp1.b1", "text": "Built a robust REST API in Go for 3 clinics"})
    good = answer({"id": "exp1.b1", "text": "Built a REST API in Go for 3 clinics"})
    llm = FakeLlm(bad, good)
    plan, problems = tailor(ITEMS, JOB, config, llm)
    assert len(llm.calls) == 2 and "robust" in llm.calls[1]["prompt"]
    assert plan == [{"id": "exp1.b1", "text": "Built a REST API in Go for 3 clinics"}] and problems == []


def test_a_second_style_failure_keeps_the_master_line(config):
    bad = answer({"id": "exp1.b1", "text": "Built a REST API \u2014 in Go for 3 clinics"}, {"id": "exp1.b2"})
    llm = FakeLlm(bad, bad)
    plan, problems = tailor(ITEMS, JOB, config, llm)
    assert len(llm.calls) == 2
    assert plan == [{"id": "exp1.b1"}, {"id": "exp1.b2"}]
    assert len(problems) == 1 and problems[0].startswith("exp1.b1:") and "dash" in problems[0]


def test_text_equal_to_the_master_line_counts_as_unchanged(config):
    llm = FakeLlm(answer({"id": "exp1.b2", "text": "Cut page load time by 40% with Redis caching"}))
    assert tailor(ITEMS, JOB, config, llm) == ([{"id": "exp1.b2"}], [])
