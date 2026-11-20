"""Overlay links open in Chrome (where the live searches and Tampermonkey
are), not the Windows default browser - a default Firefox installed nothing."""

from __future__ import annotations

import subprocess
import webbrowser

from sniper import browser


def test_opens_in_chrome_when_installed(monkeypatch, tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    launched, defaulted = [], []
    monkeypatch.setattr(browser, "chrome_path", lambda: str(chrome))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append(args))
    monkeypatch.setattr(webbrowser, "open", lambda url: defaulted.append(url))

    assert browser.open_url("http://127.0.0.1:8765/valdo-sniper.user.js") == "chrome"
    assert launched == [[str(chrome), "http://127.0.0.1:8765/valdo-sniper.user.js"]]
    assert defaulted == []


def test_falls_back_to_the_default_browser_without_chrome(monkeypatch):
    defaulted = []
    monkeypatch.setattr(browser, "chrome_path", lambda: None)
    monkeypatch.setattr(webbrowser, "open", lambda url: defaulted.append(url))
    assert browser.open_url("https://example.invalid") == "default"
    assert defaulted == ["https://example.invalid"]


def test_falls_back_when_chrome_fails_to_launch(monkeypatch):
    defaulted = []

    def broken(*a, **kw):
        raise OSError("gone")

    monkeypatch.setattr(browser, "chrome_path", lambda: r"C:\nowhere\chrome.exe")
    monkeypatch.setattr(subprocess, "Popen", broken)
    monkeypatch.setattr(webbrowser, "open", lambda url: defaulted.append(url))
    assert browser.open_url("https://example.invalid") == "default"
    assert defaulted == ["https://example.invalid"]


def test_install_location_fallback(monkeypatch, tmp_path):
    """No App Paths registration: the usual per-user install is found."""
    import winreg

    def no_key(*a, **kw):
        raise OSError

    monkeypatch.setattr(winreg, "OpenKey", no_key)
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert browser.chrome_path() == str(exe)
