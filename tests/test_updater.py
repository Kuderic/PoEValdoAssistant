"""Self-update from GitHub Releases (sniper.updater) and the packaged exe's
layered config (sniper.paths / load_config(defaults=...)).

The network is an httpx.MockTransport; the "exe" is a file in tmp_path, so
the swap logic runs for real on the filesystem without a packaged build.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from sniper import updater
from sniper.bus import UpdateStatus
from sniper.config import (
    SCORING_OVERRIDES_NAME,
    ConfigError,
    UpdatesConfig,
    load_config,
    merge_config,
)
from sniper.updater import (
    ASSET_NAME,
    Updater,
    apply_staged,
    cleanup_old,
    is_newer,
    parse_release,
    parse_version,
    relaunch_args,
    staged_files,
    staged_version,
    wait_hint_s,
)

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.example/repos/o/r/releases/latest"
ASSET_URL = "https://dl.example/ValdoSniper.exe"
BUILD = b"MZ" + b"\x01" * 1_200_000  # big enough to pass the sanity floor
SHA = hashlib.sha256(BUILD).hexdigest()
BUILD_SIZE = len(BUILD)


def release_json(tag="v1.1.0", sha=SHA, size=BUILD_SIZE, **kw) -> dict:
    asset = {"name": ASSET_NAME, "browser_download_url": ASSET_URL, "size": size}
    if sha:
        asset["digest"] = f"sha256:{sha}"
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/o/r/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
        "assets": [asset],
        **kw,
    }


# ------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("tag", "parsed"),
    [("v1.2.3", (1, 2, 3)), ("1.10.0", (1, 10, 0)), ("v1.2", None), ("latest", None)],
)
def test_parse_version(tag, parsed):
    assert parse_version(tag) == parsed


def test_versions_compare_numerically_not_as_text():
    assert is_newer("1.10.0", "1.9.9")
    assert not is_newer("1.0.0", "1.0.0")
    assert not is_newer("0.9.0", "1.0.0")
    assert not is_newer("garbage", "1.0.0")


def test_the_app_has_a_release_version():
    from sniper import __version__

    assert parse_version(__version__) is not None


# -------------------------------------------------------------- release


def test_parse_release_finds_the_exe_and_its_digest():
    r = parse_release(release_json())
    assert (r.version, r.asset_url, r.asset_size, r.sha256) == ("1.1.0", ASSET_URL, len(BUILD), SHA)


def test_drafts_and_prereleases_are_ignored():
    assert parse_release(release_json(draft=True)) is None
    assert parse_release(release_json(prerelease=True)) is None


def test_a_release_without_the_exe_can_only_be_announced():
    data = release_json()
    data["assets"] = [{"name": "notes.txt"}]
    r = parse_release(data)
    assert r.version == "1.1.0" and r.asset_url is None


# ---------------------------------------------------------- rate limits


def test_retry_after_is_honoured():
    assert wait_hint_s(httpx.Headers({"Retry-After": "120"})) == 120


def test_an_exhausted_quota_waits_for_its_reset():
    headers = httpx.Headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"})
    assert wait_hint_s(headers, now=400) == 600
    assert wait_hint_s(httpx.Headers({"X-RateLimit-Remaining": "5"})) is None


# ------------------------------------------------------- staged install


def fake_exe(tmp_path: Path, content: bytes = b"old build") -> Path:
    exe = tmp_path / ASSET_NAME
    exe.write_bytes(content)
    return exe


def stage(exe: Path, version: str, content: bytes = BUILD) -> None:
    new, meta, _part, _old = staged_files(exe)
    new.write_bytes(content)
    meta.write_text(json.dumps({"version": version, "size": len(content)}), encoding="utf-8")


def test_apply_staged_swaps_the_exe_and_keeps_the_old_one_aside(tmp_path):
    exe = fake_exe(tmp_path)
    stage(exe, "1.1.0")
    assert apply_staged(exe, "1.0.0") == "1.1.0"
    assert exe.read_bytes() == BUILD
    assert staged_files(exe)[3].read_bytes() == b"old build"
    assert staged_version(exe) is None  # consumed
    cleanup_old(exe)
    assert not staged_files(exe)[3].exists()


def test_a_staged_build_that_is_not_newer_is_discarded_never_installed(tmp_path):
    """e.g. the user downloaded a newer exe by hand: no downgrade."""
    exe = fake_exe(tmp_path)
    stage(exe, "1.1.0")
    assert apply_staged(exe, "1.2.0") is None
    assert exe.read_bytes() == b"old build"
    assert not staged_files(exe)[0].exists()


def test_a_truncated_staged_build_does_not_count(tmp_path):
    exe = fake_exe(tmp_path)
    stage(exe, "1.1.0")
    staged_files(exe)[0].write_bytes(BUILD[:1000])
    assert staged_version(exe) is None
    assert apply_staged(exe, "1.0.0") is None
    assert exe.read_bytes() == b"old build"


def test_nothing_staged_is_a_no_op(tmp_path):
    exe = fake_exe(tmp_path)
    assert apply_staged(exe, "1.0.0") is None
    assert exe.read_bytes() == b"old build"


def test_relaunch_drops_the_flags_a_previous_relaunch_added():
    argv = ["--config", "x.yaml", "--wait-pid", "12", "--updated-from=1.0.0", "--headless"]
    assert relaunch_args(argv) == ["--config", "x.yaml", "--headless"]


# --------------------------------------------------------------- checker


class FakeGitHub:
    """Serves /releases/latest and the asset; records every request."""

    def __init__(self, release=None, status=200, headers=None, asset=BUILD):
        self.release = release if release is not None else release_json()
        self.status = status
        self.headers = headers or {}
        self.asset = asset
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == ASSET_URL:
            return httpx.Response(200, content=self.asset)
        if request.headers.get("If-None-Match") == '"e1"':
            return httpx.Response(304, headers=self.headers)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers, json={})
        return httpx.Response(200, json=self.release, headers={"ETag": '"e1"', **self.headers})


def make_updater(gh: FakeGitHub, exe: Path | None, current="1.0.0", **cfg):
    published: list[UpdateStatus] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(gh))
    config = UpdatesConfig(api_base="https://api.example", repo="o/r", **cfg)
    return Updater(config, exe, published.append, current=current, client=client), published


async def test_source_checkout_only_announces():
    up, published = make_updater(FakeGitHub(), exe=None)
    delay = await up.check_once()
    assert [s.state for s in published] == ["available"]
    assert published[0].version == "1.1.0"
    assert delay == 6 * 3600


async def test_packaged_exe_downloads_verifies_and_stages(tmp_path):
    exe = fake_exe(tmp_path)
    gh = FakeGitHub()
    up, published = make_updater(gh, exe)
    await up.check_once()
    assert [s.state for s in published] == ["downloading", "ready"]
    assert staged_version(exe) == "1.1.0"
    assert exe.read_bytes() == b"old build"  # nothing replaced while running
    # the next check finds it already staged: no second download
    await up.check_once()
    assert sum(str(r.url) == ASSET_URL for r in gh.requests) == 1


async def test_a_download_that_fails_its_checksum_is_thrown_away(tmp_path):
    exe = fake_exe(tmp_path)
    up, published = make_updater(FakeGitHub(asset=BUILD[:-1] + b"X"), exe)
    delay = await up.check_once()
    assert [s.state for s in published] == ["downloading", "failed"]
    assert "SHA-256" in published[-1].detail
    assert staged_version(exe) is None
    assert not any(p.exists() for p in staged_files(exe))
    assert delay >= updater.BACKOFF_BASE_S


async def test_a_size_mismatch_is_thrown_away_without_a_digest(tmp_path):
    exe = fake_exe(tmp_path)
    up, published = make_updater(FakeGitHub(release_json(sha=None, size=len(BUILD) + 5)), exe)
    await up.check_once()
    assert published[-1].state == "failed"
    assert staged_version(exe) is None


async def test_auto_download_off_only_announces(tmp_path):
    exe = fake_exe(tmp_path)
    up, published = make_updater(FakeGitHub(), exe, auto_download=False)
    await up.check_once()
    assert [s.state for s in published] == ["available"]
    assert staged_version(exe) is None


async def test_the_same_version_publishes_nothing(tmp_path):
    up, published = make_updater(FakeGitHub(release_json(tag="v1.0.0")), fake_exe(tmp_path))
    await up.check_once()
    assert published == []


async def test_no_release_yet_is_not_an_error():
    up, published = make_updater(FakeGitHub(status=404), exe=None)
    assert await up.check_once() == 6 * 3600
    assert published == []


async def test_a_429_backs_off_for_at_least_retry_after():
    up, published = make_updater(FakeGitHub(status=429, headers={"Retry-After": "7200"}), None)
    assert await up.check_once() >= 7200
    assert published == []


async def test_an_exhausted_quota_403_waits_for_the_reset():
    reset = str(int(time.time()) + 9000)
    gh = FakeGitHub(status=403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
    up, _ = make_updater(gh, None)
    assert await up.check_once() >= 8900


async def test_backoff_grows_on_repeated_failures():
    up, _ = make_updater(FakeGitHub(status=503), None)
    first = await up.check_once()
    second = await up.check_once()
    assert second == 2 * first


async def test_conditional_request_reuses_the_cached_release():
    gh = FakeGitHub()
    up, published = make_updater(gh, None)
    await up.check_once()
    await up.check_once()
    assert gh.requests[-1].headers["If-None-Match"] == '"e1"'
    assert [s.state for s in published] == ["available"]  # unchanged: not re-sent


async def test_the_user_agent_carries_no_personal_details():
    gh = FakeGitHub()
    up, _ = make_updater(gh, None)
    up._client.headers["User-Agent"] = updater.USER_AGENT
    await up.check_once()
    ua = gh.requests[0].headers["User-Agent"]
    assert ua.startswith("valdo-sniper-updater/") and "@" not in ua


# ------------------------------------------------- packaged config layering


def test_merge_config_layers_mappings_and_replaces_lists():
    base = {"alerts": {"volume": 0.5, "expiry_seconds": 16}, "mod_pairings": [1, 2]}
    over = {"alerts": {"expiry_seconds": 20}, "mod_pairings": [3]}
    assert merge_config(base, over) == {
        "alerts": {"volume": 0.5, "expiry_seconds": 20},
        "mod_pairings": [3],
    }


def test_a_partial_user_config_overrides_only_what_it_names(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("alerts:\n  expiry_seconds: 33\n", encoding="utf-8")
    shipped = load_config(ROOT / "config.yaml")
    layered = load_config(user, defaults=ROOT / "config.yaml", state_dir=tmp_path)
    assert layered.alerts.expiry_seconds == 33
    # everything else - pairings included - still comes from the defaults
    assert layered.alerts.volume == shipped.alerts.volume
    assert layered.mod_scoring.pairings == shipped.mod_scoring.pairings


def test_the_commented_template_alone_means_all_defaults(tmp_path):
    from sniper.paths import USER_CONFIG_TEMPLATE

    user = tmp_path / "config.yaml"
    user.write_text(USER_CONFIG_TEMPLATE, encoding="utf-8")
    layered = load_config(user, defaults=ROOT / "config.yaml", state_dir=tmp_path)
    assert layered.league == load_config(ROOT / "config.yaml").league


def test_no_user_config_at_all_runs_on_the_defaults(tmp_path):
    config = load_config(tmp_path / "config.yaml", defaults=ROOT / "config.yaml")
    assert config.updates.repo == "Kuderic/PoEValdoAssistant"


def test_settings_panel_overrides_are_read_from_the_state_dir(tmp_path):
    (tmp_path / SCORING_OVERRIDES_NAME).write_text("min_yield: 42\n", encoding="utf-8")
    config = load_config(
        tmp_path / "config.yaml", defaults=ROOT / "config.yaml", state_dir=tmp_path
    )
    assert config.thresholds.min_yield == 42
    assert config.state_dir == tmp_path


def test_a_bad_user_config_names_the_file(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("alerts: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="config.yaml"):
        load_config(user, defaults=ROOT / "config.yaml")


@pytest.mark.parametrize(
    ("updates", "needle"),
    [("check_interval_hours: 0.1", "at least 1"), ("repo: nope", "owner/name")],
)
def test_bad_update_settings_raise(tmp_path, updates, needle):
    user = tmp_path / "config.yaml"
    user.write_text(f"updates:\n  {updates}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=needle):
        load_config(user, defaults=ROOT / "config.yaml")


@pytest.mark.parametrize("state", ["available", "downloading", "ready", "failed", "installed"])
def test_every_update_state_has_pill_text_and_hover_help(state):
    from sniper.overlay import _update_help, _update_pill

    status = UpdateStatus(state, "1.1.0", "https://example/r", detail="boom")
    text, _dot, _fg = _update_pill(status)
    assert "1.1.0" in text
    for packaged in (True, False):
        title, body = _update_help(status, packaged)
        assert "1.1.0" in title and len(body) > 40


def test_source_users_are_told_how_to_update():
    from sniper.overlay import _update_help

    _title, body = _update_help(UpdateStatus("available", "1.1.0", ""), packaged=False)
    assert "git pull" in body
