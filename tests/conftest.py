from __future__ import annotations

import pytest

from sniper.config import (
    AlertsConfig,
    Config,
    GameConfig,
    HotkeyConfig,
    LoggingConfig,
    ManualPrice,
    ModScoringConfig,
    ModScoringRule,
    ModWarningRule,
    NinjaConfig,
    ServerConfig,
    Thresholds,
)


def make_config(
    *,
    global_profit_div: float = 20.0,
    per_map: dict[str, float] | None = None,
    prices: dict[str, ManualPrice] | None = None,
    currency_rates: dict[str, float] | None = None,
    mod_warnings: tuple[ModWarningRule, ...] = (),
    scoring_rules: tuple[ModScoringRule, ...] = (),
    scoring_base_default: float = 25.0,
    scoring_div_per_point: float = 0.2,
    ninja_enabled: bool = False,
    port: int = 8765,
) -> Config:
    return Config(
        league="Testleague",
        server=ServerConfig(host="127.0.0.1", port=port),
        thresholds=Thresholds(global_profit_div=global_profit_div, per_map=per_map or {}),
        alerts=AlertsConfig(),
        hotkey=HotkeyConfig(),
        ninja=NinjaConfig(enabled=ninja_enabled),
        currency_rates=currency_rates or {"chaos": 1, "divine": 180, "exalted": 2},
        prices=prices
        if prices is not None
        else {"Foil Mageblood": ManualPrice(currency="divine", amount=200)},
        mod_warnings=mod_warnings,
        mod_scoring=ModScoringConfig(
            base_default=scoring_base_default,
            div_per_point=scoring_div_per_point,
            rules=scoring_rules,
        ),
        game=GameConfig(),
        logging=LoggingConfig(),
    )


@pytest.fixture
def config() -> Config:
    return make_config()


def make_listing_frame(**overrides) -> dict:
    frame = {
        "type": "new_listing",
        "search_id": "search1",
        "tab_id": "tab1",
        "listing_id": "lst1",
        "item_name": "Twisted Sands",
        "price": {"amount": 100, "currency": "divine"},
        "seller": "SomeSeller",
        "reward": "Foil Mageblood",
        "mods": ["Monsters fire 2 additional Projectiles"],
        "row_index": 0,
        "detected_at": "2026-08-09T12:00:00.000Z",
    }
    frame.update(overrides)
    return frame
