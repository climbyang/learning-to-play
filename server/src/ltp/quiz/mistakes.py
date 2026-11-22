"""Mistake book: which templates the child got wrong, and when to ask them again.

Derived from attempt history, no extra state. A template enters the book on a mistake
(wrong answer, hint, skip, exhaustion). Clean answers afterwards move it along a spaced
schedule; after MASTERY clean answers in a row it leaves the book. Any new mistake
restarts the schedule.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ltp.store import Attempt

INTERVALS = (timedelta(days=1), timedelta(days=3), timedelta(days=7))
MASTERY = len(INTERVALS)
_MISTAKE_OUTCOMES = {"lost", "parts_wrong", "half", "hint"}


def is_mistake(a: Attempt) -> bool:
    return (a.state in ("exhausted", "skipped") or a.wrong_count > 0 or a.hints_used > 0
            or a.outcome in _MISTAKE_OUTCOMES)


def is_clean(a: Attempt) -> bool:
    return a.state == "correct" and not is_mistake(a)


@dataclass(frozen=True)
class Entry:
    template_id: str
    kind: str
    streak: int              # clean answers since the last mistake
    last_utc: datetime       # last mistake or clean answer
    last_instance: str

    @property
    def mastered(self) -> bool:
        return self.streak >= MASTERY

    @property
    def due_utc(self) -> datetime | None:
        return None if self.mastered else self.last_utc + INTERVALS[self.streak]


def book(attempts: list[Attempt]) -> dict[str, Entry]:
    """Mistake-book entries per template from one device's terminal attempts (any order)."""
    out: dict[str, Entry] = {}
    done = [a for a in attempts if a.state != "issued" and a.finished_utc is not None]
    for a in sorted(done, key=lambda x: (x.finished_utc, x.id)):
        prev = out.get(a.template_id)
        if is_mistake(a):
            out[a.template_id] = Entry(a.template_id, a.snapshot.get("kind", ""), 0,
                                       a.finished_utc, a.instance_id)
        elif prev is not None and is_clean(a):
            out[a.template_id] = Entry(a.template_id, prev.kind, prev.streak + 1,
                                       a.finished_utc, a.instance_id)
    return out


def due(entries: dict[str, Entry], now: datetime) -> list[Entry]:
    """Entries due for review, longest overdue first."""
    ready = [e for e in entries.values() if e.due_utc is not None and e.due_utc <= now]
    return sorted(ready, key=lambda e: (e.due_utc, e.template_id))
