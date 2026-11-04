from datetime import UTC, datetime

import pytest

from jobapplier.apply.targets import apply_url, find_targets
from jobapplier.core.clusters import assign, connect, record_resume
from jobapplier.core.identity import posting_identity
from jobapplier.models import Posting
from tests.conftest import FakeNotion

NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://jobs.lever.co/acme/1a2b-3c", "https://jobs.lever.co/acme/1a2b-3c/apply"),
        ("https://jobs.lever.co/acme/1a2b-3c/", "https://jobs.lever.co/acme/1a2b-3c/apply"),
        ("https://jobs.lever.co/acme/1a2b-3c/apply", "https://jobs.lever.co/acme/1a2b-3c/apply"),
        ("https://jobs.ashbyhq.com/acme/9f8e", "https://jobs.ashbyhq.com/acme/9f8e/application"),
        ("https://acme.breezy.hr/p/abc123-developer", "https://acme.breezy.hr/p/abc123-developer/apply"),
        (
            "https://jobs.smartrecruiters.com/Acme/744000-software-developer",
            "https://jobs.smartrecruiters.com/Acme/744000-software-developer?oga=true",
        ),
        (
            "https://job-boards.greenhouse.io/acme/jobs/123",
            "https://job-boards.greenhouse.io/acme/jobs/123",
        ),
        ("https://careers.acme.com/jobs/42", "https://careers.acme.com/jobs/42"),
    ],
)
def test_job_pages_turn_into_application_pages(url, want):
    assert apply_url(url) == want


def text(value):
    return [{"text": {"content": value}}]


def row(job_id, link="", resume="not-in-output.pdf"):
    return {
        "Title": {"title": text("Developer")},
        "Company": {"rich_text": text("Acme")},
        "Job ID": {"rich_text": text(job_id)},
        "Status": {"select": {"name": "Tailored"}},
        "Link": {"url": link or None},
        "Resume": {"files": [{"name": resume}]},  # ready rows have one; it may be named differently
    }


def add(conn, rules, n, *urls, description=""):
    """One cluster per call: its members have these URLs, in order."""
    cid = None
    for k, url in enumerate(urls):
        p = Posting("indeed", f"{n}-{k}", f"Developer {n}", "Acme", "Toronto, ON", url, description="")
        p.description = description
        p.identity = posting_identity(url, rules)
        cid = assign(conn, p, rules, NOW, False)
    return cid


@pytest.fixture
def home(tmp_path, config, rules):
    config["notion"]["data_source_id"] = "ds1"
    (tmp_path / "output").mkdir()
    return tmp_path, config, rules, connect(":memory:")


def pdf(root, name):
    path = root / "output" / name
    path.write_bytes(b"%PDF")
    return path


def test_the_ats_member_wins_and_the_recorded_pdf_is_used(home):
    root, cfg, rules, conn = home
    cid = add(
        conn,
        rules,
        1,
        "https://ca.linkedin.com/jobs/view/1",
        "https://jobs.lever.co/acme/abc",
        description="We pay $90,000 - $110,000.",
    )
    record_resume(conn, cid, str(pdf(root, "A_Acme.pdf")))
    n = FakeNotion()
    n.rows = {"p1": row("J1", link="https://ca.linkedin.com/jobs/view/1")}
    targets, skipped = find_targets(conn, n, cfg, rules)
    assert skipped == []
    [t] = targets
    assert (t.job_id, t.company, t.url) == ("J1", "Acme", "https://jobs.lever.co/acme/abc/apply")
    assert t.pdf == root / "output" / "A_Acme.pdf" and "$90,000" in t.description
    assert t.page["id"] == "p1"


def test_a_non_aggregator_member_is_used_when_no_member_is_an_ats(home):
    root, cfg, rules, conn = home
    add(conn, rules, 1, "https://www.indeed.com/viewjob?jk=1", "https://careers.acme.com/jobs/42")
    record_resume(conn, 1, str(pdf(root, "A_Acme_J1.pdf")))
    n = FakeNotion()
    n.rows = {"p1": row("J1")}
    [t], _ = find_targets(conn, n, cfg, rules)
    assert t.url == "https://careers.acme.com/jobs/42"


def test_aggregator_only_rows_are_skipped_unless_link_holds_the_employer_page(home):
    root, cfg, rules, conn = home
    add(conn, rules, 1, "https://ca.linkedin.com/jobs/view/1", "https://www.indeed.com/viewjob?jk=1")
    add(conn, rules, 2, "https://ca.linkedin.com/jobs/view/2")
    record_resume(conn, 1, str(pdf(root, "A_J1.pdf")))
    record_resume(conn, 2, str(pdf(root, "A_J2.pdf")))
    n = FakeNotion()
    n.rows = {"p1": row("J1", link="https://ca.linkedin.com/jobs/view/1"), "p2": row("J2")}
    targets, skipped = find_targets(conn, n, cfg, rules)
    assert targets == []
    assert skipped == [
        "J1 (Acme): only a job-board link: find the employer's posting and put it in Link",
        "J2 (Acme): only a job-board link: find the employer's posting and put it in Link",
    ]
    n.rows["p1"]["Link"] = {"url": "https://jobs.ashbyhq.com/acme/77"}  # the owner found it
    targets, _ = find_targets(conn, n, cfg, rules, "J1")
    assert [t.url for t in targets] == ["https://jobs.ashbyhq.com/acme/77/application"]


def test_a_row_without_a_pdf_is_skipped(home):
    root, cfg, rules, conn = home
    add(conn, rules, 1, "https://jobs.lever.co/acme/abc")
    record_resume(conn, 1, str(root / "output" / "gone.pdf"))  # deleted since
    n = FakeNotion()
    n.rows = {"p1": row("J1")}
    targets, skipped = find_targets(conn, n, cfg, rules)
    assert targets == [] and skipped == ["J1 (Acme): no tailored PDF on record: run tailor --job J1 again"]


def test_the_pdf_is_only_the_one_tailor_recorded(home):
    root, cfg, rules, conn = home
    add(conn, rules, 1, "https://jobs.lever.co/acme/abc")
    pdf(root, "not-in-output.pdf")  # the row's Notion Resume name
    pdf(root, "A_J1.pdf")  # a file named for the job, but not recorded
    n = FakeNotion()
    n.rows = {"p1": row("J1")}
    targets, skipped = find_targets(conn, n, cfg, rules)
    assert targets == [] and skipped == ["J1 (Acme): no tailored PDF on record: run tailor --job J1 again"]


def test_a_row_without_a_cluster_is_skipped(home):
    _, cfg, rules, conn = home
    n = FakeNotion()
    n.rows = {"p1": row("J9")}
    assert find_targets(conn, n, cfg, rules) == ([], ["J9 (Acme): no cluster in data/jobs.db"])
