import pathlib

import pytest

from jobapplier.resume.master import MasterError, parse, plain, render

EXAMPLE = (pathlib.Path(__file__).parents[2] / "resume" / "example.master.tex").read_text()


def is_item(line):
    return line.lstrip().startswith(("\\resumeItem{", "\\item \\textbf{"))


def by_id(tex=EXAMPLE):
    return {i.id: i for i in parse(tex)}


def test_parse_reads_ids_sections_text_and_required():
    items = parse(EXAMPLE)
    assert [i.id for i in items] == [
        "exp1.b1",
        "exp1.b2",
        "exp1.b3",
        "exp2.b1",
        "exp2.b2",
        "proj1.b1",
        "proj1.b2",
        "skills.lang",
        "skills.tools",
    ]
    b1, b2 = items[0], items[1]
    assert (b1.section, b1.required) == ("Experience", True)
    assert b1.text == "Built a REST API in Go that served 3 clinics"
    assert b2.text == r"Cut page load time by 40\% with Redis caching" and not b2.required
    assert EXAMPLE.splitlines()[b1.line_no - 1].lstrip().startswith(r"\resumeItem{Built")


def test_parse_takes_the_text_after_a_label():
    lang = by_id()["skills.lang"]
    assert lang.section == "Skills" and lang.text == "Python, Go, TypeScript, SQL"
    assert lang.prefix.strip() == r"\item \textbf{Languages:}"


def test_parse_keeps_nested_braces_inside_the_argument():
    [item] = parse(r"\section{X}" + "\n" + r"\resumeItem{Used \textbf{Redis} here} % id:a" + "\n")
    assert item.text == r"Used \textbf{Redis} here"


def test_parse_reports_a_missing_id_with_its_line():
    with pytest.raises(MasterError, match="line 2"):
        parse("\\section{X}\n\\resumeItem{a} % id:\n")


def test_parse_reports_a_duplicate_id_with_both_lines():
    with pytest.raises(MasterError, match=r"line 3.*a.*line 2"):
        parse("\\section{X}\n\\resumeItem{a} % id:a\n\\resumeItem{b} % id:a\n")


def test_parse_reports_an_unknown_word_after_the_id():
    with pytest.raises(MasterError, match="line 1"):
        parse("\\resumeItem{a} % id:a requred\n")


def test_an_escaped_percent_is_not_a_marker():
    assert parse("\\resumeItem{40\\% id:x}\n") == []


def test_plain_drops_latex():
    assert plain(r"Cut by 40\% with \textbf{Redis} \& Go") == "Cut by 40% with Redis & Go"


def test_render_with_every_item_unchanged_is_byte_for_byte():
    plan = [{"id": i.id} for i in parse(EXAMPLE)]
    assert render(EXAMPLE, by_id(), plan) == EXAMPLE


def test_render_rewords_reorders_adds_and_omits():
    plan = [
        {"id": "exp1.b2"},
        {"id": "exp1.b1", "text": "Built a Go REST API for 3 clinics"},
        {"new": "Wrote the API docs", "source": "exp1.b1"},
        {"id": "exp2.b1"},
        {"id": "proj1.b1"},
        {"id": "skills.lang"},
    ]
    out = render(EXAMPLE, by_id(), plan)
    lines = out.splitlines()
    i = lines.index(r"  \resumeItem{Cut page load time by 40\% with Redis caching} % id:exp1.b2")
    assert lines[i + 1].strip() == r"\resumeItem{Built a Go REST API for 3 clinics}"
    assert lines[i + 2].strip() == r"\resumeItem{Wrote the API docs}"
    assert lines[i + 3] == r"\end{itemize}"
    assert "billing" not in out and "on-call" not in out and "offline" not in out and "Tools:" not in out
    # everything that is not an item line is unchanged
    assert [x for x in lines if not is_item(x)] == [x for x in EXAMPLE.splitlines() if not is_item(x)]


def test_render_keeps_required_items_the_plan_leaves_out():
    out = render(EXAMPLE, by_id(), [{"id": "exp1.b2"}])
    assert "Built a REST API" in out and "Wrote unit tests" in out and "Languages:" in out
    assert out.index("Built a REST API") < out.index("Cut page load")  # at its master position


def test_render_keeps_one_item_in_a_list_the_plan_empties():
    out = render(EXAMPLE, by_id(), [{"id": i.id} for i in parse(EXAMPLE) if not i.id.startswith("proj1")])
    assert "plans hikes" in out and "offline maps" not in out


def test_render_puts_an_item_back_in_its_own_list():
    out = render(EXAMPLE, by_id(), [{"id": "exp2.b2"}, {"id": "exp1.b2"}])
    assert out.index("Cut page load") < out.index("Sample Retail Co") < out.index("on-call")


def test_an_added_line_before_its_required_source_keeps_the_source():
    out = render(EXAMPLE, by_id(), [{"new": "Wrote a Go API", "source": "exp1.b1"}, {"id": "exp1.b1"}])
    assert "Wrote a Go API" in out and "Built a REST API in Go that served 3 clinics" in out


def test_an_added_line_alone_does_not_replace_its_required_source():
    out = render(EXAMPLE, by_id(), [{"new": "Wrote a Go API", "source": "exp1.b1"}])
    assert "Wrote a Go API" in out and "Built a REST API in Go that served 3 clinics" in out
