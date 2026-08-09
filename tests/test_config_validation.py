"""M5: config validation produces precise startup errors, never tracebacks."""

import pytest

from sniper.config import ConfigError, load_config

BASE = """
league: auto
server: {{ host: 127.0.0.1, port: {port} }}
thresholds: {{ global_profit_div: {threshold} }}
alerts: {{ expiry_seconds: {expiry}, max_display: {max_display} }}
hotkey: {{ combo: "{combo}", consume: {consume} }}
ninja: {{ enabled: true, refresh_minutes: {refresh} }}
currency_rates: {{ chaos: 1, divine: {divine_rate} }}
prices: {{}}
mod_warnings: {warnings}
mod_scoring:
  rules: {scoring}
logging: {{ level: INFO, dir: logs }}
"""


def write(tmp_path, **overrides):
    values = {
        "port": 8765,
        "threshold": 10,
        "expiry": 12,
        "max_display": 3,
        "combo": "ctrl+alt+t",
        "consume": "all",
        "refresh": 10,
        "divine_rate": 180,
        "warnings": "[]",
        "scoring": "[]",
    }
    values.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(BASE.format(**values), encoding="utf-8")
    return path


def test_valid_config_loads(tmp_path):
    config = load_config(write(tmp_path))
    assert config.server.port == 8765
    assert config.thresholds.global_profit_div == 10


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"port": 99999}, "server.port"),
        ({"expiry": 0}, "expiry_seconds"),
        ({"max_display": 0}, "max_display"),
        ({"consume": "everything"}, "consume"),
        ({"combo": " "}, "combo"),
        ({"refresh": 0.1}, "refresh_minutes"),
        ({"divine_rate": -5}, "currency_rates.divine"),
        ({"threshold": "'lots'"}, "numeric"),
        (
            {"warnings": '[{ label: x, severity: warn, match: "reflect(", regex: true }]'},
            "invalid regex",
        ),
        (
            {"scoring": '[{ label: x, match: "a(", regex: true, min_base: 5 }]'},
            "invalid regex",
        ),
        ({"scoring": '[{ label: x, match: "a" }]'}, "min_base/multiplier/warning"),
        ({"scoring": "[{ label: x, min_base: 5 }]"}, "match"),
    ],
)
def test_bad_configs_raise_precise_errors(tmp_path, overrides, needle):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, **overrides))
    assert needle in str(err.value)


def test_unknown_section_key_names_allowed_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        BASE.format(
            port=8765,
            threshold=10,
            expiry=12,
            max_display=3,
            combo="ctrl+alt+t",
            consume="all",
            refresh=10,
            divine_rate=180,
            warnings="[]",
            scoring="[]",
        ).replace("alerts: { expiry_seconds: 12, max_display: 3 }", "alerts: { expiry_secs: 12 }"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "expiry_secs" in str(err.value)
    assert "expiry_seconds" in str(err.value)  # tells the user what IS allowed
