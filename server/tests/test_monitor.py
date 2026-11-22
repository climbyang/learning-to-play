from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ltp.config import load_settings
from ltp.monitor import CURFEW_TOAST, Monitor
from ltp.notify import Command
from ltp.rules import AllowedWhen, AppRule
from ltp.store import Store
from ltp.tv.fake import FakeTV

YT = "youtube.leanback.v4"
TUBI = "com.tubitv.ott"
HOME = "com.webos.app.home"
PARENT = 111


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


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("mode: enforce\n", encoding="utf-8")
    s = load_settings(cfg / "settings.yaml", {})
    clock = Clock()
    tv = FakeTV({YT: "YouTube"})
    sink = Sink()
    store = Store(tmp_path / "ltp.db")
    mon = Monitor(tv, store, sink, s, now=clock)
    return mon, tv, sink, clock, store, s


async def watch(mon, clock, minutes, step=10):
    """Advance time with ticks every `step` seconds."""
    for _ in range(int(minutes * 60 / step)):
        clock.advance(seconds=step)
        await mon.tick()


def used_min(mon, clock):
    return mon.usage(clock())[1] / 60


async def test_time_counts_only_while_connected(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 3)
    assert used_min(mon, clock) == pytest.approx(3)
    await tv.set(connected=False)
    await watch(mon, clock, 5)
    assert used_min(mon, clock) == pytest.approx(3)


async def test_home_screen_does_not_count(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=HOME)
    await watch(mon, clock, 5)
    assert used_min(mon, clock) == 0


async def test_power_on_and_first_play_notified_once(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    assert sink.has("电视开机")
    assert sink.has("开始娱乐：YouTube")
    n = len(sink.msgs)
    await tv.set(app_id=TUBI)
    assert len(sink.msgs) == n  # app switch quiet by default


async def test_power_on_debounced(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=HOME)
    await watch(mon, clock, 1)
    await tv.set(connected=True, power_on=False)
    clock.advance(minutes=2)
    await tv.set(app_id=HOME)
    assert sum("电视开机" in m for m in sink.msgs) == 1
    await tv.set(connected=True, power_on=False)
    clock.advance(minutes=10)
    await tv.set(app_id=HOME)
    assert sum("电视开机" in m for m in sink.msgs) == 2


async def test_warning_then_enforce_closes_app(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 15.5)
    assert sink.has("还剩 5 分钟")
    assert "⏳ 娱乐时间还剩 5 分钟" in tv.toasts
    await watch(mon, clock, 4.5)
    assert tv.closed == [YT]
    assert sink.has("已退出 YouTube")


async def test_observe_mode_never_touches_tv(env):
    mon, tv, sink, clock, store, s = env
    store.set_kv("mode", "observe")
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert tv.closed == [] and tv.toasts == [] and tv.powered_off == 0
    assert sink.has("观察模式，未执行")
    assert sum("时间已用完" in m for m in sink.msgs) == 1  # not repeated every tick


async def test_repeated_reopen_powers_off(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    assert tv.closed == [YT]
    clock.advance(minutes=1)
    await tv.set(app_id=TUBI)          # any counting app counts as a re-open
    assert tv.closed == [YT, TUBI]
    clock.advance(minutes=1)
    await tv.set(app_id=YT)
    assert tv.closed == [YT, TUBI, YT]  # natural expiry no longer counts: 2 violations so far
    clock.advance(minutes=1)
    await tv.set(app_id=TUBI)
    assert tv.powered_off == 1
    assert sink.has("已关机")


async def test_violation_window_survives_restart(env, tmp_path):
    mon, tv, sink, clock, store, s = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    await tv.set(app_id=TUBI)
    await tv.set(app_id=YT)
    mon2 = Monitor(tv, store, sink, s, now=clock)  # daemon restart
    await tv.set(connected=False)
    await tv.set(app_id=TUBI)
    assert tv.powered_off == 1
    assert mon2 is not None


async def test_parent_add_rearms_and_allows_more(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    assert tv.closed == [YT]
    reply = await mon.handle(Command(PARENT, PARENT, "add", "10"))
    assert "已加 10 分钟" in reply.text
    await tv.set(app_id=YT)
    await watch(mon, clock, 5.5)
    assert tv.closed == [YT]           # not closed again yet
    assert sum("还剩 5 分钟" in m for m in sink.msgs) == 2
    await watch(mon, clock, 5)
    assert tv.closed == [YT, YT]


async def test_add_rejects_out_of_range(env):
    mon, *_ = env
    assert "范围" in (await mon.handle(Command(PARENT, PARENT, "add", "999"))).text
    assert "用法" in (await mon.handle(Command(PARENT, PARENT, "add", "abc"))).text


async def test_lock_persists_and_enforces_immediately(env):
    mon, tv, sink, clock, store, s = env
    await tv.set(app_id=YT)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    assert tv.closed == [YT]
    mon2 = Monitor(tv, store, sink, s, now=clock)
    assert mon2.locked
    await mon2.handle(Command(PARENT, PARENT, "unlock", ""))
    assert not mon.locked


async def test_curfew_enforces(env):
    mon, tv, sink, clock, *_ = env
    clock.t = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 21:00 local
    await tv.set(app_id=YT)
    assert tv.closed == [YT]
    assert sink.has("休息时段")


async def test_heartbeat_gap_not_counted(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 2)
    clock.advance(minutes=30)          # server suspended / stalled
    await mon.tick()
    await watch(mon, clock, 1)
    assert used_min(mon, clock) == pytest.approx(3)


async def test_failed_probe_reconnects_and_stops_counting(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 1)
    tv.refresh_fails = True
    clock.advance(seconds=10)
    await mon.tick()
    assert not tv.state.connected
    assert sink.has("电视连接中断")
    await watch(mon, clock, 5)
    assert used_min(mon, clock) == pytest.approx(1, abs=0.2)


async def test_midnight_resets_quota(env):
    mon, tv, sink, clock, *_ = env
    clock.t = datetime(2026, 11, 18, 13, 50, tzinfo=timezone.utc)  # 23:50 local
    mon.s = replace(mon.s, curfew_enabled=False)
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)        # crosses midnight
    day, used, allowance = mon.usage(clock())
    assert str(day) == "2026-11-19"
    assert used / 60 == pytest.approx(10)
    assert tv.closed == []


async def test_daily_summary_once(env):
    mon, tv, sink, clock, *_ = env
    clock.t = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)  # 20:00 local
    await mon.tick()
    await mon.tick()
    assert sum("每日汇总" in m for m in sink.msgs) == 1


async def test_status_text(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 2)
    r = await mon.handle(Command(PARENT, PARENT, "status", ""))
    assert "已用 2 / 20 分钟" in r.text and "YouTube" in r.text and "执行" in r.text
    assert r.buttons


async def test_reason_change_in_same_session_is_new_violation(env):
    mon, tv, sink, clock, store, s = env
    store.set_kv("mode", "observe")
    clock.t = datetime(2026, 11, 18, 20, 50, tzinfo=timezone.utc)  # 06:50 local, curfew
    await tv.set(app_id=YT)
    assert sink.has("休息时段")
    await watch(mon, clock, 31)        # curfew ends 07:00, quota (20 min) runs out ~07:20
    assert sink.has("时间已用完")


async def test_failed_power_off_is_retried_not_downgraded(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    clock.advance(minutes=1)
    await tv.set(app_id=TUBI)
    clock.advance(minutes=1)
    await tv.set(app_id=YT)
    tv.power_off_fails = True
    clock.advance(minutes=1)
    await tv.set(app_id=TUBI)          # 3rd violation -> power_off, which fails
    assert tv.powered_off == 1
    closed_before = len(tv.closed)
    tv.power_off_fails = False
    clock.advance(seconds=10)
    await mon.tick()                   # same session still in front: retry power_off
    assert tv.powered_off == 2
    assert len(tv.closed) == closed_before


async def test_queued_close_dropped_if_parent_adds_time_meanwhile(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 19.9)
    orig_toast = tv.show_toast
    replies = []

    async def toast_then_parent_adds(text):
        await orig_toast(text)
        if text.startswith("⛔"):
            replies.append((await mon.handle(Command(PARENT, PARENT, "add", "10"))).text)

    tv.show_toast = toast_then_parent_adds
    await watch(mon, clock, 0.2)       # quota runs out -> toast, then close queued
    assert replies and "已加 10 分钟" in replies[0]
    assert tv.closed == []
    assert not sink.has("已退出")      # never claim an action that did not happen


async def test_stale_power_off_dropped_and_not_reported(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    clock.advance(minutes=1)
    await tv.set(app_id=TUBI)
    clock.advance(minutes=1)
    orig_execute = mon._execute

    async def parent_adds_before_execution(actions):
        if actions:
            await mon.handle(Command(PARENT, PARENT, "add", "30"))
        await orig_execute(actions)

    mon._execute = parent_adds_before_execution
    await tv.set(app_id=YT)            # 3rd violation decided -> parent adds -> stale
    assert tv.powered_off == 0
    assert not sink.has("已关机")


async def test_failed_close_reports_failure_not_success(env):
    mon, tv, sink, clock, *_ = env

    async def broken_close(app_id):
        raise ConnectionError("tv said no")

    tv.close_app = broken_close
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)
    assert not sink.has("已退出")
    assert sink.has("close_app 失败")


async def test_switch_to_enforce_acts_now_with_toast_and_report(env):
    mon, tv, sink, clock, store, s = env
    store.set_kv("mode", "observe")
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert sink.has("观察模式，未执行") and tv.closed == []
    sink.msgs.clear()
    reply = await mon.handle(Command(PARENT, PARENT, "mode", "enforce"))
    assert "执行" in reply.text
    assert tv.closed == [YT]
    assert "⛔ 时间已用完" in tv.toasts or any(t.startswith("⛔") for t in tv.toasts)
    assert sink.has("⛔ 今日娱乐时间已用完，已退出 YouTube")
    assert not sink.has("第 0 次")
    assert store.count_events("violation", clock() - timedelta(hours=1)) == 0


async def test_on_command_wakes_offline_tv(env):
    mon, tv, sink, clock, *_ = env
    r = await mon.handle(Command(PARENT, PARENT, "on", ""))
    assert tv.powered_on == 1 and "开机信号" in r.text
    assert any(data == "on" for row in mon._buttons() for _, data in row)


async def test_on_command_noop_when_tv_already_on(env):
    mon, tv, *_ = env
    await tv.set(app_id=HOME)
    r = await mon.handle(Command(PARENT, PARENT, "on", ""))
    assert tv.powered_on == 0 and "已经开着" in r.text
    assert any(data == "off" for row in mon._buttons() for _, data in row)


async def test_on_command_reports_failure(env):
    mon, tv, sink, clock, store, s = env
    tv.power_on_error = ValueError("LG_TV_MAC not set in .env")
    r = await mon.handle(Command(PARENT, PARENT, "on", ""))
    assert "失败" in r.text and "LG_TV_MAC" in r.text
    assert store.count_events("action_failed", clock() - timedelta(hours=1)) == 1


async def test_power_button_offers_on_in_standby(env):
    mon, tv, *_ = env
    await tv.set(connected=True, power_on=False)
    assert any(data == "on" for row in mon._buttons() for _, data in row)


async def test_business_day_matches_usage_across_midnight(env):
    mon, tv, sink, clock, *_ = env
    before = datetime(2026, 11, 18, 13, 59, 59, tzinfo=timezone.utc)  # 23:59:59 local
    after = before + timedelta(seconds=1)  # 00:00:00 next day
    assert mon.business_day(before) == mon.usage(before)[0]
    assert mon.business_day(after) == mon.usage(after)[0]
    assert mon.business_day(after) - mon.business_day(before) == timedelta(days=1)


async def test_natural_expiry_is_not_a_violation(env):
    mon, tv, sink, clock, store, s = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert tv.closed == [YT]
    for i in range(3):
        await mon.handle(Command(PARENT, PARENT, "add", "1"))
        await tv.set(app_id=YT)
        await watch(mon, clock, 2)
        assert tv.closed == [YT] * (i + 2)
    assert tv.powered_off == 0
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == 0


async def test_reopen_after_expiry_counts(env):
    mon, tv, sink, clock, store, s = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    for _ in range(3):
        await tv.set(app_id=HOME)
        clock.advance(seconds=10)
        await tv.set(app_id=YT)
    assert tv.powered_off == 1
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == 3


QUIZ = "ltp.quiz"


def _with_quiz(env):
    mon, tv, sink, clock, store, s = env
    mon.s = replace(s, app_rules={**s.app_rules, QUIZ: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)})
    mon.quiz_app_id = QUIZ
    return mon, tv, sink, clock, store


async def test_quota_close_then_launches_quiz(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert tv.closed == [YT]
    assert tv.launched == [QUIZ]
    assert tv.powered_off == 0


async def test_launch_failure_notifies_once_per_day(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    tv.launch_fails = True
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    await mon.handle(Command(PARENT, PARENT, "add", "1"))
    await tv.set(app_id=YT)
    await watch(mon, clock, 2)
    assert tv.closed == [YT, YT]
    assert tv.launched == []
    assert sum("答题 App 打不开" in m for m in sink.msgs) == 1


async def test_power_off_does_not_launch_quiz(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    for _ in range(3):
        await tv.set(app_id=HOME)
        clock.advance(seconds=10)
        await tv.set(app_id=YT)
    assert tv.powered_off == 1
    # the natural-expiry close and the two violation closes hand over; the power-off does not
    assert tv.launched == [QUIZ] * 3
    assert len(tv.closed) == 3


async def test_observe_mode_does_not_launch_quiz(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    store.set_kv("mode", "observe")
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert tv.closed == [] and tv.launched == []


async def test_lock_close_does_not_launch_quiz(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    assert tv.closed == [YT]
    assert tv.launched == []


async def test_quiz_app_closed_during_lock(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    await tv.set(app_id=QUIZ)
    assert tv.closed == [QUIZ]
    assert tv.launched == []


async def test_transition_empty_home_quiz_not_counted(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    closed, launched = list(tv.closed), list(tv.launched)
    used = used_min(mon, clock)
    for app in ("", HOME, QUIZ):
        clock.advance(seconds=10)
        await tv.set(app_id=app)
    await watch(mon, clock, 2)
    assert tv.closed == closed and tv.launched == launched
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == 0
    assert used_min(mon, clock) == pytest.approx(used)


async def test_warn_toast_mentions_quiz_only_when_available(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await watch(mon, clock, 16)
    assert any(t.endswith(" · Open Quiz to earn more time") for t in tv.toasts)


# -- quiz_txn ---------------------------------------------------------------
async def test_award_partial_when_near_cap(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    day = mon.business_day(clock())
    store.add_credit(day, 35, "quiz", "seed", clock(), ref="quiz:seed")
    got = await mon.quiz_txn(lambda ctx: ctx.award("a1", 10, "n"))
    assert got == pytest.approx(5)
    assert store.quiz_credit_minutes(day) == pytest.approx(40)


async def test_award_zero_writes_no_credit(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    day = mon.business_day(clock())
    store.add_credit(day, 40, "quiz", "seed", clock(), ref="quiz:seed")
    got = await mon.quiz_txn(lambda ctx: ctx.award("a1", 10, "n"))
    assert got == 0
    assert len(store.credits(day)) == 1


async def test_txn_rollback_discards_credit_and_notice(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    day = mon.business_day(clock())

    def fn(ctx):
        ctx.award("a1", 10, "n")
        ctx.notify("hello")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await mon.quiz_txn(fn)
    assert store.quiz_credit_minutes(day) == 0
    assert not sink.has("hello")
    await mon.quiz_txn(lambda ctx: ctx.award("a1", 10, "n"))  # lock released, store usable


async def test_notify_sent_after_commit(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await mon.quiz_txn(lambda ctx: ctx.notify("done"))
    assert sink.has("done")


async def test_award_ends_quota_enforcement(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    store.set_kv("mode", "observe")  # YT stays in front so the enforcement stays active
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    assert mon._enforced is not None
    await mon.quiz_txn(lambda ctx: ctx.award("a1", 5, "n"))
    assert mon._enforced is None
    store.set_kv("mode", "enforce")
    await watch(mon, clock, 1)
    assert tv.closed == []


async def test_award_does_not_end_lock(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    blocked = await mon.quiz_txn(lambda ctx: ctx.blocked)
    assert blocked == "locked"
    with pytest.raises(RuntimeError):
        await mon.quiz_txn(lambda ctx: ctx.award("a1", 5, "n"))


async def test_blocked_curfew(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    clock.t = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)  # 23:00 local
    assert await mon.quiz_txn(lambda ctx: ctx.blocked) == "curfew"


async def test_award_while_quiz_in_front_no_close(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    await tv.set(app_id=QUIZ)
    closed = list(tv.closed)
    await mon.quiz_txn(lambda ctx: ctx.award("a1", 5, "n"))
    await watch(mon, clock, 1)
    assert tv.closed == closed


async def test_reads_under_lock_do_not_repeat_close(env):
    mon, tv, sink, clock, store = _with_quiz(env)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    real_close = tv.close_app

    async def stubborn_close(app_id):  # TV ignores the close: app stays in front
        tv.closed.append(app_id)

    tv.close_app = stubborn_close
    await tv.set(app_id=QUIZ)
    assert tv.closed == [QUIZ]
    for _ in range(5):
        await mon.quiz_txn(lambda ctx: None)  # a pure status read
    assert tv.closed == [QUIZ]
    tv.close_app = real_close
    clock.advance(seconds=10)
    await mon.tick()  # the regular tick still retries
    assert tv.closed == [QUIZ, QUIZ]



async def test_disable_quiz_revokes_exemption(env):
    mon, tv, sink, clock, store, s = env
    mon.s = replace(s, app_rules={**s.app_rules, QUIZ: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)})
    mon.enable_quiz(QUIZ, None)
    await mon.disable_quiz()
    assert mon.quiz_app_id is None
    assert QUIZ not in mon.s.app_rules
    assert mon.s.app_rules.keys() == s.app_rules.keys()


async def test_disable_quiz_restores_users_own_rule(env):
    mon, tv, sink, clock, store, s = env
    own = AppRule(True, AllowedWhen.RESTRICTED)
    mon.s = replace(s, app_rules={**s.app_rules, QUIZ: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)})
    mon.enable_quiz(QUIZ, own)
    await mon.disable_quiz()
    assert mon.s.app_rules[QUIZ] == own
    await mon.disable_quiz()  # idempotent
    assert mon.s.app_rules[QUIZ] == own


async def test_quiz_app_blocked_again_after_disable(env):
    mon, tv, sink, clock, store, s = env
    mon.s = replace(s, app_rules={**s.app_rules, QUIZ: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)})
    mon.enable_quiz(QUIZ, None)
    await mon.handle(Command(PARENT, PARENT, "lock", ""))
    await mon.disable_quiz()
    await tv.set(app_id=QUIZ)
    assert tv.closed == [QUIZ]  # restricted like any unknown app while locked


async def _exhausted_session_in_front(env):
    """Quota used up, then YouTube is (re)opened and the TV ignores the close, so the
    same session stays in front. Returns the violation count recorded for its start."""
    mon, tv, sink, clock, store, s = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)
    await tv.set(app_id=HOME)
    clock.advance(seconds=10)

    async def stubborn_close(app_id):
        tv.closed.append(app_id)

    tv.close_app = stubborn_close
    await tv.set(app_id=YT)
    assert mon._session_had_quota is False
    return store.count_events("violation", clock() - timedelta(minutes=60))


async def test_grant_via_add_makes_later_expiry_natural(env):
    mon, tv, sink, clock, store, s = env
    base = await _exhausted_session_in_front(env)
    assert base == 1
    await mon.handle(Command(PARENT, PARENT, "add", "2"))
    await watch(mon, clock, 3)
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == base


async def test_grant_via_quiz_award_makes_later_expiry_natural(env):
    mon, tv, sink, clock, store, s = env
    base = await _exhausted_session_in_front(env)
    await mon.quiz_txn(lambda ctx: ctx.award("a1", 2, "n"))
    await watch(mon, clock, 3)
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == base


async def test_exhausted_session_without_grant_still_violates_only_once(env):
    mon, tv, sink, clock, store, s = env
    base = await _exhausted_session_in_front(env)
    await watch(mon, clock, 3)
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == base == 1


async def test_restricted_non_counting_app_enforced_when_exhausted_without_counting(env):
    mon, tv, sink, clock, store, s = env
    FREE = "free.restricted"
    mon.s = replace(s, app_rules={**s.app_rules, FREE: AppRule(False, AllowedWhen.RESTRICTED)})
    await tv.set(app_id=YT)
    await watch(mon, clock, 21)  # shared quota used up on a counted app
    await tv.set(app_id=HOME)
    clock.advance(seconds=10)

    async def stubborn_close(app_id):
        tv.closed.append(app_id)

    tv.close_app = stubborn_close
    before = mon.usage(clock())[1]
    await tv.set(app_id=FREE)
    assert tv.closed[-1] == FREE  # exhausted quota enforced on a RESTRICTED non-counting app
    base = store.count_events("violation", clock() - timedelta(minutes=60))
    assert base == 1 and mon._session_had_quota is False
    await watch(mon, clock, 1)
    assert mon.usage(clock())[1] == before  # the session itself never counts
    await mon.handle(Command(PARENT, PARENT, "add", "2"))
    await watch(mon, clock, 3)  # grant expires naturally: no new violation
    assert store.count_events("violation", clock() - timedelta(minutes=60)) == base
    assert mon.usage(clock())[1] == before


class _StubQuiz:
    def daily_digest(self, day):
        return "QUIZ-DIGEST"

    def today_lines(self, day):
        return ["quiz line"]


async def test_disable_quiz_turns_off_command_and_digest(env):
    mon, tv, sink, clock, store, s = env
    mon.enable_quiz(QUIZ, None, _StubQuiz())
    assert mon.quiz is not None
    await mon.disable_quiz()
    assert mon.quiz is None
    r = await mon.handle(Command(PARENT, PARENT, "quiz", ""))
    assert r.text == "答题功能未启用"
    clock.t = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)  # 20:00 local
    await mon.tick()
    assert sink.has("每日汇总") and not sink.has("QUIZ-DIGEST")


async def test_enabled_quiz_digest_present(env):
    mon, tv, sink, clock, store, s = env
    mon.enable_quiz(QUIZ, None, _StubQuiz())
    clock.t = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    await mon.tick()
    assert sink.has("QUIZ-DIGEST")


def _monitor(tmp_path, yaml: str):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "settings.yaml").write_text(yaml, encoding="utf-8")
    s = load_settings(cfg / "settings.yaml", {})
    clock, tv, sink = Clock(), FakeTV({YT: "YouTube"}), Sink()
    store = Store(tmp_path / "ltp.db")
    return Monitor(tv, store, sink, s, now=clock), tv, sink, clock, store


async def _reopen_after_power_off(tv, clock, app_id, seconds=30):
    await tv.set(connected=False)
    clock.advance(seconds=seconds)
    await tv.set(app_id=app_id)


CURFEW_YAML = ("mode: enforce\ntimezone: Etc/GMT-10\n"
               "enforcement:\n  curfew_power_off: true\n")


async def test_curfew_power_off_on_first_open(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)  # 21:00 local
    await tv.set(app_id=YT)
    assert tv.powered_off == 1 and tv.closed == []
    assert tv.toasts and tv.toasts[-1] == CURFEW_TOAST and ":" not in tv.toasts[-1]
    assert sink.has("休息时段，打开了 YouTube，已关机")


async def test_curfew_power_off_is_opt_in(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, "mode: enforce\ntimezone: Etc/GMT-10\n")
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    assert tv.powered_off == 0 and tv.closed == [YT]


async def test_repeat_violation_messages_are_merged(tmp_path):
    mon, tv, sink, clock, store = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    for _ in range(5):
        await _reopen_after_power_off(tv, clock, YT)
    assert tv.powered_off == 6  # every open is still enforced
    assert store.count_events("violation", clock() - timedelta(hours=1)) == 6
    assert sum("已关机" in m for m in sink.msgs) == 1
    await tv.set(connected=False)
    await watch(mon, clock, 9)
    assert not sink.has("重复打开")  # run still within the window
    await watch(mon, clock, 2)
    assert sum("又重复打开了 5 次，其中 5 次关机" in m for m in sink.msgs) == 1
    await watch(mon, clock, 15)
    assert sum("重复打开" in m for m in sink.msgs) == 1  # summary sent once


async def test_escalation_to_power_off_is_still_reported(env):
    mon, tv, sink, clock, *_ = env
    await tv.set(app_id=YT)
    await watch(mon, clock, 20)            # natural expiry: not part of a run
    for app in (TUBI, YT, TUBI, YT):       # violations 1..4
        clock.advance(minutes=1)
        await tv.set(app_id=app)
        if tv.powered_off:
            await _reopen_after_power_off(tv, clock, HOME, seconds=1)
    assert sink.has("已退出 Tubi") or sink.has("已退出 com.tubitv.ott")
    assert not sink.has("第 2 次打开")      # second close merged
    assert sum("已关机" in m for m in sink.msgs) == 1  # first power-off reported, second merged


async def test_new_run_after_quiet_window_reports_again(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    await _reopen_after_power_off(tv, clock, YT)
    await tv.set(connected=False)
    clock.advance(minutes=30)              # no tick in between: flushed by the next violation
    await tv.set(app_id=YT)
    assert sink.has("又重复打开了 1 次")
    assert sum("已关机" in m and "又重复" not in m for m in sink.msgs) == 2


async def test_curfew_power_off_applies_while_also_locked(tmp_path):
    mon, tv, sink, clock, store = _monitor(tmp_path, CURFEW_YAML)
    store.set_kv("locked", "1")
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    assert tv.powered_off == 1 and tv.closed == []


async def test_failed_quiet_power_off_not_counted_in_summary(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    real = tv.power_off

    async def broken():
        raise ConnectionError("boom")
    tv.power_off = broken
    await _reopen_after_power_off(tv, clock, YT)
    tv.power_off = real
    await _reopen_after_power_off(tv, clock, YT)
    await tv.set(connected=False)
    await watch(mon, clock, 11)
    assert sink.has("又重复打开了 2 次，其中 1 次关机")


async def test_retried_quiet_power_off_counted_once(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)
    await tv.set(app_id=YT)
    real = tv.power_off

    async def broken():
        raise ConnectionError("boom")
    tv.power_off = broken
    await _reopen_after_power_off(tv, clock, YT)   # merged power-off fails, TV stays on
    tv.power_off = real
    clock.advance(seconds=10)
    await mon.tick()                               # retry succeeds
    assert tv.powered_off == 2
    await tv.set(connected=False)
    await watch(mon, clock, 11)
    assert sink.has("又重复打开了 1 次，其中 1 次关机")


async def test_adult_mode_does_not_count_or_enforce(tmp_path):
    mon, tv, sink, clock, store = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)                      # 21:00 local: curfew
    reply = await mon.handle(Command(PARENT, PARENT, "adult", ""))
    assert "成人模式已开启" in reply.text
    await tv.set(app_id=YT)
    await watch(mon, clock, 30)
    assert tv.powered_off == 0 and tv.closed == [] and tv.toasts == []
    assert used_min(mon, clock) == 0
    assert store.count_events("violation", clock() - timedelta(hours=1)) == 0


async def test_adult_mode_ends_when_tv_turns_off(tmp_path):
    mon, tv, sink, clock, store = _monitor(tmp_path, CURFEW_YAML)
    clock.advance(hours=11)
    await mon.handle(Command(PARENT, PARENT, "adult", ""))
    await tv.set(app_id=YT)
    await tv.set(connected=False)
    assert mon.adult_until() is None and sink.has("电视已关，已回到孩子模式")
    clock.advance(minutes=1)
    await tv.set(app_id=YT)                      # next power-on is kid mode again
    assert tv.powered_off == 1


async def test_adult_mode_times_out_and_counting_resumes(env):
    mon, tv, sink, clock, store, s = env
    await mon.handle(Command(PARENT, PARENT, "adult", "10"))
    await tv.set(app_id=YT)
    await watch(mon, clock, 15)
    assert sink.has("成人模式到时间了")
    assert used_min(mon, clock) == pytest.approx(5, abs=0.3)


async def test_kid_command_switches_back_mid_session(env):
    mon, tv, sink, clock, store, s = env
    await mon.handle(Command(PARENT, PARENT, "adult", ""))
    await tv.set(app_id=YT)
    await watch(mon, clock, 5)
    reply = await mon.handle(Command(PARENT, PARENT, "kid", ""))
    assert "已回到孩子模式" in reply.text
    await watch(mon, clock, 3)
    assert used_min(mon, clock) == pytest.approx(3, abs=0.3)
    assert "用法" in (await mon.handle(Command(PARENT, PARENT, "adult", "abc"))).text
    assert "1–480" in (await mon.handle(Command(PARENT, PARENT, "adult", "999"))).text


async def test_quiz_app_during_curfew_gets_the_same_notice_and_power_off(tmp_path):
    mon, tv, sink, clock, _ = _monitor(tmp_path, CURFEW_YAML)
    mon.s = replace(mon.s, app_rules={**mon.s.app_rules,
                                      QUIZ: AppRule(False, AllowedWhen.QUOTA_EXHAUSTED)})
    clock.advance(hours=11)  # 21:00 local
    await tv.set(app_id=QUIZ)
    assert tv.powered_off == 1 and tv.toasts[-1] == CURFEW_TOAST
