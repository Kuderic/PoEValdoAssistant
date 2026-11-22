"""Jackpot tier: a listing with BOTH big net and big yield.

Motivated by a half-price Nimis (+79.5d net, +144 yield, 55.2 difficulty)
that expired unseen. These pin the detection rule, its exclusions
(generic hits, currency mismatches), the ranking guarantee that a jackpot is
the hotkey's top pick, and the ordered sound worker. (A jackpot used to
loop an alarm and pull the overlay forward; both were removed on request.)
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from conftest import make_config, make_listing_frame
from test_config_validation import write

from sniper import margin, sound
from sniper.alerts import AlertStore, jackpots_to_ring
from sniper.config import AlertsConfig, ConfigError, load_config
from sniper.models import Listing, parse_frame
from sniper.modrules import ModRules
from sniper.prices import PriceBook
from sniper.sound import SoundCommand, SoundWorker

CONFIG = make_config()
BOOK = PriceBook(CONFIG)
NO_RULES = ModRules([])
DEFAULTS = AlertsConfig()


def decision(listing_id: str, net: float, difficulty: float, **listing_overrides):
    """An alert decision with exactly this net and difficulty."""
    frame = parse_frame(make_listing_frame(listing_id=listing_id, **listing_overrides))
    assert isinstance(frame, Listing), frame
    d = margin.evaluate(frame, BOOK, CONFIG.thresholds, NO_RULES)
    assert d.verdict == "alert", d
    return replace(d, net_div=net, difficulty=difficulty)


def store(**kw) -> AlertStore:
    return AlertStore(
        expiry_s=10,
        jackpot_min_net=DEFAULTS.jackpot_min_net,
        jackpot_min_yield=DEFAULTS.jackpot_min_yield,
        **kw,
    )


# ------------------------------------------------------------------- rule


def test_the_missed_nimis_is_a_jackpot_under_the_defaults():
    assert margin.is_jackpot(79.5, 55.2, DEFAULTS.jackpot_min_net, DEFAULTS.jackpot_min_yield)


@pytest.mark.parametrize(
    ("net", "difficulty", "expected"),
    [
        (30, 60, True),  # both exactly at the bar (yield 50)
        (29.9, 10, False),  # huge yield, too little money
        (300, 1000, False),  # huge money, mediocre yield (30)
        (None, 50, False),  # unpriced
        (100, 0, False),  # no difficulty -> no yield
        (-50, 20, False),
    ],
)
def test_both_net_and_yield_must_clear(net, difficulty, expected):
    assert margin.is_jackpot(net, difficulty, 30, 50) is expected


def test_disabled_when_a_threshold_is_none():
    assert not margin.is_jackpot(500, 10, None, 50)
    assert not margin.is_jackpot(500, 10, 30, None)


# ------------------------------------------------------------------ store


def test_view_carries_the_flag():
    s = store()
    s.insert(decision("jp", 80, 55))
    s.insert(decision("meh", 10, 100))
    flags = {v.listing_id: v.jackpot for v in s.views()}
    assert flags == {"jp": True, "meh": False}


def test_store_without_thresholds_never_flags():
    s = AlertStore(expiry_s=10)
    s.insert(decision("jp", 80, 55))
    assert not s.views()[0].jackpot


def test_currency_mismatch_is_never_a_jackpot():
    """The scam guard's case must not be made louder and pushed to the top."""
    s = store()
    d = replace(decision("scam", 150, 25), currency_mismatch=True)
    assert not s.is_jackpot(d)


def test_generic_hit_is_never_a_jackpot():
    s = store()
    d = decision("ring", 150, 25)
    d = replace(d, listing=replace(d.listing, generic=True))
    assert not s.is_jackpot(d)


def test_jackpot_outranks_a_higher_yield_non_jackpot():
    """The hotkey must take the alert that is ringing, even when a tiny
    easy map has a better ratio (here +20d at difficulty 10 = yield 200)."""
    s = store()
    s.insert(decision("easy", 20, 10))  # yield 200, net below 30
    s.insert(decision("jp", 80, 55))  # yield 145, jackpot
    assert [v.listing_id for v in s.views()] == ["jp", "easy"]
    assert s.consume_best().decision.listing.listing_id == "jp"


def test_jackpot_outranks_a_newer_generic_hit():
    s = store()
    s.insert(decision("jp", 80, 55))
    time.sleep(0.01)
    g = decision("ring", 0, 0)
    s.insert(replace(g, listing=replace(g.listing, generic=True)))
    assert s.consume_best().decision.listing.listing_id == "jp"


def test_restored_runner_up_keeps_its_jackpot_flag():
    s = store(consume_mode="all")
    s.insert(decision("jp", 80, 55))
    s.insert(decision("jp2", 60, 50))
    best = s.consume_best()
    assert s.settle_travel(best.decision.listing.listing_id, ok=False)
    assert s.views()[0].jackpot


# ----------------------------------------------------------------- config


def test_config_defaults():
    assert DEFAULTS.jackpot_min_net == 30
    assert DEFAULTS.jackpot_min_yield == 50


def test_jackpot_keys_load_and_validate(tmp_path):
    path = write(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "volume: 0.5 }", "volume: 0.5, jackpot_min_net: 40, jackpot_min_yield: 70 }"
    )
    path.write_text(text, encoding="utf-8")
    alerts = load_config(path).alerts
    assert (alerts.jackpot_min_net, alerts.jackpot_min_yield) == (40, 70)

    bad = text.replace("jackpot_min_yield: 70", "jackpot_min_yield: -1")
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError, match="jackpot_min"):
        load_config(path)


def test_alarm_keys_load(tmp_path):
    path = write(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "volume: 0.5 }", "volume: 0.5, jackpot_alarm: false, jackpot_sound: 'x.wav' }"
    )
    path.write_text(text, encoding="utf-8")
    alerts = load_config(path).alerts
    assert (alerts.jackpot_alarm, alerts.jackpot_sound) == (False, "x.wav")
    assert DEFAULTS.jackpot_alarm is True and DEFAULTS.jackpot_sound == ""


def test_removed_jackpot_volume_key_is_rejected(tmp_path):
    """jackpot_volume no longer exists (the alarm plays at the volume
    slider); a config that still sets it gets a clear error naming the
    allowed keys."""
    path = write(tmp_path)
    old = path.read_text(encoding="utf-8")
    path.write_text(old.replace("volume: 0.5 }", "volume: 0.5, jackpot_volume: 1 }"), "utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


# ------------------------------------------------------------------ alarm


def _views(*specs):
    """(listing_id, net, difficulty) -> the store's ranked views."""
    s = store()
    for lid, net, diff in specs:
        s.insert(decision(lid, net, diff))
    return s.views()


def test_alarm_rings_for_a_live_jackpot_only():
    assert jackpots_to_ring(_views(("jp", 80, 55), ("meh", 10, 100)), set()) == {"jp"}
    assert jackpots_to_ring(_views(("meh", 10, 100)), set()) == set()


def test_silencing_stops_that_jackpot_but_a_new_one_still_rings():
    """The banner click: 'I am not going to this one'. It must not also
    swallow the next, different jackpot."""
    silenced = {"jp"}
    assert jackpots_to_ring(_views(("jp", 80, 55)), silenced) == set()
    assert jackpots_to_ring(_views(("jp", 80, 55), ("jp2", 90, 50)), silenced) == {"jp2"}


def test_travel_ends_the_alarm():
    """Traveling consumes the alert, so nothing is left to ring for."""
    s = store()
    s.insert(decision("jp", 80, 55))
    s.consume_best()
    assert jackpots_to_ring(s.views(), set()) == set()


def test_jackpot_alarm_has_its_own_default_sound():
    assert sound.JACKPOT_WAV not in (sound.DEFAULT_WAV, sound.GENERIC_WAV)
    assert sound.source_path("", sound.JACKPOT_WAV) == sound.JACKPOT_WAV


# ------------------------------------------------------------------ sound


def test_sound_worker_runs_commands_in_order_and_coalesces():
    """A later command must never be overtaken by an earlier one, and a
    burst queued behind a busy worker plays only its last command."""
    gate = threading.Event()
    seen: list[str] = []

    def backend(cmd: SoundCommand) -> None:
        seen.append(cmd.kind)
        if cmd.kind == "first":
            gate.wait(2)

    worker = SoundWorker(backend)
    worker.submit(SoundCommand("first"))
    time.sleep(0.05)  # worker is now blocked inside "first"
    worker.submit(SoundCommand("valdo", loop=True))
    worker.submit(SoundCommand("stop"))
    gate.set()
    assert worker.wait_idle()
    # the burst behind "first" collapsed to its LAST command, the stop
    assert seen == ["first", "stop"]


def test_sound_worker_survives_backend_errors():
    errors: list[Exception] = []
    seen: list[str] = []

    def backend(cmd: SoundCommand) -> None:
        if cmd.kind == "boom":
            raise RuntimeError("no audio device")
        seen.append(cmd.kind)

    worker = SoundWorker(backend, on_error=errors.append)
    worker.submit(SoundCommand("boom"))
    assert worker.wait_idle()
    worker.submit(SoundCommand("valdo"))
    assert worker.wait_idle()
    assert seen == ["valdo"] and len(errors) == 1


# --------------------------------------------------------- settings panel


def test_settings_panel_jackpot_bars_round_trip(tmp_path):
    """The ⚙ panel saves the bars into the overrides file; the next start
    must pick them up over config.yaml."""
    from sniper.config import SCORING_OVERRIDES_NAME, ModScoringConfig, save_scoring_overrides

    cfg = write(tmp_path)
    save_scoring_overrides(
        ModScoringConfig(base_default=25.0, rules=()),
        tmp_path / SCORING_OVERRIDES_NAME,
        jackpot_min_net=45,
        jackpot_min_yield=80,
    )
    alerts = load_config(cfg).alerts
    assert (alerts.jackpot_min_net, alerts.jackpot_min_yield) == (45, 80)


def test_live_threshold_change_applies_to_new_alerts_only():
    s = store()
    s.insert(decision("before", 40, 60))  # yield 67: jackpot at 30/50
    s.set_jackpot_thresholds(50, 100)
    s.insert(decision("after", 40, 60))  # same numbers, now below the bar
    flags = {v.listing_id: v.jackpot for v in s.views()}
    # the live alert keeps the flag it rang with
    assert flags == {"before": True, "after": False}
    assert s.is_jackpot(decision("big", 80, 55))
