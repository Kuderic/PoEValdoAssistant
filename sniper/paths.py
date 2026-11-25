"""Where things live, for a source checkout and for the packaged exe.

Source checkout (start.bat / `python -m sniper`): everything sits in the repo
folder, exactly as before - config.yaml there is both the defaults and the
user's copy, and per-user state (scoring_overrides.yaml, price_cache.json,
logs/, .env) is written beside it.

Packaged exe (PyInstaller one-file): the shipped config.yaml and the browser
script are bundled INSIDE the exe, so every update brings new defaults (new
pairings, risks, rules) with it. Per-user state (config.yaml,
scoring_overrides.yaml, price_cache.json, logs/, .env) lives in
%LOCALAPPDATA%/ValdoSniper, NOT beside the exe: people run the exe straight
from Downloads, and an app scattering its files there is a mess. Versions
before 1.3.4 wrote them beside the exe; the first start moves them over
(migrate_state). Only the updater's transient files (.new, .part, .old) stay
beside the exe, because the swap is a rename and must stay on its volume.
The user's config.yaml is optional and is layered over the bundled defaults
key by key, so it only needs what the user changed.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "config.yaml"
APP_FOLDER = "ValdoSniper"
# per-user files a pre-1.3.4 exe kept beside itself (see migrate_state)
STATE_FILES = ("config.yaml", "scoring_overrides.yaml", "price_cache.json", ".env", "logs")

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
    """The packaged exe's own folder (only the updater writes here)."""
    return Path(sys.executable).resolve().parent


def default_config() -> Path:
    """The shipped config.yaml (bundled copy when packaged)."""
    return resource(CONFIG_NAME)


def state_dir() -> Path:
    """Where per-user files live: %LOCALAPPDATA%/ValdoSniper for the exe,
    the repo folder for a source checkout. Without LOCALAPPDATA (never on a
    normal Windows login) the exe falls back to its own folder."""
    if not FROZEN:
        return SOURCE_ROOT
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / APP_FOLDER if base else app_dir()


def migrate_state(old: Path, new: Path) -> list[str]:
    """Move a pre-1.3.4 exe's files from its own folder into `new`.

    Never overwrites: a file already in `new` wins and the old copy stays
    where it is. An untouched config.yaml template is just deleted (a fresh
    one is written in `new`). Returns what moved, for the startup log (this
    runs before logging is set up). A file that cannot be moved (locked,
    read-only) is left behind; the app still starts.
    """
    moved: list[str] = []
    if old.resolve() == new.resolve():
        return moved
    for name in STATE_FILES:
        src, dst = old / name, new / name
        if not src.exists() or dst.exists():
            continue
        try:
            if name == CONFIG_NAME and src.read_text(encoding="utf-8") == USER_CONFIG_TEMPLATE:
                src.unlink()
                continue
            shutil.move(str(src), str(dst))
            moved.append(name)
        except OSError:
            continue
    return moved


def prepare_frozen_dir() -> list[str]:
    """Packaged only: make the state folder, move a previous version's
    files into it, run from it so the relative paths in the config (logs/)
    and .env resolve there, and drop a commented config.yaml template the
    first time so the user can find where settings go. Returns the names
    migrate_state moved."""
    if not FROZEN:
        return []
    state = state_dir()
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        state = app_dir()  # cannot create it: keep the old behaviour
    moved = migrate_state(app_dir(), state)
    os.chdir(state)
    user = state / CONFIG_NAME
    if not user.exists():
        # a read-only folder is fine: the defaults still work
        with contextlib.suppress(OSError):
            user.write_text(USER_CONFIG_TEMPLATE, encoding="utf-8")
    return moved
