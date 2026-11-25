"""Search presets: a set of Valdo live searches opened together with one click.

A new user's first hurdle is building the searches. A preset opens a proven
set as Chrome tabs, already on their live pages, and the userscript connects
each one as usual.

The trade site's search ids are self-contained: each is the search query,
gzipped and base64url-encoded, with no league in it. So one id works in
every league, and only the URL names the league. The ids below are the exact
ones the trade site issued (copied from live tabs). The site's compressor
does not match zlib's byte for byte, so the ids are kept verbatim rather
than rebuilt from the reward names. tests/test_presets.py decodes each one
to check it searches what its label says.

Opening a preset is the user's click opening pages in their own browser: no
request goes out from the app, and nothing is bought or travelled to.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import quote

TRADE_SITE = "https://www.pathofexile.com"
# gap between tabs: six page loads at once is a burst of trade-site requests
# from one click; spread out, it looks like a person opening tabs
TAB_GAP_MS = 1000


@dataclass(frozen=True)
class Preset:
    name: str
    blurb: str
    # (reward key as the app names it, trade search id)
    searches: tuple[tuple[str, str], ...]

    @property
    def rewards(self) -> tuple[str, ...]:
        return tuple(reward for reward, _ in self.searches)


# Each: any instant-buyout ("securable") foil Valdo map with that reward
HIGH_PROFIT = Preset(
    name="High profit",
    blurb="Six high-value rewards",
    searches=(
        (
            "Foil Headhunter",
            "H4sIAAAAAAAACl2NUQqAIBAF7_K-O4En6A4RseVGgamsKxHi3cO-qvkb5mMKkpLmBFMQou7BwyDxkoVmx6jd0xPMUKBXZBiQt-iw7k5ZWhjr-NKCg-L00l9ZwhEdt9EkfJLYz7lnslv2yoLauAGHcMZZnwAAAA",
        ),
        (
            "Foil Mageblood",
            "H4sIAAAAAAAACl2NUQqAIBAF7_K-PYF36AQRseUWgbWiKxHi3cO-qvkb5mMKkpLmBFsgQTc5YJF4zpEmz6jm6Qm2L9ArMCzocDBYNq8cWxjq8NKCncL40l-ZZQ-e22iMfFJ0n3NHK09exKE2bsTSSHSeAAAA",
        ),
        (
            "Foil Nimis",
            "H4sIAAAAAAAACl2NUQqAIBAF7_K-O4GH6AIRseUGgqa4KxHi3cO-qvkb5mMqREmLwFTEpC4eMBDeSqbVM9rwdIGZKvRKDAM6LAbszivnHuY2v7QiUFpe-itbDMlzHy2ZT8r2cx5dcILWuQE-wbfimgAAAA",
        ),
        (
            "Foil Progenesis",
            "H4sIAAAAAAAACl2NQQqDMBBF7_LXniCncC8i02RaAjEJMyMiIXcv6Ur7do-3eA1qZIfCNZRqsWQ4KPtD6JUYffp1hVsa7KoMB8oBE94xGcsIa19v2rBT3W76V3zZa-Ix2oRPkvA4z1I-nFmjog--6bLhG58AAAA",
        ),
        (
            "Foil Sublime Vision",
            "H4sIAAAAAAAACl2NQQqEMBAEvyJ99gX5hrAXERl1hEBiQmaCLCF_l3hS-1ZdhyoQJc0CUxCi2nDAQHjNiRbHqP3tBWYs0H9kGNCxocdunXJqYqrTAws8xfmBH7MGHx230Jz4pLS9ykNenPXc_ay0o7ZdgUd3SaMAAAA",
        ),
        (
            "Foil Wine of the Prophet",
            "H4sIAAAAAAAACl2NQQqDMBBFrxL-2hPkFO66KCJTHTEQM0MyIiXk7iVd2b7d4y1eRTGys8BXiFqQBI_Cy5npFRlt-PYC_6ywtzI8KK0YsIVonHuY2nTTioN0vulfWeTQyH00Z74orz_nR0jsZHO2sxuz6M6G1vkAhMyLx6gAAAA",
        ),
    ),
)

PRESETS: tuple[Preset, ...] = (HIGH_PROFIT,)


def live_url(league: str, search_id: str) -> str:
    """The search's live page: the trade site pushes new listings into it,
    which is what the userscript watches."""
    return f"{TRADE_SITE}/trade/search/{quote(league, safe='')}/{search_id}/live"


def still_closed(preset: Preset, open_rewards: Collection[str]) -> list[tuple[str, str]]:
    """The preset's searches whose reward no connected tab is already
    watching: a second click, or a user with some tabs open, gets only the
    missing ones."""
    return [(reward, sid) for reward, sid in preset.searches if reward not in open_rewards]
