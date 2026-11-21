"""Quiz service: server-authoritative attempts, grading, rewards and limits.

Every operation runs inside `Monitor.quiz_txn` (one lock, one transaction). Attempts
carry a snapshot of everything needed to render and finish them, so bank updates
never affect a running attempt. All times come from the server clock.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, TypeVar

from ltp.config import QuizSettings
from ltp.monitor import Monitor, QuizContext
from ltp.quiz import passage as ps
from ltp.quiz.bank import WORD_SET_KIND, Instance, QuizBank, check_answer
from ltp.store import Attempt, Store

T = TypeVar("T")
TIERS = ("regular", "challenge")
SEED_KEY = "quiz:challenge_seed"
DISABLED_PREFIX = "quiz:disabled:"
SKIP_LIMIT_EVENT = "quiz_skip_limit"


class QuizUnavailable(Exception):
    """The quiz cannot be used right now: locked | curfew | paused | disabled."""

    def __init__(self, reason: str, retry_after_s: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


class QuizRejected(Exception):
    """A request was refused: not_found | cooldown | skip_limit | no_skip_challenge | bad_input
    | review_required."""

    def __init__(self, code: str, retry_after_s: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class AttemptView:
    id: str
    rev: int
    tier: str
    state: str
    prompt: str
    figure: str | None
    answer_type: str
    options: list[dict]
    hints: list[str]
    hints_total: int
    tries_left: int | None
    skips_left: int
    outcome: str
    credited_minutes: float
    nominal_minutes: float
    explanation: str | None
    correct_answer: str | None
    # passage sets only; empty defaults for single questions (see ltp.quiz.passage)
    passage: str | None = None
    stage: str | None = None  # parts | vocab | done
    step: int | None = None
    parts: list[dict] = field(default_factory=list)
    vocab: list[dict] = field(default_factory=list)
    parts_right: int = 0
    parts_total: int = 0


@dataclass(frozen=True)
class StatusView:
    remaining_s: float
    quiz_today: int
    earned_minutes: float
    challenge_state: str  # "none" or the state of today's challenge attempt
    available: bool
    reason: str | None
    retry_after_s: int | None = None  # seconds left in a wrong-answer pause, else None


def pick_challenge_index(seed: bytes, day: date, count: int) -> int:
    """Secret-seeded daily choice; unpredictable without the seed."""
    digest = hmac.new(seed, day.isoformat().encode(), hashlib.sha256).digest()
    return int.from_bytes(digest, "big") % count


def kv_seed_provider(store: Store) -> Callable[[], bytes]:
    """Default seed provider: kv `quiz:challenge_seed` (hex), created on first use.

    Called inside the service's transaction, so creation commits with the attempt.
    """
    def provide() -> bytes:
        raw = store.get_kv(SEED_KEY)
        if raw is None:
            seed = secrets.token_bytes(32)
            store.set_kv(SEED_KEY, seed.hex())
            return seed
        return bytes.fromhex(raw)
    return provide


def _fmt(minutes: float) -> str:
    return f"{minutes:g}"


TIER_LABEL = {"regular": "普通", "challenge": "挑战"}
STATE_LABEL = {"correct": "答对", "exhausted": "答错", "skipped": "跳过",
               "expired": "过期", "issued": "进行中"}


TELEGRAM_LIMIT = 4096


def _passage_check_text(inst: Instance, budget: int = TELEGRAM_LIMIT) -> str:
    """Parent spot-check of a passage set: text, each part with its answer, vocab meanings.
    Only the passage text is truncated (with an ellipsis) to fit one Telegram message."""
    p = inst.passage or {}
    lines: list[str] = []
    for n, part in enumerate(p["parts"], 1):
        correct = next((t for oid, t in part["options"] if oid == part["answer"]), part["answer"])
        lines.append(f"第{n}题：{part['prompt']}\n答案：{correct}")
    for card in p["vocab"]:
        lines.append(f"单词：{card['word']} — {card['meaning']}")
    tail = "\n\n" + "\n".join(lines)
    text = p["text"]
    room = budget - len(tail)
    if len(text) > room:
        text = text[:max(0, room - 1)].rstrip() + "…"
    out = text + tail
    if len(out) > budget:  # parts+vocab alone overflow: hard-cap the whole message
        out = out[:max(0, budget - 1)].rstrip() + "…"
    return out


class QuizService:
    def __init__(self, bank: QuizBank, monitor: Monitor, store: Store, cfg: QuizSettings,
                 rng: random.Random, seed_provider: Callable[[], bytes] | None = None) -> None:
        self.bank = bank
        self.monitor = monitor
        self.store = store
        self.cfg = cfg
        self.rng = rng
        self.seed_provider = seed_provider or kv_seed_provider(store)

    # -- transaction plumbing ------------------------------------------------
    async def _txn(self, fn: Callable[[QuizContext], T], *, write: bool, device: str) -> T:
        def run(ctx: QuizContext) -> T:
            self.store.expire_before(ctx.day, ctx.now)
            if write:
                self._guard(ctx, device)
            return fn(ctx)
        return await self.monitor.quiz_txn(run)

    async def _write(self, device: str, attempt_id: str, rev: int,
                     fn: Callable[[QuizContext, Attempt], AttemptView]) -> AttemptView:
        """A write on an existing attempt. Ownership and the rev check run before the
        availability guard: a stale-rev retry (e.g. of the submit that triggered the pause)
        must get the current view, not 503, because its write already landed."""
        def run(ctx: QuizContext) -> AttemptView:
            self.store.expire_before(ctx.day, ctx.now)
            a = self._owned(device, attempt_id)
            if rev != a.rev:
                return self._view(a)
            self._guard(ctx, device)
            return fn(ctx, a)
        return await self.monitor.quiz_txn(run)

    def _unavailable(self, ctx: QuizContext, device: str) -> QuizUnavailable | None:
        if not self.cfg.enabled:
            return QuizUnavailable("disabled")
        if ctx.blocked is not None:
            return QuizUnavailable(ctx.blocked)
        if self.store.wrong_total(device, ctx.day) >= self.cfg.wrong_pause_threshold:
            wrongs = [a.last_wrong_utc for a in self._today(device, ctx.day)
                      if a.last_wrong_utc is not None]
            if wrongs:
                until = max(wrongs) + timedelta(minutes=self.cfg.wrong_pause_minutes)
                if ctx.now < until:
                    return QuizUnavailable(
                        "paused", math.ceil((until - ctx.now).total_seconds()))
        return None

    def _guard(self, ctx: QuizContext, device: str) -> None:
        exc = self._unavailable(ctx, device)
        if exc is not None:
            raise exc

    def _today(self, device: str, day: date) -> list[Attempt]:
        return [a for a in self.store.attempts_on(day) if a.device == device]

    def _owned(self, device: str, attempt_id: str) -> Attempt:
        a = self.store.get_attempt(attempt_id)
        if a is None or a.device != device:
            raise QuizRejected("not_found")
        return a

    def _skips_used(self, device: str, day: date) -> int:
        # A skipped passage always uses a skip, even after a wrong part.
        return sum(1 for a in self._today(device, day)
                   if a.tier == "regular" and a.state == "skipped"
                   and (not a.reward_lost or ps.is_passage(a)))

    # -- views ---------------------------------------------------------------
    def _view(self, a: Attempt) -> AttemptView:
        snap = a.snapshot
        passage = ps.is_passage(a)
        by_id = {o["id"]: o for o in snap["options"]}
        options = [] if passage else [dict(by_id[oid]) for oid in (a.option_order or [])]
        terminal = a.state != "issued"
        choice = snap["answer_type"] == "choice"
        skips_left = (0 if a.tier == "challenge"
                      else max(0, self.cfg.skips_per_day - self._skips_used(a.device, a.day)))
        extra = ps.view_fields(a) if passage else {}
        return AttemptView(
            id=a.id, rev=a.rev, tier=a.tier, state=a.state, prompt=snap["prompt"], figure=snap["figure"],
            answer_type=snap["answer_type"], options=options,
            hints=list(snap["hints"][:a.hints_used]), hints_total=len(snap["hints"]),
            tries_left=(None if choice or passage
                        else max(0, self.cfg.numeric_tries - a.wrong_count)),
            skips_left=skips_left, outcome=a.outcome,
            credited_minutes=a.credited_minutes, nominal_minutes=a.nominal_minutes,
            explanation=snap["explanation"] if terminal and not passage else None,
            correct_answer=a.answer if terminal and not passage else None,
            **extra,
        )

    # -- selection -----------------------------------------------------------
    def _enabled(self, tier: str) -> list[Instance]:
        return [i for i in self.bank.instances(tier)
                if self.store.get_kv(DISABLED_PREFIX + i.template_id) is None]

    # -- parent-facing read/write helpers (called under the monitor lock) ---------
    def today_lines(self, day: date) -> list[str]:
        tz = self.monitor.s.timezone
        lines = []
        for a in self.store.attempts_on(day):
            line = (f"{a.issued_utc.astimezone(tz):%H:%M} {TIER_LABEL.get(a.tier, a.tier)} "
                    f"{a.template_id} {STATE_LABEL.get(a.state, a.state)} "
                    f"+{_fmt(a.credited_minutes)}")
            if ps.is_passage(a) and a.progress["parts"]:
                right = sum(1 for p in a.progress["parts"] if p["correct"])
                line += f" 答对 {right}/{len(a.snapshot['parts'])}"
            lines.append(line)
        return lines

    def spot_check(self, budget: int = TELEGRAM_LIMIT) -> tuple[str, str]:
        pool = self._enabled("regular") + self._enabled("challenge")
        if not pool:
            raise QuizUnavailable("disabled")
        inst = self.rng.choice(pool)
        if inst.answer_type == "passage":
            return inst.instance_id, _passage_check_text(inst, budget)
        answer = inst.answer
        if inst.answer_type == "choice":
            answer = next((text for oid, text in inst.options if oid == inst.answer), answer)
        return inst.instance_id, f"{inst.prompt}\n答案：{answer}"

    def disable_template(self, template_id: str) -> bool:
        if template_id not in self.bank.templates():
            return False
        self.store.set_kv(DISABLED_PREFIX + template_id, "1")
        return True

    def daily_digest(self, day: date) -> str:
        attempts = self.store.attempts_on(day)
        correct = sum(1 for a in attempts if a.state == "correct")
        too_fast = sum(1 for a in attempts if a.outcome == "too_fast")
        start, _ = self.monitor._day_bounds(day)
        skip_limit = self.store.count_events(SKIP_LIMIT_EVENT, start)
        denied = self.store.count_events("quiz_denied", start)
        return (f"答题：答对 {correct} 题，计入 {_fmt(self.store.quiz_credit_minutes(day))} 分钟，"
                f"太快 {too_fast} 次，跳过达上限 {skip_limit} 次，拒绝请求 {denied} 次")

    def _pick_regular(self, device: str, now: datetime) -> Instance:
        pool = self._enabled("regular")
        if not pool:
            raise QuizUnavailable("disabled")
        # "Seen" = any terminal attempt (its answer was revealed), not only correct ones,
        # so skip/exhaust cannot be used to read an answer and get the item back.
        seen = self.store.last_seen_by_template(
            device, "regular", now - timedelta(days=self.cfg.dedup_days))
        last = self.store.last_attempt(device, "regular")
        last_kind = last.snapshot["kind"] if last is not None else None
        unseen = [i for i in pool if i.template_id not in seen]
        if unseen:
            # Freshness beats kind variety.
            candidates = [i for i in unseen if i.kind != last_kind] or unseen
        else:
            # Everything was seen recently: fall back to the longest-ago template(s).
            oldest = min(seen[i.template_id] for i in pool)
            candidates = [i for i in pool if seen[i.template_id] == oldest]
        return self.rng.choice(candidates)

    def _pick_challenge(self, day: date) -> Instance:
        pool = self._enabled("challenge")
        if not pool:
            raise QuizUnavailable("disabled")
        return pool[pick_challenge_index(self.seed_provider(), day, len(pool))]

    def _issue(self, device: str, tier: str, inst: Instance, ctx: QuizContext) -> Attempt:
        options = [{"id": oid, "text": text} for oid, text in inst.options]
        order: Any = None
        answer: Any = inst.answer
        progress: Any = None
        if inst.answer_type == "choice":
            order = [o["id"] for o in options]
            self.rng.shuffle(order)
        snapshot: dict[str, Any] = {
            "prompt": inst.prompt, "figure": inst.figure, "hints": list(inst.hints),
            "explanation": inst.explanation, "answer_type": inst.answer_type,
            "options": options, "content_hash": inst.content_hash,
            "subject": inst.subject, "kind": inst.kind,
        }
        if inst.min_seconds is not None:
            snapshot["min_seconds"] = inst.min_seconds
        if inst.answer_type == ps.PASSAGE:
            extra, answer, order, progress = ps.build(inst, self.rng)
            snapshot.update(extra)
        a = Attempt(id=secrets.token_urlsafe(16), device=device, tier=tier,
                    template_id=inst.template_id, instance_id=inst.instance_id,
                    content_hash=inst.content_hash, snapshot=snapshot, answer=answer,
                    option_order=order, day=ctx.day, issued_utc=ctx.now, state="issued",
                    progress=progress)
        self.store.insert_attempt(a)
        return a

    # -- public API ------------------------------------------------------------
    async def status(self, device: str) -> StatusView:
        def fn(ctx: QuizContext) -> StatusView:
            _, used_s, allowance = self.monitor.usage(ctx.now)
            challenge = next((a for a in self._today(device, ctx.day)
                              if a.tier == "challenge"), None)
            exc = self._unavailable(ctx, device)
            return StatusView(
                remaining_s=max(0.0, allowance * 60 - used_s),
                quiz_today=self.store.count_attempts(device, ctx.day, state="correct"),
                earned_minutes=self.store.quiz_credit_minutes(ctx.day),
                challenge_state=challenge.state if challenge is not None else "none",
                available=exc is None, reason=None if exc is None else exc.reason,
                retry_after_s=None if exc is None else exc.retry_after_s)
        return await self._txn(fn, write=False, device=device)

    async def result(self, device: str, attempt_id: str) -> AttemptView:
        return await self._txn(lambda ctx: self._view(self._owned(device, attempt_id)),
                               write=False, device=device)

    async def current(self, device: str, tier: str) -> AttemptView:
        if tier not in TIERS:
            raise QuizRejected("bad_input")

        def fn(ctx: QuizContext) -> AttemptView:
            a = self.store.open_attempt(device, tier)
            if a is None and tier == "challenge":
                a = next((x for x in self._today(device, ctx.day) if x.tier == "challenge"), None)
                if a is None:
                    a = self._issue(device, tier, self._pick_challenge(ctx.day), ctx)
            elif a is None:
                a = self._issue(device, tier, self._pick_regular(device, ctx.now), ctx)
            return self._view(a)
        return await self._txn(fn, write=True, device=device)

    async def open_attempts(self, device: str) -> list[AttemptView]:
        """All issued attempts of the device, regular first. Read-only: never issues."""
        def fn(ctx: QuizContext) -> list[AttemptView]:
            open_ = [a for a in self.store.attempts_on(ctx.day)
                     if a.device == device and a.state == "issued"]
            open_.sort(key=lambda a: TIERS.index(a.tier) if a.tier in TIERS else len(TIERS))
            return [self._view(a) for a in open_]
        return await self._txn(fn, write=False, device=device)

    async def hint(self, device: str, attempt_id: str, rev: int) -> AttemptView:
        def fn(ctx: QuizContext, a: Attempt) -> AttemptView:
            # Passage hints are only available while parts remain.
            usable = not ps.is_passage(a) or ps.stage(a) == "parts"
            if a.state == "issued" and usable and a.hints_used < len(a.snapshot["hints"]):
                self.store.update_attempt(a.id, hints_used=a.hints_used + 1)
                a = self._owned(device, attempt_id)
            return self._view(a)
        return await self._write(device, attempt_id, rev, fn)

    async def skip(self, device: str, attempt_id: str, rev: int) -> AttemptView:
        def fn(ctx: QuizContext, a: Attempt) -> AttemptView:
            if a.state != "issued":
                return self._view(a)
            if a.tier == "challenge":
                raise QuizRejected("no_skip_challenge")
            free = a.reward_lost and not ps.is_passage(a)
            if not free and self._skips_used(device, ctx.day) >= self.cfg.skips_per_day:
                raise QuizRejected("skip_limit")
            self.store.update_attempt(a.id, state="skipped", finished_utc=ctx.now)
            return self._view(self._owned(device, attempt_id))
        return await self._write(device, attempt_id, rev, fn)

    async def submit(self, device: str, attempt_id: str, answer: str, rev: int,
                     step: int | None = None) -> AttemptView:
        # `step` is required for passage sets and ignored for single questions.
        def fn(ctx: QuizContext, a: Attempt) -> AttemptView:
            if a.state != "issued":
                return self._view(a)
            if ps.is_passage(a):
                self._submit_passage(a, answer, step, ctx)
                return self._view(self._owned(device, attempt_id))
            atype = a.snapshot["answer_type"]
            if atype == "choice":
                ids = {o["id"] for o in a.snapshot["options"]}
                if not isinstance(answer, str) or answer.strip() not in ids:
                    raise QuizRejected("bad_input")
            elif a.last_wrong_utc is not None:
                ready = a.last_wrong_utc + timedelta(seconds=self.cfg.wrong_cooldown_s)
                if ctx.now < ready:
                    raise QuizRejected("cooldown",
                                       math.ceil((ready - ctx.now).total_seconds()))
            verdict = check_answer(atype, a.answer, answer)
            if verdict is None:
                raise QuizRejected("bad_input")
            if verdict:
                self._finish_correct(a, ctx)
            elif atype == "choice":
                self.store.update_attempt(a.id, reward_lost=1, wrong_count=a.wrong_count + 1,
                                          last_wrong_utc=ctx.now)
            else:
                wrong = a.wrong_count + 1
                changes: dict[str, Any] = {"wrong_count": wrong, "last_wrong_utc": ctx.now}
                if wrong >= self.cfg.numeric_tries:
                    changes.update(state="exhausted", finished_utc=ctx.now)
                self.store.update_attempt(a.id, **changes)
            return self._view(self._owned(device, attempt_id))
        return await self._write(device, attempt_id, rev, fn)

    async def review(self, device: str, attempt_id: str, rev: int) -> AttemptView:
        """The child re-read the current vocab card after a wrong check answer."""
        def fn(ctx: QuizContext, a: Attempt) -> AttemptView:
            if a.state != "issued" or not ps.is_passage(a):
                return self._view(a)
            card = ps.current_card(a) if ps.stage(a) == "vocab" else None
            if card is not None and a.progress["vocab"][card]["need_review"]:
                self.store.update_attempt(
                    a.id, progress=ps.with_card(a.progress, card, need_review=False))
                a = self._owned(device, attempt_id)
            return self._view(a)
        return await self._write(device, attempt_id, rev, fn)

    # -- passage sets ------------------------------------------------------------
    def _submit_passage(self, a: Attempt, answer: str, step: int | None,
                        ctx: QuizContext) -> None:
        """Apply one passage submission; a step other than the current one is a no-op."""
        if not isinstance(step, int) or isinstance(step, bool) or not isinstance(answer, str):
            raise QuizRejected("bad_input")
        if step != ps.current_step(a):
            return
        given = answer.strip()
        if ps.parts_left(a):
            i = len(a.progress["parts"])
            if given not in a.option_order["parts"][i]:
                raise QuizRejected("bad_input")
            correct = given == a.answer["parts"][i]
            changes: dict[str, Any] = {"progress": ps.with_part(
                a.progress, given, correct, len(a.snapshot["parts"]), ctx.now)}
            if not correct:  # wrong parts feed the daily wrong pause; vocab wrongs never do
                changes.update(wrong_count=a.wrong_count + 1, last_wrong_utc=ctx.now)
            self.store.update_attempt(a.id, **changes)
            settled = self.store.get_attempt(a.id)
            if (settled is not None and not ps.parts_left(settled)
                    and ps.current_card(settled) is None):  # no vocab cards (word set)
                self._finish_passage(settled, ctx)
            return
        card = ps.current_card(a)
        if card is None:
            return
        if a.progress["vocab"][card]["need_review"]:
            raise QuizRejected("review_required")
        last = ps.last_vocab_utc(a)
        if last is not None:
            ready = last + timedelta(seconds=self.cfg.vocab_cooldown_s)
            if ctx.now < ready:
                raise QuizRejected("cooldown", math.ceil((ready - ctx.now).total_seconds()))
        if given not in a.option_order["vocab"][card]:
            raise QuizRejected("bad_input")
        stamp = ctx.now.isoformat()
        if given == a.answer["vocab"][card]:
            progress = ps.with_card(a.progress, card, done=True, last_utc=stamp)
        else:
            c = a.progress["vocab"][card]
            progress = ps.with_card(a.progress, card, wrong=c["wrong"] + 1,
                                    need_review=True, last_utc=stamp)
        self.store.update_attempt(a.id, progress=progress)
        settled = self.store.get_attempt(a.id)
        if settled is not None and ps.current_card(settled) is None:
            self._finish_passage(settled, ctx)

    def _finish_passage(self, a: Attempt, ctx: QuizContext) -> None:
        """Settle a passage once its last vocab card is done."""
        wrong = ps.wrong_parts(a)
        word_set = a.snapshot.get("kind") == WORD_SET_KIND
        if wrong >= (1 if word_set else 2):  # no reward and no notification
            self.store.update_attempt(a.id, state="exhausted", finished_utc=ctx.now,
                                      outcome="parts_wrong", credited_minutes=0.0)
            return
        challenge = a.tier == "challenge"
        base = self._base_minutes(a)
        nominal = base // 2 if wrong == 1 else base
        if a.hints_used > 0:
            nominal //= 2
        min_s = a.snapshot.get("min_seconds") or (
            self.cfg.min_seconds_passage_challenge if challenge
            else self.cfg.min_seconds_passage_regular)
        done = ps.parse_utc(a.progress["parts_done_utc"])
        elapsed = ((done or ctx.now) - a.issued_utc).total_seconds()
        if elapsed < min_s:
            outcome, credited = "too_fast", 0.0
            reason = f"（答得太快：用时 {elapsed:.0f} 秒，少于 {min_s} 秒，未计入）"
        elif (limit := self._kind_limit_reached(a, ctx.day)) is not None:
            outcome, credited = "kind_cap", 0.0
            reason = f"（这类题今天已奖励 {limit} 个，这题算练习，未计入）"
        else:
            credited = ctx.award(a.id, nominal, f"{a.tier}:{a.template_id}")
            if credited <= 0:
                outcome, reason = "cap_full", "（今日答题奖励已到上限，未计入）"
            elif credited < nominal:
                outcome = "cap"
                reason = f"（已到上限，只计入 {_fmt(credited)}/{nominal} 分钟）"
            elif wrong == 1:
                outcome = "half"
                reason = ("（错 1 道小题且用了提示，奖励两次减半）" if a.hints_used > 0
                          else "（错 1 道小题，奖励减半）")
            elif a.hints_used > 0:
                outcome, reason = "hint", "（用了提示，奖励减半）"
            else:
                outcome, reason = "credited", ""
        self.store.update_attempt(a.id, state="correct", finished_utc=ctx.now,
                                  nominal_minutes=float(nominal),
                                  credited_minutes=float(credited), outcome=outcome)
        n = self.store.count_attempts(a.device, ctx.day, state="correct")
        total = self.store.quiz_credit_minutes(ctx.day)
        n_parts = len(a.snapshot["parts"])
        label = "单词题" if word_set else "阅读题"
        ctx.notify(f"✅ {label}答对 {n_parts - wrong}/{n_parts} +{_fmt(credited)} 分钟"
                   f"（今日 {n} 题，已赚 {_fmt(total)} 分钟）{reason}")
        if challenge:
            ctx.notify("🏆 今日挑战完成")

    # -- grading -----------------------------------------------------------------
    @staticmethod
    def _family(kind: str, table: Mapping[str, int]) -> str | None:
        """The key of `table` covering this kind ("vocab" covers vocab_*), if any."""
        return next((f for f in table if kind == f or kind.startswith(f + "_")), None)

    def _base_minutes(self, a: Attempt) -> int:
        if a.tier == "challenge":
            return self.cfg.challenge_minutes
        family = self._family(a.snapshot.get("kind") or "", self.cfg.kind_minutes)
        return self.cfg.regular_minutes if family is None else self.cfg.kind_minutes[family]

    def _kind_limit_reached(self, a: Attempt, day: date) -> int | None:
        """The daily limit when this attempt's kind family already used it up today."""
        if a.tier == "challenge":
            return None
        table = self.cfg.kind_daily_limit
        family = self._family(a.snapshot.get("kind") or "", table)
        if family is None:
            return None
        used = sum(1 for x in self.store.attempts_on(day)
                   if x.id != a.id and x.tier == "regular" and x.credited_minutes > 0
                   and self._family(x.snapshot.get("kind") or "", {family: 0}) == family)
        return table[family] if used >= table[family] else None

    def _finish_correct(self, a: Attempt, ctx: QuizContext) -> None:
        challenge = a.tier == "challenge"
        base = self._base_minutes(a)
        nominal = base // 2 if a.hints_used > 0 else base
        min_s = a.snapshot.get("min_seconds") or (
            self.cfg.min_seconds_challenge if challenge else self.cfg.min_seconds_regular)
        elapsed = (ctx.now - a.issued_utc).total_seconds()
        if elapsed < min_s:
            outcome, credited = "too_fast", 0.0
            reason = f"（答得太快：用时 {elapsed:.0f} 秒，少于 {min_s} 秒，未计入）"
        elif a.reward_lost:
            outcome, credited = "lost", 0.0
            reason = "（选择题答错过，未计入）"
        elif (limit := self._kind_limit_reached(a, ctx.day)) is not None:
            outcome, credited = "kind_cap", 0.0
            reason = f"（这类题今天已奖励 {limit} 个，这题算练习，未计入）"
        else:
            credited = ctx.award(a.id, nominal, f"{a.tier}:{a.template_id}")
            if credited <= 0:
                outcome, reason = "cap_full", "（今日答题奖励已到上限，未计入）"
            elif credited < nominal:
                outcome = "cap"
                reason = f"（已到上限，只计入 {_fmt(credited)}/{nominal} 分钟）"
            elif a.hints_used > 0:
                outcome, reason = "hint", "（用了提示，奖励减半）"
            else:
                outcome, reason = "credited", ""
        self.store.update_attempt(a.id, state="correct", finished_utc=ctx.now,
                                  nominal_minutes=float(nominal),
                                  credited_minutes=float(credited), outcome=outcome)
        n = self.store.count_attempts(a.device, ctx.day, state="correct")
        total = self.store.quiz_credit_minutes(ctx.day)
        ctx.notify(f"✅ 答对 1 题 +{_fmt(credited)} 分钟（今日 {n} 题，已赚 {_fmt(total)} 分钟）"
                   + reason)
        if challenge:
            ctx.notify("🏆 今日挑战完成")
