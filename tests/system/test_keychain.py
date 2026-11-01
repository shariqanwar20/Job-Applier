import subprocess

from jobapplier.system.keychain import keychain


def test_keychain_strips_stdout(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "secret\n", ""))
    assert keychain("svc") == "secret"
    assert calls == [["security", "find-generic-password", "-s", "svc", "-w"]]
