import pytest
from conftest import make_listing_frame

from sniper.models import ClickResult, FrameError, Hello, Listing, parse_frame


def test_valid_new_listing():
    frame = parse_frame(make_listing_frame())
    assert isinstance(frame, Listing)
    assert frame.listing_id == "lst1"
    assert frame.price.amount == 100.0
    assert frame.price.currency == "divine"
    assert frame.mods == ("Monsters fire 2 additional Projectiles",)
    assert frame.price_key == "Foil Mageblood"


def test_currency_lowercased():
    frame = parse_frame(make_listing_frame(price={"amount": 5, "currency": "Divine"}))
    assert isinstance(frame, Listing)
    assert frame.price.currency == "divine"


def test_price_key_falls_back_to_item_name():
    frame = parse_frame(make_listing_frame(reward=None))
    assert isinstance(frame, Listing)
    assert frame.price_key == "Twisted Sands"


def test_missing_fields_rejected():
    frame = parse_frame(make_listing_frame(seller=""))
    assert isinstance(frame, FrameError)
    assert "seller" in frame.reason


def test_bad_price_rejected():
    for price in (
        None,
        {},
        {"amount": "x", "currency": "divine"},
        {"amount": -5, "currency": "divine"},
        {"amount": 5},
    ):
        frame = parse_frame(make_listing_frame(price=price))
        assert isinstance(frame, FrameError), price


def test_invalid_mods_rejected():
    frame = parse_frame(make_listing_frame(mods=[1, 2]))
    assert isinstance(frame, FrameError)


def test_hello_and_click_result():
    assert parse_frame({"type": "hello", "search_id": "s", "tab_id": "t"}) == Hello("s", "t")
    cr = parse_frame({"type": "click_result", "listing_id": "x", "ok": False, "reason": "row_gone"})
    assert cr == ClickResult("x", False, "row_gone")


def test_unknown_and_garbage():
    assert isinstance(parse_frame({"type": "warp_drive"}), FrameError)
    assert isinstance(parse_frame("not a dict"), FrameError)
    assert isinstance(parse_frame({"type": "hello"}), FrameError)


def test_hello_carries_userscript_version():
    frame = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t", "version": "0.6.0"})
    assert frame.version == "0.6.0"


def test_hello_without_version_is_still_valid():
    """Older tabs predate the field; they must still connect."""
    frame = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t"})
    assert frame.version is None


def test_hello_rejects_non_string_version():
    frame = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t", "version": 6})
    assert frame.version is None


# ------------------------------------------------------------ index lag
# GGG's index time vs when the browser saw it: the head start every other
# sniper had. Only the network capture path can supply it.


def test_index_lag_measures_head_start():
    frame = parse_frame(
        make_listing_frame(
            indexed_at="2026-08-10T06:23:39Z",
            detected_at="2026-08-10T06:23:42.500Z",
        )
    )
    assert frame.index_lag_ms == pytest.approx(3500)


def test_index_lag_none_without_an_index_time():
    """DOM-captured rows, and any tab on a userscript older than 0.7.0."""
    frame = parse_frame(make_listing_frame(detected_at="2026-08-10T06:23:42Z"))
    assert frame.indexed_at is None
    assert frame.index_lag_ms is None


def test_index_lag_survives_a_junk_timestamp():
    frame = parse_frame(make_listing_frame(indexed_at="not a timestamp"))
    assert frame.index_lag_ms is None


def test_index_lag_handles_offset_timezones():
    frame = parse_frame(
        make_listing_frame(
            indexed_at="2026-08-10T06:23:39+00:00",
            detected_at="2026-08-10T08:23:40+02:00",  # same instant +1s
        )
    )
    assert frame.index_lag_ms == pytest.approx(1000)


def test_negative_lag_is_reported_not_clamped():
    """A browser clock behind GGG's yields a negative figure; surfacing it
    is how clock skew becomes visible instead of silently biasing the stat."""
    frame = parse_frame(
        make_listing_frame(indexed_at="2026-08-10T06:23:45Z", detected_at="2026-08-10T06:23:42Z")
    )
    assert frame.index_lag_ms == pytest.approx(-3000)


def test_capture_source_recorded():
    assert parse_frame(make_listing_frame(capture="net")).capture == "net"
    assert parse_frame(make_listing_frame(capture="dom")).capture == "dom"


def test_capture_source_defaults_blank_for_older_userscripts():
    assert parse_frame(make_listing_frame()).capture == ""
    assert parse_frame(make_listing_frame(capture="nonsense")).capture == ""
