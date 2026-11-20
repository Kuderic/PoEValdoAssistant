"""The app serves its own userscript so installing is one click and
Tampermonkey's @updateURL keeps every tab on the version the app expects."""

from __future__ import annotations

import re
from pathlib import Path

from websockets.datastructures import Headers
from websockets.http11 import Request

from sniper.server import EXPECTED_USERSCRIPT_VERSION, INSTALL_PATH, process_http


def get(path: str, **headers) -> object:
    return process_http(None, Request(path, Headers(list(headers.items()))))


def test_serves_the_shipped_userscript():
    resp = get(INSTALL_PATH)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/javascript")
    assert resp.body == Path("userscript/valdo-sniper.user.js").read_bytes()
    assert int(resp.headers["Content-Length"]) == len(resp.body)


def test_query_string_is_ignored():
    """Tampermonkey's update checks may append a cache-buster."""
    assert get(INSTALL_PATH + "?t=123").status_code == 200


def test_websocket_upgrades_pass_through():
    assert get(INSTALL_PATH, Upgrade="websocket") is None
    assert get("/", Upgrade="WebSocket") is None


def test_landing_page_links_the_installer():
    resp = get("/")
    assert resp.status_code == 200 and INSTALL_PATH.encode() in resp.body


def test_unknown_paths_fall_through_to_the_handshake():
    assert get("/favicon.ico") is None


def test_update_url_points_back_at_this_endpoint():
    src = Path("userscript/valdo-sniper.user.js").read_text(encoding="utf-8")
    for key in ("updateURL", "downloadURL"):
        url = re.search(rf"^// @{key}\s+(\S+)", src, re.M).group(1)
        assert url.endswith(INSTALL_PATH), url
    assert re.search(r"^// @version\s+(\S+)", src, re.M).group(1) == EXPECTED_USERSCRIPT_VERSION
