"""A desktop notification after a run, only when something happened.

It tells you when the run needs you (a question only you can answer, a form
the bot can't fill, a sign-in or security check, resumes waiting for your
Approve) and, unless notify.applied is false, which applications were sent.
A run where nothing changed stays quiet. Sending is best effort: a missing
notifier never fails a run.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

from jobright import config


def summarize(results: list, ready: int = 0, new_questions: int = 0,
              include_applied: bool = True) -> tuple[str, str] | None:
    """(title, body) for one run's results, or None when there is nothing to say."""
    applied = [job for job, o in results if o.status == "applied"]
    needs = [job for job, o in results if o.status == "needs_you"]
    blocked = sorted({o.detail for _, o in results if o.status == "blocked"})
    lines = []
    if blocked:
        lines.append("Paused: " + "; ".join(blocked))
    if needs:
        lines.append(f"{len(needs)} application(s) need you: " + _names(needs))
    if new_questions:
        lines.append(f"{new_questions} new question(s) to answer")
    if ready:
        lines.append(f"{ready} resume(s) waiting for your Approve")
    wants_you = bool(lines)
    if applied and include_applied:
        lines.append(f"Applied to {len(applied)}: " + _names(applied))
    if not lines:
        return None
    title = "jobright needs you" if wants_you else f"jobright applied to {len(applied)} job(s)"
    return title, "\n".join(lines)


def _names(jobs: list[dict], most: int = 3) -> str:
    names = [j["company"] for j in jobs[:most]]
    return ", ".join(names) + (f" +{len(jobs) - most} more" if len(jobs) > most else "")


def after_run(results: list, ready: int = 0, new_questions: int = 0, log=print) -> None:
    s = config.settings().get("notify") or {}
    if not s.get("enabled", True):
        return
    msg = summarize(results, ready, new_questions, include_applied=s.get("applied", True))
    if msg:
        port = config.settings()["dashboard"]["port"]
        send(msg[0], f"{msg[1]}\nOpen http://127.0.0.1:{port}", log)


def send(title: str, body: str, log=print) -> bool:
    try:
        system = platform.system()
        if system == "Darwin":
            script = f"display notification {_applescript(body)} with title {_applescript(title)} sound name \"default\""
            subprocess.run(["osascript", "-e", script], check=True, timeout=15, capture_output=True)
        elif system == "Windows":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  "$n.Visible=$true;$n.ShowBalloonTip(15000,$env:JR_TITLE,$env:JR_BODY,'Info');"
                  "Start-Sleep 16;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             env={**os.environ, "JR_TITLE": title, "JR_BODY": body})
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "jobright", title, body], check=True, timeout=15)
        else:
            return False
        return True
    except Exception as e:
        log(f"  notification not shown: {type(e).__name__}")
        return False


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
