"""Question generation: planning, checks, review and the parent's publish/drop."""
import json
import random
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ltp.config import QuizSettings
from ltp.quiz import generate as gen
from ltp.quiz.bank import QuizBank
from ltp.store import Attempt, Store

SAMPLE = Path(__file__).resolve().parent / "sample_bank"
NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
DEV = "tv1"
CHECKS = ["answer_unique_correct", "distractors_unambiguous", "reading_level",
          "child_appropriate", "factual_accuracy"]


def variant(prompt="A shop packs {boxes} boxes of {per_box} apples and sells {sold}. "
                   "How many apples are left?", verify="boxes * per_box - sold", answer="280"):
    return {"id": "x", "subject": "math", "kind": "multi_step", "tier": "regular", "grade": 5,
            "est_minutes": 3, "prompt": prompt, "hints": ["Find the total first."],
            "explanation": "Total minus sold.", "answer_type": "integer",
            "plausible": {"min": 0, "max": 2000, "step": 1}, "verify": verify,
            "instances": [{"id": "a", "params": {"boxes": 12, "per_box": 25, "sold": 20},
                           "answer": answer}]}


class FakeLLM:
    def __init__(self, generated, verdict="pass"):
        self.generated = generated
        self.verdict = verdict
        self.calls = []

    def __call__(self, prompt, model):
        self.calls.append(prompt)
        if prompt.startswith("You write quiz questions"):
            return "Here you go:\n" + json.dumps(self.generated)
        ids = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith("{")]
        checks = {c: self.verdict == "pass" for c in CHECKS}
        return "REVIEW_JSON: " + json.dumps(
            {i: {"verdict": self.verdict, "checks": checks, "notes": "ok"} for i in ids})


def setup(tmp_path, low_water=1):
    bank = tmp_path / "quiz" / "bank"
    shutil.copytree(SAMPLE / "bank", bank)
    shutil.copytree(SAMPLE / "reviews", tmp_path / "quiz" / "reviews")
    cfg = replace(QuizSettings(), bank_dir=bank, extra_bank_dir=tmp_path / "extra",
                  generate_low_water=low_water, tokens={DEV: "t"})
    return cfg, Store(tmp_path / "ltp.db")


def skip(store, tid, when):
    store.insert_attempt(Attempt(
        id=f"{tid}-skip", device=DEV, tier="regular", template_id=tid, instance_id=f"{tid}-1",
        content_hash="h", snapshot={"kind": "multi_step"}, answer="1", option_order=None,
        day=when.date(), issued_utc=when, state="skipped", finished_utc=when, outcome="skipped"))


def test_plan_targets_mistakes_and_low_stock(tmp_path):
    cfg, store = setup(tmp_path, low_water=50)
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    targets = gen.plan(gen.read_templates(cfg.bank_dir), store, [DEV], NOW, cfg, random.Random(0))
    assert targets[0].reason == "mistake" and targets[0].variant_of == "math-pencils-shared"
    low = [t.subject for t in targets if t.reason == "low_stock"]
    assert sorted(low) == ["english", "logic", "math"]
    two = gen.plan(gen.read_templates(cfg.bank_dir), store, [DEV, "tv2"], NOW, cfg,
                   random.Random(0))
    assert sorted(t.subject for t in two if t.reason == "low_stock") == sorted(low)
    assert sum(t.count for t in targets) <= cfg.generate_max


def test_run_puts_checked_and_reviewed_questions_in_pending_only(tmp_path):
    cfg, store = setup(tmp_path)
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    bad = variant(prompt="Other {boxes} {per_box} {sold}?", answer="999")  # verify mismatch
    llm = FakeLLM([variant(), bad])
    result = gen.run(cfg, store, [DEV], NOW, llm, random.Random(0))
    assert len(result.pending) == 1 and result.pending[0]["variant_of"] == "math-pencils-shared"
    assert any("verify" in r for r in result.rejected)
    paths = gen.Paths(cfg.extra_bank_dir)
    assert list(paths.pending.glob("*.json")) and not list(paths.bank.glob("*.json"))
    assert "待你确认 1 题" in gen.summary(result)


def test_failed_review_keeps_question_out(tmp_path):
    cfg, store = setup(tmp_path)
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    result = gen.run(cfg, store, [DEV], NOW, FakeLLM([variant()], verdict="fail"),
                     random.Random(0))
    assert result.pending == [] and any("review failed" in r for r in result.rejected)


def test_publish_and_drop(tmp_path):
    cfg, store = setup(tmp_path)
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    batch = gen.run(cfg, store, [DEV], NOW, FakeLLM([variant()]), random.Random(0)).batch
    assert batch in gen.pending_batches(cfg)
    assert "→ 280" in gen.show(gen.pending_batches(cfg)[batch][0])
    assert gen.publish(cfg, "../" + batch) == (0, "找不到这个批次")
    assert gen.publish(cfg, batch) == (1, None)
    paths = gen.Paths(cfg.extra_bank_dir)
    bank = QuizBank.load(cfg.bank_dir, cfg.bank_dir.parent / "reviews",
                         extra=((paths.bank, paths.reviews),))
    assert any(t.startswith(batch) for t in bank.templates())
    assert gen.drop(cfg, batch) is False  # already published


def test_suggestion_only_when_useful(tmp_path):
    cfg, store = setup(tmp_path, low_water=1)
    assert gen.suggestion(cfg, store, [DEV], NOW) is None
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    hint = gen.suggestion(cfg, store, [DEV], NOW)
    assert hint and "1 道错题" in hint and "/quiz generate" in hint


def test_service_reloads_when_generated_files_change(tmp_path):
    from quiz_fixtures import Env
    env = Env(tmp_path / "env")
    env.svc.cfg = replace(env.svc.cfg, extra_bank_dir=tmp_path / "extra")
    loads = []
    env.svc.bank_loader = lambda: loads.append(1) or env.bank
    assert env.svc.maybe_reload() is True and loads == [1]  # first check always loads
    assert env.svc.maybe_reload() is False
    (tmp_path / "extra" / "bank").mkdir(parents=True)
    (tmp_path / "extra" / "bank" / "gen-x.json").write_text("[]", encoding="utf-8")
    assert env.svc.maybe_reload() is True and loads == [1, 1]
    assert env.svc.maybe_reload() is False

    def broken():
        raise ValueError("bad batch")
    env.svc.bank_loader = broken
    (tmp_path / "extra" / "bank" / "gen-y.json").write_text("[]", encoding="utf-8")
    old = env.svc.bank
    assert env.svc.maybe_reload() is False and env.svc.bank is old


async def test_telegram_generate_pending_publish(tmp_path):
    import asyncio

    from ltp.notify import Command
    from quiz_fixtures import Env
    env = Env(tmp_path / "env")
    cfg, store = setup(tmp_path)
    env.svc.cfg = cfg
    env.mon.quiz = env.svc
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    batch = gen.run(cfg, store, [DEV], NOW, FakeLLM([variant()]), random.Random(0)).batch

    def cmd(arg):
        return env.mon.handle(Command(1, 1, "quiz", arg))
    assert batch in (await cmd("pending")).text and "→ 280" in (await cmd("pending")).text
    env.svc.generation_job = lambda now: "🧠 fake run done"
    assert "开始生成" in (await cmd("generate")).text
    for _ in range(20):
        await asyncio.sleep(0.01)
        if env.sink.has("fake run done"):
            break
    assert env.sink.has("fake run done")
    assert "已上线" in (await cmd(f"publish {batch}")).text
    assert "没有待确认" in (await cmd("pending")).text


def test_no_devices_and_repeated_runs(tmp_path):
    cfg, store = setup(tmp_path)
    assert gen.plan(gen.read_templates(cfg.bank_dir), store, [], NOW, cfg, random.Random(0)) == []
    skip(store, "math-pencils-shared", NOW - timedelta(days=1))
    a = gen.run(cfg, store, [DEV], NOW, FakeLLM([variant()]), random.Random(0)).batch
    b = gen.run(cfg, store, [DEV], NOW, FakeLLM([variant(prompt="Pack {boxes} {per_box} {sold}.")]),
                random.Random(0)).batch
    assert a != b
