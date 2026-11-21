"""Quota rules. Pure functions: no I/O, no clock — everything is passed in."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable

from ltp.config import Settings
from ltp.rules import AllowedWhen, AppRule
from ltp.store import Credit


class Action(Enum):
    NONE = "none"
    WARN = "warn"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class Decision:
    action: Action
    remaining_s: float
    reason: str = ""
    warn_tier: int | None = None      # minutes tier for WARN
    warn_key: str | None = None       # dedup key; includes day + allowance so it re-arms


__all__ = ["Action", "AllowedWhen", "AppRule", "Decision", "allowance_minutes", "evaluate",
           "in_curfew", "is_counting", "rule_for"]

_DEFAULT_RULE = AppRule(True, AllowedWhen.RESTRICTED)


def rule_for(app_id: str, settings: Settings) -> AppRule:
    """Rule for an app; unlisted apps count and are fully restricted ("" = nothing in front)."""
    return settings.app_rules.get(app_id or "", _DEFAULT_RULE)


def is_counting(app_id: str, settings: Settings) -> bool:
    return rule_for(app_id, settings).counts


def in_curfew(now_local: datetime, settings: Settings) -> bool:
    if not settings.curfew_enabled:
        return False
    t = now_local.astimezone(settings.timezone).time()
    start, end = settings.curfew_start, settings.curfew_end
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # window wraps midnight


def allowance_minutes(credits: Iterable[Credit], settings: Settings) -> float:
    """min(free + quiz, cap) + parent. Free minutes are implicit (not stored)."""
    quiz = parent = 0.0
    for cr in credits:
        if cr.source == "parent":
            parent += cr.minutes
        else:
            quiz += cr.minutes
    capped = min(settings.free_minutes + quiz, settings.daily_cap_minutes)
    return max(0.0, capped + parent)


def evaluate(*, used_s: float, allowance_min: float, counting: bool, locked: bool,
             curfew: bool, warned: frozenset[str], settings: Settings, day: date,
             allowed_when: AllowedWhen | None = None) -> Decision:
    remaining = allowance_min * 60 - used_s
    if allowed_when is None:  # legacy semantics: uncounted apps are never acted on
        allowed_when = AllowedWhen.RESTRICTED if counting else AllowedWhen.ALWAYS
    if allowed_when is AllowedWhen.ALWAYS:
        return Decision(Action.NONE, remaining)
    if locked:
        return Decision(Action.ENFORCE, remaining, reason="locked")
    if curfew:
        return Decision(Action.ENFORCE, remaining, reason="curfew")
    if allowed_when is AllowedWhen.QUOTA_EXHAUSTED:
        return Decision(Action.NONE, remaining)
    if remaining <= 0:
        return Decision(Action.ENFORCE, remaining, reason="quota")
    tier = None
    for m in settings.warn_minutes:  # descending; keep the most urgent tier reached
        if remaining <= m * 60:
            tier = m
    if tier is None:
        return Decision(Action.NONE, remaining)
    key = f"warn:{day.isoformat()}:{tier}:{allowance_min:g}"
    if key in warned:
        return Decision(Action.NONE, remaining)
    return Decision(Action.WARN, remaining, reason="warn", warn_tier=tier, warn_key=key)
