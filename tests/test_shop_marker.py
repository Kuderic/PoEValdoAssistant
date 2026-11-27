"""In-game shop marker: where the item sits on screen in Faustus' Merchant
window, when the marker goes up and down, and PoE's client log that times it.

The default grid comes from a real 2560x1440 capture with the Merchant
window open: the blue grid border spans x 412-1261, y 345-1195, and a 2x3
item whose top-left cell is (8, 9) sat with its red backing at ~(978, 982).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import make_config

from sniper import clientlog, shopgrid
from sniper.bus import AlertsChanged, AreaEntered, Bus, ClickOutcome, ClientLogStatus, Traveled
from sniper.clientlog import ClientLogWatcher, logs_in, parse_area
from sniper.config import ConfigError, load_config
from sniper.gamewindow import GameRect
from sniper.shopgrid import (
    DEFAULT_GRID,
    ShopGrid,
    ShopMarker,
    ShopSpot,
    cell_box,
    from_corners,
    grid_from_mapping,
    grid_side,
    load_saved,
    place,
    save,
)

# ------------------------------------------------------------------ geometry


def test_the_default_grid_is_the_measured_one_at_its_own_resolution():
    assert place(DEFAULT_GRID, 2560, 1440) == (412, 345, 850)


def test_the_measured_item_cell_lands_on_the_item():
    x0, y0, x1, y1 = cell_box(DEFAULT_GRID, 2560, 1440, 8, 9)
    assert x0 == pytest.approx(978, abs=2)
    assert y0 == pytest.approx(982, abs=2)
    assert x1 - x0 == pytest.approx(850 / 12)


def test_the_grid_scales_with_height_and_stays_centred_left_of_the_inventory():
    left, top, size = place(DEFAULT_GRID, 1920, 1080)
    assert size == pytest.approx(850 * 0.75)
    assert top == pytest.approx(345 * 0.75)
    # its centre keeps the same height-proportional gap left of the centre
    gap_1440 = 1280 - (412 + 425)
    assert 960 - (left + size / 2) == pytest.approx(gap_1440 * 0.75)


def test_ultrawide_moves_the_grid_right_by_the_extra_width_halved():
    left_16_9 = place(DEFAULT_GRID, 2560, 1440)[0]
    left_21_9 = place(DEFAULT_GRID, 3440, 1440)[0]
    assert left_21_9 - left_16_9 == pytest.approx((3440 - 2560) / 2)


def test_a_cell_past_eleven_means_a_quad_tab():
    assert grid_side(11, 11) == 12
    assert grid_side(12, 0) == 24
    x0, _, x1, _ = cell_box(DEFAULT_GRID, 2560, 1440, 23, 0)
    assert x1 == pytest.approx(412 + 850)
    assert x1 - x0 == pytest.approx(850 / 24)


def test_corners_make_a_grid_whichever_order_they_are_clicked():
    a, b = (412, 345), (1262, 1195)
    assert from_corners(a, b, 2560, 1440) == from_corners(b, a, 2560, 1440)
    g = from_corners(a, b, 2560, 1440)
    assert (g.left, g.top, g.size, g.width, g.height) == (412, 345, 850, 2560, 1440)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((400, 400), (450, 450)),  # a double click, not two corners
        ((400, 300), (1300, 600)),  # far from square
    ],
)
def test_a_mis_click_is_refused(a, b):
    with pytest.raises(ValueError):
        from_corners(a, b, 2560, 1440)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"left": 1, "top": 1, "size": 500},  # no resolution
        {"left": 1, "top": 1, "size": 500, "width": 2560, "height": 1440, "x": 1},
        {"left": "a", "top": 1, "size": 500, "width": 2560, "height": 1440},
        {"left": 2500, "top": 1, "size": 500, "width": 2560, "height": 1440},  # off screen
        {"left": 1, "top": 1, "size": 10, "width": 2560, "height": 1440},  # too small
    ],
)
def test_a_bad_grid_mapping_is_refused(raw):
    with pytest.raises(ValueError):
        grid_from_mapping(raw)


def test_saved_settings_round_trip_and_a_broken_file_is_ignored(tmp_path):
    path = tmp_path / shopgrid.SHOP_OVERLAY_NAME
    grid = ShopGrid(300, 260, 640, 1920, 1080)
    save(path, False, grid)
    assert load_saved(path) == {"enabled": False, "grid": grid}
    save(path, True, None)
    assert load_saved(path) == {"enabled": True}
    path.write_text("grid: [not, a, mapping", encoding="utf-8")
    assert load_saved(path) == {}
    assert load_saved(tmp_path / "missing.yaml") == {}


# --------------------------------------------------------------- the timing


STASH = ("7", 8, 9)
A = ShopSpot("L1", "7", 8, 9, "Mageblood 80 divine")
B = ShopSpot("L2", "7", 0, 0, "Nimis 40 divine")
C = ShopSpot("L3", "8", 3, 3, "Headhunter 30 divine")


def test_the_marker_waits_for_the_next_area_load_after_the_travel():
    m = ShopMarker(show_s=60)
    assert m.travel_ok((A,), "Bob", now=0.0, log_live=True) is None
    assert m.shown is None
    assert m.area_entered(now=1.5) == ("show", (A,))
    # leaving the seller's hideout takes it down
    assert m.area_entered(now=30.0) == ("hide", None)
    assert m.area_entered(now=40.0) == ("", None)


def test_a_load_long_after_the_travel_is_not_the_sellers_hideout():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    assert m.area_entered(now=shopgrid.ARM_S + 1) == ("", None)


def test_the_marker_times_out():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    m.area_entered(now=1.0)
    assert m.tick(now=60.0) is False
    assert m.tick(now=61.0) is True
    assert m.shown is None
    assert m.tick(now=62.0) is False


def test_without_the_log_it_shows_as_soon_as_the_travel_lands():
    m = ShopMarker(show_s=60)
    assert m.travel_ok((A, B), "Bob", now=0.0, log_live=False) == (A, B)
    assert m.shown == (A, B)


def test_a_new_travel_while_marked_replaces_it_on_the_next_load():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    m.area_entered(now=1.0)
    m.travel_ok((C,), "Eve", now=10.0, log_live=True)
    assert m.area_entered(now=11.0) == ("show", (C,))
    assert m.seller == "Eve"


def test_cancel_drops_an_armed_and_a_shown_marker():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    assert m.cancel() is False
    assert m.area_entered(now=1.0) == ("", None)
    m.show_now((A,), now=0.0, seconds=10)
    assert m.cancel() is True


def test_the_sellers_next_item_joins_a_marker_still_waiting_for_the_hideout():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    assert m.add(B, "Bob", now=1.0) == "armed"
    assert m.area_entered(now=2.0) == ("show", (A, B))


def test_the_sellers_next_item_joins_a_marker_on_screen_and_keeps_it_up():
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    m.area_entered(now=1.0)
    assert m.add(B, "Bob", now=55.0) == "shown"
    assert m.shown == (A, B)
    assert m.tick(now=70.0) is False  # a late arrival gets ADDED_MIN_S on screen
    assert m.tick(now=55.0 + shopgrid.ADDED_MIN_S) is True


@pytest.mark.parametrize(
    ("seller", "spot", "when"),
    [
        ("Eve", B, 1.0),  # someone else's item
        ("", B, 1.0),  # seller unknown
        ("Bob", A, 1.0),  # already marked
        ("Bob", B, 100.0),  # the marker is long gone
    ],
)
def test_other_items_do_not_join(seller, spot, when):
    m = ShopMarker(show_s=60)
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    m.area_entered(now=0.5)
    if when > 60:
        m.tick(now=when)
    assert m.add(spot, seller, now=when) == ""


def test_nothing_joins_when_no_marker_is_pending():
    m = ShopMarker(show_s=60)
    assert m.add(B, "Bob", now=0.0) == ""
    m.travel_ok((A,), "Bob", now=0.0, log_live=True)
    m.tick(now=shopgrid.ARM_S + 1)  # the travel never loaded anywhere
    assert m.add(B, "Bob", now=shopgrid.ARM_S + 2) == ""
    assert m.seller == ""


def test_the_marker_holds_at_most_max_spots():
    m = ShopMarker(show_s=60, max_spots=2)
    m.travel_ok((A, B), "Bob", now=0.0, log_live=False)
    assert m.add(C, "Bob", now=1.0) == ""


def test_the_test_view_is_not_any_sellers():
    m = ShopMarker(show_s=60)
    m.show_now((A,), now=0.0, seconds=10)
    assert m.add(B, "", now=1.0) == ""


# ---------------------------------------------------------------- client log

# verbatim from a real LatestClient.txt (3.30): the seller's hideout load
REAL_LOAD = (
    "2026/11/27 12:52:09 129113515 1186a803 [DEBUG Client 32916] Generating level 60 "
    'area "HideoutTemplarLab" with seed 1'
)
REAL_ENTERED = (
    "2026/11/27 12:52:09 129114046 cffb06be [INFO Client 32916] : "
    "You have entered Cartographer's Hideout."
)


def test_an_area_load_line_names_the_area():
    assert parse_area(REAL_LOAD) == "HideoutTemplarLab"
    assert parse_area(REAL_LOAD + "\r") == "HideoutTemplarLab"


@pytest.mark.parametrize(
    "line",
    [
        REAL_ENTERED,
        '2026/11/27 12:52:09 1 2 [INFO Client 1] @From Seller: Generating level 1 area "x"',
        "",
    ],
)
def test_other_lines_are_not_area_loads(line):
    assert parse_area(line) is None


def test_latestclient_is_preferred_unless_it_is_stale(tmp_path):
    import os

    logs = tmp_path / "logs"
    logs.mkdir()
    assert logs_in(tmp_path) is None
    full = logs / "Client.txt"
    full.write_text("x")
    assert logs_in(tmp_path) == full
    latest = logs / "LatestClient.txt"
    latest.write_text("x")
    assert logs_in(tmp_path) == latest
    old = time.time() - 3600
    os.utime(latest, (old, old))
    assert logs_in(tmp_path) == full


def test_an_override_path_is_used_as_is(tmp_path):
    log = tmp_path / "Client.txt"
    assert clientlog.find_client_log((), str(log)) is None
    log.write_text("")
    assert clientlog.find_client_log((), str(log)) == log


def test_the_watcher_reports_loads_appended_after_it_started(tmp_path):
    log = tmp_path / "Client.txt"
    log.write_text(REAL_LOAD + "\n")  # history: never replayed
    areas: list[str] = []
    statuses: list[str | None] = []
    got = threading.Event()

    def on_area(area: str) -> None:
        areas.append(area)
        got.set()

    w = ClientLogWatcher((), on_area, statuses.append, override=str(log), poll_s=0.02)
    w.start()
    try:
        deadline = time.monotonic() + 5
        while not statuses and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)  # let it seek to the end
        with log.open("a", encoding="utf-8") as f:
            f.write(REAL_ENTERED + "\n")
            f.write(REAL_LOAD.replace("HideoutTemplarLab", "Hideout2"))  # half a line
            f.flush()
            time.sleep(0.1)
            f.write("\n")
        assert got.wait(5)
    finally:
        w.stop()
    assert statuses == [str(log)]
    assert areas == ["Hideout2"]


# -------------------------------------------------------------------- config


def write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_config_reads_the_section_and_the_saved_file_wins(tmp_path):
    cfg = write(
        tmp_path / "config.yaml",
        "shop_overlay:\n  seconds: 30\n  enabled: true\n"
        "  grid: {left: 300, top: 260, size: 640, width: 1920, height: 1080}\n",
    )
    c = load_config(cfg).shop_overlay
    assert (c.seconds, c.enabled, c.grid) == (30, True, ShopGrid(300, 260, 640, 1920, 1080))
    save(tmp_path / shopgrid.SHOP_OVERLAY_NAME, False, DEFAULT_GRID)
    c = load_config(cfg).shop_overlay
    assert (c.enabled, c.grid) == (False, DEFAULT_GRID)


@pytest.mark.parametrize(
    "body",
    [
        "shop_overlay:\n  colour: red\n",
        "shop_overlay:\n  seconds: 0\n",
        "shop_overlay:\n  enabled: maybe\n",
        "shop_overlay:\n  grid: {left: 1}\n",
    ],
)
def test_config_rejects_a_bad_section(tmp_path, body):
    with pytest.raises(ConfigError, match="shop_overlay"):
        load_config(write(tmp_path / "config.yaml", body))


def test_the_shipped_config_loads_with_the_default_grid():
    c = load_config("config.yaml").shop_overlay
    assert c.enabled is True
    assert c.grid is None or isinstance(c.grid, ShopGrid)


# ------------------------------------------------------------------- overlay

RECT = GameRect(x=0, y=0, w=2048, h=1152, pw=2560, ph=1440)


class FakeWindow:
    def __init__(self):
        self.visible = False
        self.shown: list[tuple] = []

    def show(self, rect, grid, spots, test=False):
        self.visible = True
        self.shown.append((rect, grid, spots, test))

    @property
    def last_cells(self):
        """[(listing id, tab, x, y)] of the latest drawing."""
        return [(s.listing_id, s.tab, s.x, s.y) for s in self.shown[-1][2]]

    def hide(self):
        self.visible = False


@pytest.fixture
def overlay(monkeypatch, tmp_path):
    from sniper import overlay as overlay_mod

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    monkeypatch.setattr(overlay_mod, "game_client_rect", lambda title: RECT)
    bus = Bus()
    config = replace(make_config(), state_dir=tmp_path)
    ov = overlay_mod.Overlay(root, bus, config)
    ov._shop_window = FakeWindow()
    yield ov, bus
    root.destroy()


def alert(lid="L1", stash=STASH, seller="Bob"):
    from test_jackpot_sound import view

    return replace(view(lid), stash=stash, seller=seller)


def traveled(lid="L1", stash=STASH, seller="Bob"):
    return Traveled(alert(lid, stash, seller))


def arrived(v, others=()):
    """The bus event for a newly arrived alert `v`."""
    return AlertsChanged((v, *others), new_alert=True, new_view=v)


def test_a_landed_travel_marks_the_item_when_the_hideout_loads(overlay):
    ov, bus = overlay
    bus.put(ClientLogStatus("C:/PoE/logs/LatestClient.txt"))
    bus.put(traveled())
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.shown == []  # still in the old area
    bus.put(AreaEntered("HideoutTemplarLab"))
    ov._drain_once()
    ((rect, grid, spots, test),) = ov._shop_window.shown
    assert (rect, grid, test) == (RECT, DEFAULT_GRID, False)
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9)]
    assert spots[0].caption == "Nimis 80 divine"
    bus.put(AreaEntered("MapWorldsStrand"))  # off to run the map
    ov._drain_once()
    assert ov._shop_window.visible is False


def test_without_the_log_the_mark_goes_up_when_the_travel_lands(overlay):
    ov, bus = overlay
    bus.put(traveled())
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9)]


@pytest.mark.parametrize(
    "events",
    [
        lambda: [traveled(), ClickOutcome("L1", False, "item_no_longer_available")],
        lambda: [traveled(stash=None), ClickOutcome("L1", True, "")],
        lambda: [traveled("L2"), ClickOutcome("L1", True, "")],
    ],
    ids=["travel failed", "position unknown", "another listing"],
)
def test_nothing_is_marked_without_a_landed_travel_and_a_position(overlay, events):
    ov, bus = overlay
    for ev in events():
        bus.put(ev)
    ov._drain_once()
    assert ov._shop_window.shown == []


def test_the_sellers_other_alerts_are_marked_with_the_one_traveled_to(overlay):
    ov, bus = overlay
    bus.put(arrived(alert("L2", ("7", 0, 0))))
    bus.put(arrived(alert("L3", ("8", 3, 3), seller="Eve")))  # someone else
    bus.put(arrived(alert("L4", None)))  # the same seller, but no known cell
    bus.put(arrived(alert("L1")))
    bus.put(traveled("L1"))
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9), ("L2", "7", 0, 0)]


def test_a_same_seller_alert_known_sold_or_long_ago_is_left_out(overlay, monkeypatch):
    from sniper import overlay as overlay_mod

    ov, bus = overlay
    bus.put(arrived(alert("L2", ("7", 0, 0))))
    bus.put(traveled("L2"))
    bus.put(ClickOutcome("L2", False, "item_no_longer_available"))
    ov._drain_once()
    real = time.monotonic
    monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: real() - shopgrid.SAME_SELLER_S - 5)
    bus.put(arrived(alert("L3", ("7", 1, 1))))  # seen "long ago"
    ov._drain_once()
    monkeypatch.setattr(overlay_mod.time, "monotonic", real)
    bus.put(traveled("L1"))
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9)]


def test_a_sellers_alert_arriving_before_the_hideout_loads_is_marked(overlay):
    ov, bus = overlay
    bus.put(ClientLogStatus("C:/PoE/logs/LatestClient.txt"))
    bus.put(traveled("L1"))
    bus.put(ClickOutcome("L1", True, ""))
    bus.put(arrived(alert("L2", ("7", 0, 0))))
    bus.put(AreaEntered("HideoutTemplarLab"))
    ov._drain_once()
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9), ("L2", "7", 0, 0)]


def test_a_sellers_alert_arriving_in_the_hideout_is_drawn_at_once(overlay):
    ov, bus = overlay
    bus.put(ClientLogStatus("C:/PoE/logs/LatestClient.txt"))
    bus.put(traveled("L1"))
    bus.put(ClickOutcome("L1", True, ""))
    bus.put(AreaEntered("HideoutTemplarLab"))
    ov._drain_once()
    assert len(ov._shop_window.shown) == 1
    bus.put(arrived(alert("L3", ("8", 3, 3), seller="Eve")))  # not this shop
    ov._drain_once()
    assert len(ov._shop_window.shown) == 1
    bus.put(arrived(alert("L2", ("7", 0, 0))))
    ov._drain_once()
    assert ov._shop_window.last_cells == [("L1", "7", 8, 9), ("L2", "7", 0, 0)]


def test_switched_off_nothing_is_marked_and_the_choice_is_saved(overlay, tmp_path):
    ov, bus = overlay
    ov._set_shop_enabled(False)
    bus.put(traveled())
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.shown == []
    assert load_saved(tmp_path / shopgrid.SHOP_OVERLAY_NAME) == {"enabled": False}


def test_a_calibrated_grid_is_what_gets_drawn(overlay):
    ov, bus = overlay
    mine = ShopGrid(300, 260, 640, 1920, 1080)
    ov._shop_grid = mine
    bus.put(traveled())
    bus.put(ClickOutcome("L1", True, ""))
    ov._drain_once()
    assert ov._shop_window.shown[0][1] == mine


def test_typed_grid_numbers_are_saved_from_the_settings_panel(overlay, tmp_path):
    ov, _ = overlay
    ov._open_tuning()
    boxes = ov._shop_fields
    for key, value in (("left", "300"), ("top", "260"), ("size", "640"), ("at", "1920x1080")):
        boxes[key].delete(0, "end")
        boxes[key].insert(0, value)
    ov._shop_fields_changed()
    assert ov._shop_grid == ShopGrid(300, 260, 640, 1920, 1080)
    assert load_saved(tmp_path / shopgrid.SHOP_OVERLAY_NAME)["grid"] == ov._shop_grid
    boxes["size"].delete(0, "end")
    boxes["size"].insert(0, "5")  # not a grid: refused, the old one stays
    ov._shop_fields_changed()
    assert ov._shop_grid == ShopGrid(300, 260, 640, 1920, 1080)
    assert "not saved" in ov._shop_note.cget("text")


def test_calibration_saves_the_clicked_corners(overlay, tmp_path):
    ov, _ = overlay
    ov._shop_calibrate()
    cal = ov._calibrator
    # Tk pixels are 0.8 of physical here (a 125% display): clicks are scaled.
    # The handlers are driven directly - synthetic Tk clicks are dropped when
    # the window is not mapped yet, which made this flaky in the full run.
    for x, y in ((412, 345), (1262, 1195)):
        cal._click(SimpleNamespace(x=round(x * 0.8), y=round(y * 0.8)))
    assert cal._grid is not None
    cal._finish(cal._grid)  # what Enter does
    g = ov._shop_grid
    assert (g.width, g.height) == (2560, 1440)
    assert g.left == pytest.approx(412, abs=1)
    assert g.size == pytest.approx(850, abs=2)
    assert load_saved(tmp_path / shopgrid.SHOP_OVERLAY_NAME)["grid"] == g
