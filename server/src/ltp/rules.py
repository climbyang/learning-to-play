"""Per-app rule types shared by config (loading) and policy (evaluation)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AllowedWhen(Enum):
    ALWAYS = "always"                    # never acted on (home, launcher, ...)
    QUOTA_EXHAUSTED = "quota_exhausted"  # usable after quota runs out; locked/curfew still apply
    RESTRICTED = "restricted"            # normal app: locked, curfew and quota all apply


@dataclass(frozen=True)
class AppRule:
    counts: bool
    allowed_when: AllowedWhen
