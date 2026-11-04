import pathlib
import subprocess
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from jobapplier.resume.build import BuildError, build, fit, pdf_path
from jobapplier.resume.master import parse

TEX = (pathlib.Path(__file__).parents[2] / "resume" / "example.master.tex").read_text()
ITEMS = parse(TEX)
ALL = [{"id": i.id} for i in ITEMS]


def write_pdf(path, pages):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    with open(path, "wb") as f:
        w.write(f)


class FakeTectonic:
    """Writes <outdir>/<stem>.pdf with pages(tex) pages."""

    def __init__(self, pages=lambda tex: 1, returncode=0):
        self.pages, self.returncode, self.calls = pages, returncode, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        src = pathlib.Path(argv[-1])
        tex = src.read_text()
        self.texts = [*getattr(self, "texts", []), tex]
        if self.returncode == 0:
            write_pdf(pathlib.Path(argv[argv.index("--outdir") + 1]) / f"{src.stem}.pdf", self.pages(tex))
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="! Undefined control sequence.")


def bullets(tex):
    return tex.count("\\resumeItem{")


def test_build_runs_tectonic_in_a_temp_dir_and_counts_pages(tmp_path):
    fake = FakeTectonic(lambda tex: 2)
    out = tmp_path / "out" / "r.pdf"
    assert build(TEX, out, "/bin/tectonic", tmp_path / "resume", fake) == 2
    argv, kw = fake.calls[0]
    assert argv[0] == "/bin/tectonic" and f"search-path={tmp_path / 'resume'}" in argv
    assert kw["cwd"] == str(pathlib.Path(argv[-1]).parent) and kw["timeout"] == 300
    assert len(PdfReader(out).pages) == 2


def test_build_failure_raises_with_the_log_tail(tmp_path):
    with pytest.raises(BuildError, match="Undefined control sequence"):
        build(TEX, tmp_path / "r.pdf", "tectonic", tmp_path, FakeTectonic(returncode=1))


def test_build_timeout_raises(tmp_path):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 300)

    with pytest.raises(BuildError, match="timed out"):
        build(TEX, tmp_path / "r.pdf", "tectonic", tmp_path, slow)


def test_fit_drops_optional_lines_until_one_page(tmp_path, config):
    fake = FakeTectonic(lambda tex: 1 if bullets(tex) <= 4 else 2)
    out = tmp_path / "r.pdf"
    assert fit(TEX, ITEMS, ALL, out, config["tailor"], tmp_path, fake) == []
    # Experience has the most optional lines (3), then ties Projects (2 each) and goes first, then Projects
    last = fake.texts[-1]
    assert "on-call" not in last and "billing" not in last and "offline maps" not in last
    assert "Cut page load" in last and "plans hikes" in last and len(fake.calls) == 4
    assert len(PdfReader(out).pages) == 1


def test_fit_never_drops_required_lines(tmp_path, config):
    fake = FakeTectonic(lambda tex: 2)
    problems = fit(TEX, ITEMS, ALL, tmp_path / "r.pdf", config["tailor"], tmp_path, fake)
    assert problems and "2 pages" in problems[0]
    last = fake.texts[-1]
    for required in ("Built a REST API", "Wrote unit tests", "Languages:"):
        assert required in last


def test_fit_stops_after_max_drops(tmp_path, config):
    fake = FakeTectonic(lambda tex: 2)
    problems = fit(TEX, ITEMS, ALL, tmp_path / "r.pdf", {**config["tailor"], "max_drops": 2}, tmp_path, fake)
    assert len(fake.calls) == 3 and "2 pages" in problems[0]


def test_pdf_path_names_the_file(tmp_path):
    p = pdf_path(
        tmp_path, "Alex Q. Example", "Acme Inc. (Canada)", "Software Engineer I, Back-End (Remote)", "J7"
    )
    assert p == tmp_path / "Alex_Example_Acme_Inc_Software_Engineer_I_Back_End.pdf"


def test_pdf_path_cuts_the_role_to_six_words_and_adds_the_job_id_on_a_collision(tmp_path):
    first = pdf_path(tmp_path, "Alex Example", "Acme", "One Two Three Four Five Six Seven", "J7")
    assert first.name == "Alex_Example_Acme_One_Two_Three_Four_Five_Six.pdf"
    first.write_bytes(b"x")
    assert pdf_path(tmp_path, "Alex Example", "Acme", "One Two Three Four Five Six", "J8").name == (
        "Alex_Example_Acme_One_Two_Three_Four_Five_Six_J8.pdf"
    )


def test_pdf_path_needs_a_name(tmp_path):
    with pytest.raises(ValueError, match=r"profile\.name"):
        pdf_path(tmp_path, " ", "Acme", "Dev", "J1")


def test_fit_passes_the_configured_tectonic_timeout(tmp_path, config):
    fake = FakeTectonic()
    fit(TEX, ITEMS, ALL, tmp_path / "r.pdf", {**config["tailor"], "tectonic_timeout": 42}, tmp_path, fake)
    assert fake.calls[0][1]["timeout"] == 42
