"""Mistake book: spaced re-asking of templates the child got wrong."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ltp.quiz import mistakes as mk
from ltp.store import Attempt
from quiz_fixtures import DEV
from test_quiz_passage import make_env, numeric

T0 = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def attempt(tid, minutes, state="correct", wrong=0, hints=0, outcome="credited", inst=None):
    t = T0 + timedelta(minutes=minutes)
    return Attempt(id=f"{tid}-{minutes}", device=DEV, tier="regular", template_id=tid,
                   instance_id=inst or f"{tid}-1", content_hash="h", snapshot={"kind": "k"},
                   answer="1", option_order=None, day=t.date(), issued_utc=t, state=state,
                   wrong_count=wrong, hints_used=hints, finished_utc=t, outcome=outcome)


def test_schedule_and_mastery():
    day = 24 * 60
    hist = [attempt("a", 0, state="skipped", outcome="skipped")]
    e = mk.book(hist)["a"]
    assert e.streak == 0 and e.due_utc == T0 + timedelta(days=1)
    hist.append(attempt("a", day))
    assert mk.book(hist)["a"].due_utc == T0 + timedelta(days=4)
    hist.append(attempt("a", 4 * day))
    assert mk.book(hist)["a"].due_utc == T0 + timedelta(days=11)
    hist.append(attempt("a", 11 * day))
    assert mk.book(hist)["a"].mastered and mk.due(mk.book(hist), T0 + timedelta(days=99)) == []
    hist.append(attempt("a", 12 * day, wrong=1, outcome="credited"))  # a slip restarts it
    assert mk.book(hist)["a"].streak == 0


def test_clean_answers_alone_never_enter_the_book():
    assert mk.book([attempt("b", 0), attempt("b", 5, hints=0)]) == {}
    assert "c" in mk.book([attempt("c", 0, hints=1, outcome="hint")])
    assert mk.due(mk.book([attempt("d", 0, state="exhausted", outcome="")]),
                  T0 + timedelta(hours=23)) == []


async def _third_question_after_a_mistake(env, mistake_tid, inst_id):
    """Day 1: record a skipped attempt of `mistake_tid`; day 2: answer two regular
    questions; return the third."""
    v = await env.svc.current(DEV, "regular")
    env.store.update_attempt(v.id, state="skipped", outcome="skipped", finished_utc=env.clock(),
                             template_id=mistake_tid, instance_id=inst_id)
    env.clock.advance(days=1, hours=1)
    for _ in range(2):
        v = await env.svc.current(DEV, "regular")
        env.clock.advance(seconds=30)
        await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    v = await env.svc.current(DEV, "regular")
    return v, env.store.get_attempt(v.id)


async def test_due_mistake_comes_back_with_other_numbers_and_full_reward(tmp_path):
    m = numeric("m1")
    pool = [m, replace(m, instance_id="m1-2"), numeric("f1"), numeric("f2"), numeric("f3")]
    env = make_env(tmp_path, pool)
    v, a = await _third_question_after_a_mistake(env, "m1", "m1-1")
    assert a.template_id == "m1" and a.instance_id == "m1-2"
    assert a.snapshot["review"] == "fresh"
    env.clock.advance(seconds=30)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "credited" and r.credited_minutes == 10


async def test_single_version_mistake_comes_back_as_practice(tmp_path):
    env = make_env(tmp_path, [numeric(t) for t in ("s1", "f1", "f2", "f3")])
    v, a = await _third_question_after_a_mistake(env, "s1", "s1-1")
    assert a.template_id == "s1" and a.snapshot["review"] == "practice"
    env.clock.advance(seconds=30)
    r = await env.svc.submit(DEV, v.id, env.answer(v), env.rev(v.id))
    assert r.outcome == "practice" and r.credited_minutes == 0
    assert env.sink.has("错题复习")


async def test_digest_reports_the_mistake_book(tmp_path):
    env = make_env(tmp_path, [numeric(t) for t in ("s1", "f1")])
    env.svc.cfg = replace(env.svc.cfg, tokens={DEV: "x"})
    v = await env.svc.current(DEV, "regular")
    await env.svc.skip(DEV, v.id, env.rev(v.id))
    day = env.clock().astimezone(env.settings.timezone).date()
    text = env.svc.daily_digest(day)
    assert "错题本：待复习 1 题" in text and "今天新错：" in text
