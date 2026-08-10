"""Compile config mod rules once; evaluate them per listing (hot path).

Two engines:
- ModRules: warn/block severities (block downgrades a listing to log-only).
- ModScoring: difficulty score = max(base_default, min_bases of matched
  rules) x product(multipliers of matched rules), plus special warnings.
  The required profit scales with the score: threshold x score / 100
  (see margin.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sniper.config import ModScoringConfig, ModWarningRule
from sniper.models import ModHit

Matcher = Callable[[str], bool]


def _matcher(pattern: str, is_regex: bool) -> Matcher:
    if is_regex:
        compiled = re.compile(pattern, re.IGNORECASE)
        return lambda mod: bool(compiled.search(mod))
    needle = pattern.casefold()
    return lambda mod: needle in mod.casefold()


class CompiledRule:
    def __init__(self, rule: ModWarningRule):
        self.label = rule.label
        self.severity = rule.severity
        patterns = rule.match_all if rule.match_all else (rule.match or "",)
        self._matchers = [_matcher(p, rule.regex) for p in patterns]

    def hits(self, mods: Iterable[str]) -> bool:
        """True when every pattern matches at least one mod line."""
        mods = list(mods)
        return all(any(m(mod) for mod in mods) for m in self._matchers)


class ModRules:
    def __init__(self, rules: Iterable[ModWarningRule]):
        self._rules = [CompiledRule(r) for r in rules]

    def evaluate(self, mods: Iterable[str]) -> tuple[ModHit, ...]:
        mods = list(mods)
        return tuple(
            ModHit(label=r.label, severity=r.severity) for r in self._rules if r.hits(mods)
        )


@dataclass(frozen=True)
class DifficultyResult:
    score: float
    matched: tuple[str, ...]  # labels of every matched scoring rule
    warnings: tuple[tuple[str, str], ...]  # (label, color) for warning rules


class _CompiledScoringRule:
    def __init__(self, rule):
        self.rule = rule
        patterns = rule.match_all if rule.match_all else (rule.match or "",)
        self._matchers = [_matcher(p, rule.regex) for p in patterns]

    def hits(self, mods: list[str]) -> bool:
        return all(any(m(mod) for mod in mods) for m in self._matchers)


class ModScoring:
    def __init__(self, config: ModScoringConfig):
        self._config = config
        self._rules = [_CompiledScoringRule(r) for r in config.rules]

    def evaluate(self, mods: Iterable[str]) -> DifficultyResult:
        mods = list(mods)
        matched = [c.rule for c in self._rules if c.hits(mods)]
        base = max(
            [self._config.base_default] + [r.min_base for r in matched if r.min_base is not None]
        )
        score = base
        for r in matched:
            if r.multiplier is not None:
                score *= r.multiplier
        return DifficultyResult(
            score=round(score, 2),
            matched=tuple(r.label for r in matched),
            warnings=tuple((r.label, r.warning) for r in matched if r.warning),
        )

    @staticmethod
    def _rule_level(rule) -> str:
        """Severity tier for UI coloring: warning rules always carry their
        warning color (BISMUTH/ULTIMATUM/BLIGHT/... must stand out wherever
        mods render, not just on the chip); otherwise ×1 mods stay uncolored,
        ×(1, 1.4] yellow, above ×1.4 red; base-difficulty mods yellow."""
        if rule.warning:
            return rule.warning
        if rule.multiplier is not None:
            if rule.multiplier > 1.4:
                return "red"
            if rule.multiplier > 1.0:
                return "yellow"
            return "none"
        if rule.min_base is not None:
            return "yellow"
        return "none"

    def annotate(self, mods: Iterable[str]) -> tuple[tuple[str, str, str], ...]:
        """Per-mod scoring annotations for the UI: (display text, note,
        level) where note is e.g. '×1.8' or 'base 100' (empty when nothing
        contributes) and level is 'none' | 'yellow' | 'red'.

        Lines claimed by one match_all combo rule (two-liner mods like the
        less-damage pair) merge into a SINGLE display row with the modifier
        shown once."""
        rank = {"none": 0, "yellow": 1, "red": 2}
        mods = list(mods)
        matched = [c for c in self._rules if c.hits(mods)]

        def describe(lines: list[str]) -> tuple[str, str, str]:
            notes: list[str] = []
            level = "none"
            warn_color = None
            for compiled in matched:
                if not any(m(line) for line in lines for m in compiled._matchers):
                    continue
                rule = compiled.rule
                if rule.min_base is not None:
                    notes.append(f"base {rule.min_base:g}")
                if rule.multiplier is not None:
                    notes.append(f"×{rule.multiplier:g}")
                if rank[self._rule_level(rule)] > rank[level]:
                    level = self._rule_level(rule)
                if rule.warning and (warn_color != "red"):
                    warn_color = rule.warning
            # warning rules stamp their emoji on the row itself, so
            # VOID/ULTIMATUM/etc. are marked wherever mods are listed
            text = " + ".join(lines)
            text = {"red": f"❗ {text}", "yellow": f"⚠️ {text}"}.get(warn_color, text)
            return (text, " · ".join(dict.fromkeys(notes)), level)

        # assign each line to at most one matched combo rule
        group_of: dict[int, int] = {}  # mod index -> index into `matched`
        for ci, compiled in enumerate(matched):
            if not compiled.rule.match_all:
                continue
            for i, mod in enumerate(mods):
                if i not in group_of and any(m(mod) for m in compiled._matchers):
                    group_of[i] = ci

        rows: list[tuple[str, str, str]] = []
        seen_groups: set[int] = set()
        for i, mod in enumerate(mods):
            ci = group_of.get(i)
            if ci is None:
                rows.append(describe([mod]))
            elif ci not in seen_groups:
                seen_groups.add(ci)
                members = [mods[j] for j in sorted(group_of) if group_of[j] == ci]
                rows.append(describe(members))
        return tuple(rows)
