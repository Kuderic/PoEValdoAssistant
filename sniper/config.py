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
# alerts.* tier bars the settings panel saves (and load_config re-applies)
JACKPOT_KEYS = (
    "jackpot_min_net",
    "jackpot_min_yield",
    "super_jackpot_min_net",
    "super_jackpot_min_yield",
)


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
    saved_pairings = raw.get("pairings") or {}
    pairings = tuple(
        replace(p, multiplier=float(saved_pairings[p.label])) if p.label in saved_pairings else p
        for p in scoring.pairings
    )
    saved_risks = raw.get("risks") or {}
    risks = tuple(
        replace(r, fail_chance=min(1.0, max(0.0, float(saved_risks[r.label]))))
        if r.label in saved_risks
        else r
        for r in scoring.risks
    )
    return ModScoringConfig(
        base_default=float(raw.get("base_default", scoring.base_default)),
        rules=tuple(rules),
        pairings=pairings,
        risks=risks,
    )


def save_scoring_overrides(
    scoring: ModScoringConfig,
    path: Path,
    min_yield: float | None = None,
    hotkey_combo: str | None = None,
    alert_volume: float | None = None,
    running_cost: float | None = None,
    min_net: float | None = None,
    jackpot_min_net: float | None = None,
    jackpot_min_yield: float | None = None,
    super_jackpot_min_net: float | None = None,
    super_jackpot_min_yield: float | None = None,
    raise_overlay: bool | None = None,
) -> None:
    """Persist tuning-panel values (numbers, hotkey combo, alert volume;
    match patterns stay in config.yaml)."""
    data = {
        "base_default": scoring.base_default,
        "rules": {
            r.label: {"min_base": r.min_base, "multiplier": r.multiplier} for r in scoring.rules
        },
        # separate key: a pairing may share a label with nothing, but keeping
        # the namespaces apart means neither can ever clobber the other
        "pairings": {p.label: p.multiplier for p in scoring.pairings},
        # failure chances (0-1) per risk label; match patterns stay in config
        "risks": {r.label: r.fail_chance for r in scoring.risks},
    }
    if min_yield is not None:
        data["min_yield"] = min_yield
    if min_net is not None:
        data["min_net"] = min_net
    if hotkey_combo is not None:
        data["hotkey_combo"] = hotkey_combo
    if alert_volume is not None:
        data["alert_volume"] = alert_volume
    if running_cost is not None:
        data["running_cost"] = running_cost
    if jackpot_min_net is not None:
        data["jackpot_min_net"] = jackpot_min_net
    if jackpot_min_yield is not None:
        data["jackpot_min_yield"] = jackpot_min_yield
    if super_jackpot_min_net is not None:
        data["super_jackpot_min_net"] = super_jackpot_min_net
    if super_jackpot_min_yield is not None:
        data["super_jackpot_min_yield"] = super_jackpot_min_yield
    if raise_overlay is not None:
        data["raise_overlay"] = raise_overlay
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_extra_overrides(path: Path) -> dict:
    """Non-scoring values saved by the settings panel (threshold, hotkey,
    alert volume)."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    keys = (
        "min_yield",
        "min_net",
        "hotkey_combo",
        "alert_volume",
        "running_cost",
        "raise_overlay",
        *JACKPOT_KEYS,
    )
    return {k: raw[k] for k in keys if raw.get(k) is not None}


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class Thresholds:
    """Profit thresholds in divine orbs (absolute, not percentages)."""

    min_yield: float = 10.0
    # A listing must ALSO net at least this many divines to alert: a cheap
    # trivial map can clear the yield bar on a profit too small to bother
    # travelling for. 0 = yield alone decides.
    min_net: float = 0.0
    per_map: dict[str, float] = field(default_factory=dict)
    # Fixed divine toll subtracted from every listing's profit: running any
    # map costs time, so an easy map with a tiny margin is not really worth
    # it. Comes off before profit, yield and the alert cutoff are computed.
    running_cost: float = 1.0


@dataclass(frozen=True)
class AlertsConfig:
    expiry_seconds: float = 12.0
    max_display: int = 3
    sound: str = ""
    # Non-Valdo live-search alerts get their own sound so they are
    # distinguishable without looking; empty falls back to sound.GENERIC_WAV.
    generic_sound: str = ""
    volume: float = 0.5  # 0.0-1.0; scales both alert WAVs (see sniper/sound.py)
    traveled_display_seconds: float = 10.0  # keep the traveled map's mods readable
    feed_rows: int = 40  # live-feed history rows (the area scrolls)
    # Jackpot: net AND yield both at or above these (see margin.is_jackpot).
    # Gets its own colour and the hotkey's top slot.
    jackpot_min_net: float = 30.0
    jackpot_min_yield: float = 50.0
    # A jackpot plays an alarm (twice; a super jackpot's three times)
    # instead of the ordinary alert sound.
    # jackpot_sound empty -> sound.JACKPOT_WAV. Plays at `volume`.
    jackpot_alarm: bool = True
    jackpot_sound: str = ""
    # Super jackpot: a jackpot that also clears these higher bars. Outranks
    # a jackpot, gold styling, own alarm (empty -> sound.SUPER_JACKPOT_WAV).
    super_jackpot_min_net: float = 60.0
    super_jackpot_min_yield: float = 100.0
    super_jackpot_sound: str = ""


@dataclass(frozen=True)
class HotkeyConfig:
    combo: str = "ctrl+alt+t"
    consume: str = "all"  # "all" | "top"
    # bring the overlay to the front (restoring it if minimized) on every
    # press, without taking focus from the game
    raise_overlay: bool = False


@dataclass(frozen=True)
class NinjaConfig:
    enabled: bool = True
    refresh_minutes: float = 10.0
    base_url: str = "https://poe.ninja"


@dataclass(frozen=True)
class TradePricingConfig:
    """Official trade API pricing of unid uniques for active rewards."""

    enabled: bool = True
    # Every active reward is re-priced each cycle, so the request rate scales
    # with how many live searches are open. At 10 a busy session earned a
    # 30-minute API backoff; 30 cuts the sustained load to a third and still
    # re-prices well inside the cached-price staleness window.
    refresh_minutes: float = 30.0
    max_listings: int = 3  # cheapest N listings averaged (after outlier drop)
    # listings below this fraction of the sample median are price-fixers
    # (the perennial 2-chaos Mageblood) and are dropped before averaging
    outlier_cutoff: float = 0.5
    # unid+uncorrupted needs at least this many listings, else fall back to
    # identified+uncorrupted (and to identified+corrupted if that has none)
    min_unid_listings: int = 10
    # which listings are the market: securable (instant buyout - all async
    # trade can buy) | online | any (adds offline/negotiable listings, whose
    # stale prices undercut the real market)
    listing_status: str = "securable"
    # Reward prices are written to price_cache.json and reloaded at startup
    # so listings can be judged immediately instead of waiting out a fetch.
    # Anything older than this is ignored as too stale to trade on.
    cache_max_age_minutes: float = 720.0
    base_url: str = "https://www.pathofexile.com"
    # Uniques that only exist corrupted - searched unid + corrupted: true
    # (skipping the fallback ladder).
    corrupted_uniques: tuple[str, ...] = (
        "Impossible Escape",
        "Forbidden Flame",
        "Forbidden Flesh",
        "Rain of Splinters",
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
class ModPairing:
    """Two mods that are far worse together than the product of their
    individual multipliers suggests - e.g. a damage penalty plus a timer is
    not 'harder', it is unrunnable without a specific build.

    Kept separate from ModScoringRule because a pairing must NOT merge its
    mod lines into one display row (the individual rules still own those
    lines) and because it carries a `note` explaining what actually goes
    wrong, which is the part worth reading.
    """

    label: str
    match_all: tuple[str, ...]
    multiplier: float
    note: str = ""
    regex: bool = False


@dataclass(frozen=True)
class ModRisk:
    """A mod (or combination) that can make the map FAIL outright, losing
    what you paid for it. Unlike a difficulty multiplier this is not about
    effort: it turns net into an expected value,
        net = (1 - fail_chance) x market price - asking price - running cost
    e.g. Bismuth bugging out, or Protect the Altar under less damage.
    Several risks on one map combine as independent chances."""

    label: str
    match_all: tuple[str, ...]  # every pattern must find a line (one is fine)
    fail_chance: float  # 0.0-1.0
    note: str = ""
    regex: bool = False
    # skip this risk when ANY of these patterns finds a line: lets a plain
    # "Ultimatum 10%" step aside for the more specific "Ultimatum + less
    # damage 25%" instead of stacking with it
    unless: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModScoringConfig:
    """required_profit = min_yield * score / 100: the base threshold
    is what a 100-difficulty map needs; 200 difficulty doubles it."""

    base_default: float = 25.0
    rules: tuple[ModScoringRule, ...] = ()
    pairings: tuple[ModPairing, ...] = ()
    risks: tuple[ModRisk, ...] = ()


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
class UpdatesConfig:
    """Self-update from GitHub Releases (sniper.updater). The packaged exe
    downloads a newer release in the background and installs it when the
    user clicks the update pill or on the next launch; a source checkout
    only says a newer version exists."""

    enabled: bool = True
    repo: str = "Kuderic/PoEValdoAssistant"
    check_interval_hours: float = 6.0
    # False: only announce a new version, never download it
    auto_download: bool = True
    api_base: str = "https://api.github.com"


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
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    # folder for per-user state: scoring_overrides.yaml, price_cache.json
    state_dir: Path = field(default_factory=Path)


def _read_yaml(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must be a mapping of settings")
    return raw


def merge_config(base: dict, over: dict) -> dict:
    """`over` layered on `base`: mappings merge key by key, anything else
    (lists included - a list of pairings is replaced, not appended to) is
    taken from `over`."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    path: str | Path = "config.yaml",
    defaults: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> Config:
    """`path` is the config to load. `defaults`, when given, is a full config
    that `path` is layered over (the packaged exe: bundled defaults + the
    user's partial config.yaml); `path` may then be missing. `state_dir` is
    where scoring_overrides.yaml lives (default: beside `path`)."""
    load_dotenv()
    path = Path(path)
    if defaults is not None:
        raw = _read_yaml(Path(defaults))
        if path.exists() and path.resolve() != Path(defaults).resolve():
            raw = merge_config(raw, _read_yaml(path))
    elif not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()}")
    else:
        raw = _read_yaml(path)
    state = Path(state_dir) if state_dir is not None else path.parent
    overrides_path = state / SCORING_OVERRIDES_NAME

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
    if not 0.0 <= alerts.volume <= 1.0:
        raise ConfigError(f"alerts.volume must be between 0 and 1, got {alerts.volume}")
    if alerts.jackpot_min_net < 0 or alerts.jackpot_min_yield < 0:
        raise ConfigError("alerts.jackpot_min_net / jackpot_min_yield must be >= 0")
    if alerts.super_jackpot_min_net < 0 or alerts.super_jackpot_min_yield < 0:
        raise ConfigError("alerts.super_jackpot_min_net / super_jackpot_min_yield must be >= 0")

    ninja = build(NinjaConfig, "ninja")
    if ninja.enabled and ninja.refresh_minutes < 1:
        raise ConfigError("ninja.refresh_minutes must be >= 1 (be kind to poe.ninja)")

    trade_pricing = build(TradePricingConfig, "trade_pricing")
    if trade_pricing.listing_status not in ("securable", "online", "any"):
        raise ConfigError(
            "trade_pricing.listing_status must be securable, online or any, "
            f"got {trade_pricing.listing_status!r}"
        )
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
    # Labels are the identity of a rule: the tuning panel keys its entry
    # widgets by label and save_scoring_overrides writes a label->values
    # dict, so a duplicate silently discards one row's edits and applies one
    # value to both rules. Catch it here instead.
    seen_labels = set()
    for rule in scoring_rules:
        if rule.label in seen_labels:
            raise ConfigError(
                f"mod_scoring.rules has two rules labelled {rule.label!r}; "
                "labels must be unique (the settings panel keys on them)"
            )
        seen_labels.add(rule.label)

    pairings = []
    pairing_labels = set()
    for i, entry in enumerate(raw.get("mod_pairings") or []):
        if not isinstance(entry, dict) or "label" not in entry:
            raise ConfigError(f"mod_pairings[{i}] needs 'label'")
        patterns = entry.get("match_all") or []
        if not isinstance(patterns, list) or len(patterns) < 2:
            raise ConfigError(
                f"mod_pairings[{i}] ({entry['label']}) needs 'match_all' with at least "
                "two patterns - a pairing is about mods appearing TOGETHER"
            )
        if entry.get("multiplier") is None:
            raise ConfigError(f"mod_pairings[{i}] ({entry['label']}) needs 'multiplier'")
        if entry["label"] in pairing_labels:
            raise ConfigError(f"mod_pairings has two entries labelled {entry['label']!r}")
        pairing_labels.add(entry["label"])
        for pattern in patterns:
            check_pattern(f"mod_pairings[{i}]", pattern, bool(entry.get("regex")))
        pairings.append(
            ModPairing(
                label=entry["label"],
                match_all=tuple(patterns),
                multiplier=float(entry["multiplier"]),
                note=str(entry.get("note", "")),
                regex=bool(entry.get("regex", False)),
            )
        )

    risks = []
    risk_labels = set()
    for i, entry in enumerate(raw.get("mod_risks") or []):
        if not isinstance(entry, dict) or "label" not in entry:
            raise ConfigError(f"mod_risks[{i}] needs 'label'")
        label = entry["label"]
        patterns = entry.get("match_all") or ([entry["match"]] if entry.get("match") else [])
        if not isinstance(patterns, list) or not patterns:
            raise ConfigError(f"mod_risks[{i}] ({label}) needs 'match' or 'match_all'")
        try:
            chance = float(entry["fail_chance"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(
                f"mod_risks[{i}] ({label}) needs 'fail_chance' between 0 and 1"
            ) from None
        if not 0.0 <= chance <= 1.0:
            raise ConfigError(
                f"mod_risks[{i}] ({label}) fail_chance must be between 0 and 1, got {chance}"
                " (0.3 means 30%)"
            )
        if label in risk_labels:
            raise ConfigError(f"mod_risks has two entries labelled {label!r}")
        risk_labels.add(label)
        unless = entry.get("unless") or []
        if isinstance(unless, str):
            unless = [unless]
        for pattern in [*patterns, *unless]:
            check_pattern(f"mod_risks[{i}]", pattern, bool(entry.get("regex")))
        risks.append(
            ModRisk(
                label=label,
                match_all=tuple(patterns),
                fail_chance=chance,
                note=str(entry.get("note", "")),
                regex=bool(entry.get("regex", False)),
                unless=tuple(unless),
            )
        )

    mod_scoring = ModScoringConfig(
        base_default=float(scoring_raw.get("base_default", 25.0)),
        rules=tuple(scoring_rules),
        pairings=tuple(pairings),
        risks=tuple(risks),
    )
    mod_scoring = apply_scoring_overrides(mod_scoring, overrides_path)

    prices = {}
    for name, p in section("prices").items():
        if not isinstance(p, dict) or "currency" not in p or "amount" not in p:
            raise ConfigError(f"prices[{name!r}] needs 'currency' and 'amount'")
        prices[name] = ManualPrice(currency=str(p["currency"]), amount=float(p["amount"]))

    try:
        thresholds = Thresholds(
            min_yield=float(section("thresholds").get("min_yield", 10.0)),
            min_net=float(section("thresholds").get("min_net", 0.0)),
            per_map={k: float(v) for k, v in (section("thresholds").get("per_map") or {}).items()},
            running_cost=float(section("thresholds").get("running_cost", 1.0)),
        )
        currency_rates = {k: float(v) for k, v in section("currency_rates").items()}
    except (TypeError, ValueError) as e:
        raise ConfigError(f"thresholds/currency_rates must be numeric: {e}") from None
    for name, rate in currency_rates.items():
        if rate <= 0:
            raise ConfigError(f"currency_rates.{name} must be > 0, got {rate}")

    # settings-panel overrides (saved next to the config) win over config.yaml
    extra = load_extra_overrides(overrides_path)
    if "min_yield" in extra:
        thresholds = replace(thresholds, min_yield=float(extra["min_yield"]))
    if "min_net" in extra:
        thresholds = replace(thresholds, min_net=float(extra["min_net"]))
    if thresholds.min_net < 0:
        raise ConfigError(f"thresholds.min_net must be >= 0, got {thresholds.min_net}")
    if "hotkey_combo" in extra:
        hotkey = replace(hotkey, combo=str(extra["hotkey_combo"]))
    if "raise_overlay" in extra:
        hotkey = replace(hotkey, raise_overlay=extra["raise_overlay"] is True)
    if "running_cost" in extra:
        thresholds = replace(thresholds, running_cost=float(extra["running_cost"]))
    if "alert_volume" in extra:
        volume = min(1.0, max(0.0, float(extra["alert_volume"])))
        alerts = replace(alerts, volume=volume)
    for key in JACKPOT_KEYS:
        if key in extra:
            alerts = replace(alerts, **{key: max(0.0, float(extra[key]))})

    updates = build(UpdatesConfig, "updates")
    if updates.check_interval_hours < 1:
        raise ConfigError("updates.check_interval_hours must be at least 1")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", updates.repo):
        raise ConfigError(f"updates.repo must look like owner/name, got {updates.repo!r}")

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
        updates=updates,
        state_dir=state,
    )
