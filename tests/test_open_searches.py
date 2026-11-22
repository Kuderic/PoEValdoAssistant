"""App.open_live_searches: imported Better Trading searches open as live
tabs in the sniper Chrome, paced under the search limit, only the ones not
already open, never before the profile exists, and never alongside pricing."""

import base64
import gzip
import json
from types import SimpleNamespace

from conftest import make_config

from sniper import __main__ as main_mod
from sniper.__main__ import App
from sniper.bus import Bus, Notice


def export(*slugs: str) -> str:
    trs = [{"tit": f"t{s}", "loc": f"1:search:{s}"} for s in slugs]
    body = {"icn": None, "tit": "Valdo", "ver": "1", "trs": trs}
    return "3:" + base64.b64encode(json.dumps(body).encode()).decode()


def query_slug(reward: str) -> str:  # what Better Trading 1.9 stores
    q = {"filters": {"map_filters": {"filters": {"map_completion_reward": {"option": reward}}}}}
    return base64.urlsafe_b64encode(gzip.compress(json.dumps(q).encode())).decode().rstrip("=")


def setup(monkeypatch, tmp_path, first_run=False):
    path = tmp_path / "searches.txt"
    app = App(make_config(), Bus(), searches_path=path)
    app.book._league = "Covens"
    opened, sleeps = [], []
    monkeypatch.setattr(main_mod.browser, "open_url", lambda *u: opened.extend(u) or "chrome")
    monkeypatch.setattr(main_mod.browser, "is_first_run", lambda: first_run)

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(main_mod.asyncio, "sleep", fake_sleep)
    return app, path, opened, sleeps


async def test_opens_each_search_live_with_spacing(monkeypatch, tmp_path):
    app, path, opened, sleeps = setup(monkeypatch, tmp_path)
    path.write_text(export("Aa1", "Bb2", "Cc3"))
    await app.open_live_searches()
    assert opened == [
        f"https://www.pathofexile.com/trade/search/Covens/{s}/live" for s in ("Aa1", "Bb2", "Cc3")
    ]
    assert sleeps == [main_mod.SEARCH_SPACING_S] * 2


async def test_first_run_opens_setup_page_instead(monkeypatch, tmp_path):
    app, path, opened, _ = setup(monkeypatch, tmp_path, first_run=True)
    path.write_text(export("Aa1"))
    await app.open_live_searches()
    assert len(opened) == 1 and opened[0].endswith(":8765/")
    assert not any("/trade/search/" in u for u in opened)


async def test_nothing_imported_says_so(monkeypatch, tmp_path):
    app, path, opened, _ = setup(monkeypatch, tmp_path)
    await app.open_live_searches()
    assert opened == []
    notes = [e for e in app.bus.drain() if isinstance(e, Notice)]
    assert notes and not notes[-1].ok


def tab(search_id: str, reward: str | None = None):
    return SimpleNamespace(search_id=search_id, search_reward=reward)


async def test_skips_searches_whose_tab_is_already_open(monkeypatch, tmp_path):
    app, path, opened, _ = setup(monkeypatch, tmp_path)
    hh, mb = query_slug("Headhunter"), query_slug("Mageblood")
    path.write_text(export("Aa1", hh, mb))
    # Aa1 by its id; the Headhunter tab reports its reward (its URL id changes)
    app.server.tabs = {1: tab("Aa1"), 2: tab("live", "Foil Headhunter")}
    await app.open_live_searches()
    assert opened == [f"https://www.pathofexile.com/trade/search/Covens/{mb}/live"]


async def test_big_import_slows_down_after_the_burst(monkeypatch, tmp_path):
    app, path, opened, sleeps = setup(monkeypatch, tmp_path)
    path.write_text(export(*(f"S{i}" for i in range(14))))
    await app.open_live_searches()
    assert len(opened) == 14
    burst = main_mod.SEARCH_BURST
    assert sleeps == [main_mod.SEARCH_SPACING_S] * (burst - 1) + [
        main_mod.SEARCH_SPACING_SLOW_S
    ] * (14 - burst)


async def test_schedule_fits_every_search_limit_window(monkeypatch, tmp_path):
    """Live limits 2026-11-22: 5/10s, 15/60s, 30/300s. Leave the pricer and a
    manual search or two some room: at most 4, 12 and 26 tab loads."""
    app, path, opened, sleeps = setup(monkeypatch, tmp_path)
    path.write_text(export(*(f"S{i}" for i in range(60))))
    await app.open_live_searches()
    times = [sum(sleeps[:i]) for i in range(len(opened))]
    for period, cap in ((10, 4), (60, 12), (300, 26)):
        assert max(sum(t0 <= t < t0 + period for t in times) for t0 in times) <= cap


async def test_waits_out_a_rate_limit_lockout(monkeypatch, tmp_path):
    app, path, opened, sleeps = setup(monkeypatch, tmp_path)
    path.write_text(export("Aa1"))
    remaining = [40.0, 10.0, 0.0]

    class LockedPricer:
        in_backoff = property(lambda self: remaining[0] > 0)
        backoff_remaining = property(lambda self: remaining[0])

    app._pricer = LockedPricer()

    async def sleep(s):
        sleeps.append(s)
        remaining.pop(0)

    monkeypatch.setattr(main_mod.asyncio, "sleep", sleep)
    await app.open_live_searches()
    assert sleeps == [15, 10] and len(opened) == 1


async def test_second_click_during_a_run_is_ignored(monkeypatch, tmp_path):
    app, path, opened, _ = setup(monkeypatch, tmp_path)
    path.write_text(export("Aa1"))
    app._opening = True
    await app.open_live_searches()
    assert opened == []


async def test_saved_rewards_are_priced_without_open_tabs(tmp_path):
    path = tmp_path / "searches.txt"
    path.write_text(export(query_slug("Headhunter"), "Aa1"))
    app = App(make_config(), Bus(), searches_path=path)
    assert app.server.active_rewards() == {"Foil Headhunter"}
