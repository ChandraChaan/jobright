"""What the after-run notification says, and when it stays quiet."""

from jobright import notify
from jobright.apply import Outcome


def _job(company):
    return {"id": company, "company": company, "title": "Flutter Developer"}


def test_quiet_when_nothing_happened():
    assert notify.summarize([]) is None
    assert notify.summarize([(_job("Acme"), Outcome("unavailable", "removed"))]) is None


def test_applied_only():
    title, body = notify.summarize([(_job("Acme"), Outcome("applied", "Easy Apply submitted"))])
    assert title == "jobright applied to 1 job(s)"
    assert "Applied to 1: Acme" in body
    assert notify.summarize([(_job("Acme"), Outcome("applied"))], include_applied=False) is None


def test_needs_you_leads():
    results = [(_job(c), Outcome("needs_you", "questions")) for c in ("A", "B", "C", "D")]
    results += [(_job("E"), Outcome("blocked", "not signed in to Naukri")),
                (_job("F"), Outcome("applied"))]
    title, body = notify.summarize(results, ready=2, new_questions=1)
    assert title == "jobright needs you"
    assert body.splitlines() == ["Paused: not signed in to Naukri", "4 application(s) need you: A, B, C +1 more",
                                 "1 new question(s) to answer", "2 resume(s) waiting for your Approve",
                                 "Applied to 1: F"]


def test_disabled_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(notify.config, "settings", lambda: {"notify": {"enabled": False}, "dashboard": {"port": 8765}})
    notify.after_run([(_job("Acme"), Outcome("applied"))])
    assert sent == []
    monkeypatch.setattr(notify.config, "settings", lambda: {"notify": {}, "dashboard": {"port": 8765}})
    notify.after_run([(_job("Acme"), Outcome("applied"))])
    assert sent and sent[0][0] == "jobright applied to 1 job(s)"


def test_applescript_quoting():
    assert notify._applescript('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'
