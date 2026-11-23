"""Entry point for the installed app: python\\pythonw.exe launch.py.

The installer ships python.org's own runtime (pythonw.exe is signed by the
Python Software Foundation) next to the source, instead of a PyInstaller
exe: PyInstaller's bootloader is shared with a lot of malware, so Windows
Defender's machine-learning scan flags unsigned PyInstaller builds as
"Trojan:Win32/Wacatac" and deletes them.

pythonw has no console, so two things start.bat users get for free are done
here: run from the install folder (config.yaml, .env, logs/, searches.txt
all live there), and show a startup failure in a dialog instead of a console
that never existed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _show_error(text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Valdo Sniper", text)
        root.destroy()
    except Exception:  # noqa: BLE001 - last resort: nowhere left to report
        pass


def run() -> None:
    if Path(sys.executable).stem.lower() == "pythonw":  # the installed app
        os.chdir(Path(__file__).resolve().parent)
    from sniper.__main__ import main

    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            _show_error(str(e.code))
        raise
    except Exception as e:  # noqa: BLE001
        _show_error(f"Valdo Sniper crashed on startup:\n{e!r}\n\nSee logs/ for details.")
        raise


if __name__ == "__main__":
    run()
