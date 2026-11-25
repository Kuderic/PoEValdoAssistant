"""The exe keeps its per-user files in %LOCALAPPDATA%/ValdoSniper, not
beside itself (people run it from Downloads), and moves an older version's
files over on first start."""

from __future__ import annotations

import os
import sys

import pytest

from sniper import paths


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """A packaged run: exe in Downloads, LOCALAPPDATA in tmp."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    local = tmp_path / "Local"
    local.mkdir()
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(downloads / "ValdoSniper.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.chdir(tmp_path)  # restored after the test; prepare_frozen_dir chdirs
    return downloads, local / "ValdoSniper"


def test_the_exe_keeps_its_files_in_localappdata(frozen):
    downloads, state = frozen
    assert paths.state_dir() == state
    assert paths.prepare_frozen_dir() == []
    assert os.getcwd() == str(state)
    assert (state / "config.yaml").read_text(encoding="utf-8") == paths.USER_CONFIG_TEMPLATE
    assert sorted(p.name for p in downloads.iterdir()) == []


def test_a_previous_versions_files_move_out_of_downloads(frozen):
    downloads, state = frozen
    (downloads / "ValdoSniper.exe").write_bytes(b"exe")
    (downloads / "scoring_overrides.yaml").write_text("min_yield: 4\n", encoding="utf-8")
    (downloads / "price_cache.json").write_text("{}", encoding="utf-8")
    (downloads / ".env").write_text("POESESSID=x\n", encoding="utf-8")
    (downloads / "logs").mkdir()
    (downloads / "logs" / "sniper-20261125.jsonl").write_text("{}\n", encoding="utf-8")
    (downloads / "config.yaml").write_text(paths.USER_CONFIG_TEMPLATE, encoding="utf-8")

    moved = paths.prepare_frozen_dir()

    assert moved == ["scoring_overrides.yaml", "price_cache.json", ".env", "logs"]
    # only the exe is left in Downloads; the untouched template was deleted
    assert sorted(p.name for p in downloads.iterdir()) == ["ValdoSniper.exe"]
    assert (state / "scoring_overrides.yaml").read_text(encoding="utf-8") == "min_yield: 4\n"
    assert (state / "logs" / "sniper-20261125.jsonl").exists()
    assert (state / "config.yaml").exists()


def test_an_edited_config_moves_too(frozen):
    downloads, state = frozen
    (downloads / "config.yaml").write_text('league: "Standard"\n', encoding="utf-8")
    assert paths.prepare_frozen_dir() == ["config.yaml"]
    assert (state / "config.yaml").read_text(encoding="utf-8") == 'league: "Standard"\n'


def test_a_file_already_in_the_new_folder_is_never_overwritten(frozen):
    downloads, state = frozen
    state.mkdir(parents=True)
    (state / "scoring_overrides.yaml").write_text("min_yield: 9\n", encoding="utf-8")
    (downloads / "scoring_overrides.yaml").write_text("min_yield: 4\n", encoding="utf-8")
    assert paths.prepare_frozen_dir() == []
    assert (state / "scoring_overrides.yaml").read_text(encoding="utf-8") == "min_yield: 9\n"
    assert (downloads / "scoring_overrides.yaml").exists()  # left alone, not lost


def test_without_localappdata_the_exe_folder_is_the_fallback(frozen, monkeypatch):
    downloads, _ = frozen
    monkeypatch.delenv("LOCALAPPDATA")
    assert paths.state_dir() == downloads
    assert paths.migrate_state(downloads, downloads) == []


def test_a_source_checkout_is_unchanged(monkeypatch):
    monkeypatch.setattr(paths, "FROZEN", False)
    assert paths.state_dir() == paths.SOURCE_ROOT
    assert paths.prepare_frozen_dir() == []


def test_the_settings_panel_opens_the_folder(monkeypatch, tmp_path):
    import tkinter as tk
    from dataclasses import replace

    from conftest import make_config

    from sniper import overlay as overlay_mod
    from sniper.bus import Bus

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    opened = []
    monkeypatch.setattr(overlay_mod.os, "startfile", opened.append, raising=False)
    ov = overlay_mod.Overlay(root, Bus(), replace(make_config(), state_dir=tmp_path))
    ov._open_tuning()
    note = ov._open_state_folder()
    assert opened == [tmp_path.resolve()]
    assert note["fg"] == overlay_mod.GOOD and str(tmp_path.resolve()) in note["text"]
    root.destroy()
