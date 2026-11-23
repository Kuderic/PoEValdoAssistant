"""packaging/launch.py: the windowed exe has no console, so a startup
failure must surface as a dialog, and a clean exit must not."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "launch", Path(__file__).parent.parent / "packaging" / "launch.py"
)
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


@pytest.mark.parametrize("code,shown", [("config error: bad", True), (0, False), (None, False)])
def test_startup_exit_shows_a_dialog_only_on_failure(monkeypatch, code, shown):
    import sniper.__main__ as main_mod

    seen = []
    monkeypatch.setattr(launch, "_show_error", seen.append)

    def fake_main():
        raise SystemExit(code)

    monkeypatch.setattr(main_mod, "main", fake_main)
    with pytest.raises(SystemExit):
        launch.run()
    assert bool(seen) is shown
