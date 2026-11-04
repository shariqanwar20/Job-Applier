"""One LLM call that picks, orders and rewords master lines for a job; every changed line is checked."""

import re

from jobapplier.resume.grounding import escape, grounding_problems, style_problems
from jobapplier.resume.master import Item, anchor, lines_per_run, plain, runs

__all__ = ["tailor"]

_STR = {"type": "string"}
_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": _STR,
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": _STR, "text": _STR, "new": _STR, "source": _STR},
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["section", "items"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}

_RULES = """\
Tailor my resume to the job below. Write every line you reword or add with the humanizer skill and then
the asd-ste100 skill, in embedded mode: apply them and return only the final text, with no notes.

Return the lines to print, section by section, in the order to print them. Each line is one of:
- {{"id": ID}} to keep a master line as it is;
- {{"id": ID, "text": TEXT}} to reword a master line;
- {{"new": TEXT, "source": ID}} to add a line that comes from exactly one master line.

Rules:
- Keep every line unless it plainly does not help with this job. Leave out at most {max_omit} lines in total.
- Prefer rewording a weak line toward the job, or replacing it with an added line that comes from it,
  over leaving it out.
- Keep at least {min_lines} lines for every job and project.
- Always keep the lines marked (required).
- Within each job or project, put the lines that matter most for this job first.
- Reword a line to use the job's terms where the master line supports them. Keep its facts and numbers.
- Never invent facts. Every number, technology, employer and job title must be in the master line.
- Use the job description's terms only where the master line supports them.
- Write plain text: no LaTeX, no backslashes or braces, no dashes as punctuation.
- At most {max_words} words per line.
- Never use these words: {banned}.
"""


def _prompt(items: list[Item], job: dict, cfg: dict) -> str:
    t = cfg["tailor"]
    lines, section = [], None
    for i in items:
        if i.section != section:
            section = i.section
            lines.append(f"\n## {section}")
        lines.append(f"- {i.id}{' (required)' if i.required else ''}: {plain(i.text)}")
    return (
        _RULES.format(
            max_words=t["max_words"],
            max_omit=t["max_omit"],
            min_lines=t["min_lines_per_entry"],
            banned=", ".join(t["banned_phrases"]),
        )
        + f"\nJob: {job['title']} at {job['company']}\n\nJob description:\n"
        + job["description"][: t["max_description_chars"]]
        + "\n\nMaster resume lines (id: text):"
        + "\n".join(lines)
        + "\n"
    )


def _check(
    answer: dict, by_id: dict[str, Item], cfg: dict, job_text: str
) -> tuple[list[dict], list[str], list[str]]:
    """(plan, grounding problems, style problems); a rejected line is already back to its master text."""
    plan, problems, style = [], [], []
    for e in (x for s in answer.get("sections", []) for x in s.get("items", [])):
        key = e.get("source") if "new" in e else e.get("id")
        if key not in by_id:
            problems.append(f"{key}: not a master id; line left out")
            continue
        text = e.get("new", e.get("text"))
        master = plain(by_id[key].text)
        if text is None or ("new" not in e and text == master):
            plan.append({"id": key})
            continue
        if not re.search(r"[A-Za-z]", text):
            problems.append(f"{key}: the new text has no words; kept the master line")
        elif why := grounding_problems(text, master, cfg["tailor"], job_text):
            problems.append(f"{key}: {'; '.join(why)}; kept the master line")
        elif why := style_problems(text, cfg["tailor"]):
            style.append(f"{key}: {'; '.join(why)}")
        else:
            plan.append(
                {"new": escape(text), "source": key} if "new" in e else {"id": key, "text": escape(text)}
            )
            continue
        plan.append({"id": key})
    seen: set[str] = set()
    unique = []
    for e in plan:  # the one dedupe; a reverted added line can repeat its source
        if "id" in e and e["id"] in seen:
            continue
        seen.add(e.get("id", ""))
        unique.append(e)
    return unique, problems, style


def _fill(plan: list[dict], items: list[Item], minimum: int) -> list[dict]:
    """Puts omitted master lines back, in master order, into each entry printed with fewer than minimum."""
    for run, count in zip(runs(items), lines_per_run(items, plan), strict=True):
        printed = {e["id"] for e in plan if "new" not in e}
        omitted = [{"id": i.id} for i in run if not i.required and i.id not in printed]
        back = omitted[: max(0, minimum - count)]
        if back:
            ids = {i.id for i in run}
            at = max((k + 1 for k, e in enumerate(plan) if anchor(e) in ids), default=len(plan))
            plan = plan[:at] + back + plan[at:]
    return plan


def tailor(items: list[Item], job: dict, cfg: dict, llm_run) -> tuple[list[dict], list[str]]:
    """(plan for master.render, problems). job has company, title and description.

    A style failure asks once more with the problems listed; after that, failing lines keep master text.
    """
    by_id = {i.id: i for i in items}
    prompt, model = _prompt(items, job, cfg), cfg["llm"]["models"]["tailor"]
    job_text = job["description"][: cfg["tailor"]["max_description_chars"]]
    plan, problems, style = _check(llm_run(prompt, _SCHEMA, model, writing=True), by_id, cfg, job_text)
    if style:
        retry = (
            prompt + "\nYour last answer broke these rules. Fix them:\n" + "\n".join(f"- {s}" for s in style)
        )
        plan, problems, style = _check(llm_run(retry, _SCHEMA, model, writing=True), by_id, cfg, job_text)
    plan = _fill(plan, items, cfg["tailor"]["min_lines_per_entry"])  # owner: reword or replace, not remove
    return plan, problems + [f"{s}; kept the master line" for s in style]
