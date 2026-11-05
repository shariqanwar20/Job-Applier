"""One Haiku agent per application, in that application's tab. Python reads the page, the model picks the
next actions, and page.act does them under guards in code. The owner submits; this code never does.

Each application is its own loop with no shared conversation: every call gets the profile, resume, job,
the steps so far (refusals included) and the page now.
"""

import functools
import hashlib
import json
import logging
from contextlib import suppress
from pathlib import Path

import pypdf
from playwright.sync_api import Error as PlaywrightError

from jobapplier.apply.browser import LOGIN_NOTE, banner
from jobapplier.apply.page import ClickRules, RefusedError, Snapshot, act, read
from jobapplier.apply.targets import Target
from jobapplier.llm import LlmError
from jobapplier.resume.master import Item, plain

__all__ = ["Application", "describe", "resume_text"]

_log = logging.getLogger("jobapplier")
_FIELD_OPS = frozenset({"fill", "select", "check", "uncheck", "upload"})
_HANDOFFS = frozenset({"needs_account", "needs_owner", "captcha"})  # stops the owner can get past
_LOG_VALUE_MAX = 80  # longer values are free-text answers: the log gets their length only
_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["fill", "select", "check", "uncheck", "upload", "click"],
                    },
                    "ref": {"type": "integer"},
                    "value": {"type": "string"},
                },
                "required": ["op", "ref"],
                "additionalProperties": False,
            },
        },
        "status": {"type": "string", "enum": ["continue", "ready_to_submit", "needs_account", "needs_owner"]},
        "note": {"type": "string"},
    },
    "required": ["actions", "status", "note"],
    "additionalProperties": False,
}
_RULES = """\
You fill in my job application in a web browser. Each turn you get the page as JSON and return the next
actions. Code checks every action and refuses unsafe ones; refusals come back to you under "Steps so far".

Actions, each on an element's ref:
- {"op": "fill", "ref": R, "value": TEXT} types into a text field (it replaces what is there);
- {"op": "select", "ref": R, "value": OPTION} picks an option of a select by its exact text;
- {"op": "check", "ref": R} or {"op": "uncheck", "ref": R} for a checkbox, or check for a radio option;
- {"op": "upload", "ref": R} puts my resume PDF in a file field (the code supplies the file);
- {"op": "click", "ref": R} clicks a button or link.

Rules:
- Fill every field you can from my profile and resume. Leave a field empty when they do not answer it.
- On a job description page, click its button or link that starts the application ("Apply now",
  "Apply for this job", "Start application").
- Upload my resume into the resume or CV field. Do the upload alone in its turn, before the other fields:
  the site's resume parser may fill or overwrite them.
- Answer free-text questions briefly and truthfully from my resume and profile. Never invent experience,
  skills, employers, dates or numbers.
- For voluntary self-identification questions (gender, race, ethnicity, veteran, disability), pick the
  option that declines to answer.
- Tick required consent or acknowledgement boxes (terms, privacy policy, accuracy of my answers). Never tick
  marketing, newsletter, job alert, talent pool or future-contact opt-ins.
- For a custom dropdown, click it or fill its text, then click the option in the next turn.
- When this page is complete, click Next or Continue to go on.
- Never click the final submit. When only submitting is left, return status "ready_to_submit".
- A page that only asks for my email (or phone) to start the application, often saying no account is needed,
  is not a sign-in: fill my email, tick its required consent boxes and click Next.
- Return status "needs_account" only when the page asks for a password, asks me to sign in to an existing
  account, or asks for a verification code sent to my email or phone.
- For a combobox or custom dropdown: type or click it, then click the matching option (role "option") that
  the next page snapshot shows.
- Toggle-style answers (Yes/No buttons, days of the week, a title like Mr.) are clicked; use the question
  text that comes before them on the page.
- When my profile or resume does not answer a required field, leave it and keep doing everything else on
  the page first. Return status "needs_owner" (naming the fields) only when nothing else on the page can
  be done.
- Otherwise return status "continue" with this turn's actions.
- note: one short sentence on what you did or why you stopped.
"""
_STOPS = {
    "ready_to_submit": "ready for you to submit",
    "needs_account": "sign in or create the account yourself",
    "needs_owner": "it needs you",
    "max_steps": "stopped after apply.agent.max_steps ({max_steps}) steps",
    "stuck": "the page did not change for {unchanged_steps} steps",
    "max_cost": "stopped at apply.agent.max_cost_usd (${max_cost_usd:.2f})",
    "llm_error": "the model call failed",
    "page_error": "the page broke",
    "captcha": "a captcha: solve it yourself",
}
_WAIT_LINE = "Do it in this tab; JobApplier continues when the page changes."


def describe(status: str, agent_cfg: dict) -> str:
    return _STOPS.get(status, status).format(**agent_cfg)


def resume_text(items: list[Item]) -> str:
    out, section = [], None
    for i in items:
        if i.section != section:
            section = i.section
            out += [""] * bool(out) + [section]
        out.append(f"- {plain(i.text)}")
    return "\n".join(out)


@functools.cache
def _pdf_text(pdf: Path | None) -> str:
    try:
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(pdf).pages).strip() if pdf else ""
    except (OSError, pypdf.errors.PyPdfError):
        return ""


def _prompt(target: Target, cfg: dict, values: dict, resume: str, history: list[str], snap: Snapshot) -> str:
    profile = "\n".join(f"- {k}: {v}" for k, v in values.items() if v)
    resume = _pdf_text(target.pdf) or resume  # the tailored PDF has employers and dates; master lines do not
    description = target.description[: cfg["apply"]["agent"]["description_chars"]]
    steps = "\n".join(history) or "(none: this is the first step)"
    return (
        f"{_RULES}\nMy profile:\n{profile}\n\nMy resume:\n{resume}\n\n"
        f"Job: {target.title} at {target.company}\n\nJob description:\n{description}\n\n"
        f"Steps so far:\n{steps}\n\nThe page now (JSON):\n{snap.text}\n"
    )


def _settle(page, cfg: dict, uploaded: bool = False) -> None:
    a = cfg["apply"]
    with suppress(PlaywrightError):  # pages with live chat or analytics never finish loading
        page.wait_for_load_state("load", timeout=a["load_timeout_seconds"] * 1000)
    page.wait_for_timeout((a["agent"]["settle_seconds"] + a["parser_wait_seconds"] * uploaded) * 1000)


def _line(action: dict, snap: Snapshot, short: bool = False) -> str:
    """What an action was, as "fill 5 (Why us?) = TEXT", for the history, the trace and (short) the log."""
    ref, value = action.get("ref"), action.get("value")
    where = f" ({snap.label(ref)})" if isinstance(ref, int) and ref in snap.refs else ""
    if action.get("op") not in ("fill", "select") or not isinstance(value, str):
        value = None
    elif short and len(value) > _LOG_VALUE_MAX:
        value = f"<{len(value)} chars>"
    return f"{action.get('op')} {ref}{where}" + (f" = {value}" if value is not None else "")


def _digest(snap: Snapshot) -> str:
    return hashlib.sha256(snap.text.encode()).hexdigest()


class Application:
    """One application's loop in its tab. advance() runs it to its next stop; at a hand-off stop
    (waiting), wait_for_owner() waits for the owner to do the step, and advance() goes on with the same
    history, step count and cost. finish() leaves the final note, trace and result.

    llm_run(prompt, schema, model, writing) returns (answer, cost in USD), like llm.run_with_cost.
    """

    def __init__(
        self, page, target: Target, cfg: dict, values: dict, resume: str, llm_run, rules: ClickRules, log_dir
    ):
        self.page, self.target, self.cfg, self.values, self.resume = page, target, cfg, values, resume
        self.llm_run, self.rules, self.log_dir = llm_run, rules, Path(log_dir)
        self.history, self.steps, self.handoffs = [], [], []
        self.filled, self.answers, self.uploaded, self.refused, self.cost = [], [], False, 0, 0.0
        self.status, self.note, self.n, self.started, self.final = None, "", 0, False, False
        self.snap, self.digests, self.base = None, [], None  # base: the page when it stopped for the owner

    @property
    def waiting(self) -> bool:
        ag = self.cfg["apply"]["agent"]
        return self.status in _HANDOFFS and not self.final and len(self.handoffs) < ag["max_handoffs"]

    def advance(self) -> None:
        """Runs the loop until it stops; self.status says why. Never on a submit button."""
        a = self.cfg["apply"]
        self.status, self.note = None, ""
        try:
            if not self.started:
                self.started = True
                self.page.goto(
                    self.target.url, wait_until="domcontentloaded", timeout=a["load_timeout_seconds"] * 1000
                )
                _settle(self.page, self.cfg)
            while self.status is None:
                self._turn()
        except PlaywrightError as exc:  # the page navigated, closed or redrew under the loop
            self.status, self.note = "page_error", f"{type(exc).__name__}: {str(exc)[:200]}"
        self._stopped()

    def _turn(self) -> None:
        """One step: a snapshot, the stop checks, one model call and its actions. Sets status to stop."""
        ag = self.cfg["apply"]["agent"]
        if self.n >= ag["max_steps"]:
            self.status = "max_steps"
            return
        self.n += 1
        snap = self.snap = read(self.page, ag)
        self.digests = [*self.digests, _digest(snap)][-ag["unchanged_steps"] :]
        if snap.password:
            self.status = "needs_account"
        elif snap.captcha:
            self.status, self.note = "captcha", "solve the captcha"
        elif len(self.digests) == ag["unchanged_steps"] and len(set(self.digests)) == 1:
            self.status = "stuck"
        elif self.cost >= ag["max_cost_usd"]:
            self.status = "max_cost"
        if self.status:
            return
        prompt = _prompt(self.target, self.cfg, self.values, self.resume, self.history, snap)
        try:
            got, cost = self.llm_run(prompt, _SCHEMA, self.cfg["llm"]["models"]["apply"], writing=False)
        except LlmError as exc:
            self.status, self.note = "llm_error", str(exc)
            return
        before, uploaded = list(self.page.context.pages), self.uploaded
        if self._step(snap, got, cost):
            self.status, self.note = "needs_account", "a password or verification code field"
        elif got.get("status", "continue") != "continue":
            self.status, self.note = got["status"], got.get("note", "")
        else:
            opened = [p for p in self.page.context.pages if p not in before]
            if opened:  # an Apply link that opens the form in a new tab
                self.page = opened[-1]
                self.history.append(f"step {self.n}: a new tab opened ({self.page.url}); going on there")
            _settle(self.page, self.cfg, uploaded=self.uploaded and not uploaded)

    def _stopped(self) -> None:
        """At every stop: a screenshot, the trace so far, and for a hand-off the page to compare with."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._write_trace()
        with suppress(PlaywrightError):  # a broken page may take none; the terminal and trace say it
            path = self.log_dir / f"apply-{self.target.job_id}-{self.n}.png"
            self.page.screenshot(path=str(path), full_page=True)
        self.base = None
        if self.status in _HANDOFFS:
            with suppress(PlaywrightError):  # after the model's own actions: they are not the owner's
                self.base = _digest(read(self.page, self.cfg["apply"]["agent"]))

    def ask_owner(self) -> None:
        with suppress(PlaywrightError):
            self.page.bring_to_front()
            what = (self.note or describe(self.status, self.cfg["apply"]["agent"])).rstrip(".")
            banner(self.page, f"JobApplier needs you: {what}.", [_WAIT_LINE])

    def wait_for_owner(self) -> bool:
        """Polls until the page has changed from where it stopped and stayed the same for one poll (the
        owner is done, not mid-typing), with no password field or captcha left. True: the owner did the
        step and the loop may go on. False: the wait timed out, and the application ends as needs_owner.
        The owner, never this code, solves the captcha or types the code."""
        ag = self.cfg["apply"]["agent"]
        self.ask_owner()
        polls, last, waited = max(1, int(ag["wait_for_owner_seconds"] / ag["poll_seconds"])), None, 0
        for i in range(1, polls + 1):
            self.page.wait_for_timeout(ag["poll_seconds"] * 1000)
            waited = i * ag["poll_seconds"]
            try:
                snap = read(self.page, ag)
            except PlaywrightError:  # a reload in progress
                last = None
                continue
            now = _digest(snap)
            if now != self.base and now == last and not (snap.password or snap.captcha):
                self._handoff(waited, resumed=True)
                self.history.append(f"step {self.n}: the owner did the {self.status} step")
                self.digests = []
                return True
            last = now
        self._handoff(waited, resumed=False)
        self.note = f"no change in {waited}s after: {self.note or describe(self.status, ag)}"
        self.status, self.final = "needs_owner", True
        return False

    def _handoff(self, waited: float, resumed: bool) -> None:
        record = {"step": self.n, "status": self.status, "note": self.note}
        self.handoffs.append(record | {"waited_seconds": waited, "resumed": resumed})
        _log.info(
            "apply %s: hand-off at step %d (%s), waited %ss", self.target.job_id, self.n, self.status, waited
        )

    def _write_trace(self) -> Path:
        # The trace holds the model's answers but never the profile or resume it was given.
        t = self.target
        trace = self.log_dir / f"apply-{t.job_id}.json"
        record = {
            "job": t.job_id,
            "company": t.company,
            "url": t.url,
            "status": self.status,
            "note": self.note,
        }
        record |= {"cost": self.cost, "handoffs": self.handoffs, "steps": self.steps}
        trace.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        return trace

    def finish(self) -> dict:
        """The final note on the page, the trace and the result: {"status", "note", "filled": n, "missing":
        [labels], "answers": [(label, text)], "steps": n, "cost": usd}."""
        ag = self.cfg["apply"]["agent"]
        try:
            missing = read(self.page, ag).missing
        except PlaywrightError:
            missing = self.snap.missing if self.snap else []
        _log.info("apply %s: %s after %d steps, $%.4f", self.target.job_id, self.status, self.n, self.cost)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        trace = self._write_trace()
        with suppress(PlaywrightError):
            head = (
                LOGIN_NOTE if self.status == "needs_account" else f"JobApplier: {describe(self.status, ag)}."
            )
            lines = _banner_lines(self, self.note, missing, trace)
            banner(self.page, f"{head} Check every page, then submit yourself.", lines)
        return {
            "status": self.status,
            "note": self.note,
            "filled": len(self.filled),
            "missing": missing,
            "answers": self.answers,
            "steps": self.n,
            "cost": self.cost,
        }

    def _step(self, snap: Snapshot, got: dict, cost: float) -> bool:
        """Does the step's actions; True when one was refused as a sign-in or account step."""
        n, page, rules, values = self.n, self.page, self.rules, self.values
        timeout = self.cfg["apply"]["field_timeout_seconds"] * 1000
        actions = got.get("actions") or []
        record = {
            "step": n,
            # the page as the model saw it (page content only, never the profile or resume): for debugging
            "snapshot": {"url": snap.view["url"], "elements": len(snap.view["elements"]), "view": snap.view},
            "asked": actions,
            "done": [],
            "refused": [],
            "failed": [],
            "status": got.get("status"),
            "note": got.get("note", ""),
            "cost": cost,
        }
        self.steps.append(record)
        self.cost += cost
        account, logged = False, []
        for action in actions:
            if not isinstance(action, dict):
                action = {"op": repr(action)}
            line = _line(action, snap)
            try:
                act(page, snap, action, self.target.pdf, rules, len(self.filled), timeout)
            except RefusedError as exc:
                self.refused += 1
                record["refused"].append(f"{line}: {exc}")
                self.history.append(f"step {n}: REFUSED {line}: {exc}")
                logged.append(f"refused {_line(action, snap, short=True)}: {exc}")
                if exc.account:
                    account = True
                    break
                continue
            except PlaywrightError as exc:  # a timeout or a redraw: the model hears of it and may retry
                record["failed"].append(f"{line}: {type(exc).__name__}")
                self.history.append(f"step {n}: FAILED {line}: {type(exc).__name__}")
                logged.append(f"failed {_line(action, snap, short=True)}")
                continue
            record["done"].append(line)
            self.history.append(f"step {n}: done {line}")
            logged.append(_line(action, snap, short=True))
            if action["op"] in _FIELD_OPS:
                label = snap.label(action["ref"])
                self.filled.append(label)
                self.uploaded = self.uploaded or action["op"] == "upload"
                value = action.get("value")
                if action["op"] == "fill" and value not in values.values():
                    self.answers.append((label, value))
        _log.info(
            "apply %s step %d: %s; cost $%.4f (total $%.4f)",
            self.target.job_id,
            n,
            "; ".join(logged) or "no actions",
            cost,
            self.cost,
        )
        return account


def _banner_lines(run: Application, note: str, missing: list[str], trace: Path) -> list[str]:
    t = run.target
    lines = [f"Note: {note}"] if note else []
    if run.filled:
        lines.append(f"Filled {len(run.filled)}: {', '.join(run.filled)}")
    if run.uploaded:
        lines.append(f"Uploaded {t.pdf.name}.")
    else:
        lines.append(f"No resume uploaded: upload {t.pdf} yourself if the form asks for it.")
    if run.answers:
        lines.append("Answers Haiku wrote; check each one:")
        lines += [f"  {label}: {text}" for label, text in run.answers]
    if missing:
        lines.append(f"Required and still empty: {', '.join(missing)}")
    if run.refused:
        lines.append(f"The guards refused {run.refused} actions; see {trace}.")
    return lines
