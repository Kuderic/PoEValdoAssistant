"""Open overlay links in the dedicated sniper Chrome, not the default browser.

The live searches and Tampermonkey live in Chrome (the only supported
browser), so the userscript installer and the trade-site link must open
there: opening them in a default Firefox installs nothing and searches
nothing. Chrome is found where its installer registers it (App Paths), then
at its usual install locations. Without Chrome the default browser is the
fallback - a link that opens somewhere beats a dead click.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


def chrome_path() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _APP_PATHS) as key:
                path = winreg.QueryValue(key, None)
        except OSError:
            continue
        if path and Path(path).is_file():
            return path
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(base)
        if root:
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return str(candidate)
    return None


# The sniper Chrome: its own profile folder, so it is a separate Chrome
# process that honours these flags even while your normal Chrome is open
# (a second profile made in Chrome's menu shares the running process and
# silently drops them). Live-search tabs are mostly hidden, and Chrome
# otherwise throttles hidden tabs.
SNIPER_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=IntensiveWakeUpThrottling",
    "--no-first-run",
    "--no-default-browser-check",
)


def sniper_profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "ValdoSniperChrome" if base else Path.home() / ".valdo-sniper-chrome"


def is_first_run() -> bool:
    """No sniper profile yet: Tampermonkey and the trade login are still to do."""
    return not sniper_profile_dir().exists()


def open_url(*urls: str) -> str:
    """Open `urls` in the sniper Chrome (same profile every time, so a
    running one just gets new tabs), else the default browser. Returns which
    one was used ("chrome" | "default"), for the log."""
    path = chrome_path()
    if path is not None:
        try:
            # this process exits immediately; a running sniper Chrome takes
            # the URLs as new tabs in its existing window
            subprocess.Popen(
                [path, f"--user-data-dir={sniper_profile_dir()}", *SNIPER_FLAGS, *urls],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return "chrome"
        except OSError:
            pass
    for url in urls:
        webbrowser.open(url)
    return "default"
