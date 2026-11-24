"""PyInstaller entry point for ValdoSniper.exe (tools/build_exe.py).

PyInstaller needs a script, not `-m sniper`; this is that script. It must
stay this thin: everything belongs in sniper/__main__.py, where the source
checkout runs the same code.
"""

from sniper.__main__ import _run

if __name__ == "__main__":
    _run()
