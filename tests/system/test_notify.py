from jobapplier.system import notify as notify_mod


def test_notify_quotes_strings(monkeypatch):
    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    notify_mod.notify('Job"Applier', 'say "hi"')
    assert (
        calls[0][:2] == ["osascript", "-e"]
        and '"Job\\"Applier"' in calls[0][2]
        and '"say \\"hi\\""' in calls[0][2]
    )
