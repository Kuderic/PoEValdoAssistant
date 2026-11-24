"""Build dist/ValdoSniper.exe: one file, no console, nothing to install.

    pip install -e .[dev]            # brings the pinned PyInstaller
    python tools/build_exe.py        # -> dist/ValdoSniper.exe

The shipped config.yaml and the browser script are bundled inside the exe
(see sniper/paths.py); per-user state is written beside the exe at run time.
The icon is generated from sniper/icon.py's pixel map, so there is no binary
to keep in git. After building, the exe is run with --self-check, which
proves the bundle is complete (config loads, browser script present, every
native module imports) - a broken build fails here, not on a user's machine.

--expect-version vX.Y.Z (the release workflow passes the git tag) refuses to
build when sniper.__version__ disagrees, so a release can never ship an exe
that thinks it is an older version and would update itself in a loop.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sniper import __version__  # noqa: E402
from sniper.icon import PALETTE, PIXELS  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"
NAME = "ValdoSniper"
ENTRY = ROOT / "tools" / "exe_entry.py"


# ------------------------------------------------------------------ icon


def _png(scale: int) -> bytes:
    """The 16x16 pixel map scaled by `scale` (nearest neighbour) as RGBA PNG."""
    rows = []
    for line in PIXELS:
        row = bytearray([0])  # filter: none
        for ch in line:
            colour = PALETTE.get(ch)
            px = bytes.fromhex(colour[1:]) + b"\xff" if colour else b"\0\0\0\0"
            row += px * scale
        rows.extend([bytes(row)] * scale)
    size = len(PIXELS) * scale

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def write_icon(path: Path) -> None:
    """A multi-size .ico of PNG frames (16, 32, 48, 256 px)."""
    frames = [(s, _png(s // 16)) for s in (16, 32, 48, 256)]
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        dim = 0 if size == 256 else size  # 0 means 256 in an ICO directory
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(out + b"".join(data for _s, data in frames))


# --------------------------------------------------------- version resource


def write_version_file(path: Path) -> None:
    """Windows file properties (Details tab, Task Manager description)."""
    nums = tuple(int(n) for n in __version__.split(".")) + (0,)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Valdo Sniper'),
      StringStruct('ProductName', 'Valdo Sniper'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


# ------------------------------------------------------------------- build


def build() -> Path:
    import PyInstaller.__main__

    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "icon.ico"
    version_file = BUILD / "version.txt"
    write_icon(icon)
    write_version_file(version_file)
    sep = os.pathsep
    PyInstaller.__main__.run(
        [
            str(ENTRY),
            "--name",
            NAME,
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD / "work"),
            "--specpath",
            str(BUILD),
            "--icon",
            str(icon),
            "--version-file",
            str(version_file),
            "--add-data",
            f"{ROOT / 'config.yaml'}{sep}.",
            "--add-data",
            f"{ROOT / 'userscript' / 'valdo-sniper.user.js'}{sep}userscript",
            # dev/tool-only packages that must not bloat the exe
            "--exclude-module",
            "pytest",
            "--exclude-module",
            "PIL",
        ]
    )
    return DIST / f"{NAME}.exe"


def self_check(exe: Path) -> bool:
    # windowed exe: no console output, the exit code is the verdict
    result = subprocess.run([str(exe), "--self-check"], timeout=120)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-version", help="git tag, e.g. v1.2.0")
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()
    if args.expect_version and args.expect_version.lstrip("v") != __version__:
        print(
            f"tag {args.expect_version} != sniper.__version__ {__version__}: "
            "bump sniper/__init__.py before tagging"
        )
        return 1
    exe = build()
    size_mb = exe.stat().st_size / 1e6
    print(f"built {exe.relative_to(ROOT)} ({size_mb:.1f} MB), version {__version__}")
    if not args.skip_check:
        if not self_check(exe):
            print("SELF-CHECK FAILED: the exe is missing something it needs")
            return 1
        print("self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
