"""Headless `claude -p` calls (spec §9): one JSON answer per call, shaped by a JSON schema."""

import json
import logging
import subprocess
from pathlib import Path

__all__ = ["LlmError", "run"]

_ROOT = Path(__file__).resolve().parents[1]  # writing calls load the repo's .claude/skills from here
_log = logging.getLogger("jobapplier")
# Writing calls may use only the Skill tool (humanizer, asd-ste100); classification calls load nothing.
_WRITING = ["--setting-sources", "project", "--tools", "Skill"]
_CLASSIFY = ["--setting-sources", "", "--tools", ""]


class LlmError(Exception):
    pass


def run(
    prompt: str,
    schema: dict,
    model: str,
    writing: bool,
    claude: str,
    runner=subprocess.run,
    timeout: int = 300,
) -> dict:
    """Returns the schema-shaped answer. Errors never carry the prompt, which holds the owner's resume."""
    argv = [claude, "-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(schema)]
    argv += [*(_WRITING if writing else _CLASSIFY), "--strict-mcp-config", "--no-session-persistence"]
    try:
        done = runner(argv, input=prompt, capture_output=True, text=True, cwd=_ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LlmError(f"claude timed out after {timeout}s") from None
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    if done.returncode != 0:
        why = (out or {}).get("subtype") if isinstance(out, dict) else None
        raise LlmError(f"claude exited {done.returncode}: {why or done.stderr[:200]}")
    if not isinstance(out, dict):
        raise LlmError("claude: the output is not JSON")
    if out.get("is_error"):
        raise LlmError(f"claude error: {out.get('subtype')}")
    if not isinstance(out.get("structured_output"), dict):
        raise LlmError(f"claude: no structured_output ({out.get('subtype')})")
    _log.info("claude %s cost $%s", model, out.get("total_cost_usd"))
    return out["structured_output"]
