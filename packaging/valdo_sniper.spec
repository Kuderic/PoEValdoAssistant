# PyInstaller spec: python -m PyInstaller packaging/valdo_sniper.spec
# One-folder build (fast start, no unpack-to-temp), windowed.
# The userscript ships inside the bundle, where sniper/server.py looks for it
# (Path(__file__).parent.parent / "userscript").
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "userscript" / "valdo-sniper.user.js"), "userscript")],
    excludes=["pytest", "playwright", "PIL"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ValdoSniper",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ValdoSniper")
