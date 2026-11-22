"""Entry point for the packaged ValdoSniper.exe (PyInstaller).

The exe is windowed (no console), so two things start_bat users get for
free are done here: run from the install folder (config.yaml, .env, logs/,
searches.txt all live beside the exe), and show a startup failure in a
dialog instead of a console that never existed.
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
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
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
