"""Fast second-launch path; bootstrap only when no authenticated instance answers."""

from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

try:
    from classcue.desktop_runtime import SingleInstance
    SingleInstance(PROJECT / ".runtime", lambda _action: None).send("activate", attempts=2)
except Exception:
    subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", str(PROJECT / "scripts" / "desktop_bootstrap.ps1")],
        cwd=PROJECT, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
