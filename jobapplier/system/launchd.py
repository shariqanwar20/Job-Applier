import os
import plistlib
import subprocess
from pathlib import Path

__all__ = ["install", "uninstall"]

_LABEL = "local.jobapplier"


def _plist(repo: Path, uv: str) -> dict:
    log = str(repo / "logs" / "jobapplier.log")
    return {
        "Label": _LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-i",
            uv,
            "run",
            "--project",
            str(repo),
            "python",
            "-m",
            "jobapplier",
            "discover",
        ],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/usr/bin:/bin"},
        "StartInterval": 7200,
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _bootout(run) -> None:
    run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{_LABEL}"], check=False, capture_output=True
    )  # fails when not loaded yet


def install(repo: Path, uv: str, run=subprocess.run) -> Path:
    path = _agent_path()
    (repo / "logs").mkdir(exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(_plist(repo, uv), f)
    _bootout(run)  # so a reinstall replaces the loaded agent
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return path


def uninstall(run=subprocess.run) -> None:
    _bootout(run)
    _agent_path().unlink(missing_ok=True)
