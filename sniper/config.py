"""Load config.yaml and .env. Full validation with precise errors lands in M5."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


# Written by the overlay's tuning panel; merged over config.yaml's
# mod_scoring at load time so quick tweaks survive restarts without
# rewriting the (commented) main config.
SCORING_OVERRIDES_NAME = "scoring_overrides.yaml"


def apply_scoring_overrides(scoring: ModScoringConfig, path: Path) -> ModScoringConfig:
    if not path.exists():
        return scoring
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    per_label = raw.get("rules") or {}
    rules = []
    for r in scoring.rules:
        o = per_label.get(r.label) or {}
        rules.append(
            replace(
                r,
                min_base=float(o["min_base"]) if o.get("min_base") is not None else r.min_base,
                multiplier=float(o["multiplier"])
                if o.get("multiplier") is not None
                else r.multiplier,
            )
        )
    return ModScoringConfig(
        base_default=float(raw.get("base_default", scoring.base_default)),
        rules=tuple(rules),
    )


def save_scoring_overrides(
    scoring: ModScoringConfig,
    path: Path,
    global_profit_div: float | None = None,
    hotkey_combo: str | None = None,
) -> None:
    """Persist tuning-panel values (numbers + hotkey combo; match patterns
    stay in config.yaml)."""
    data = {
        "base_default": scoring.base_default,
        "rules": {
            r.label: {"min_base": r.min_base, "multiplier": r.multiplier} for r in scoring.rules
        },
    }
    if global_profit_div is not None:
        data["global_profit_div"] = global_profit_div
    if hotkey_combo is not None:
        data["hotkey_combo"] = hotkey_combo
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_extra_overrides(path: Path) -> dict:
    """Non-scoring values saved by the settings panel (threshold, hotkey)."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: raw[k] for k in ("global_profit_div", "hotkey_combo") if raw.get(k) is not None}


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class Thresholds:
    """Profit thresholds in divine orbs (absolute, not percentages)."""

    global_profit_div: float = 10.0
    per_map: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertsConfig:
    expiry_seconds: float = 12.0
    max_display: int = 3
    sound: str = ""
    traveled_display_seconds: float = 10.0  # keep the traveled map's mods readable
    feed_rows: int = 6  # live-feed rows shown at the bottom of the overlay


@dataclass(frozen=True)
class HotkeyConfig:
    combo: str = "ctrl+alt+t"
    consume: str = "all"  # "all" | "top"


@dataclass(frozen=True)
class NinjaConfig:
    enabled: bool = True
    refresh_minutes: float = 10.0
    base_url: str = "https://poe.ninja"


@dataclass(frozen=True)
class TradePricingConfig:
    """Official trade API pricing of unid uniques for active rewards."""

    enabled: bool = True
    refresh_minutes: float = 10.0
    max_listings: int = 3  # cheapest N listings averaged
    base_url: str = "https://www.pathofexile.com"
    # Uniques that only exist corrupted - searched with corrupted: true
    # (everything else is priced from uncorrupted listings).
    corrupted_uniques: tuple[str, ...] = (
        "Impossible Escape",
        "The Adorned",
        "Forbidden Flame",
        "Forbidden Flesh",
    )


@dataclass(frozen=True)
class ModWarningRule:
    label: str
    severity: str  # "warn" | "block"
    match: str | None = None
    regex: bool = False
    match_all: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModScoringRule:
    """One difficulty-scoring rule. min_base raises the base score floor,
    multiplier scales the final score, warning ("red" | "yellow") surfaces a
    colored special chip on the overlay."""

    label: str
    match: str | None = None
    regex: bool = False
    match_all: tuple[str, ...] = ()
    min_base: float | None = None
    multiplier: float | None = None
    warning: str | None = None  # "red" | "yellow" | None


@dataclass(frozen=True)
class ModScoringConfig:
    """required_profit = global_profit_div * score / 100: the base threshold
    is what a 100-difficulty map needs; 200 difficulty doubles it."""

    base_default: float = 25.0
    rules: tuple[ModScoringRule, ...] = ()


@dataclass(frozen=True)
class ManualPrice:
    currency: str
    amount: float


@dataclass(frozen=True)
class GameConfig:
    process_names: tuple[str, ...] = (
        "PathOfExile.exe",
        "PathOfExile_x64.exe",
        "PathOfExileSteam.exe",
        "PathOfExile_KG.exe",
    )
    window_title: str = "Path of Exile"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"


@dataclass(frozen=True)
class Config:
    league: str
    server: ServerConfig
    thresholds: Thresholds
    alerts: AlertsConfig
    hotkey: HotkeyConfig
    ninja: NinjaConfig
    trade_pricing: TradePricingConfig
    currency_rates: dict[str, float]
    prices: dict[str, ManualPrice]
    mod_warnings: tuple[ModWarningRule, ...]
    mod_scoring: ModScoringConfig
    game: GameConfig
    logging: LoggingConfig


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def section(name: str) -> dict:
        value = raw.get(name) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"config section '{name}' must be a mapping")
        return value

    def build(cls, name: str):
        data = section(name)
        allowed = {f.name for f in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(
                f"config section '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            )
        try:
            return cls(**data)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"config section '{name}' is invalid: {e}") from None

    def check_pattern(context: str, pattern: str, is_regex: bool) -> None:
        if is_regex:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                raise ConfigError(f"{context}: invalid regex {pattern!r} ({e})") from None

    server = build(ServerConfig, "server")
    if not 1 <= int(server.port) <= 65535:
        raise ConfigError(f"server.port must be 1-65535, got {server.port}")

    alerts = build(AlertsConfig, "alerts")
    if alerts.expiry_seconds <= 0:
        raise ConfigError("alerts.expiry_seconds must be > 0")
    if alerts.max_display < 1:
        raise ConfigError("alerts.max_display must be >= 1")

    ninja = build(NinjaConfig, "ninja")
    if ninja.enabled and ninja.refresh_minutes < 1:
        raise ConfigError("ninja.refresh_minutes must be >= 1 (be kind to poe.ninja)")

    trade_pricing = build(TradePricingConfig, "trade_pricing")
    if trade_pricing.enabled and trade_pricing.refresh_minutes < 5:
        raise ConfigError("trade_pricing.refresh_minutes must be >= 5 (trade API rate limits)")

    hotkey = build(HotkeyConfig, "hotkey")
    if hotkey.consume not in ("all", "top"):
        raise ConfigError(f"hotkey.consume must be 'all' or 'top', got {hotkey.consume!r}")
    if not hotkey.combo.strip():
        raise ConfigError("hotkey.combo must not be empty")

    rules = []
    for i, entry in enumerate(raw.get("mod_warnings") or []):
        if not isinstance(entry, dict) or "label" not in entry or "severity" not in entry:
            raise ConfigError(f"mod_warnings[{i}] needs 'label' and 'severity'")
        if entry["severity"] not in ("warn", "block"):
            raise ConfigError(f"mod_warnings[{i}].severity must be 'warn' or 'block'")
        if not entry.get("match") and not entry.get("match_all"):
            raise ConfigError(f"mod_warnings[{i}] needs 'match' or 'match_all'")
        for pattern in [entry.get("match")] if entry.get("match") else entry.get("match_all", []):
            check_pattern(f"mod_warnings[{i}]", pattern, bool(entry.get("regex")))
        rules.append(
            ModWarningRule(
                label=entry["label"],
                severity=entry["severity"],
                match=entry.get("match"),
                regex=bool(entry.get("regex", False)),
                match_all=tuple(entry.get("match_all") or ()),
            )
        )

    scoring_raw = section("mod_scoring")
    scoring_rules = []
    for i, entry in enumerate(scoring_raw.get("rules") or []):
        if not isinstance(entry, dict) or "label" not in entry:
            raise ConfigError(f"mod_scoring.rules[{i}] needs 'label'")
        if not entry.get("match") and not entry.get("match_all"):
            raise ConfigError(f"mod_scoring.rules[{i}] needs 'match' or 'match_all'")
        if (
            entry.get("min_base") is None
            and entry.get("multiplier") is None
            and not entry.get("warning")
        ):
            raise ConfigError(
                f"mod_scoring.rules[{i}] needs at least one of min_base/multiplier/warning"
            )
        warning = entry.get("warning")
        if warning is True:  # back-compat: bare `warning: true` means red
            warning = "red"
        if warning not in (None, False, "red", "yellow"):
            raise ConfigError(f"mod_scoring.rules[{i}].warning must be 'red' or 'yellow'")
        for pattern in [entry.get("match")] if entry.get("match") else entry.get("match_all", []):
            check_pattern(f"mod_scoring.rules[{i}]", pattern, bool(entry.get("regex")))
        scoring_rules.append(
            ModScoringRule(
                label=entry["label"],
                match=entry.get("match"),
                regex=bool(entry.get("regex", False)),
                match_all=tuple(entry.get("match_all") or ()),
                min_base=None if entry.get("min_base") is None else float(entry["min_base"]),
                multiplier=None if entry.get("multiplier") is None else float(entry["multiplier"]),
                warning=warning or None,
            )
        )
    mod_scoring = ModScoringConfig(
        base_default=float(scoring_raw.get("base_default", 25.0)),
        rules=tuple(scoring_rules),
    )
    mod_scoring = apply_scoring_overrides(mod_scoring, path.with_name(SCORING_OVERRIDES_NAME))

    prices = {}
    for name, p in section("prices").items():
        if not isinstance(p, dict) or "currency" not in p or "amount" not in p:
            raise ConfigError(f"prices[{name!r}] needs 'currency' and 'amount'")
        prices[name] = ManualPrice(currency=str(p["currency"]), amount=float(p["amount"]))

    try:
        thresholds = Thresholds(
            global_profit_div=float(section("thresholds").get("global_profit_div", 10.0)),
            per_map={k: float(v) for k, v in (section("thresholds").get("per_map") or {}).items()},
        )
        currency_rates = {k: float(v) for k, v in section("currency_rates").items()}
    except (TypeError, ValueError) as e:
        raise ConfigError(f"thresholds/currency_rates must be numeric: {e}") from None
    for name, rate in currency_rates.items():
        if rate <= 0:
            raise ConfigError(f"currency_rates.{name} must be > 0, got {rate}")

    # settings-panel overrides (saved next to the config) win over config.yaml
    extra = load_extra_overrides(path.with_name(SCORING_OVERRIDES_NAME))
    if "global_profit_div" in extra:
        thresholds = replace(thresholds, global_profit_div=float(extra["global_profit_div"]))
    if "hotkey_combo" in extra:
        hotkey = replace(hotkey, combo=str(extra["hotkey_combo"]))

    game_raw = section("game")
    return Config(
        league=str(raw.get("league", "")),
        server=server,
        thresholds=thresholds,
        alerts=alerts,
        hotkey=hotkey,
        ninja=ninja,
        trade_pricing=trade_pricing,
        currency_rates=currency_rates,
        prices=prices,
        mod_warnings=tuple(rules),
        mod_scoring=mod_scoring,
        game=GameConfig(
            process_names=tuple(game_raw.get("process_names") or GameConfig.process_names),
            window_title=str(game_raw.get("window_title", GameConfig.window_title)),
        ),
        logging=build(LoggingConfig, "logging"),
    )
