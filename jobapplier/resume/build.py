"""Compile the tailored resume with tectonic and keep it to one page (spec §8.3)."""

import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader

from jobapplier.resume.master import Item, anchor, lines_per_run, render, runs

__all__ = ["BuildError", "build", "fit", "pdf_path"]

_PARENS = re.compile(r"\([^)]*\)")


class BuildError(Exception):
    pass


def build(
    tex: str, out_pdf: Path, tectonic: str, search_dir: Path, runner=subprocess.run, timeout=300
) -> int:
    """Writes out_pdf and returns its page count. search_dir holds files the tex inputs (a .cls, images)."""
    with TemporaryDirectory() as d:
        src = Path(d) / "resume.tex"
        src.write_text(tex)
        argv = [tectonic, "-Z", f"search-path={search_dir}", "--outdir", d, str(src)]
        try:
            done = runner(argv, capture_output=True, text=True, cwd=d, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BuildError(f"tectonic timed out after {timeout}s") from None
        if done.returncode != 0:
            raise BuildError(f"tectonic failed: {(done.stderr or done.stdout)[-300:].strip()}")
        pages = len(PdfReader(src.with_suffix(".pdf")).pages)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src.with_suffix(".pdf"), out_pdf)
    return pages


def _drop_one(plan: list[dict], items: list[Item], minimum: int) -> list[dict] | None:
    """plan less the last optional line of the section with the most that can go; None when none can.

    A line can go when it is not required and its entry (job or project) keeps at least minimum lines.
    """
    by_id = {i.id: i for i in items}
    run_of = {i.id: n for n, run in enumerate(runs(items)) for i in run}
    sizes = lines_per_run(items, plan)
    optional: dict[str, list[int]] = {}
    for n, e in enumerate(plan):
        item = by_id[anchor(e)]
        if ("new" in e or not item.required) and sizes[run_of[item.id]] > minimum:
            optional.setdefault(item.section, []).append(n)
    if not optional:
        return None
    victim = max(optional.values(), key=len)[-1]  # max keeps the first section on a tie
    return plan[:victim] + plan[victim + 1 :]


def fit(
    tex: str,
    items: list[Item],
    plan: list[dict],
    out_pdf: Path,
    cfg: dict,
    search_dir: Path,
    runner=subprocess.run,
) -> list[str]:
    """Builds out_pdf, dropping optional lines until it is one page. Returns problems (empty when it fits).

    Never drops a required line, nor a line that would leave its entry under min_lines_per_entry.
    """
    by_id = {i.id: i for i in items}
    for drops in range(cfg["max_drops"] + 1):
        pages = build(
            render(tex, by_id, plan), out_pdf, cfg["tectonic"], search_dir, runner, cfg["tectonic_timeout"]
        )
        if pages <= 1:
            return []
        smaller = _drop_one(plan, items, cfg["min_lines_per_entry"]) if drops < cfg["max_drops"] else None
        if smaller is None:
            break
        plan = smaller
    return [f"the resume is still {pages} pages after dropping {drops} optional lines"]


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", _PARENS.sub(" ", text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9\s]", "", re.sub(r"[-/]", " ", text)).split()


def pdf_path(out_dir: Path, name: str, company: str, title: str, job_id: str) -> Path:
    """out_dir/First_Last_Company_Role.pdf, the role cut to 6 words; with _<job_id> if that file exists."""
    names = _words(name)
    if not names:
        raise ValueError("set profile.name in config.yaml (it names the resume file)")
    stem = "_".join([*dict.fromkeys([names[0], names[-1]]), *_words(company), *_words(title)[:6]])
    path = out_dir / f"{stem}.pdf"
    return out_dir / f"{stem}_{job_id}.pdf" if path.exists() else path
