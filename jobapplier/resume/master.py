"""The master resume: `resume/master.tex`, where each tailorable line ends with `% id:<id> [required]`."""

import re
from dataclasses import dataclass

__all__ = ["Item", "MasterError", "parse", "plain", "render"]

_COMMENT = re.compile(r"(?<!\\)%")
_MARKER = re.compile(r"\s*id:(\S*)(.*)$")
_SECTION = re.compile(r"\\section\*?\{([^}]*)\}")
_LEAD_COMMAND = re.compile(r"\s*(?:\\[A-Za-z]+\*?\s*)?")


class MasterError(Exception):
    pass


@dataclass
class Item:
    id: str
    section: str
    text: str  # the LaTeX the tailor may change
    required: bool
    line_no: int  # 1-based
    prefix: str  # the line before text
    suffix: str = ""  # the line after text, up to the marker


def _unescaped(code: str, i: int) -> bool:
    return i == 0 or code[i - 1] != "\\"


def _split(code: str) -> tuple[str, str, str]:
    """(prefix, text, suffix): the last brace group's content, or the text after the last group or command."""
    if code.endswith("}"):
        depth = 0
        for i in range(len(code) - 1, -1, -1):
            if code[i] in "{}" and _unescaped(code, i):
                depth += 1 if code[i] == "}" else -1
                if depth == 0:
                    return code[: i + 1], code[i + 1 : -1], "}"
        raise ValueError("unbalanced braces")
    closes = [i for i, ch in enumerate(code) if ch == "}" and _unescaped(code, i)]
    start = closes[-1] + 1 if closes else _LEAD_COMMAND.match(code).end()
    start += len(code[start:]) - len(code[start:].lstrip())
    return code[:start], code[start:], ""


def parse(tex: str) -> list[Item]:
    items, seen, section = [], {}, ""
    for n, line in enumerate(tex.splitlines(), 1):
        code, comment = [*_COMMENT.split(line, maxsplit=1), ""][:2]
        if s := _SECTION.search(code):
            section = s.group(1)
        m = _MARKER.match(comment)
        if not (m and code.strip()):  # a comment-only line may mention the marker
            continue
        code = code.rstrip()
        item_id, rest = m.group(1), m.group(2).strip()
        if not item_id or rest not in ("", "required"):
            raise MasterError(f"master.tex line {n}: write the marker as '% id:<id>' or '% id:<id> required'")
        if item_id in seen:
            raise MasterError(f"master.tex line {n}: id {item_id} is already used on line {seen[item_id]}")
        try:
            prefix, text, suffix = _split(code)
        except ValueError:
            raise MasterError(f"master.tex line {n}: unbalanced braces") from None
        if not text.strip():
            raise MasterError(f"master.tex line {n}: id {item_id} has no text")
        seen[item_id] = n
        items.append(Item(item_id, section, text, rest == "required", n, prefix, suffix))
    return items


def plain(text: str) -> str:
    """The words of a LaTeX fragment, as the LLM and the checks see them."""
    text = re.sub(r"\\([&%$#_{}])", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+\*?\s*", "", text).replace("~", " ")
    return re.sub(r"\s+", " ", re.sub(r"(?<!\\)[{}]", "", text)).strip()


def _runs(items: list[Item]) -> list[list[Item]]:
    """Items on consecutive lines: one list (one job's bullets), whose items stay together."""
    runs: list[list[Item]] = []
    for item in sorted(items, key=lambda i: i.line_no):
        if runs and runs[-1][-1].line_no == item.line_no - 1:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _line(entry: dict, items_by_id: dict[str, Item], lines: list[str]) -> str:
    if "new" in entry:
        src = items_by_id[entry["source"]]
        return f"{src.prefix}{entry['new']}{src.suffix}\n"
    item = items_by_id[entry["id"]]
    if "text" in entry:
        return f"{item.prefix}{entry['text']}{item.suffix}\n"
    return lines[item.line_no - 1]


def render(tex: str, items_by_id: dict[str, Item], plan: list[dict]) -> str:
    """tex with each list of item lines rebuilt from plan, in plan order; other lines stay byte-for-byte.

    plan entries are {"id"}, {"id", "text"} or {"new", "source"}, with text already LaTeX and each id
    at most once (tailor makes sure). An entry goes in the list its id (or source) is in. Required items
    the plan leaves out stay where they were.
    """
    lines = tex.splitlines(keepends=True)
    out_of: dict[int, list[str]] = {}  # first line number of a run -> its new lines
    skip = set()
    for run in _runs(list(items_by_id.values())):
        ids = {i.id for i in run}
        entries = [e for e in plan if (e["source"] if "new" in e else e["id"]) in ids]
        printed = {e["id"] for e in entries if "new" not in e}  # an added line never stands in for its source
        for pos, item in enumerate(run):
            if item.required and item.id not in printed:
                entries.insert(min(pos, len(entries)), {"id": item.id})
        if not entries:  # an empty itemize does not compile
            entries = [{"id": run[0].id}]
        out_of[run[0].line_no] = [_line(e, items_by_id, lines) for e in entries]
        skip |= {i.line_no for i in run}
    out = []
    for n, line in enumerate(lines, 1):
        if n in out_of:
            out += out_of[n]
        elif n not in skip:
            out.append(line)
    return "".join(out)
