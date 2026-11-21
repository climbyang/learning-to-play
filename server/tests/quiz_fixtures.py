"""Shared quiz test fixtures: injected clock, notification sink and a real-SQLite Env."""
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ltp.config import load_settings
from ltp.monitor import Monitor
from ltp.notify import Command
from ltp.quiz.bank import QuizBank
from ltp.quiz.service import (QuizRejected, QuizService, QuizUnavailable, kv_seed_provider,
                              pick_challenge_index)
from ltp.store import Store
from ltp.tv.fake import FakeTV

# Test-only copy of the pre-production sample bank: tests never depend on production content.
BANK_DIR = Path(__file__).resolve().parent / "sample_bank" / "bank"
DEV = "tv1"


class Clock:
    def __init__(self):
        # 2026-11-18 10:00 local (UTC+10) = 00:00 UTC
        self.t = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class Sink:
    def __init__(self):
        self.msgs: list[str] = []

    async def send(self, text, buttons=None):
        self.msgs.append(text)

    def has(self, needle):
        return any(needle in m for m in self.msgs)


class Env:
    def __init__(self, tmp_path, yaml_text="mode: enforce\n", seed=b"s" * 32):
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "settings.yaml").write_text(yaml_text, encoding="utf-8")
        self.settings = load_settings(cfg / "settings.yaml", {})
        self.qcfg = replace(self.settings.quiz, enabled=True)
        self.db = tmp_path / "ltp.db"
        self.bank = QuizBank.load(BANK_DIR, BANK_DIR.parent / "reviews")
        self.clock = Clock()
        self.seed = seed
        self.build()

    def build(self):
        self.store = Store(self.db)
        self.sink = Sink()
        self.tv = FakeTV()
        self.mon = Monitor(self.tv, self.store, self.sink, self.settings, now=self.clock)
        seed = self.seed
        self.svc = QuizService(self.bank, self.mon, self.store, self.qcfg, random.Random(1),
                               (lambda: seed) if seed is not None else kv_seed_provider(self.store))

    def restart(self):
        self.store.close()
        self.build()

    def answer(self, view):
        return self.store.get_attempt(view.id).answer

    def rev(self, attempt_id):
        """Current revision of an attempt, so a test call is never a stale-rev no-op."""
        return self.store.get_attempt(attempt_id).rev

    def wrong(self, view):
        a = self.store.get_attempt(view.id)
        if a.snapshot["answer_type"] == "choice":
            return next(o["id"] for o in view.options if o["id"] != a.answer)
        return str(int(float(a.answer)) + 1)

    def quiz_credits(self):
        return [c for c in self.store.credits(self.clock().astimezone(
            self.settings.timezone).date()) if c.source == "quiz"]
