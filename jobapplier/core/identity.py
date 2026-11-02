import re
from urllib.parse import parse_qs, urlsplit

from jobapplier.core.rules import Rules

_GREENHOUSE_HOST = re.compile(r"(job-)?boards(\.eu)?\.greenhouse\.io")
_GREENHOUSE_PATH = re.compile(r"/([^/]+)/jobs/([^/]+)")
_LEVER_PATH = re.compile(r"/([^/]+)/([^/]+)")
_WORKDAY_HOST = re.compile(r"([^.]+)\.wd\d+\.myworkdayjobs\.com")


def posting_identity(url: str, rules: Rules) -> str | None:
    """Stable ID for the same job seen via different sources; None when the URL can't identify one."""
    parts = urlsplit(url.strip().lower())
    host, path = parts.hostname or "", parts.path.rstrip("/")
    if not host or any(a in host for a in rules.aggregators):
        return None
    if _GREENHOUSE_HOST.fullmatch(host):
        if m := _GREENHOUSE_PATH.match(path):
            return f"greenhouse:{m[1]}:{m[2]}"
        q = parse_qs(parts.query)
        if path == "/embed/job_app" and "for" in q and "token" in q:
            return f"greenhouse:{q['for'][0]}:{q['token'][0]}"
    elif host in ("jobs.lever.co", "jobs.ashbyhq.com") and (m := _LEVER_PATH.match(path)):
        return f"{'lever' if 'lever' in host else 'ashby'}:{m[1]}:{m[2]}"
    elif m := _WORKDAY_HOST.fullmatch(host):
        segments = [s for s in path.split("/") if s]
        if segments and segments[-1] == "apply":
            segments.pop()
        if segments:
            return f"workday:{m[1]}:{segments[-1]}"
    return f"url:{host}{path}"
