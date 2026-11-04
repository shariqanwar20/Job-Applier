import plistlib
import subprocess

from jobapplier.system import launchd

UV = "/Users/me/.local/bin/uv"


def fake_run(calls):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_install_creates_dirs_writes_plist_then_bootout_and_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "my repo & <stuff>"
    repo.mkdir()
    calls = []
    path = launchd.install(repo, UV, run=fake_run(calls))
    assert path == tmp_path / "Library" / "LaunchAgents" / "local.jobapplier.plist"
    assert (repo / "logs").is_dir()
    d = plistlib.loads(path.read_bytes())  # awkward characters in the path survive the XML round trip
    assert d["Label"] == "local.jobapplier" and d["StartInterval"] == 7200 and d["RunAtLoad"] is True
    assert d["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-i",
        UV,
        "run",
        "--project",
        str(repo),
        "python",
        "-m",
        "jobapplier",
        "discover",
    ]
    assert d["WorkingDirectory"] == str(repo)
    assert d["EnvironmentVariables"] == {"PATH": "/Users/me/.local/bin:/usr/bin:/bin"}
    assert d["StandardOutPath"] == d["StandardErrorPath"] == str(repo / "logs" / "jobapplier.log")
    assert [c[0][:2] for c in calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert calls[0][0][2].endswith("/local.jobapplier") and not calls[0][1].get("check")
    assert calls[1][0][3] == str(path) and calls[1][1]["check"] is True


def test_uninstall_boots_out_and_removes_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "local.jobapplier.plist").write_text("x")
    calls = []
    launchd.uninstall(run=fake_run(calls))
    assert [c[0][:2] for c in calls] == [["launchctl", "bootout"]]
    assert not (agents / "local.jobapplier.plist").exists()
    launchd.uninstall(run=fake_run(calls))  # nothing to remove: no error
