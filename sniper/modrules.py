"""Compile config mod rules once; evaluate them per listing (hot path).

Two engines:
- ModRules: warn/block severities (block downgrades a listing to log-only).
- ModScoring: difficulty score = max(base_default, min_bases of matched
  rules) x product(multipliers of matched rules), plus special warnings.
  The score raises the required divine profit (see margin.py).
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

    @property
    def div_per_point(self) -> float:
        return self._config.div_per_point

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
