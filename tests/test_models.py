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
