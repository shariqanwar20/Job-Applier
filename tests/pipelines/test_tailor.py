import dataclasses
import pathlib
import shutil
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from jobapplier.core.clusters import assign, connect
from jobapplier.llm import LlmError
from jobapplier.pipelines.tailor import run_tailor
from tests.conftest import AFFIRM_INDEED, FakeNotion

EXAMPLE = pathlib.Path(__file__).parents[2] / "resume" / "example.master.tex"
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def text(value):
    return [{"text": {"content": value}}]


def row(job_id, decision="Approve", status="New", title="Software Engineer II", company="Affirm"):
    return {
        "Title": {"title": text(title)},
        "Company": {"rich_text": text(company)},
        "Job ID": {"rich_text": text(job_id)},
        "Decision": {"select": {"name": decision}},
        "Status": {"select": {"name": status}},
    }


def tectonic(argv, **kw):
    w = PdfWriter()
    w.add_blank_page(612, 792)
    src = pathlib.Path(argv[-1])
    with open(pathlib.Path(argv[argv.index("--outdir") + 1]) / f"{src.stem}.pdf", "wb") as f:
        w.write(f)
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeLlm:
    """answers maps a job title (found in the prompt) to the answer, or to an exception to raise."""

    def __init__(self, answers=None):
        self.answers, self.prompts = dict(answers or {}), []

    def __call__(self, prompt, schema, model, writing):
        self.prompts.append(prompt)
        got = next((a for t, a in self.answers.items() if t in prompt), {"sections": []})
        if isinstance(got, Exception):
            raise got
        return got


def posting(**kw):
    # identity=None: another test's discover run sets it on the shared AFFIRM_INDEED, merging clusters
    return dataclasses.replace(AFFIRM_INDEED, identity=None, **kw)


@pytest.fixture
def home(tmp_path, config, rules):
    (tmp_path / "resume").mkdir()
    shutil.copy(EXAMPLE, tmp_path / "resume" / "master.tex")
    config["profile"]["name"] = "Alex Example"
    config["notion"]["data_source_id"] = "ds1"
    conn = connect(":memory:")
    for n, desc in ((1, "short"), (2, "We build Go services on PostgreSQL."), (3, "x")):
        p = posting(source_id=f"in-{n}", title=f"Role {n}", description=desc)
        assign(conn, p, rules, NOW, False)
    return tmp_path, config, conn


def run(home, n, llm, job_id=None):
    root, cfg, conn = home
    return run_tailor(conn, cfg, n, llm, root, job_id, runner=tectonic)


def test_approved_rows_get_a_resume_and_a_status(home):
    n = FakeNotion()
    n.rows = {
        "p1": row("J1"),
        "p2": row("J2", "Approve + review", title="Backend Developer"),
        "p3": row("J3", "Skip"),
    }
    llm = FakeLlm()
    result = run(home, n, llm)
    assert result == {"tailored": 1, "review": 1, "errors": []}
    assert n.rows["p1"]["Status"] == {"select": {"name": "Tailored"}}
    assert n.rows["p2"]["Status"] == {"select": {"name": "Needs review"}}
    assert n.rows["p3"]["Status"] == {"select": {"name": "New"}} and "Resume" not in n.rows["p3"]
    assert n.rows["p1"]["Resume"]["files"][0]["name"] == "Alex_Example_Affirm_Software_Engineer_II.pdf"
    assert (home[0] / "output" / "Alex_Example_Affirm_Software_Engineer_II.pdf").exists()
    assert len(n.uploads) == 2 and "Software Engineer II" in llm.prompts[0]
    assert "Backend Developer" in llm.prompts[1]


def test_the_prompt_gets_the_longest_member_description(home, rules):
    _, _, conn = home
    longer = posting(source_id="in-9", title="Role 1", description="L" * 50)
    assert assign(conn, longer, rules, NOW, False) == 1
    n = FakeNotion()
    n.rows = {"p1": row("J1")}
    llm = FakeLlm()
    run(home, n, llm)
    assert "L" * 50 in llm.prompts[0]


def test_problems_send_the_row_to_review_with_notes(home):
    n = FakeNotion()
    n.rows = {"p1": row("J1")}
    bad = {"sections": [{"section": "Experience", "items": [{"id": "exp1.b1", "text": "Built 9 APIs"}]}]}
    assert run(home, n, FakeLlm({"Software Engineer II": bad}))["review"] == 1
    assert n.rows["p1"]["Status"] == {"select": {"name": "Needs review"}}
    assert "exp1.b1" in n.rows["p1"]["Notes"]["rich_text"][0]["text"]["content"]


def test_a_failing_row_is_reported_and_the_next_one_still_runs(home):
    n = FakeNotion()
    n.rows = {"p1": row("J1", title="Broken"), "p2": row("J2")}
    result = run(home, n, FakeLlm({"Broken": LlmError("claude exited 1: usage limit")}))
    assert result["tailored"] == 1 and result["errors"] == ["J1: claude exited 1: usage limit"]
    assert n.rows["p1"]["Status"] == {"select": {"name": "New"}}  # retried next run
    assert n.rows["p2"]["Status"] == {"select": {"name": "Tailored"}}


def test_a_row_without_a_cluster_is_an_error(home):
    n = FakeNotion()
    n.rows = {"p1": row("J99")}
    assert run(home, n, FakeLlm())["errors"] == ["J99: no cluster for J99 in data/jobs.db"]


def test_one_job_runs_whatever_its_decision(home):
    n = FakeNotion()
    n.rows = {"p1": row("J1", "Skip", status="Tailored"), "p2": row("J2")}
    assert run(home, n, FakeLlm(), "J1")["tailored"] == 1
    assert "Resume" in n.rows["p1"] and "Resume" not in n.rows["p2"]


def test_one_job_without_a_row_is_an_error(home):
    assert run(home, FakeNotion(), FakeLlm(), "J5")["errors"] == ["no Notion row with Job ID J5"]


def test_the_schema_is_ensured_once_per_run(home):
    n = FakeNotion()
    n.rows = {"p1": row("J1"), "p2": row("J2")}
    run(home, n, FakeLlm())
    assert [c[0] for c in n.calls].count("data_sources.retrieve") == 1
    assert "Resume" in n.ds_properties


def test_missing_setup_is_reported_before_any_notion_call(home):
    root, cfg, _ = home
    n = FakeNotion()
    cfg["profile"]["name"] = ""
    assert "profile.name" in run(home, n, FakeLlm())["errors"][0]
    cfg["profile"]["name"] = "Alex Example"
    (root / "resume" / "master.tex").unlink()
    assert "resume/example.master.tex" in run(home, n, FakeLlm())["errors"][0]
    (root / "resume" / "master.tex").write_text("\\resumeItem{a} % id:\n")
    assert "line 1" in run(home, n, FakeLlm())["errors"][0]
    assert n.calls == []
