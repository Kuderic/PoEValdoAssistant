"""The userscript names a tab's reward from the search form's "Map Completion
Reward" dropdown, so a live search with no results yet is still priced
before its first listing.

Before this, 49 of 67 Valdo tab connections arrived with no reward: the
script read the page-load `window.tradeOpts`, which the current trade site
no longer has. The selectors are read out of the userscript itself and
applied to a trimmed copy of the real page, so neither can drift alone.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "userscript" / "valdo-sniper.user.js").read_text(encoding="utf-8")
SNAPSHOT = ROOT / "tests" / "fixtures" / "reward_filter_snapshot.html"
VOID = {"input", "br", "img", "meta", "link", "hr", "source", "wbr"}


def selector(name: str) -> str:
    return re.search(rf"^\s*{name}: '([^']+)'", SCRIPT, re.M).group(1)


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children: list[Node] = []
        self.text = ""

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def all_text(self) -> str:
        return self.text + "".join(c.all_text() for c in self.children)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def closest(self, cls: str):
        node = self
        while node is not None and cls not in node.classes:
            node = node.parent
        return node

    def find(self, cls: str):
        return next((n for n in self.walk() if cls in n.classes), None)


class Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = self.cur = Node("#root", {}, None)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.text += data


MISSING = object()  # JS undefined: the form has not rendered the filter


def reward_filter(html: str):
    """rewardFilter() from the userscript, step for step: the option, '' for
    "Any", None while open, MISSING when the filter is not on the page."""
    tree = Tree()
    tree.feed(html)
    title_cls = selector("filterTitle").lstrip(".")
    wanted = re.search(r"REWARD_FILTER_TITLE = '([^']+)'", SCRIPT).group(1)
    for node in tree.root.walk():
        if title_cls not in node.classes or node.all_text().strip() != wanted:
            continue
        block = node.closest(selector("filterBlock").lstrip("."))
        if block is None or block.find(selector("filterOpen").lstrip(".")):
            return None
        box = block.find(selector("filterInput").lstrip("."))
        option = (
            (box.attrs.get("value") or box.attrs.get("placeholder") or "") if box else ""
        ).strip()
        return option if option and option != "Any" else ""
    return MISSING


def dropdown_reward(html: str) -> str | None:
    """dropdownReward(): rewardFilter() || null."""
    found = reward_filter(html)
    return found if isinstance(found, str) and found else None


def test_the_real_page_names_its_reward():
    assert dropdown_reward(SNAPSHOT.read_text(encoding="utf-8")) == "Mageblood"


def test_other_reward_filters_are_not_mistaken_for_it():
    """'Reward Type' and 'Reward Unique' also contain the word Reward."""
    html = SNAPSHOT.read_text(encoding="utf-8")
    assert html.count("filter-title") == 3
    assert "Reward Type" in html and "Reward Unique" in html
    # without the real dropdown there is no answer, not a wrong one
    start = html.index("Map Completion Reward")
    block_start = html.rindex('<div class="filter ', 0, start)
    block_end = html.index('<div class="filter ', start)
    assert dropdown_reward(html[:block_start] + html[block_end:]) is None


def test_any_and_an_open_dropdown_give_no_reward():
    html = SNAPSHOT.read_text(encoding="utf-8")
    assert (
        dropdown_reward(
            html.replace('value="Mageblood"', 'value=""').replace(
                'placeholder="Mageblood"', 'placeholder="Any"'
            )
        )
        is None
    )
    # open: the input holds whatever the user is typing
    opened = html.replace("multiselect multiselect--disabled", "multiselect multiselect--active", 1)
    assert dropdown_reward(opened) is None


def test_the_script_rechecks_until_the_reward_is_known():
    """The form can render after the first hello; the 2s sentinel looks
    again while the reward is unknown, before it needs a results container."""
    sentinel = SCRIPT[SCRIPT.index("setInterval(() => {\n    if (location.pathname") :]
    recheck = sentinel.index("if (!lastSentReward) refreshRewardIdentity();")
    assert recheck < sentinel.index("if (!observedContainer || !observedContainer.isConnected)")
    assert "dropdownReward() || stateReward()" in SCRIPT


def any_reward(html: str) -> str:
    return html.replace('value="Mageblood"', 'value="Any"').replace(
        'placeholder="Mageblood"', 'placeholder="Any"'
    )


def test_a_reward_filter_on_any_means_a_non_valdo_search():
    """A live search for some other item leaves the reward on "Any". The form
    says so before any result row exists, so the tab is not counted as a
    Valdo search "still loading" (tradeOpts, the old source, is gone)."""
    html = SNAPSHOT.read_text(encoding="utf-8")
    assert reward_filter(html) == "Mageblood"
    assert reward_filter(any_reward(html)) == ""
    start = html.index("Map Completion Reward")
    block_start = html.rindex('<div class="filter ', 0, start)
    block_end = html.index('<div class="filter ', start)
    assert reward_filter(html[:block_start] + html[block_end:]) is MISSING


def test_the_generic_decision_reads_the_form_first():
    body = SCRIPT[SCRIPT.index("function isGenericSearch()") :]
    body = body[: body.index("\n  }\n")]
    form = body.index("rewardFilter()")
    assert form < body.index("queryState()") < body.index("rowsLookGeneric()")
    # "Any" still yields to a row carrying a reward: a Valdo search left on Any
    assert "if (fromForm === '') return !rowsCarryReward();" in body
    assert "if (fromForm) return false;" in body


def test_the_script_re_announces_a_changed_search_kind():
    """Without this a non-Valdo tab whose form renders after the first hello
    stays "still loading" until the next 30s heartbeat."""
    body = SCRIPT[SCRIPT.index("function refreshRewardIdentity()") :]
    body = body[: body.index("\n  }\n")]
    assert "searchKind() !== lastSentKind" in body
