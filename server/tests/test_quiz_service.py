import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from ltp.notify import Command
from ltp.quiz.bank import QuizBank
from ltp.quiz.service import (QuizRejected, QuizService, QuizUnavailable, kv_seed_provider,
                              pick_challenge_index)
from quiz_fixtures import BANK_DIR, DEV, Clock, Env, Sink  # noqa: F401


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def numeric_regular(env):
    """Issue regular attempts (skipping choice ones) until a numeric one is current."""
    for _ in range(10):
        v = await env.svc.current(DEV, "regular")
        if v.answer_type != "choice":
            return v
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    raise AssertionError("no numeric regular question")


async def choice_regular(env):
    for _ in range(10):
        v = await env.svc.current(DEV, "regular")
        if v.answer_type == "choice":
            return v
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    raise AssertionError("no choice regular question")


# -- issue / re-enter -------------------------------------------------------

async def test_reenter_returns_same_attempt(env):
    v1 = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=30)
    v2 = await env.svc.current(DEV, "regular")
    assert v1.id == v2.id and v1.prompt == v2.prompt and v1.options == v2.options
    assert v1.state == "issued" and v1.explanation is None and v1.correct_answer is None


async def test_restart_keeps_attempt_and_cooldown(env):
    v = await numeric_regular(env)
    await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    env.restart()
    v2 = await env.svc.current(DEV, "regular")
    assert v2.id == v.id and v2.tries_left == 2
    env.clock.advance(seconds=10)
    with pytest.raises(QuizRejected) as e:
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert e.value.code == "cooldown" and 0 < e.value.retry_after_s <= 60


async def test_options_shuffled_and_stable(env):
    v = await choice_regular(env)
    a = env.store.get_attempt(v.id)
    assert [o["id"] for o in v.options] == a.option_order
    assert sorted(a.option_order) == sorted(o["id"] for o in a.snapshot["options"])
    assert (await env.svc.current(DEV, "regular")).options == v.options


# -- rewards ------------------------------------------------------------------

async def test_correct_after_60s_credits_10(env):
    v = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=60)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.state == "correct" and r.outcome == "credited"
    assert r.credited_minutes == 10 and r.nominal_minutes == 10
    assert [c.minutes for c in env.quiz_credits()] == [10]
    assert env.sink.has("✅ 答对 1 题 +10 分钟（今日 1 题，已赚 10 分钟）")


async def test_correct_before_60s_is_too_fast(env):
    v = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=59)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.state == "correct" and r.outcome == "too_fast"
    assert r.credited_minutes == 0 and r.nominal_minutes == 10
    assert env.quiz_credits() == []
    assert env.sink.has("太快")


async def test_challenge_min_180s(env):
    v = await env.svc.current(DEV, "challenge")
    env.clock.advance(seconds=179)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "too_fast" and env.quiz_credits() == []
    assert env.sink.has("🏆 今日挑战完成")
    env.clock.advance(days=1)
    v = await env.svc.current(DEV, "challenge")
    env.clock.advance(seconds=180)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "credited" and r.credited_minutes == 25


async def test_cap_partial_and_full(env):
    day = env.clock().astimezone(env.settings.timezone).date()
    env.store.add_credit(day, 35, "quiz", "seed", env.clock(), ref="seed")
    v = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=60)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "cap" and r.credited_minutes == 5 and r.nominal_minutes == 10
    assert env.sink.has("上限")
    v = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=60)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "cap_full" and r.credited_minutes == 0


# -- wrong answers -------------------------------------------------------------

async def test_numeric_three_wrong_exhausts(env):
    v = await numeric_regular(env)
    for i in range(3):
        r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
        env.clock.advance(seconds=61)
    assert r.state == "exhausted" and r.tries_left == 0
    assert r.explanation and r.correct_answer == env.answer(v)
    a = env.store.get_attempt(v.id)
    assert a.wrong_count == 3
    # terminal: re-submit is a no-op
    r2 = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r2.state == "exhausted" and env.quiz_credits() == []
    assert (await env.svc.current(DEV, "regular")).id != v.id


async def test_cooldown_rejects_without_counting(env):
    v = await numeric_regular(env)
    r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    assert r.tries_left == 2
    env.clock.advance(seconds=30)
    with pytest.raises(QuizRejected) as e:
        await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    assert e.value.code == "cooldown" and e.value.retry_after_s == 30
    assert env.store.get_attempt(v.id).wrong_count == 1
    env.clock.advance(seconds=30)
    r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    assert r.tries_left == 1


async def test_bad_input_not_counted(env):
    v = await numeric_regular(env)
    for bad in ("", "abc", "１２", "9" * 65):
        with pytest.raises(QuizRejected) as e:
            await env.svc.submit(DEV, v.id, bad, env.rev(v.id))
        assert e.value.code == "bad_input"
    assert env.store.get_attempt(v.id).wrong_count == 0
    c = await choice_regular(env)
    with pytest.raises(QuizRejected) as e:
        await env.svc.submit(DEV, c.id, "zz", env.rev(c.id))
    assert e.value.code == "bad_input"
    assert env.store.get_attempt(c.id).reward_lost == 0


async def test_choice_wrong_loses_reward_but_can_continue(env):
    v = await choice_regular(env)
    r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    assert r.state == "issued" and r.tries_left is None and r.explanation is None
    assert env.store.get_attempt(v.id).reward_lost == 1
    r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))  # no cooldown for choice
    env.clock.advance(seconds=60)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.state == "correct" and r.outcome == "lost" and r.credited_minutes == 0
    assert env.quiz_credits() == [] and env.sink.has("选择题答错过")


# -- hints ---------------------------------------------------------------------

async def test_hint_halves_reward(env):
    v = await env.svc.current(DEV, "regular")
    h = await env.svc.hint(DEV, v.id, env.rev(v.id))
    assert h.hints == [env.store.get_attempt(v.id).snapshot["hints"][0]]
    env.clock.advance(seconds=60)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "hint" and r.credited_minutes == 5 and r.nominal_minutes == 5
    assert env.sink.has("（用了提示，奖励减半）")
    c = await env.svc.current(DEV, "challenge")
    await env.svc.hint(DEV, c.id, env.rev(c.id))
    await env.svc.hint(DEV, c.id, env.rev(c.id))
    env.clock.advance(seconds=180)
    r = await env.svc.submit(DEV, c.id, env.answer(c), env.rev(c.id))
    assert r.credited_minutes == 12 and r.nominal_minutes == 12


async def test_hint_repeat_not_double_counted(env):
    v = await env.svc.current(DEV, "regular")
    total = v.hints_total
    for _ in range(total + 3):
        h = await env.svc.hint(DEV, v.id, env.rev(v.id))
    assert len(h.hints) == total
    assert env.store.get_attempt(v.id).hints_used == total
    assert h.hints == env.store.get_attempt(v.id).snapshot["hints"]


async def test_explanation_only_when_terminal(env):
    v = await env.svc.current(DEV, "regular")
    assert v.explanation is None and v.correct_answer is None
    r = await env.svc.result(DEV, v.id)
    assert r.explanation is None
    s = await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert s.state == "skipped" and s.explanation and s.correct_answer == env.answer(v)


# -- skips / challenge ---------------------------------------------------------

async def test_fourth_skip_rejected(env):
    for i in range(3):
        v = await env.svc.current(DEV, "regular")
        r = await env.svc.skip(DEV, v.id, env.rev(v.id))
        assert r.skips_left == 2 - i
    v = await env.svc.current(DEV, "regular")
    with pytest.raises(QuizRejected) as e:
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert e.value.code == "skip_limit"
    assert env.store.get_attempt(v.id).state == "issued"


async def test_skip_of_lost_choice_is_free(env):
    for _ in range(3):
        v = await env.svc.current(DEV, "regular")
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    v = await choice_regular_no_skip(env)
    await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    r = await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert r.state == "skipped" and r.skips_left == 0


async def choice_regular_no_skip(env):
    """Answer numeric questions correctly until a choice question is current."""
    for _ in range(10):
        v = await env.svc.current(DEV, "regular")
        if v.answer_type == "choice":
            return v
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    raise AssertionError("no choice question")


async def test_challenge_cannot_skip(env):
    v = await env.svc.current(DEV, "challenge")
    with pytest.raises(QuizRejected) as e:
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert e.value.code == "no_skip_challenge"


async def test_one_challenge_per_day(env):
    v = await env.svc.current(DEV, "challenge")
    env.clock.advance(seconds=200)
    await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    again = await env.svc.current(DEV, "challenge")
    assert again.id == v.id and again.state == "correct"
    assert len(env.quiz_credits()) == 1


async def test_regular_and_challenge_coexist(env):
    r = await env.svc.current(DEV, "regular")
    c = await env.svc.current(DEV, "challenge")
    assert r.id != c.id and r.tier == "regular" and c.tier == "challenge"
    assert (await env.svc.current(DEV, "regular")).id == r.id
    assert (await env.svc.current(DEV, "challenge")).id == c.id


async def test_other_device_cannot_see_attempt(env):
    v = await env.svc.current(DEV, "regular")
    for call in (env.svc.result(DEV + "x", v.id), env.svc.hint(DEV + "x", v.id, env.rev(v.id)),
                 env.svc.submit(DEV + "x", v.id, "1", env.rev(v.id)), env.svc.skip(DEV + "x", v.id, env.rev(v.id))):
        with pytest.raises(QuizRejected) as e:
            await call
        assert e.value.code == "not_found"


# -- lock / curfew / pause -------------------------------------------------------

async def test_locked_rejects_all_writes_without_change(env):
    v = await numeric_regular(env)
    wrong = env.wrong(v)
    env.store.set_kv("locked", "1")
    n_before = env.store.count_attempts(DEV, env.store.get_attempt(v.id).day)
    before = env.store.get_attempt(v.id)
    for call in (env.svc.current(DEV, "regular"), env.svc.current(DEV, "challenge"),
                 env.svc.hint(DEV, v.id, env.rev(v.id)), env.svc.submit(DEV, v.id, wrong, env.rev(v.id)),
                 env.svc.skip(DEV, v.id, env.rev(v.id))):
        with pytest.raises(QuizUnavailable) as e:
            await call
        assert e.value.reason == "locked"
    assert env.store.get_attempt(v.id) == before
    assert env.store.count_attempts(DEV, before.day) == n_before
    st = await env.svc.status(DEV)
    assert st.available is False and st.reason == "locked"
    assert (await env.svc.result(DEV, v.id)).id == v.id


async def test_curfew_rejects(env):
    v = await env.svc.current(DEV, "regular")
    before = env.store.get_attempt(v.id)
    env.clock.t = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 21:00 local
    for call in (env.svc.current(DEV, "regular"), env.svc.current(DEV, "challenge"),
                 env.svc.hint(DEV, v.id, env.rev(v.id)), env.svc.submit(DEV, v.id, "1", env.rev(v.id)),
                 env.svc.skip(DEV, v.id, env.rev(v.id))):
        with pytest.raises(QuizUnavailable) as e:
            await call
        assert e.value.reason == "curfew"
    after = env.store.get_attempt(v.id)
    assert after == before and after.wrong_count == 0 and after.hints_used == 0
    assert env.store.count_attempts(DEV, before.day) == 1
    assert (await env.svc.status(DEV)).reason == "curfew"


async def test_disabled_service_unavailable(env):
    env.svc = QuizService(env.bank, env.mon, env.store, replace(env.qcfg, enabled=False),
                          random.Random(1), lambda: b"s" * 32)
    with pytest.raises(QuizUnavailable) as e:
        await env.svc.current(DEV, "regular")
    assert e.value.reason == "disabled"


async def test_ten_wrong_pauses_15_min(env):
    day = env.clock().astimezone(env.settings.timezone).date()
    while env.store.wrong_total(DEV, day) < 10:
        v = await env.svc.current(DEV, "regular")
        await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
        env.clock.advance(seconds=61)
    with pytest.raises(QuizUnavailable) as e:
        await env.svc.current(DEV, "regular")
    assert e.value.reason == "paused" and e.value.retry_after_s == 15 * 60 - 61
    assert (await env.svc.status(DEV)).reason == "paused"
    env.clock.advance(seconds=15 * 60 - 61)
    assert (await env.svc.current(DEV, "regular")).state == "issued"


@pytest.fixture
def nenv(tmp_path):
    """Numeric-only in-memory bank, so pause tests do not depend on the production bank."""
    from test_quiz_passage import make_env, numeric
    return make_env(tmp_path, [numeric("n1"), numeric("n2")])


async def pause_by_wrongs(env):
    """Submit wrong answers until the daily pause triggers; return the last (view, rev)."""
    day = env.clock().astimezone(env.settings.timezone).date()
    while True:
        v = await env.svc.current(DEV, "regular")
        rev = env.rev(v.id)
        await env.svc.submit(DEV, v.id, env.wrong(v), rev)
        if env.store.wrong_total(DEV, day) >= env.qcfg.wrong_pause_threshold:
            return v, rev
        env.clock.advance(seconds=61)


async def test_status_reports_pause_countdown(nenv):
    env = nenv
    st = await env.svc.status(DEV)
    assert st.available and st.retry_after_s is None
    await pause_by_wrongs(env)
    env.clock.advance(seconds=100)
    st = await env.svc.status(DEV)
    assert st.reason == "paused" and st.retry_after_s == env.qcfg.wrong_pause_minutes * 60 - 100
    env.clock.advance(seconds=st.retry_after_s)
    st = await env.svc.status(DEV)
    assert st.available and st.retry_after_s is None


async def test_status_lock_has_no_countdown(env):
    env.store.set_kv("locked", "1")
    st = await env.svc.status(DEV)
    assert st.reason == "locked" and st.retry_after_s is None


async def test_stale_rev_retry_of_pausing_submit_returns_view(nenv):
    env = nenv
    v, old_rev = await pause_by_wrongs(env)
    before = env.store.get_attempt(v.id)
    # The retried submit (timeout, same rev) gets the current view, not 503.
    again = await env.svc.submit(DEV, v.id, env.wrong(v), old_rev)
    assert again.id == v.id and again.rev == before.rev
    assert env.store.get_attempt(v.id) == before
    for stale in (env.svc.hint(DEV, v.id, old_rev), env.svc.skip(DEV, v.id, old_rev)):
        assert (await stale).rev == before.rev
    # A fresh-rev write is still refused while paused.
    with pytest.raises(QuizUnavailable) as e:
        await env.svc.submit(DEV, v.id, env.wrong(v), before.rev)
    assert e.value.reason == "paused"
    assert env.store.get_attempt(v.id) == before


async def test_stale_rev_wins_over_lock(nenv):
    env = nenv
    v = await env.svc.current(DEV, "regular")
    old = v.rev
    await env.svc.submit(DEV, v.id, env.wrong(v), old)
    env.store.set_kv("locked", "1")
    before = env.store.get_attempt(v.id)
    assert (await env.svc.submit(DEV, v.id, env.wrong(v), old)).rev == before.rev
    with pytest.raises(QuizUnavailable):
        await env.svc.submit(DEV, v.id, env.wrong(v), before.rev)
    with pytest.raises(QuizRejected) as e:  # ownership is still checked first
        await env.svc.submit("other", v.id, env.wrong(v), old)
    assert e.value.code == "not_found"
    assert env.store.get_attempt(v.id) == before


async def test_attempt_expires_at_midnight(tmp_path):
    env = Env(tmp_path, "mode: enforce\ncurfew:\n  enabled: false\n")
    env.clock.t = datetime(2026, 11, 18, 13, 59, tzinfo=timezone.utc)  # 23:59 local
    v = await env.svc.current(DEV, "regular")
    env.clock.t = datetime(2026, 11, 18, 14, 0, 30, tzinfo=timezone.utc)  # 00:00:30
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.state == "expired" and r.credited_minutes == 0
    assert env.store.credits(date(2026, 11, 18)) == []
    assert env.store.credits(date(2026, 11, 19)) == []
    nxt = await env.svc.current(DEV, "regular")
    assert nxt.id != v.id


# -- selection -------------------------------------------------------------------

async def test_no_same_kind_twice_in_a_row(env):
    kinds = []
    for _ in range(8):
        v = await env.svc.current(DEV, "regular")
        kinds.append(env.store.get_attempt(v.id).snapshot["kind"])
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert all(a != b for a, b in zip(kinds, kinds[1:]))


async def test_bank_exhausted_falls_back(env):
    regular_templates = {i.template_id for i in env.bank.instances("regular")}
    order = []
    for _ in range(len(regular_templates)):
        v = await env.svc.current(DEV, "regular")
        order.append(env.store.get_attempt(v.id).template_id)
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert set(order) == regular_templates  # dedup: every template once
    v = await env.svc.current(DEV, "regular")
    assert v.state == "issued"
    assert env.store.get_attempt(v.id).template_id == order[0]  # oldest correct


def regular_templates(env):
    return {i.template_id for i in env.bank.instances("regular")}


async def assert_no_repeat_while_unseen(env, seen):
    """While unseen templates remain, every new regular pick is an unseen one."""
    seen = set(seen)
    while seen != regular_templates(env):
        v = await env.svc.current(DEV, "regular")
        tid = env.store.get_attempt(v.id).template_id
        assert tid not in seen, f"{tid} re-issued although its answer was revealed"
        seen.add(tid)
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))


async def test_skipped_answer_not_reissued(env):
    seen = set()
    for _ in range(10):  # answer numeric ones until a choice question comes up
        v = await env.svc.current(DEV, "regular")
        seen.add(env.store.get_attempt(v.id).template_id)
        if v.answer_type == "choice":
            break
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
    s = await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert s.state == "skipped" and s.correct_answer is not None and s.skips_left == 3
    await assert_no_repeat_while_unseen(env, seen)


async def test_exhausted_not_reissued(env):
    seen = set()
    for _ in range(10):  # answer choice ones until a numeric question comes up
        v = await env.svc.current(DEV, "regular")
        seen.add(env.store.get_attempt(v.id).template_id)
        if v.answer_type != "choice":
            break
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    for _ in range(3):
        r = await env.svc.submit(DEV, v.id, env.wrong(v), env.rev(v.id))
        env.clock.advance(seconds=61)
    assert r.state == "exhausted"
    await assert_no_repeat_while_unseen(env, seen)


async def test_fresh_preferred_over_kind_variety(env):
    stickers = env.bank.get("logic-stickers-1")
    twin = replace(stickers, template_id="logic-stickers-twin",
                   instance_id="logic-stickers-twin-1")
    bank = QuizBank(env.bank.instances("regular") + env.bank.instances("challenge") + [twin],
                    env.bank.templates() + [twin.template_id])
    env.svc = QuizService(bank, env.mon, env.store, env.qcfg, random.Random(1), lambda: b"s" * 32)
    keep = ["math-pencils-shared", "logic-stickers", "logic-stickers-twin"]
    for tid in bank.templates():
        if tid not in keep:
            env.store.set_kv(f"quiz:disabled:{tid}", "1")
    env.store.set_kv("quiz:disabled:logic-stickers", "1")
    env.store.set_kv("quiz:disabled:logic-stickers-twin", "1")

    async def answer_next():
        v = await env.svc.current(DEV, "regular")
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
        return env.store.get_attempt(v.id).template_id

    assert await answer_next() == "math-pencils-shared"
    env.store.del_kv("quiz:disabled:logic-stickers")
    assert await answer_next() == "logic-stickers"
    env.store.del_kv("quiz:disabled:logic-stickers-twin")
    # the only unseen template shares the last kind; it still beats a seen template
    assert await answer_next() == "logic-stickers-twin"


async def test_disabled_template_excluded(env):
    for tid in {i.template_id for i in env.bank.instances("regular")} - {"logic-stickers"}:
        env.store.set_kv(f"quiz:disabled:{tid}", "1")
    for _ in range(2):
        v = await env.svc.current(DEV, "regular")
        assert env.store.get_attempt(v.id).template_id == "logic-stickers"
        await env.svc.skip(DEV, v.id, env.rev(v.id))


async def test_challenge_pick_depends_on_secret_seed(tmp_path):
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(30)]
    n = 2
    seq_a = [pick_challenge_index(b"a" * 32, d, n) for d in days]
    seq_b = [pick_challenge_index(b"b" * 32, d, n) for d in days]
    assert seq_a != seq_b
    assert seq_a == [pick_challenge_index(b"a" * 32, d, n) for d in days]
    # same seed, same day -> same challenge instance across independent services
    e1, e2 = Env(tmp_path / "1"), Env(tmp_path / "2")
    c1 = await e1.svc.current(DEV, "challenge")
    c2 = await e2.svc.current(DEV, "challenge")
    assert e1.store.get_attempt(c1.id).instance_id == e2.store.get_attempt(c2.id).instance_id


async def test_default_seed_provider_persists(tmp_path):
    env = Env(tmp_path, seed=None)
    await env.svc.current(DEV, "challenge")
    seed = env.store.get_kv("quiz:challenge_seed")
    assert seed is not None and len(bytes.fromhex(seed)) == 32
    env.restart()
    assert kv_seed_provider(env.store)() == bytes.fromhex(seed)


# -- status ------------------------------------------------------------------------

async def test_status_counts(env):
    st = await env.svc.status(DEV)
    assert st.available and st.reason is None and st.quiz_today == 0
    assert st.challenge_state == "none" and st.remaining_s == 20 * 60
    v = await env.svc.current(DEV, "regular")
    env.clock.advance(seconds=60)
    await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    await env.svc.current(DEV, "challenge")
    st = await env.svc.status(DEV)
    assert st.quiz_today == 1 and st.earned_minutes == 10
    assert st.remaining_s == 30 * 60 and st.challenge_state == "issued"

# -- parent commands and digest ----------------------------------------------------

def _cmd(env, name, arg=""):
    return env.mon.handle(Command(1, 1, name, arg))


async def test_quiz_command_lists_today(env):
    env.mon.quiz = env.svc
    assert (await _cmd(env, "quiz")).text == "今天还没有答题"
    v = await numeric_regular(env)
    env.clock.advance(seconds=61)
    await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    lines = (await _cmd(env, "quiz")).text.splitlines()
    assert lines[-1].startswith("10:")  # issue time in local time (UTC+10)
    assert "普通" in lines[-1] and "答对" in lines[-1] and lines[-1].endswith("+10")


async def test_quiz_check_returns_answer(env):
    env.mon.quiz = env.svc
    text = (await _cmd(env, "quiz", "check")).text
    assert "答案：" in text
    instance_id, body = env.svc.spot_check()
    assert env.bank.get(instance_id) is not None and "\n答案：" in body


async def test_quiz_bad_disables_and_excludes(env):
    env.mon.quiz = env.svc
    assert (await _cmd(env, "quiz", "bad no-such")).text == "找不到 no-such"
    tid = env.bank.instances("regular")[0].template_id
    assert (await _cmd(env, "quiz", f"bad {tid}")).text == f"已下架 {tid}"
    assert env.store.get_kv(f"quiz:disabled:{tid}") is not None
    for _ in range(len(regular_templates(env)) + 3):
        v = await env.svc.current(DEV, "regular")
        assert env.store.get_attempt(v.id).template_id != tid
        env.clock.advance(seconds=60)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))


async def test_quiz_disabled_reply(env):
    assert (await _cmd(env, "quiz")).text == "答题功能未启用"


async def test_daily_summary_includes_quiz_digest(env):
    env.mon.quiz = env.svc
    v = await numeric_regular(env)
    await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))  # too fast: no credit
    env.store.log_event("quiz_denied", "ip=x why=ip", env.clock())
    env.clock.t = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)  # 20:00 local
    await env.mon.tick()
    summary = next(m for m in env.sink.msgs if "每日汇总" in m)
    assert ("答题：答对 1 题，计入 0 分钟，太快 1 次，跳过达上限 0 次，拒绝请求 1 次"
            in summary)


# -- revision guard / open attempts ----------------------------------------------

async def test_stale_rev_submit_is_noop(env):
    v = await numeric_regular(env)
    wrong = env.wrong(v)
    first = await env.svc.submit(DEV, v.id, wrong, v.rev)
    again = await env.svc.submit(DEV, v.id, wrong, v.rev)  # same (now stale) rev
    a = env.store.get_attempt(v.id)
    assert a.wrong_count == 1
    assert first.rev == a.rev == v.rev + 1 and again == first


async def test_stale_rev_hint_is_noop(env):
    v = await env.svc.current(DEV, "regular")
    await env.svc.hint(DEV, v.id, v.rev)
    again = await env.svc.hint(DEV, v.id, v.rev)
    assert env.store.get_attempt(v.id).hints_used == 1
    assert len(again.hints) == 1


async def test_stale_rev_skip_after_skip_returns_view(env):
    v = await env.svc.current(DEV, "regular")
    first = await env.svc.skip(DEV, v.id, v.rev)
    again = await env.svc.skip(DEV, v.id, v.rev)
    assert first.state == "skipped" and again == first


async def test_stale_rev_checked_before_other_rules(env):
    v = await env.svc.current(DEV, "challenge")
    await env.svc.hint(DEV, v.id, v.rev)
    # A stale skip on a challenge returns the view instead of no_skip_challenge.
    r = await env.svc.skip(DEV, v.id, v.rev)
    assert r.state == "issued"


async def test_open_attempts_never_issues(env):
    assert await env.svc.open_attempts(DEV) == []
    assert env.store.attempts_on(env.mon.business_day(env.clock())) == []


async def test_open_attempts_lists_both_tiers(env):
    c = await env.svc.current(DEV, "challenge")
    r = await env.svc.current(DEV, "regular")
    got = await env.svc.open_attempts(DEV)
    assert [v.id for v in got] == [r.id, c.id]
    await env.svc.skip(DEV, r.id, r.rev)
    assert [v.id for v in await env.svc.open_attempts(DEV)] == [c.id]
    assert await env.svc.open_attempts(DEV + "x") == []
