from sniper.config import ModWarningRule
from sniper.modrules import ModRules


def rules(*specs) -> ModRules:
    return ModRules([ModWarningRule(**s) for s in specs])


def test_substring_case_insensitive():
    r = rules({"label": "no regen", "severity": "warn", "match": "cannot regenerate"})
    hits = r.evaluate(["Players CANNOT Regenerate Life, Mana or Energy Shield"])
    assert [h.label for h in hits] == ["no regen"]
    assert hits[0].severity == "warn"


def test_regex():
    r = rules(
        {
            "label": "ele reflect",
            "severity": "block",
            "match": r"reflect(s)? \d+% of elemental",
            "regex": True,
        }
    )
    assert r.evaluate(["Monsters reflect 18% of Elemental Damage"])
    assert not r.evaluate(["Monsters reflect Physical Damage"])


def test_match_all_combo():
    r = rules(
        {
            "label": "no leech + phys reflect",
            "severity": "warn",
            "match_all": ["cannot leech", r"reflect(s)? \d+% of physical"],
            "regex": True,
        }
    )
    both = ["Cannot Leech Life from Monsters", "Monsters reflect 15% of Physical Damage"]
    assert r.evaluate(both)
    assert not r.evaluate(both[:1])
    assert not r.evaluate(both[1:])


def test_no_hits():
    r = rules({"label": "x", "severity": "warn", "match": "nope"})
    assert r.evaluate(["Monsters deal extra damage"]) == ()
