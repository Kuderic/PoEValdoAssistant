"""Setup step run by start.bat before every launch.

Checks the Python version, then installs whatever pyproject.toml dependency
is missing from the running interpreter (the .venv start.bat created). Once
everything imports it does nothing, so it adds well under a second to each
start. Dependencies are read from pyproject.toml so the list lives in one
place.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# distribution name -> the module it installs, where the two differ
MODULE_OF = {"pywin32": "win32gui", "PyYAML": "yaml", "python-dotenv": "dotenv"}


def missing_requirements() -> list[str]:
    import tomllib  # 3.11+: imported after main()'s version check, not at the top

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    missing = []
    for spec in project["dependencies"]:
        name = re.split(r"[<>=!~;\s\[]", spec, maxsplit=1)[0]
        if "sys_platform == 'win32'" in spec and sys.platform != "win32":
            continue
        module = MODULE_OF.get(name, name.lower().replace("-", "_"))
        if importlib.util.find_spec(module) is None:
            missing.append(spec)
    return missing


def main() -> int:
    # start.bat may have found an older Python: say so instead of crashing
    if sys.version_info < (3, 12):  # noqa: UP036
        print(f"Valdo Sniper needs Python 3.12 or newer (this is {sys.version.split()[0]}).")
        return 1
    missing = missing_requirements()
    if not missing:
        return 0
    print("Installing dependencies (first run only):", ", ".join(missing))
    return subprocess.call(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", *missing]
    )


if __name__ == "__main__":
    sys.exit(main())
