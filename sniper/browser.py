"""Open overlay links in Chrome, not the Windows default browser.

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


def open_url(url: str) -> str:
    """Open `url` in Chrome if installed, else the default browser. Returns
    which one was used ("chrome" | "default"), for the log."""
    path = chrome_path()
    if path is not None:
        try:
            # a running Chrome takes the URL as a new tab in its existing
            # window; this process exits immediately either way
            subprocess.Popen(
                [path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True
            )
            return "chrome"
        except OSError:
            pass
    webbrowser.open(url)
    return "default"
