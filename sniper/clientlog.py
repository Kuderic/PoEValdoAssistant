"""Follow PoE's own client log to learn when the player changes area.

The game writes a line like

    2026/11/27 12:52:09 129113515 1186a803 [DEBUG Client 32916] Generating
    level 60 area "HideoutTemplarLab" with seed 1

the moment a load starts (about a second before "You have entered ...",
which is localised). The shop marker goes up on the first such line after a
successful travel click: that load is the seller's hideout.

Reading the file is all this does - nothing is sent to the game. The log
sits in <game folder>/logs; LatestClient.txt (this session only, small) is
preferred over the ever-growing Client.txt when both exist. The game folder
comes from the running process, so Steam and standalone installs both work;
`shop_overlay.client_log` in config.yaml overrides the search.

Runs on its own daemon thread: file reads are blocking and must stay off
both the asyncio loop and the Tk thread.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

import psutil

from sniper.logging_setup import event

AREA_RE = re.compile(r'\] Generating level \d+ area "([^"]+)"')
LOG_NAMES = ("LatestClient.txt", "Client.txt")
# a line longer than this is not one we want; keeps a garbage file from
# growing the buffer without bound
MAX_LINE = 4096


def parse_area(line: str) -> str | None:
    """The area id an area-load line names, else None."""
    m = AREA_RE.search(line)
    return m.group(1) if m else None


def logs_in(game_dir: Path) -> Path | None:
    """The log to follow in a game folder: LatestClient.txt when present
    (and not stale next to a fresher Client.txt), else Client.txt."""
    found = []
    for name in LOG_NAMES:
        p = game_dir / "logs" / name
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not found:
        return None
    if len(found) == 2 and found[1][0] - found[0][0] > 60:
        return found[1][1]  # LatestClient.txt is left over from an old session
    return found[0][1]


def find_client_log(process_names, override: str = "") -> Path | None:
    """The running game's client log, or None (game not running, or the
    log could not be found)."""
    if override:
        p = Path(os.path.expandvars(override)).expanduser()
        return p if p.is_file() else None
    names = {n.lower() for n in process_names}
    for proc in psutil.process_iter(["name", "exe"]):
        if (proc.info.get("name") or "").lower() not in names:
            continue
        exe = proc.info.get("exe")
        if exe:
            found = logs_in(Path(exe).parent)
            if found is not None:
                return found
    return None


class ClientLogWatcher:
    """Tails the client log on a daemon thread. `on_area(area_id)` fires
    for every area load; `on_status(path | None)` whenever the followed
    file changes (None: none found). Both are called from the watcher
    thread and must not block."""

    def __init__(
        self,
        process_names,
        on_area: Callable[[str], None],
        on_status: Callable[[str | None], None],
        override: str = "",
        poll_s: float = 0.2,
        search_s: float = 5.0,
    ):
        self._names = tuple(process_names)
        self._override = override
        self._on_area = on_area
        self._on_status = on_status
        self._poll_s = poll_s
        self._search_s = search_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="client-log", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        current: str | None = ""  # "" = nothing reported yet
        while not self._stop.is_set():
            try:
                path = find_client_log(self._names, self._override)
            except Exception as e:  # psutil hiccup: try again later
                event("client_log_search_failed", error=repr(e))
                path = None
            shown = str(path) if path else None
            if shown != current:
                current = shown
                event("client_log", path=shown)
                self._on_status(shown)
            if path is None:
                self._stop.wait(self._search_s)
                continue
            self._follow(path)  # returns when the file goes away or rotates

    def _follow(self, path: Path) -> None:
        """Read new lines from the end of `path` until it disappears, is
        replaced (a new game session recreates LatestClient.txt) or a
        better log turns up."""
        try:
            f = path.open("rb")
        except OSError:
            self._stop.wait(self._search_s)
            return
        with f:
            f.seek(0, os.SEEK_END)
            pending = b""
            last_check = 0.0
            waited = 0.0
            while not self._stop.is_set():
                chunk = f.read()
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > MAX_LINE:
                        pending = b""
                    for raw in lines:
                        area = parse_area(raw.decode("utf-8", errors="replace"))
                        if area is not None:
                            self._on_area(area)
                    continue
                self._stop.wait(self._poll_s)
                waited += self._poll_s
                if waited - last_check < self._search_s:
                    continue
                last_check = waited
                try:
                    size = path.stat().st_size
                except OSError:
                    return  # gone: search again
                if size < f.tell():
                    return  # truncated or recreated: start over at its end
                if find_client_log(self._names, self._override) != path:
                    return  # the game restarted elsewhere, or a fresher log
