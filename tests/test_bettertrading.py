import base64
import json

from sniper.bettertrading import live_url, load, parse_exports, save, slug_reward


def v3(payload: dict) -> str:  # exactly what Better Trading's serialize() emits
    return "3:" + base64.b64encode(json.dumps(payload).encode("utf-8")).decode()


FOLDER = {
    "icn": None,
    "tit": "Valdo 🗁",
    "ver": "1",
    "trs": [
        {"tit": "Foil Mageblood", "loc": "1:search:Ab12Cd34"},
        {"tit": "Foil Nimis", "loc": "1:search:Zz99"},
        {"tit": "Chaos ratio", "loc": "1:exchange:Qq11"},
        {"tit": "dupe", "loc": "1:search:Ab12Cd34"},
        {"tit": "evil", "loc": "1:search:../--remote-debugging-port=1"},
    ],
}


def test_v3_export_becomes_live_searches_with_reasons_for_the_rest():
    result = parse_exports(v3(FOLDER))
    assert [s.slug for s in result.searches] == ["Ab12Cd34", "Zz99"]
    assert result.searches[0].folder == "Valdo 🗁"
    assert any("exchange" in r for r in result.skipped)
    assert any("invalid search id" in r for r in result.skipped)


def test_v1_and_v2_exports():
    body = {"icn": "x", "tit": "old", "trs": [{"tit": "t", "loc": "search:Old1"}]}
    v1 = base64.b64encode(json.dumps(body).encode("latin-1")).decode()
    v2 = "2:" + base64.b64encode(json.dumps(body).encode("utf-8")).decode()
    assert [s.slug for s in parse_exports(v1).searches] == ["Old1"]
    assert [s.slug for s in parse_exports(v2).searches] == ["Old1"]


def test_poe2_folder_and_garbage_are_skipped_not_fatal():
    poe2 = v3({**FOLDER, "ver": "2"})
    result = parse_exports(f"not an export\n\n{poe2}\n{v3(FOLDER)}")
    assert len(result.searches) == 2
    assert any("PoE 2" in r for r in result.skipped)
    assert any("line 1" in r for r in result.skipped)


def test_live_url_encodes_the_league():
    s = parse_exports(v3(FOLDER)).searches[0]
    assert live_url(s, "Covens") == "https://www.pathofexile.com/trade/search/Covens/Ab12Cd34/live"
    assert "Hardcore%20Covens" in live_url(s, "Hardcore Covens")


def test_bad_paste_never_overwrites_saved_searches(tmp_path):
    path = tmp_path / "searches.txt"
    assert save(path, v3(FOLDER)).searches
    assert not save(path, "garbage").searches
    assert [s.slug for s in load(path).searches] == ["Ab12Cd34", "Zz99"]
    assert load(tmp_path / "missing.txt").searches == ()


def test_real_better_trading_1_9_2_export_with_compressed_query_slugs():
    """BT 1.9.x stores the query itself (base64url gzip, with - and _), not a
    short search id; the old id-only check rejected every one of these."""
    import gzip
    from pathlib import Path

    text = (Path(__file__).parent / "fixtures" / "better_trading_1.9.2_export.txt").read_text()
    result = parse_exports(text)
    assert result.skipped == ()
    titles = [s.title for s in result.searches]
    assert titles[:3] == ["Headhunter", "Sublime", "Mageblood"] and len(titles) == 10
    slug = result.searches[0].slug
    query = json.loads(gzip.decompress(base64.urlsafe_b64decode(slug + "=" * (-len(slug) % 4))))
    assert (
        query["filters"]["map_filters"]["filters"]["map_completion_reward"]["option"]
        == "Headhunter"
    )
    assert live_url(result.searches[0], "Covens").endswith(f"/Covens/{slug}/live")


def test_slug_reward_reads_the_embedded_query():
    from pathlib import Path

    text = (Path(__file__).parent / "fixtures" / "better_trading_1.9.2_export.txt").read_text()
    rewards = [s.reward for s in parse_exports(text).searches]
    assert rewards[:3] == ["Foil Headhunter", "Foil Sublime Vision", "Foil Mageblood"]
    assert slug_reward("Ab12Cd34") is None  # short id: no query inside
    assert slug_reward("H4sI" + "A" * 40) is None  # truncated gzip


def test_unreadable_saved_file_does_not_crash_startup(tmp_path):
    path = tmp_path / "searches.txt"
    path.write_bytes("3:abc".encode("utf-16"))
    result = load(path)
    assert result.searches == () and "unreadable" in result.skipped[0]
