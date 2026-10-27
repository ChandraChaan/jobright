"""Run jobright on a schedule, and keep the dashboard up, on macOS, Windows or Linux.

macOS: launchd agents in ~/Library/LaunchAgents.
Windows: Task Scheduler tasks (schtasks), one every N hours and one at logon.
Linux: systemd user units (timer + service), falling back to crontab.
Runs happen while you are logged in; the computer must be awake.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from jobright import config

RUN_LABEL = "com.jobright.run"
DASH_LABEL = "com.jobright.dashboard"
_ENV = {"JOBRIGHT_HOME": str(config.DATA)}


def _cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "jobright", *args]


# --- macOS ---------------------------------------------------------------------------

AGENTS = Path.home() / "Library" / "LaunchAgents"


def _plist(label: str, args: list[str], **extra) -> Path:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": label,
        "ProgramArguments": _cmd(*args),
        "WorkingDirectory": str(config.DATA),
        "StandardOutPath": str(config.LOGS_DIR / f"{label}.log"),
        "StandardErrorPath": str(config.LOGS_DIR / f"{label}.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
                                 "PYTHONUNBUFFERED": "1", **_ENV},
        # launchd's default is 256 open files; Chrome alone can use most of that.
        "SoftResourceLimits": {"NumberOfFiles": 8192},
        "HardResourceLimits": {"NumberOfFiles": 8192},
        **extra,
    }
    AGENTS.mkdir(parents=True, exist_ok=True)
    path = AGENTS / f"{label}.plist"
    with path.open("wb") as f:
        plistlib.dump(data, f)
    return path


def _launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], check=False, capture_output=True)


def _mac_install(every_hours: float, dashboard: bool) -> list[str]:
    domain = f"gui/{os.getuid()}"
    done = []
    run = _plist(RUN_LABEL, ["run"], StartInterval=int(every_hours * 3600), RunAtLoad=False)
    _launchctl("bootout", domain, str(run))
    _launchctl("bootstrap", domain, str(run))
    done.append(str(run))
    if dashboard:
        dash = _plist(DASH_LABEL, ["dashboard"], KeepAlive=True, RunAtLoad=True)
        _launchctl("bootout", domain, str(dash))
        _launchctl("bootstrap", domain, str(dash))
        done.append(str(dash))
    return done


def _mac_remove() -> None:
    domain = f"gui/{os.getuid()}"
    for label in (RUN_LABEL, DASH_LABEL):
        path = AGENTS / f"{label}.plist"
        if path.exists():
            _launchctl("bootout", domain, str(path))
            path.unlink()


# --- Windows -------------------------------------------------------------------------

def _win_wrapper(name: str, args: list[str]) -> Path:
    """A .cmd that sets JOBRIGHT_HOME and logs, for Task Scheduler to start."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA / f"{name}.cmd"
    exe = sys.executable.replace("python.exe", "pythonw.exe") if name == "dashboard" else sys.executable
    path.write_text(f'@echo off\r\nset JOBRIGHT_HOME={config.DATA}\r\nset PYTHONUNBUFFERED=1\r\n'
                    f'"{exe}" -m jobright {" ".join(args)} >> "{config.LOGS_DIR / (name + ".log")}" 2>&1\r\n')
    return path


def _win_install(every_hours: float, dashboard: bool) -> list[str]:
    run = _win_wrapper("run", ["run"])
    subprocess.run(["schtasks", "/Create", "/F", "/TN", "jobright run", "/TR", f'"{run}"',
                    "/SC", "HOURLY", "/MO", str(max(1, int(every_hours)))], check=False)
    done = ["jobright run (Task Scheduler)"]
    if dashboard:
        dash = _win_wrapper("dashboard", ["dashboard"])
        subprocess.run(["schtasks", "/Create", "/F", "/TN", "jobright dashboard", "/TR", f'"{dash}"', "/SC", "ONLOGON"], check=False)
        subprocess.Popen(["cmd", "/c", str(dash)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        done.append("jobright dashboard (Task Scheduler, at logon)")
    return done


def _win_remove() -> None:
    for name in ("jobright run", "jobright dashboard"):
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], check=False, capture_output=True)


# --- Linux ---------------------------------------------------------------------------

UNITS = Path.home() / ".config" / "systemd" / "user"


def _linux_install(every_hours: float, dashboard: bool) -> list[str]:
    env = " ".join(f'"{k}={v}"' for k, v in {**_ENV, "PYTHONUNBUFFERED": "1"}.items())
    cmd = " ".join(_cmd("run"))
    if shutil.which("systemctl"):
        UNITS.mkdir(parents=True, exist_ok=True)
        (UNITS / "jobright-run.service").write_text(f"[Unit]\nDescription=jobright run\n\n[Service]\nType=oneshot\n"
                                                     f"Environment={env}\nExecStart={cmd}\nLimitNOFILE=8192\n")
        (UNITS / "jobright-run.timer").write_text(f"[Unit]\nDescription=jobright every {every_hours:g}h\n\n[Timer]\n"
                                                   f"OnBootSec=10min\nOnUnitActiveSec={int(every_hours * 60)}min\n\n"
                                                   "[Install]\nWantedBy=timers.target\n")
        done = ["jobright-run.timer (systemd --user)"]
        units = ["jobright-run.timer"]
        if dashboard:
            (UNITS / "jobright-dashboard.service").write_text(
                f"[Unit]\nDescription=jobright dashboard\n\n[Service]\nEnvironment={env}\n"
                f"ExecStart={' '.join(_cmd('dashboard'))}\nRestart=always\n\n[Install]\nWantedBy=default.target\n")
            units.append("jobright-dashboard.service")
            done.append("jobright-dashboard.service (systemd --user)")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", *units], check=False)
        return done
    line = f"0 */{max(1, int(every_hours))} * * * JOBRIGHT_HOME={config.DATA} {cmd} >> {config.LOGS_DIR}/run.log 2>&1"
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [l for l in current.splitlines() if "jobright run" not in l] + [line]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=False)
    return ["crontab entry"]


def _linux_remove() -> None:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "jobright-run.timer", "jobright-dashboard.service"],
                       check=False, capture_output=True)
        for name in ("jobright-run.service", "jobright-run.timer", "jobright-dashboard.service"):
            (UNITS / name).unlink(missing_ok=True)
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "jobright run" in current:
        lines = [l for l in current.splitlines() if "jobright run" not in l]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=False)


# --- public ----------------------------------------------------------------------------

def install(every_hours: float, dashboard: bool = True) -> list[str]:
    system = platform.system()
    if system == "Darwin":
        return _mac_install(every_hours, dashboard)
    if system == "Windows":
        return _win_install(every_hours, dashboard)
    return _linux_install(every_hours, dashboard)


def uninstall() -> None:
    {"Darwin": _mac_remove, "Windows": _win_remove}.get(platform.system(), _linux_remove)()


def status() -> str:
    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        lines = [l for l in out.splitlines() if "com.jobright" in l]
        return "\n".join(lines) or "Not scheduled."
    if system == "Windows":
        out = subprocess.run(["schtasks", "/Query", "/FO", "LIST"], capture_output=True, text=True).stdout
        return "\n".join(l for l in out.splitlines() if "jobright" in l) or "Not scheduled."
    if shutil.which("systemctl"):
        return subprocess.run(["systemctl", "--user", "list-timers", "jobright-run.timer"], capture_output=True, text=True).stdout
    return subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout or "Not scheduled."
