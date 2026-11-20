import pytest
from conftest import make_config

from sniper.prices import PriceBook


class FakeNinja:
    """Stands in for NinjaClient during PriceBook.refresh tests."""

    def __init__(self):
        self.currency = {
            "lines": [
                {"currencyTypeName": "Divine Orb", "chaosEquivalent": 197.2},
                {"currencyTypeName": "Exalted Orb", "chaosEquivalent": 2.0},
                {"currencyTypeName": "Mirror of Kalandra", "chaosEquivalent": 100937},
            ]
        }
        self.valdo = {
            "lines": [
                {"variant": "Foil Voices", "chaosValue": 1_000_000, "name": "Blazing Dwelling"},
                {"variant": "Foil Tabula Rasa", "chaosValue": 100_000, "name": "Cursed Spires"},
                {"variant": "Foil Tabula Rasa", "chaosValue": 120_000, "name": "Iron Nadir"},
                {"variant": "Foil Tabula Rasa", "chaosValue": 900_000, "name": "PriceFixed"},
                {"variant": "Bad Line", "chaosValue": 0, "name": "Zero"},
            ]
        }

    async def current_league(self) -> str:
        return "Testleague"

    async def currency_overview(self, league: str) -> dict:
        return self.currency

    async def valdo_overview(self, league: str) -> dict:
        return self.valdo


def test_manual_reference_with_config_rates(config):
    book = PriceBook(config)
    assert book.status == "manual"
    ref = book.reference_for("Foil Mageblood")
    assert ref.source == "manual"
    assert ref.display_currency == "divine"
    assert ref.chaos_value == 200 * 180
    assert book.reference_for("Foil Unknown Thing") is None


def test_to_chaos_and_unknown_currency(config):
    book = PriceBook(config)
    assert book.to_chaos(2, "divine") == 360
    assert book.to_chaos(5, "chaos") == 5
    assert book.to_chaos(1, "wisdom-scroll") is None


async def test_refresh_builds_medians_and_live_rates(config):
    book = PriceBook(config)
    stats = await book.refresh(FakeNinja())
    assert book.status == "live"
    assert stats["rewards"] == 2  # zero-value line dropped

    ref = book.reference_for("Foil Tabula Rasa")
    assert ref.source == "live"
    assert ref.chaos_value == 120_000  # median of 100k/120k/900k resists the outlier
    assert ref.display_currency == "divine"
    assert ref.display_amount == round(120_000 / 197.2, 1)

    # live rate beats the config rate while fresh
    assert book.rate_chaos("divine") == 197.2
    # manual price entry still overrides ninja
    assert book.reference_for("Foil Mageblood").source == "manual"


async def test_stale_marks_sources_and_falls_back_to_config_rates(config):
    book = PriceBook(config, fresh_ttl_s=-1.0)  # any age counts as stale
    await book.refresh(FakeNinja())
    assert book.status == "stale"
    assert book.reference_for("Foil Voices").source == "stale"
    # config rate preferred over stale ninja rate
    assert book.rate_chaos("divine") == 180


async def test_league_auto_resolution():
    config = make_config()
    object.__setattr__(config, "league", "auto")
    book = PriceBook(config)
    assert book.league is None
    await book.refresh(FakeNinja())
    assert book.league == "Testleague"


@pytest.mark.parametrize("currency,expected", [("chaos", 1.0), ("divine", 180.0)])
def test_rate_chaos_defaults(config, currency, expected):
    assert PriceBook(config).rate_chaos(currency) == expected


async def test_trade_price_layer_divine_native(config):
    book = PriceBook(config)
    await book.refresh(FakeNinja())  # live: divine = 197.2

    # trade average stored in divine; displayed verbatim regardless of rate
    book.set_trade_price("Foil Sublime Vision", 82.0)
    ref = book.reference_for("Foil Sublime Vision")
    assert ref.source == "trade"
    assert ref.display_amount == 82.0
    assert ref.display_currency == "divine"
    assert ref.chaos_value == 82.0 * 197.2  # chaos derived at CURRENT rate

    # trade beats the ninja median; ninja still covers unpriced rewards
    book.set_trade_price("Foil Tabula Rasa", 500.0)
    assert book.reference_for("Foil Tabula Rasa").source == "trade"
    assert book.reference_for("Foil Voices").source == "live"


def test_trade_price_expires_to_ninja_fallback(config):
    book = PriceBook(config)
    book._ninja_rewards = {"Foil Sublime Vision": 18284.0}
    book.set_trade_price("Foil Sublime Vision", 82.0)
    assert book.reference_for("Foil Sublime Vision").source == "trade"
    book._trade_rewards["Foil Sublime Vision"] = (82.0, -(10**9), False, 0.0)  # force stale
    ref = book.reference_for("Foil Sublime Vision")
    assert ref.source in ("live", "stale")  # fell back to the ninja median


def test_to_divine(config):
    book = PriceBook(config)
    assert book.to_divine(180, "chaos") == 1.0
    assert book.to_divine(82, "divine") == 82.0


# ------------------------------------------------- session price overrides
# The settings panel lets the user correct a reference price that is wrong.
# It must beat every automatic source, be reversible, and never be written
# anywhere - it is a fix for right now, not a new configuration.


def test_override_beats_every_other_source(config):
    book = PriceBook(config)
    book._ninja_rewards = {"Foil Mageblood": 30_000.0}
    book.set_trade_price("Foil Mageblood", 150.0)
    # config.yaml already carries a manual 200 divine for this reward
    assert book.reference_for("Foil Mageblood").source == "manual"

    book.set_override("Foil Mageblood", 175.0)
    ref = book.reference_for("Foil Mageblood")
    assert (ref.source, ref.display_amount, ref.display_currency) == ("override", 175.0, "divine")
    assert ref.chaos_value == 175.0 * 180  # divine rate from config


def test_override_is_reversible(config):
    book = PriceBook(config)
    book.set_trade_price("Foil Sublime Vision", 82.0)
    book.set_override("Foil Sublime Vision", 60.0)
    assert book.reference_for("Foil Sublime Vision").display_amount == 60.0

    book.set_override("Foil Sublime Vision", None)
    ref = book.reference_for("Foil Sublime Vision")
    assert (ref.source, ref.display_amount) == ("trade", 82.0)


def test_auto_reference_reports_what_the_override_replaced(config):
    """The panel shows the automatic price beside the field so a correction
    is typed next to the number it replaces."""
    book = PriceBook(config)
    book.set_trade_price("Foil Sublime Vision", 82.0)
    book.set_override("Foil Sublime Vision", 60.0)
    assert book.auto_reference_for("Foil Sublime Vision").display_amount == 82.0
    assert book.override_for("Foil Sublime Vision") == 60.0


def test_override_counts_as_a_primary_price(config):
    """A pinned price is authoritative, so the startup warm-up stops holding
    that reward's listings (see __main__._hold_listing)."""
    book = PriceBook(config)
    assert not book.has_primary_price("Foil Nimis")
    book.set_override("Foil Nimis", 40.0)
    assert book.has_primary_price("Foil Nimis")
    # nothing fetched it, so there is no age to report
    assert book.price_age_s("Foil Nimis") is None


def test_override_is_never_persisted(config):
    """trade_prices() is what gets written to price_cache.json."""
    book = PriceBook(config)
    book.set_override("Foil Nimis", 40.0)
    assert "Foil Nimis" not in book.trade_prices()


def test_override_does_not_block_the_underlying_refresh(config):
    """A trade refresh landing under an override is still recorded, so
    clearing the pin returns to a current price rather than a frozen one."""
    book = PriceBook(config)
    book.set_override("Foil Nimis", 40.0)
    book.set_trade_price("Foil Nimis", 51.0)
    assert book.reference_for("Foil Nimis").display_amount == 40.0
    book.set_override("Foil Nimis", None)
    assert book.reference_for("Foil Nimis").display_amount == 51.0
