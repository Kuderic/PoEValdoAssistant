"""Where things live, for a source checkout and for the packaged exe.

Source checkout (start.bat / `python -m sniper`): everything sits in the repo
folder, exactly as before - config.yaml there is both the defaults and the
user's copy, and per-user state (scoring_overrides.yaml, price_cache.json,
logs/, .env) is written beside it.

Packaged exe (PyInstaller one-file): the shipped config.yaml and the browser
script are bundled INSIDE the exe, so every update brings new defaults (new
pairings, risks, rules) with it. Per-user state lives in the folder holding
ValdoSniper.exe. A config.yaml placed there is optional and is layered over
the bundled defaults key by key, so it only needs what the user changed.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "config.yaml"

USER_CONFIG_TEMPLATE = """\
# Your own Valdo Sniper settings. Anything here overrides the built-in
# defaults key by key; leave a setting out to keep the default, which
# updates with the app. Settings changed in the overlay's settings panel
# are saved to scoring_overrides.yaml instead, not here.
#
# Every available setting, with comments:
#   https://github.com/Kuderic/PoEValdoAssistant/blob/main/config.yaml
#
# Examples:
# league: "Standard"
# alerts:
#   expiry_seconds: 20
"""


def resource(relative: str) -> Path:
    """A file shipped with the app: inside the exe's bundle when packaged,
    in the repo otherwise."""
    base = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT)) if FROZEN else SOURCE_ROOT
    return base / relative


def app_dir() -> Path:
    """The packaged exe's own folder (per-user state is written here)."""
    return Path(sys.executable).resolve().parent


def default_config() -> Path:
    """The shipped config.yaml (bundled copy when packaged)."""
    return resource(CONFIG_NAME)


def prepare_frozen_dir() -> None:
    """Packaged only: run from the exe's folder so the relative paths in the
    config (logs/) and .env resolve there, and drop a commented config.yaml
    template the first time so the user can find where settings go."""
    if not FROZEN:
        return
    os.chdir(app_dir())
    user = app_dir() / CONFIG_NAME
    if not user.exists():
        # a read-only folder is fine: the defaults still work
        with contextlib.suppress(OSError):
            user.write_text(USER_CONFIG_TEMPLATE, encoding="utf-8")
