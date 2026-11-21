"""Passage-set attempts: parts once each, vocab cards with review, settlement."""
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from ltp.config import load_settings
from ltp.quiz.bank import Instance, QuizBank
from ltp.quiz.http import build_app
from ltp.quiz.service import QuizRejected
from quiz_fixtures import DEV, Clock, Env

ENFORCE = "mode: enforce\n"
SENTENCE = "The old lighthouse keeper was reluctant to leave his tower."


def _opts(prefix, n):
    return tuple((f"{prefix}{i}", f"{prefix} option {i}") for i in range(n))


def passage(tid, tier="regular", n_parts=3, n_cards=1):
    """A passage Instance shaped like the bank loader builds it. Built directly so
    these tests do not depend on bank-file review provenance."""
    words = 300 if tier == "challenge" else 150
    text = SENTENCE + " " + " ".join(f"word{i}" for i in range(words - len(SENTENCE.split())))
    parts = [{"id": f"{tid}q{i}", "prompt": f"Question {tid}q{i}?",
              "options": list(_opts(f"{tid}q{i}o", 4)), "answer": f"{tid}q{i}o1",
              "explanation": f"Because {tid}q{i}."} for i in range(n_parts)]
    vocab = [{"id": f"{tid}v{i}", "word": w, "meaning": f"meaning of {w}", "sentence": SENTENCE,
              "check_prompt": f"{w} means?", "options": list(_opts(f"{tid}v{i}o", 3)),
              "answer": f"{tid}v{i}o0"}
             for i, w in enumerate(["reluctant", "lighthouse"][:n_cards])]
    return Instance(template_id=tid, instance_id=tid, subject="english", kind="reading",
                    tier=tier, prompt="", figure=None, hints=("Reread the first sentence.",),
                    explanation="", answer_type="passage", answer="", options=(),
                    content_hash=f"hash-{tid}",
                    passage={"text": text, "parts": parts, "vocab": vocab})


def numeric(tid):
    return Instance(template_id=tid, instance_id=f"{tid}-1", subject="math", kind="multi_step",
                    tier="regular", prompt="2 + 3 = ?", figure=None, hints=(),
                    explanation="Add.", answer_type="integer", answer="5", options=(),
                    content_hash=f"hash-{tid}")


class PassageEnv(Env):
    """quiz_fixtures.Env with an in-memory bank instead of the production bank files."""

    def __init__(self, tmp_path, instances, yaml_text=ENFORCE):
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "settings.yaml").write_text(yaml_text, encoding="utf-8")
        self.settings = load_settings(cfg / "settings.yaml", {})
        self.qcfg = replace(self.settings.quiz, enabled=True)
        self.db = tmp_path / "ltp.db"
        self.bank = QuizBank(instances, [i.template_id for i in instances])
        self.clock = Clock()
        self.seed = b"s" * 32
        self.build()


def make_env(tmp_path, instances, yaml_text=ENFORCE):
    return PassageEnv(tmp_path, instances, yaml_text)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path, [passage("p1"), passage("c1", tier="challenge", n_parts=4)])


def att(env, v):
    return env.store.get_attempt(v.id)


def part_answer(env, v, right=True):
    a = att(env, v)
    i = len(a.progress["parts"])
    good = a.answer["parts"][i]
    return good if right else next(o for o in a.option_order["parts"][i] if o != good)


def card_answer(env, v, right=True):
    a = att(env, v)
    i = next(k for k, c in enumerate(a.progress["vocab"]) if not c["done"])
    good = a.answer["vocab"][i]
    return good if right else next(o for o in a.option_order["vocab"][i] if o != good)


async def submit(env, v, answer, step=None, rev=None):
    cur = env.svc
    view = await cur.result(DEV, v.id)
    return await cur.submit(DEV, v.id, answer, env.rev(v.id) if rev is None else rev,
                            view.step if step is None else step)


async def answer_parts(env, v, pattern, total_s=200):
    """Answer the parts by `pattern` (True = right), spreading `total_s` before the last."""
    for k, right in enumerate(pattern):
        if k == len(pattern) - 1:
            env.clock.advance(seconds=total_s)
        v = await submit(env, v, part_answer(env, v, right))
    return v


async def finish_vocab(env, v):
    while v.stage == "vocab":
        env.clock.advance(seconds=10)
        v = await submit(env, v, card_answer(env, v))
    return v


# -- happy path and rewards ----------------------------------------------------

async def test_happy_path_with_vocab_review(env):
    v = await env.svc.current(DEV, "regular")
    assert v.answer_type == "passage" and v.stage == "parts" and v.step == 0
    assert v.passage.startswith(SENTENCE) and v.parts_total == 3 and v.vocab == []
    assert v.options == [] and v.tries_left is None
    v = await answer_parts(env, v, [True, True, True])
    assert v.stage == "vocab" and v.step == 3 and v.parts_right == 3 and v.state == "issued"
    assert [p["status"] for p in v.parts] == ["right"] * 3
    env.clock.advance(seconds=10)
    v = await submit(env, v, card_answer(env, v, right=False))
    assert v.step == 3 and v.vocab[0]["need_review"] and v.state == "issued"
    v = await env.svc.review(DEV, v.id, env.rev(v.id))
    assert not v.vocab[0]["need_review"]
    env.clock.advance(seconds=10)
    v = await submit(env, v, card_answer(env, v))
    assert v.state == "correct" and v.stage == "done" and v.outcome == "credited"
    assert v.credited_minutes == 10 and v.nominal_minutes == 10
    assert [c.minutes for c in env.quiz_credits()] == [10]
    assert env.sink.has("✅ 阅读题答对 3/3 +10 分钟（今日 1 题，已赚 10 分钟）")
    a = att(env, v)
    assert a.progress["vocab"][0]["wrong"] == 1 and a.progress["vocab"][0]["done"]


@pytest.mark.parametrize("pattern,hint,credited,outcome", [
    ([True, False, True], False, 5, "half"),
    ([True, False, True], True, 2, "half"),
    ([True, True, True], True, 5, "hint"),
])
async def test_regular_reward_halving(env, pattern, hint, credited, outcome):
    v = await env.svc.current(DEV, "regular")
    if hint:
        v = await env.svc.hint(DEV, v.id, env.rev(v.id))
        assert v.hints == ["Reread the first sentence."]
    v = await finish_vocab(env, await answer_parts(env, v, pattern))
    assert v.state == "correct" and v.outcome == outcome and v.credited_minutes == credited
    assert [c.minutes for c in env.quiz_credits()] == [credited]
    right = sum(pattern)
    assert env.sink.has(f"✅ 阅读题答对 {right}/3 +{credited} 分钟")


async def test_two_wrong_parts_exhausted_without_notification(env):
    v = await env.svc.current(DEV, "regular")
    v = await answer_parts(env, v, [False, True, False])
    assert v.state == "issued" and v.stage == "vocab"  # must still finish the cards
    v = await finish_vocab(env, v)
    assert v.state == "exhausted" and v.outcome == "parts_wrong" and v.credited_minutes == 0
    assert env.quiz_credits() == [] and env.sink.msgs == []


async def test_challenge_one_wrong_and_hint_gives_6(env):
    v = await env.svc.current(DEV, "challenge")
    assert v.parts_total == 4
    v = await env.svc.hint(DEV, v.id, env.rev(v.id))
    v = await finish_vocab(env, await answer_parts(env, v, [True, True, False, True], 400))
    assert v.state == "correct" and v.credited_minutes == 6 and v.nominal_minutes == 6
    assert env.sink.has("✅ 阅读题答对 3/4 +6 分钟") and env.sink.has("🏆 今日挑战完成")


async def test_too_fast_measured_to_last_part(env):
    v = await env.svc.current(DEV, "regular")
    v = await answer_parts(env, v, [True, True, True], total_s=100)
    env.clock.advance(seconds=600)  # slow vocab does not rescue a rushed reading
    v = await finish_vocab(env, v)
    assert v.state == "correct" and v.outcome == "too_fast" and v.credited_minutes == 0
    assert env.quiz_credits() == [] and env.sink.has("答得太快")


async def test_cap_applies_to_passage(tmp_path):
    env = make_env(tmp_path, [passage("p1")],
                   yaml_text="mode: enforce\nquota:\n  free_minutes: 20\n  daily_cap_minutes: 26\n")
    v = await finish_vocab(env, await answer_parts(
        env, await env.svc.current(DEV, "regular"), [True, True, True]))
    assert v.outcome == "cap" and v.credited_minutes == 6


# -- idempotency and guards ----------------------------------------------------

async def test_stale_step_is_noop(env):
    v = await env.svc.current(DEV, "regular")
    rev = env.rev(v.id)
    r = await env.svc.submit(DEV, v.id, part_answer(env, v), rev, 2)
    assert r.step == 0 and env.rev(v.id) == rev and att(env, v).progress["parts"] == []


async def test_vocab_stage_stale_step_is_noop(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    assert v.stage == "vocab" and v.step == 3
    env.clock.advance(seconds=10)
    before = att(env, v)
    for step in (2, 4):  # a late part retry and a step from the future
        r = await env.svc.submit(DEV, v.id, card_answer(env, v), env.rev(v.id), step)
        assert r.step == 3 and r.stage == "vocab" and r.state == "issued"
    assert att(env, v) == before
    r = await env.svc.submit(DEV, v.id, card_answer(env, v), env.rev(v.id), 3)
    assert r.state == "correct"


async def test_double_submit_same_rev_applies_once(env):
    v = await env.svc.current(DEV, "regular")
    rev = env.rev(v.id)
    wrong = part_answer(env, v, right=False)
    await env.svc.submit(DEV, v.id, wrong, rev, 0)
    r = await env.svc.submit(DEV, v.id, wrong, rev, 0)
    a = att(env, v)
    assert len(a.progress["parts"]) == 1 and a.wrong_count == 1 and r.step == 1


async def test_submit_without_step_or_bad_option_is_bad_input(env):
    v = await env.svc.current(DEV, "regular")
    for answer, step in ((part_answer(env, v), None), ("nope", 0)):
        with pytest.raises(QuizRejected) as e:
            await env.svc.submit(DEV, v.id, answer, env.rev(v.id), step)
        assert e.value.code == "bad_input"


async def test_vocab_submit_before_review_rejected(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    env.clock.advance(seconds=10)
    v = await submit(env, v, card_answer(env, v, right=False))
    env.clock.advance(seconds=10)
    with pytest.raises(QuizRejected) as e:
        await submit(env, v, card_answer(env, v))
    assert e.value.code == "review_required"
    rev = env.rev(v.id)
    await env.svc.review(DEV, v.id, rev)
    again = await env.svc.review(DEV, v.id, rev)  # retried review is a no-op
    assert env.rev(v.id) == rev + 1 and not again.vocab[0]["need_review"]


async def test_vocab_cooldown(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    v = await submit(env, v, card_answer(env, v, right=False))
    v = await env.svc.review(DEV, v.id, env.rev(v.id))
    env.clock.advance(seconds=2)
    with pytest.raises(QuizRejected) as e:
        await submit(env, v, card_answer(env, v))
    assert e.value.code == "cooldown" and e.value.retry_after_s == 3
    env.clock.advance(seconds=3)
    assert (await submit(env, v, card_answer(env, v))).state == "correct"


async def test_vocab_wrongs_not_in_wrong_total(env):
    day = env.clock().astimezone(env.settings.timezone).date()
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, False, True])
    for _ in range(3):
        env.clock.advance(seconds=10)
        v = await submit(env, v, card_answer(env, v, right=False))
        v = await env.svc.review(DEV, v.id, env.rev(v.id))
    assert env.store.wrong_total(DEV, day) == 1
    assert att(env, v).progress["vocab"][0]["wrong"] == 3


async def test_hint_refused_after_parts(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    rev = env.rev(v.id)
    r = await env.svc.hint(DEV, v.id, rev)
    assert r.hints == [] and env.rev(v.id) == rev


# -- lifecycle -----------------------------------------------------------------

async def test_quit_during_vocab_resumes(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    env.restart()
    opened = await env.svc.open_attempts(DEV)
    assert [(o.id, o.state, o.stage) for o in opened] == [(v.id, "issued", "vocab")]
    assert (await env.svc.current(DEV, "regular")).id == v.id


async def test_midnight_expiry_during_vocab(env):
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    env.clock.advance(hours=24)  # next morning, past local midnight
    r = await submit(env, v, card_answer(env, v))
    assert r.state == "expired" and r.credited_minutes == 0
    assert env.quiz_credits() == [] and env.sink.msgs == []


async def test_skip_consumes_skip_even_after_wrong_part(tmp_path):
    env = make_env(tmp_path, [passage(f"p{i}") for i in range(5)])
    v = await env.svc.current(DEV, "regular")
    assert v.skips_left == 3
    v = await submit(env, v, part_answer(env, v, right=False))
    s = await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert s.state == "skipped" and s.skips_left == 2
    for _ in range(2):  # a skip during the vocab stage also costs one
        v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
        assert v.stage == "vocab"
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    v = await env.svc.current(DEV, "regular")
    v = await submit(env, v, part_answer(env, v, right=False))
    with pytest.raises(QuizRejected) as e:
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert e.value.code == "skip_limit"


async def test_challenge_passage_cannot_be_skipped(env):
    v = await env.svc.current(DEV, "challenge")
    with pytest.raises(QuizRejected) as e:
        await env.svc.skip(DEV, v.id, env.rev(v.id))
    assert e.value.code == "no_skip_challenge"


async def test_skipped_and_expired_passages_not_repicked(tmp_path):
    env = make_env(tmp_path, [passage("p1"), passage("p2"), passage("p3")])
    first = await env.svc.current(DEV, "regular")
    await env.svc.skip(DEV, first.id, env.rev(first.id))
    second = await env.svc.current(DEV, "regular")
    assert second.id != first.id
    env.clock.advance(hours=24)  # `second` expired at midnight
    third = await env.svc.current(DEV, "regular")
    picked = {att(env, x).template_id for x in (first, second, third)}
    assert picked == {"p1", "p2", "p3"}
    seen = env.store.last_seen_by_template(DEV, "regular", env.clock() - timedelta(days=30))
    assert set(seen) == {att(env, first).template_id, att(env, second).template_id}


# -- view hygiene --------------------------------------------------------------

async def test_view_never_leaks_answers(tmp_path):
    env = make_env(tmp_path, [passage("p1", n_cards=2)])
    v = await env.svc.current(DEV, "regular")
    a = att(env, v)
    assert v.vocab == [] and all("correct" not in p and "explanation" not in p for p in v.parts)
    assert [p["status"] for p in v.parts] == ["current", "pending", "pending"]
    assert [o["id"] for o in v.parts[0]["options"]] == a.option_order["parts"][0]
    # answers live only in the answer column
    assert all("answer" not in x for x in a.snapshot["parts"] + a.snapshot["vocab"])
    v = await submit(env, v, part_answer(env, v, right=False))
    p0 = v.parts[0]
    assert p0["status"] == "wrong" and p0["correct"] == a.answer["parts"][0]
    assert p0["given"] != p0["correct"] and p0["explanation"] == "Because p1q0."
    assert all("correct" not in p for p in v.parts[1:]) and v.vocab == []
    v = await answer_parts(env, v, [True, True])
    assert [c["word"] for c in v.vocab] == ["reluctant"]  # only up to the current card
    assert set(v.vocab[0]) == {"word", "meaning", "sentence", "check_prompt", "options",
                               "done", "need_review"}
    env.clock.advance(seconds=10)
    v = await submit(env, v, card_answer(env, v))
    assert [c["word"] for c in v.vocab] == ["reluctant", "lighthouse"] and v.step == 4
    assert v.state == "issued" and v.explanation is None and v.correct_answer is None


async def test_single_question_view_has_empty_passage_fields(tmp_path):
    env = make_env(tmp_path, [numeric("n1")])
    v = await env.svc.current(DEV, "regular")
    assert v.answer_type == "integer"
    assert (v.passage, v.stage, v.step, v.parts, v.vocab, v.parts_right, v.parts_total) == (
        None, None, None, [], [], 0, 0)


# -- config and HTTP -----------------------------------------------------------

def test_passage_config_defaults_and_range(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("mode: observe\n", encoding="utf-8")
    q = load_settings(f, {}).quiz
    assert (q.min_seconds_passage_regular, q.min_seconds_passage_challenge,
            q.vocab_cooldown_s) == (150, 300, 5)
    f.write_text("mode: observe\nquiz:\n  vocab_cooldown_s: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="vocab_cooldown_s"):
        load_settings(f, {})


async def test_http_step_and_review(env):
    tv_ip, token = "192.168.1.55", "tok-tv1-secret"
    env.qcfg = replace(env.qcfg, allowed_ips=frozenset({tv_ip}), tokens={DEV: token})
    env.restart()
    hdr = {"Authorization": f"Bearer {token}", "X-LTP-API": "2"}
    app = build_app(env.svc, env.qcfg, env.store, env.mon.notify_once, env.clock)
    transport = httpx.ASGITransport(app=app, client=(tv_ip, 5000))
    v = await answer_parts(env, await env.svc.current(DEV, "regular"), [True, True, True])
    env.clock.advance(seconds=10)
    v = await submit(env, v, card_answer(env, v, right=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://tv") as c:
        url = f"/api/attempt/{v.id}"
        r = await c.post(f"{url}/submit", headers=hdr,
                         json={"rev": env.rev(v.id), "answer": card_answer(env, v), "step": "3"})
        assert r.status_code == 400
        env.clock.advance(seconds=10)
        r = await c.post(f"{url}/submit", headers=hdr,
                         json={"rev": env.rev(v.id), "answer": card_answer(env, v), "step": 3})
        assert r.status_code == 409 and r.json()["code"] == "review_required"
        r = await c.post(f"{url}/review", headers=hdr, json={"rev": env.rev(v.id)})
        assert r.status_code == 200 and r.json()["vocab"][0]["need_review"] is False
        r = await c.post(f"{url}/submit", headers=hdr,
                         json={"rev": env.rev(v.id), "answer": card_answer(env, v), "step": 3})
        assert r.status_code == 200 and r.json()["state"] == "correct"
        r = await c.post(f"{url}/review", headers=hdr, json={})
        assert r.status_code == 400

# -- parent commands ------------------------------------------------------

def _cmd(env, name, arg=""):
    from ltp.notify import Command
    return env.mon.handle(Command(1, 1, name, arg))


async def test_spot_check_passage_contains_answers(tmp_path):
    env = make_env(tmp_path, [passage("p1")])
    env.mon.quiz = env.svc
    iid, body = env.svc.spot_check()
    assert iid == "p1" and body.startswith(SENTENCE)
    assert "第1题：Question p1q0?\n答案：p1q0o option 1" in body
    assert "单词：reluctant — meaning of reluctant" in body
    assert "答案：" in (await _cmd(env, "quiz", "check")).text


async def test_spot_check_passage_fits_4096(tmp_path):
    inst = passage("p1")
    long_text = SENTENCE + " " + "x" * 6000
    inst = replace(inst, passage={**inst.passage, "text": long_text})
    env = make_env(tmp_path, [inst])
    _, body = env.svc.spot_check()
    assert len(body) <= 4096 and "…" in body
    assert "第3题：" in body and "单词：reluctant — meaning of reluctant" in body


async def test_today_lines_passage_score(env):
    v = await env.svc.current(DEV, "regular")
    day = env.clock().astimezone(env.settings.timezone).date()
    assert "答对" not in env.svc.today_lines(day)[-1]
    v = await submit(env, v, part_answer(env, v, True))
    await submit(env, v, part_answer(env, v, False))
    assert env.svc.today_lines(day)[-1].endswith("答对 1/3")


async def test_quiz_check_reply_fits_4096_even_with_huge_parts(tmp_path):
    inst = passage("p1")
    big = [{**x, "prompt": "Q" * 3000} for x in inst.passage["parts"]]
    inst = replace(inst, passage={**inst.passage, "text": SENTENCE * 200, "parts": big})
    env = make_env(tmp_path, [inst])
    env.mon.quiz = env.svc
    text = (await _cmd(env, "quiz", "check")).text
    assert text.startswith("p1\n") and len(text) <= 4096 and text.endswith("…")
