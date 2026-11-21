"""Monitor: turns TV state changes + periodic ticks into sessions, decisions,
TV actions and parent notifications. Decisions are made under one lock; TV
actions run after the lock is released (they may re-enter via on_state)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

from ltp.config import MODES, Settings
from ltp.notify import Buttons, Command, Notifier, Reply
from ltp.policy import (Action, Decision, allowance_minutes, evaluate, in_curfew, is_counting,
                        rule_for)
from ltp.rules import AppRule
from ltp.store import Store
from ltp.tv.base import NotPairedError, TVController, TVState

if TYPE_CHECKING:
    from ltp.devmode import DevModeJob
    from ltp.quiz.service import QuizService

log = logging.getLogger(__name__)

ActionFn = Callable[[], Awaitable[None]]
T = TypeVar("T")
MAX_ADD_MINUTES = 180
TELEGRAM_MESSAGE_LIMIT = 4096  # Telegram text cap; /quiz check reserves 64 chars for the id prefix
QUIZ_TOAST_SUFFIX = " · Open Quiz to earn more time"
REASON_TEXT = {"quota": "今日娱乐时间已用完", "locked": "家长已锁定电视", "curfew": "现在是休息时段"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_min(seconds: float) -> str:
    return f"{max(0, seconds) / 60:.0f}"


@dataclass
class _Streak:
    """Violations that follow each other within the violation window."""
    first: datetime
    last: datetime
    sent: set[str]  # actions ("close_app" / "power_off") already reported in this run
    held: int = 0   # violations whose message was held back
    held_power_off: set[int | None] = field(default_factory=set)  # sessions quietly powered off
    confirmed_off: set[int | None] = field(default_factory=set)   # ... where it really happened


@dataclass
class QuizContext:
    """Handle given to a quiz transaction callback; credits are only ever written here."""
    now: datetime
    day: date
    blocked: str | None  # "locked" | "curfew" | None
    _award: Callable[[str, float, str], float]
    _notices: list[str] = field(default_factory=list)

    def award(self, attempt_id: str, nominal: float, note: str) -> float:
        """Credit up to `nominal` minutes (capped by the daily limit); returns minutes credited."""
        if self.blocked is not None:
            raise RuntimeError(f"cannot award while blocked ({self.blocked})")
        return self._award(attempt_id, nominal, note)

    def notify(self, text: str) -> None:
        """Queue a parent notification, sent after commit and outside the lock."""
        self._notices.append(text)


class Monitor:
    def __init__(self, tv: TVController, store: Store, notifier: Notifier, settings: Settings,
                 now: Callable[[], datetime] = _utcnow) -> None:
        self.tv = tv
        self.store = store
        self.notifier = notifier
        self.s = settings
        self._now = now
        self._lock = asyncio.Lock()
        self._state = TVState.OFFLINE
        self._session_id: int | None = None
        self._session_app: str | None = None
        self._last_hb: datetime | None = None
        # Whether quota remained when the current session opened (natural expiry
        # of such a session is not a violation).
        self._session_had_quota = False
        # (session id, reason, action, mode, violation count) of the last enforcement,
        # for retry dedup
        self._enforced: tuple[int | None, str, str, str, int] | None = None
        # Run of back-to-back violations whose repeat messages are being merged.
        # In memory only: a restart mid-run just loses the held-back count.
        self._streak: _Streak | None = None
        # Quiz app to hand over to after a quota close; None when the quiz service is
        # unavailable (set by the runtime wiring).
        self.quiz_app_id: str | None = None
        # The user's own rule for the quiz app id (if any), restored by disable_quiz.
        self._quiz_prior_rule: AppRule | None = None
        # Quiz service for parent commands and the digest; injected by the runtime wiring.
        self.quiz: QuizService | None = None
        # Daily developer-mode check; runs as a background task outside the lock.
        self.devmode: DevModeJob | None = None
        tv.on_state(self.on_state)

    def enable_quiz(self, app_id: str, prior_rule: AppRule | None,
                    service: QuizService | None = None) -> None:
        """Wire the quiz app and service; `prior_rule` is what the user configured for the
        app, if anything. disable_quiz undoes all three."""
        self.quiz_app_id = app_id
        self._quiz_prior_rule = prior_rule
        self.quiz = service

    async def disable_quiz(self) -> None:
        """Quiz service is unavailable: stop handing over to the quiz app and revoke the
        quota-exhausted exemption the quiz added (restoring the user's own rule, if any)."""
        async with self._lock:
            app_id = self.quiz_app_id
            self.quiz_app_id = None
            self.quiz = None  # /quiz and the daily digest follow the feature off
            if app_id is None:
                return
            rules = dict(self.s.app_rules)
            if self._quiz_prior_rule is None:
                rules.pop(app_id, None)
            else:
                rules[app_id] = self._quiz_prior_rule
            self.s = replace(self.s, app_rules=rules)

    # -- helpers ----------------------------------------------------------
    @property
    def mode(self) -> str:
        m = self.store.get_kv("mode")
        return m if m in MODES else self.s.mode

    @property
    def locked(self) -> bool:
        return self.store.get_kv("locked") == "1"

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self.s.timezone)

    def _day_bounds(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), self.s.timezone)
        end = datetime.combine(day + timedelta(days=1), time(0), self.s.timezone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _once(self, key: str) -> bool:
        """True the first time a key is seen (persisted)."""
        if self.store.get_kv(key) is not None:
            return False
        self.store.set_kv(key, "1")
        return True

    async def notify_once(self, key: str, text: str) -> None:
        """Send a parent notification at most once per business day for `key`."""
        async with self._lock:
            first = self._once(f"notice:{self.business_day(self._now())}:{key}")
        if first:
            await self.notifier.send(text, None)

    def _send(self, text: str, buttons: Buttons | None = None) -> ActionFn:
        async def run() -> None:
            await self.notifier.send(text, buttons)
        return run

    def _tv_call(self, label: str, fn: Callable[[], Awaitable[None]]) -> ActionFn:
        async def run() -> None:
            try:
                await fn()
            except Exception as exc:
                log.warning("TV action %s failed: %s", label, exc)
                self.store.log_event("action_failed", f"{label}: {exc}", self._now())
        return run

    async def _execute(self, actions: list[ActionFn]) -> None:
        for act in actions:
            try:
                await act()
            except Exception:
                log.exception("action failed")

    def _title(self, app_id: str) -> str:
        return self.tv.app_title(app_id) or app_id

    def business_day(self, now: datetime) -> date:
        """The single owner of "which day's quota/credits apply"."""
        return self._local(now).date()

    def usage(self, now: datetime) -> tuple[date, float, float]:
        day = self.business_day(now)
        start, end = self._day_bounds(day)
        used = self.store.used_seconds(start, end)
        allowance = allowance_minutes(self.store.credits(day), self.s)
        return day, used, allowance

    async def quiz_txn(self, fn: Callable[[QuizContext], T]) -> T:
        """Run synchronous `fn` under the lock inside one DB transaction.

        On success: commit, re-evaluate enforcement (a new credit may end a quota
        enforcement), then run decide-actions and queued notices outside the lock.
        On error: roll back, drop queued notices, re-raise.
        """
        async with self._lock:
            now = self._now()
            day = self.business_day(now)
            blocked = ("locked" if self.locked
                       else "curfew" if in_curfew(self._local(now), self.s) else None)

            credited_any = False

            def award(attempt_id: str, nominal: float, note: str) -> float:
                nonlocal credited_any
                room = (self.s.daily_cap_minutes - self.s.free_minutes
                        - self.store.quiz_credit_minutes(day))
                credited = max(0.0, min(nominal, room))
                if credited > 0:
                    self.store.add_credit(day, credited, "quiz", note, now,
                                          ref=f"quiz:{attempt_id}")
                    credited_any = True
                return credited

            ctx = QuizContext(now=now, day=day, blocked=blocked, _award=award)
            with self.store.transaction():
                result = fn(ctx)
            # Re-evaluate only after a credit: plain reads must not multiply enforcement retries.
            actions = self._decide(now) if credited_any else []
        await self._execute(actions + [self._send(text) for text in ctx._notices])
        return result

    # -- sessions ---------------------------------------------------------
    def _close_session(self, ts: datetime, reason: str) -> None:
        if self._session_id is not None:
            self.store.close_session(self._session_id, ts, reason)
        self._session_id = None
        self._session_app = None
        self._last_hb = None

    def _open_session(self, app_id: str, ts: datetime, had_quota: bool | None = None) -> None:
        if had_quota is None:
            _, used, allowance = self.usage(ts)
            had_quota = allowance * 60 - used > 0
        self._session_had_quota = had_quota
        self._session_id = self.store.open_session(
            app_id, self._title(app_id), is_counting(app_id, self.s), ts)
        self._session_app = app_id
        self._last_hb = ts

    def _heartbeat(self, now: datetime) -> None:
        if self._session_id is None or self._last_hb is None:
            return
        gap = (now - self._last_hb).total_seconds()
        if gap > 3 * self.s.tick_seconds or gap < 0:
            # Stall / suspend / clock jump: don't count the gap.
            app = self._session_app or ""
            self.store.log_event("heartbeat_gap", f"{gap:.0f}s", now)
            had_quota = self._session_had_quota
            self._close_session(self._last_hb, "gap")
            self._open_session(app, now, had_quota)
            return
        self.store.heartbeat(self._session_id, now)
        self._last_hb = now
        self.store.set_kv("last_active_utc", now.isoformat())

    # -- state changes ----------------------------------------------------
    async def on_state(self, state: TVState) -> None:
        async with self._lock:
            actions = self._on_state_locked(state)
        await self._execute(actions)

    def _on_state_locked(self, state: TVState) -> list[ActionFn]:
        now = self._now()
        prev, self._state = self._state, state
        actions: list[ActionFn] = []
        desired = state.app_id if state.active else None

        if desired != self._session_app or (desired is not None and self._session_id is None):
            if self._session_id is not None:
                self._heartbeat(now)
                self._close_session(now, "app_switch" if desired is not None else "offline")
            if desired is not None:
                self._open_session(desired, now)

        if state.active and not prev.active:
            actions += self._on_power_on(now)
        if prev.active and not state.active:
            self.store.set_kv("last_active_utc", now.isoformat())
            self.store.log_event("tv_off" if state.connected else "tv_lost", prev.app_id, now)
            if not state.connected and is_counting(prev.app_id, self.s):
                actions.append(self._send(
                    f"⚠️ 电视连接中断（当时在看 {self._title(prev.app_id)}）。"
                    "如果不是关机，可能是电视被断网，断网期间无法计时。"))
        if state.active and desired != prev.app_id and is_counting(desired or "", self.s):
            actions += self._on_play(desired or "", now)
        if state.active:
            actions += self._decide(now)
        return actions

    def _on_power_on(self, now: datetime) -> list[ActionFn]:
        self.store.log_event("tv_on", self._state.app_id, now)
        last = self.store.get_kv("last_active_utc")
        if last is not None:
            idle = now - datetime.fromisoformat(last)
            if idle < timedelta(minutes=self.s.power_on_debounce_minutes):
                return []
        _, used, allowance = self.usage(now)
        return [self._send(f"📺 电视开机了（{self._local(now):%H:%M}）。"
                           f"今日已用 {_fmt_min(used)}/{allowance:g} 分钟。")]

    def _on_play(self, app_id: str, now: datetime) -> list[ActionFn]:
        day, used, allowance = self.usage(now)
        title = self._title(app_id)
        if self._once(f"notice:{day}:first_play"):
            return [self._send(f"▶️ 开始娱乐：{title}（今日剩余 "
                               f"{_fmt_min(allowance * 60 - used)} 分钟）")]
        if self.s.notify_app_switch:
            return [self._send(f"🔀 切换到 {title}")]
        return []

    # -- decisions --------------------------------------------------------
    def _evaluate(self, now: datetime) -> Decision:
        day, used, allowance = self.usage(now)
        # Single point where remaining quota is computed (tick, post-award _decide, /add
        # re-checks): once a counted session sees quota left, its expiry is natural,
        # whichever path granted that quota.
        if (self._session_id is not None and is_counting(self._state.app_id, self.s)
                and allowance * 60 - used > 0):
            self._session_had_quota = True
        return evaluate(used_s=used, allowance_min=allowance,
                        counting=is_counting(self._state.app_id, self.s),
                        allowed_when=rule_for(self._state.app_id, self.s).allowed_when,
                        locked=self.locked, curfew=in_curfew(self._local(now), self.s),
                        warned=frozenset(), settings=self.s, day=day)

    def _enforce_call(self, label: str, fn: Callable[[], Awaitable[None]], reason: str, *,
                      toast: str | None = None, done_msg: str | None = None,
                      buttons: Buttons | None = None,
                      then: Callable[[], Awaitable[None]] | None = None,
                      on_done: Callable[[], None] | None = None) -> ActionFn:
        """Recheck -> toast -> destructive TV call -> confirmation, as one action.

        Actions run after the lock is released, so a parent command (/add, /unlock,
        /mode observe) or an app switch may land in between; a stale action is dropped
        silently, and the parent is only told "done" if the TV call succeeded.
        """
        session_id = self._session_id

        async def still_due() -> bool:
            async with self._lock:
                d = self._evaluate(self._now()) if self._state.active else None
                due = (self.mode == "enforce" and self._session_id == session_id
                       and d is not None and d.action is Action.ENFORCE and d.reason == reason)
            if not due:
                log.info("dropping stale %s (state changed before it ran)", label)
            return due

        async def run() -> None:
            if not await still_due():
                return
            if toast is not None:
                await self._tv_call("toast", lambda: self.tv.show_toast(toast))()
                if not await still_due():  # the toast round-trip is another window
                    return
            try:
                await fn()
            except Exception as exc:
                log.warning("TV action %s failed: %s", label, exc)
                self.store.log_event("action_failed", f"{label}: {exc}", self._now())
                if done_msg is not None:
                    await self.notifier.send(f"⚠️ {label} 失败（{exc}），下个周期重试", None)
                return
            if on_done is not None:
                on_done()
            if done_msg is not None:
                await self.notifier.send(done_msg, buttons)
            if then is not None:
                try:
                    await then()
                except Exception as exc:
                    log.warning("follow-up after %s failed: %s", label, exc)
                    now = self._now()
                    self.store.log_event("action_failed", f"{label} follow-up: {exc}", now)
                    if self._once(f"notice:{self.business_day(now)}:quiz_launch_failed"):
                        await self.notifier.send("⚠️ 答题 App 打不开，已停在主页", None)
        return run

    def _quiz_then(self, reason: str) -> Callable[[], Awaitable[None]] | None:
        """Follow-up for a confirmed quota close: open the quiz app so the kid can earn time."""
        quiz = self.quiz_app_id
        if reason != "quota" or self.mode != "enforce" or not quiz:
            return None
        return lambda: self.tv.launch_app(quiz)

    def _decide(self, now: datetime) -> list[ActionFn]:
        if not self._state.active:
            return []
        app_id = self._state.app_id
        day = self._local(now).date()
        prefix = f"warned:{day}:"
        candidate = self._evaluate(now)
        # Warn dedup is persisted in kv (survives restarts) rather than passed in.
        if candidate.action is not Action.ENFORCE:
            self._enforced = None
        if candidate.action is Action.WARN and candidate.warn_key is not None:
            if self.store.get_kv(prefix + candidate.warn_key) is not None:
                return []
            self.store.set_kv(prefix + candidate.warn_key, "1")
            return self._warn(candidate.warn_tier or 0, app_id)
        if candidate.action is Action.ENFORCE:
            return self._enforce(candidate.reason, app_id, now)
        return []

    def _warn(self, tier: int, app_id: str) -> list[ActionFn]:
        actions: list[ActionFn] = []
        if self.mode == "enforce":
            actions.append(self._tv_call("toast", lambda: self.tv.show_toast(
                f"⏳ 娱乐时间还剩 {tier} 分钟"
                + (QUIZ_TOAST_SUFFIX if self.quiz_app_id else ""))))
        if tier == max(self.s.warn_minutes):
            actions.append(self._send(f"⏳ 还剩 {tier} 分钟（{self._title(app_id)}）"))
        return actions

    def _enforce(self, reason: str, app_id: str, now: datetime) -> list[ActionFn]:
        mode = self.mode
        enforce = mode == "enforce"
        why = REASON_TEXT.get(reason, reason)
        title = self._title(app_id)
        actions: list[ActionFn] = []
        if self._enforced is not None and self._enforced[:2] == (self._session_id, reason):
            _, _, action, prev_mode, count = self._enforced
            if prev_mode == mode:
                # Same session still in front: the previous action didn't take. Retry it
                # quietly (never downgrade a failed power-off to a close).
                if not enforce:
                    return []
                if action == "power_off":
                    return [self._enforce_call("power_off", self.tv.power_off, reason,
                                               on_done=self._held_off_done())]
                return [self._enforce_call("close_app", lambda: self.tv.close_app(app_id),
                                           reason, then=self._quiz_then(reason))]
            # Mode changed (e.g. observe -> enforce): run the full flow again for the same
            # violation, without counting it twice.
            power_off = action == "power_off"
        elif reason == "quota" and self._session_had_quota:
            # Quota ran out while watching: natural expiry, not a violation.
            self._enforced = (self._session_id, reason, "close_app", mode, 0)
            msg = f"⛔ {why}，已退出 {title}"
            if not enforce:
                return [self._send(msg + "（观察模式，未执行）", self._buttons())]
            return [self._enforce_call("close_app", lambda: self.tv.close_app(app_id), reason,
                                       toast=f"⛔ {why}", done_msg=msg, buttons=self._buttons(),
                                       then=self._quiz_then(reason))]
        else:
            self.store.log_event("violation", f"{reason}:{app_id}", now)
            since = now - timedelta(minutes=self.s.violation_window_minutes)
            count = self.store.count_events("violation", since)
            power_off = (count >= self.s.violation_count
                         or (self.s.curfew_power_off and in_curfew(self._local(now), self.s)))
            pre = self._end_streak(now)  # an earlier run that ended without a tick
            if self._hold_message(now, "power_off" if power_off else "close_app"):
                # Same kind of message already sent in this run: enforce quietly.
                self._enforced = (self._session_id, reason,
                                  "power_off" if power_off else "close_app", mode, count)
                return pre + self._enforce_quiet(reason, app_id, power_off, why)
            actions = pre
        self._enforced = (self._session_id, reason, "power_off" if power_off else "close_app",
                          mode, count)
        if power_off and count >= self.s.violation_count:
            msg = f"⏻ {why}，{self.s.violation_window_minutes:g} 分钟内第 {count} 次打开 {title}，已关机"
        elif power_off:
            msg = f"⏻ {why}，打开了 {title}，已关机"
        elif count <= 1:  # 0 = natural expiry carried across a mode switch
            msg = f"⛔ {why}，已退出 {title}"
        else:
            msg = f"⛔ {why}，第 {count} 次打开 {title}，已再次退出"
        if not enforce:
            return actions + [self._send(msg + "（观察模式，未执行）",
                                         None if power_off else self._buttons())]
        if power_off:
            return actions + [self._enforce_call("power_off", self.tv.power_off, reason,
                                                 toast=f"⏻ {why}", done_msg=msg)]
        return actions + [self._enforce_call("close_app", lambda: self.tv.close_app(app_id),
                                             reason, toast=f"⛔ {why}", done_msg=msg,
                                             buttons=self._buttons(),
                                             then=self._quiz_then(reason))]

    def _enforce_quiet(self, reason: str, app_id: str, power_off: bool,
                       why: str) -> list[ActionFn]:
        """Enforcement for a violation whose parent message is merged into the run summary."""
        if self.mode != "enforce":
            return []
        if power_off:
            if self._streak is not None:
                self._streak.held_power_off.add(self._session_id)
            return [self._enforce_call("power_off", self.tv.power_off, reason, toast=f"⏻ {why}",
                                       on_done=self._held_off_done())]
        return [self._enforce_call("close_app", lambda: self.tv.close_app(app_id), reason,
                                   toast=f"⛔ {why}", then=self._quiz_then(reason))]

    # -- merging repeat violation messages -------------------------------
    def _held_off_done(self) -> Callable[[], None] | None:
        """Completion hook for a merged power-off (first try or retry): only a power-off that
        really happened is reported in the run summary, once per session."""
        st, session_id = self._streak, self._session_id
        if st is None or session_id not in st.held_power_off:
            return None

        def done() -> None:
            st.confirmed_off.add(session_id)
        return done

    def _hold_message(self, now: datetime, action: str) -> bool:
        """Add a violation to the current run; True when its message should be held back
        (a message for the same action was already sent in this run)."""
        st = self._streak
        if st is None:
            self._streak = _Streak(first=now, last=now, sent={action})
            return False
        st.last = now
        if action not in st.sent:
            st.sent.add(action)
            return False
        st.held += 1
        return True

    def _end_streak(self, now: datetime) -> list[ActionFn]:
        """Close the run once the window has passed without a new violation, and report
        what was held back."""
        st = self._streak
        window = timedelta(minutes=self.s.violation_window_minutes)
        if st is None or now - st.last < window:
            return []
        self._streak = None
        if st.held == 0:
            return []
        first, last = self._local(st.first), self._local(st.last)
        off = f"，其中 {len(st.confirmed_off)} 次关机" if st.confirmed_off else ""
        note = "" if self.mode == "enforce" else "（观察模式，未执行）"
        return [self._send(f"🔁 {first:%H:%M}–{last:%H:%M} 又重复打开了 {st.held} 次"
                           f"{off}，已合并为这一条{note}")]

    # -- tick -------------------------------------------------------------
    async def tick(self) -> None:
        if self.tv.state.connected:
            try:
                await self.tv.refresh()
            except Exception as exc:
                log.warning("TV liveness probe failed (%s); reconnecting", exc)
                await self.tv.disconnect()
        async with self._lock:
            now = self._now()
            self._heartbeat(now)
            actions = self._decide(now)
            actions += self._end_streak(now)
            actions += self._maybe_summary(now)
        await self._execute(actions)
        if self.devmode is not None:
            self.devmode.kick()  # never raises; the check itself runs in its own task

    def _maybe_summary(self, now: datetime) -> list[ActionFn]:
        at = self.s.daily_summary_at
        local = self._local(now)
        if at is None or local.time() < at:
            return []
        if not self._once(f"notice:{local.date()}:summary"):
            return []
        text = "🌙 每日汇总\n" + self.status_text(now)
        if self.quiz is not None:
            text += "\n" + self.quiz.daily_digest(self.business_day(now))
        return [self._send(text)]

    # -- parent commands --------------------------------------------------
    def _buttons(self) -> Buttons:
        lock = ("🔓 解锁", "unlock") if self.locked else ("🔒 锁定", "lock")
        power = ("⏻ 关机", "off") if self.tv.state.active else ("⏻ 开机", "on")
        return [[("+10 分钟", "add:10"), ("+30 分钟", "add:30")],
                [lock, power, ("🔄 刷新", "status")]]

    def status_text(self, now: datetime | None = None) -> str:
        now = now or self._now()
        day, used, allowance = self.usage(now)
        st = self._state
        if not st.connected:
            current = "离线/关机"
        elif not st.power_on:
            current = "待机"
        else:
            current = self._title(st.app_id) if st.app_id else "主页"
        return (f"📊 {day:%m-%d} 已用 {_fmt_min(used)} / {allowance:g} 分钟"
                f"（剩 {_fmt_min(allowance * 60 - used)}）\n"
                f"当前：{current}\n"
                f"模式：{'执行' if self.mode == 'enforce' else '观察'}"
                f" | 锁定：{'是' if self.locked else '否'}"
                f" | 休息时段：{'是' if in_curfew(self._local(now), self.s) else '否'}")

    async def handle(self, cmd: Command) -> Reply | None:
        now = self._now()
        self.store.log_event("command", f"{cmd.user_id} {cmd.name} {cmd.arg}".strip(), now)
        if cmd.name == "on":
            return await self._power_on()
        actions: list[ActionFn] = []
        async with self._lock:
            reply = self._handle_locked(cmd, now, actions)
        await self._execute(actions)
        return reply

    async def _power_on(self) -> Reply:
        if self.tv.state.active:
            return Reply("📺 电视已经开着\n" + self.status_text(), self._buttons())
        try:
            await self.tv.power_on()
        except Exception as exc:
            log.warning("power_on failed: %s", exc)
            self.store.log_event("action_failed", f"power_on: {exc}", self._now())
            return Reply(f"⚠️ 开机信号发送失败：{exc}")
        return Reply("⏻ 已发送开机信号，电视约 1 分钟后开机，连上后会通知")

    def _handle_locked(self, cmd: Command, now: datetime, actions: list[ActionFn]) -> Reply:
        name = cmd.name
        if name == "add":
            try:
                minutes = int(cmd.arg or "10")
            except ValueError:
                return Reply("用法：/add 分钟数（1–180）")
            if not 1 <= minutes <= MAX_ADD_MINUTES:
                return Reply(f"加时范围 1–{MAX_ADD_MINUTES} 分钟")
            day = self._local(now).date()
            self.store.add_credit(day, minutes, "parent", f"telegram:{cmd.user_id}", now)
            self._enforced = None
            return Reply(f"✅ 已加 {minutes} 分钟\n" + self.status_text(now), self._buttons())
        if name == "lock":
            self.store.set_kv("locked", "1")
            actions += self._decide(now)
            return Reply("🔒 已锁定\n" + self.status_text(now), self._buttons())
        if name == "unlock":
            self.store.del_kv("locked")
            self._enforced = None
            return Reply("🔓 已解锁\n" + self.status_text(now), self._buttons())
        if name == "off":
            if not self.tv.state.connected:
                return Reply("电视当前离线，无法远程关机")
            actions.append(self._tv_call("power_off", self.tv.power_off))
            return Reply("⏻ 已发送关机指令")
        if name == "mode":
            arg = cmd.arg.strip().lower()
            if arg not in MODES:
                return Reply("用法：/mode observe（只通知）或 /mode enforce（执行限制）")
            self.store.set_kv("mode", arg)
            actions += self._decide(now)  # apply the new mode right away
            return Reply(f"模式已切换为：{'执行' if arg == 'enforce' else '观察'}")
        if name == "quiz":
            return self._quiz_command(cmd.arg.strip(), now)
        if name in ("status", "today"):
            return Reply(self.status_text(now), self._buttons())
        return Reply("命令：/status 今日状态 · /add 分钟 加时 · /lock 锁定 · /unlock 解锁 · "
                     "/on 开机 · /off 关机 · /mode observe|enforce · /quiz 答题明细",
                     self._buttons())

    # -- main loop --------------------------------------------------------
    def _quiz_command(self, arg: str, now: datetime) -> Reply:
        if self.quiz is None:
            return Reply("答题功能未启用")
        from ltp.quiz.service import QuizUnavailable  # local: service imports this module
        sub, _, rest = arg.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub == "check":
            try:
                instance_id, text = self.quiz.spot_check(TELEGRAM_MESSAGE_LIMIT - 64)
            except QuizUnavailable:
                return Reply("没有可抽查的题目")
            return Reply(f"{instance_id}\n{text}")
        if sub == "bad":
            if not rest:
                return Reply("用法：/quiz bad 模板id")
            if self.quiz.disable_template(rest):
                return Reply(f"已下架 {rest}")
            return Reply(f"找不到 {rest}")
        lines = self.quiz.today_lines(self.business_day(now))
        return Reply("\n".join(lines) if lines else "今天还没有答题")

    async def _connection_loop(self) -> None:
        backoff = 5.0
        while True:
            if self.tv.state.connected:
                backoff = 5.0
                await asyncio.sleep(self.s.tick_seconds)
                continue
            try:
                await self.tv.connect()
                log.info("connected to TV")
                backoff = 5.0
            except NotPairedError:
                day = self._local(self._now()).date()
                if self._once(f"notice:{day}:not_paired"):
                    await self.notifier.send("ℹ️ 尚未与电视配对：电视开机时在服务器上运行 "
                                             "`uv run ltp pair`，并在电视上点“允许”。")
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("TV not reachable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.tick_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")

    async def run(self) -> None:
        closed = self.store.close_dangling()
        now = self._now()
        self.store.log_event("daemon_start", f"mode={self.mode} dangling={closed}", now)
        await self.notifier.send(f"🟢 Learning-to-Play 监控已启动（{'执行' if self.mode == 'enforce' else '观察'}模式）")
        await asyncio.gather(self._connection_loop(), self._tick_loop())
