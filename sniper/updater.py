"""Self-update from GitHub Releases.

The app asks the GitHub API for the latest release every 15 min (by default)
(one small, ETag-conditional request: a 304 does not count against the
unauthenticated 60/hour limit). When the release is newer than this build it
downloads the release's ValdoSniper.exe in the background, verifies its size
and SHA-256 (GitHub publishes a digest for every release asset), and stages
it beside the running exe as ValdoSniper.exe.new. Nothing is replaced while
the app runs: the staged exe is swapped in when the user clicks the overlay's
update pill, or at the next launch.

The swap relies on Windows letting a running exe be RENAMED (not deleted):
ValdoSniper.exe -> ValdoSniper.exe.old, .new -> ValdoSniper.exe, then the new
exe is started with --wait-pid so it binds the websocket port only once this
process has exited, and deletes the .old file.

A source checkout cannot replace itself (git or a ZIP owns those files), so
there the updater only announces the new version.

Rate limits (CLAUDE.md): every response's Retry-After / X-RateLimit-* headers
are honoured, and a 403/429/5xx or transport error backs off exponentially.
The User-Agent carries no personal details.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from sniper import __version__
from sniper.bus import UpdateStatus
from sniper.config import UpdatesConfig
from sniper.logging_setup import event

ASSET_NAME = "ValdoSniper.exe"
USER_AGENT = f"valdo-sniper-updater/{__version__}"
# give startup (server, pricing warm-up) the network first
STARTUP_DELAY_S = 20.0
BACKOFF_BASE_S = 300.0
BACKOFF_CAP_S = 6 * 3600.0
# a release asset smaller than this is not a real build
MIN_ASSET_BYTES = 1_000_000
# command-line flags the relaunch adds; stripped before passing args on
RELAUNCH_FLAGS = ("--wait-pid", "--updated-from")


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(candidate: str, current: str) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and cur is not None and new > cur


@dataclass(frozen=True)
class Release:
    version: str  # "1.2.0", no leading v
    page_url: str
    asset_url: str | None = None
    asset_size: int | None = None
    sha256: str | None = None  # lowercase hex


def parse_release(data: object) -> Release | None:
    """The GitHub /releases/latest payload -> Release, or None when it is
    not a usable published release with a version tag."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = parse_version(str(data.get("tag_name") or ""))
    if version is None:
        return None
    release = Release(
        version=".".join(map(str, version)),
        page_url=str(data.get("html_url") or ""),
    )
    for asset in data.get("assets") or ():
        if not isinstance(asset, dict) or asset.get("name") != ASSET_NAME:
            continue
        digest = str(asset.get("digest") or "")
        sha = digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else None
        size = asset.get("size")
        return Release(
            version=release.version,
            page_url=release.page_url,
            asset_url=str(asset.get("browser_download_url") or "") or None,
            asset_size=size if isinstance(size, int) else None,
            sha256=sha if sha and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
        )
    return release


def wait_hint_s(headers: httpx.Headers, now: float | None = None) -> float | None:
    """Seconds GitHub asks us to wait, from Retry-After or an exhausted
    X-RateLimit-Remaining with its X-RateLimit-Reset (epoch seconds)."""
    raw = headers.get("Retry-After", "")
    if raw.strip().isdigit():
        return float(raw)
    if headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset", "")
        if reset.strip().isdigit():
            return max(0.0, float(reset) - (time.time() if now is None else now))
    return None


# --------------------------------------------------------- staged install


def staged_files(exe: Path) -> tuple[Path, Path, Path, Path]:
    """(staged exe, its metadata, in-progress download, previous exe)."""
    return (
        exe.with_name(exe.name + ".new"),
        exe.with_name(exe.name + ".new.json"),
        exe.with_name(exe.name + ".part"),
        exe.with_name(exe.name + ".old"),
    )


def staged_version(exe: Path) -> str | None:
    """Version of a complete, verified staged update, if there is one."""
    new, meta, _part, _old = staged_files(exe)
    if not new.exists() or not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or parse_version(version) is None:
        return None
    if data.get("size") != new.stat().st_size:
        return None  # truncated or replaced since it was verified
    return version


def discard_staged(exe: Path) -> None:
    for path in staged_files(exe)[:3]:
        with contextlib.suppress(OSError):
            path.unlink()


def apply_staged(exe: Path, current: str) -> str | None:
    """Swap a staged update in place of `exe`. Returns the installed
    version, or None when there was nothing newer to install (a staged file
    that is not newer than `current` - e.g. the user installed a newer exe
    by hand - is discarded, never downgraded to)."""
    version = staged_version(exe)
    if version is None or not is_newer(version, current):
        discard_staged(exe)
        return None
    new, meta, _part, old = staged_files(exe)
    with contextlib.suppress(OSError):
        old.unlink()
    exe.rename(old)  # allowed while running; deleting is not
    try:
        new.rename(exe)
    except OSError:
        old.rename(exe)  # put the working exe back
        raise
    with contextlib.suppress(OSError):
        meta.unlink()
    return version


def cleanup_old(exe: Path) -> None:
    """Remove the previous exe left by a swap. It may still be locked for a
    moment while the old process exits; the next start tries again."""
    with contextlib.suppress(OSError):
        staged_files(exe)[3].unlink()
    with contextlib.suppress(OSError):
        staged_files(exe)[2].unlink()  # a download the last run did not finish


def relaunch_args(argv: list[str]) -> list[str]:
    """`argv` without the flags a previous relaunch added."""
    out: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in RELAUNCH_FLAGS:
            skip = True
            continue
        if any(arg.startswith(flag + "=") for flag in RELAUNCH_FLAGS):
            continue
        out.append(arg)
    return out


def relaunch(exe: Path, argv: list[str], wait_pid: int, updated_from: str) -> None:
    """Start the (new) exe detached. It waits for `wait_pid` to exit before
    binding the websocket port."""
    env = dict(os.environ)
    # A PyInstaller one-file exe started from inside another one would
    # otherwise reuse the parent's unpacked bundle - the OLD version.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in list(env):
        if key.startswith("_PYI") or key == "_MEIPASS2":
            del env[key]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [
            str(exe),
            *relaunch_args(argv),
            "--wait-pid",
            str(wait_pid),
            "--updated-from",
            updated_from,
        ],
        env=env,
        cwd=str(exe.parent),
        creationflags=flags,
        close_fds=True,
    )


def wait_for_exit(pid: int, timeout_s: float = 30.0) -> None:
    import psutil

    with contextlib.suppress(psutil.Error):
        psutil.Process(pid).wait(timeout=timeout_s)


# ---------------------------------------------------------------- checker


class Updater:
    """Runs on the asyncio loop. `exe` is the running ValdoSniper.exe, or
    None for a source checkout (announce only)."""

    def __init__(
        self,
        config: UpdatesConfig,
        exe: Path | None,
        publish: Callable[[UpdateStatus], None],
        current: str = __version__,
        client: httpx.AsyncClient | None = None,
    ):
        self._config = config
        self._exe = exe
        self._publish = publish
        self._current = current
        self._client = client
        self._etag: str | None = None
        self._release: Release | None = None  # last parsed latest release
        self._fails = 0
        self._last: UpdateStatus | None = None

    @property
    def _url(self) -> str:
        return f"{self._config.api_base.rstrip('/')}/repos/{self._config.repo}/releases/latest"

    async def run(self) -> None:
        if not self._config.enabled:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=60.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
        await asyncio.sleep(STARTUP_DELAY_S)
        while True:
            delay = await self.check_once()
            await asyncio.sleep(delay)

    def _backoff(self, hint: float | None, reason: str) -> float:
        self._fails += 1
        delay = min(BACKOFF_BASE_S * 2 ** (self._fails - 1), BACKOFF_CAP_S)
        if hint is not None:
            delay = max(delay, hint)
        event("update_backoff", reason=reason, retry_in_s=round(delay))
        return delay

    def _announce(self, status: UpdateStatus) -> None:
        if status != self._last:
            self._last = status
            self._publish(status)

    async def check_once(self) -> float:
        """One check (and download, if due). Returns seconds until the next."""
        interval = self._config.check_interval_hours * 3600
        headers = {"Accept": "application/vnd.github+json"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            resp = await self._client.get(self._url, headers=headers)
        except httpx.HTTPError as e:
            return self._backoff(None, f"transport: {type(e).__name__}")
        hint = wait_hint_s(resp.headers)
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            return self._backoff(hint, f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            # no published release yet: nothing to do, not a failure
            self._fails = 0
            event("update_check", latest=None, current=self._current)
            return max(interval, hint or 0)
        if resp.status_code == 200:
            try:
                self._release = parse_release(resp.json())
            except ValueError:
                return self._backoff(hint, "bad JSON")
            self._etag = resp.headers.get("ETag")
        elif resp.status_code != 304:
            return self._backoff(hint, f"HTTP {resp.status_code}")
        self._fails = 0
        release = self._release
        event(
            "update_check",
            latest=release.version if release else None,
            current=self._current,
            not_modified=resp.status_code == 304,
        )
        # an exhausted quota pushes the next check past its reset
        next_check = max(interval, hint or 0)
        if release is None or not is_newer(release.version, self._current):
            return next_check
        can_install = (
            self._exe is not None and self._config.auto_download and release.asset_url is not None
        )
        if not can_install:
            self._announce(UpdateStatus("available", release.version, release.page_url))
            return next_check
        if staged_version(self._exe) == release.version:
            self._announce(UpdateStatus("ready", release.version, release.page_url))
            return next_check
        ok = await self._download(release)
        return next_check if ok else self._backoff(None, "download failed")

    async def _download(self, release: Release) -> bool:
        assert self._exe is not None and release.asset_url is not None
        new, meta, part, _old = staged_files(self._exe)
        self._announce(UpdateStatus("downloading", release.version, release.page_url))
        event("update_download", version=release.version)
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client.stream("GET", release.asset_url) as resp:
                if resp.status_code != 200:
                    raise OSError(f"HTTP {resp.status_code}")
                with part.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            problem = None
            if size < MIN_ASSET_BYTES:
                problem = f"only {size} bytes"
            elif release.asset_size is not None and size != release.asset_size:
                problem = f"size {size} != {release.asset_size}"
            elif release.sha256 is not None and digest.hexdigest() != release.sha256:
                problem = "SHA-256 mismatch"
            if problem:
                raise OSError(problem)
            # replace an older staged build; the metadata is written last,
            # so a crash in between leaves nothing that counts as staged
            for stale in (meta, new):
                with contextlib.suppress(FileNotFoundError):
                    stale.unlink()
            part.rename(new)
            meta.write_text(
                json.dumps(
                    {"version": release.version, "size": size, "sha256": digest.hexdigest()}
                ),
                encoding="utf-8",
            )
        except (OSError, httpx.HTTPError) as e:
            with contextlib.suppress(OSError):
                part.unlink()
            event("update_download_failed", version=release.version, error=str(e))
            self._announce(UpdateStatus("failed", release.version, release.page_url, detail=str(e)))
            return False
        event(
            "update_staged",
            version=release.version,
            bytes=size,
            verified="sha256" if release.sha256 else "size",
        )
        self._announce(UpdateStatus("ready", release.version, release.page_url))
        return True
