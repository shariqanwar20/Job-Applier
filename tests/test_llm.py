import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from jobapplier import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}


def runner(stdout="", returncode=0, stderr="", exc=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


def ok(answer, cost=0.0123):
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": cost,
            "structured_output": answer,
        }
    )


def test_writing_call_loads_project_skills_and_returns_the_answer(caplog):
    caplog.set_level(logging.INFO)
    r = runner(ok({"a": "b"}))
    assert llm.run("PROMPT", SCHEMA, "claude-sonnet-5-5", writing=True, claude="/bin/claude", runner=r) == {
        "a": "b"
    }
    [(argv, kw)] = r.calls
    assert argv == [
        "/bin/claude",
        "-p",
        "--model",
        "claude-sonnet-5-5",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--setting-sources",
        "project",
        "--tools",
        "Skill",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    assert kw["input"] == "PROMPT" and kw["cwd"] == llm._ROOT and kw["timeout"] == 300
    assert "0.0123" in caplog.text and "PROMPT" not in caplog.text


def test_classification_call_loads_nothing():
    r = runner(ok({"a": "b"}))
    llm.run("p", SCHEMA, "claude-haiku-5-5", writing=False, claude="claude", runner=r, timeout=30)
    argv, kw = r.calls[0]
    assert argv[-6:] == [
        "--setting-sources",
        "",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    assert kw["timeout"] == 30


@pytest.mark.parametrize(
    ("r", "message"),
    [
        (runner("", returncode=1, stderr="E" * 300), "E" * 200),
        (runner(json.dumps({"is_error": True, "subtype": "error_max_turns"})), "error_max_turns"),
        (runner(json.dumps({"is_error": False, "subtype": "success"})), "structured_output"),
        (runner("not json"), "not JSON"),
        (runner(exc=subprocess.TimeoutExpired(["claude"], 300)), "timed out"),
    ],
)
def test_failures_raise_llm_error_without_the_prompt(r, message):
    with pytest.raises(llm.LlmError, match=message) as info:
        llm.run("SECRET PROMPT", SCHEMA, "m", writing=True, claude="claude", runner=r)
    assert "SECRET" not in str(info.value) and "E" * 201 not in str(info.value)
