"""Generate new question templates with an LLM: variants of the child's mistakes, and
top-ups for subjects running low on unseen questions.

Every candidate goes through the same checks as the shipped bank (loader validation, the
verify expression, python-chess for chess) plus an LLM review. Nothing goes live
on its own: every batch waits in pending/ until the parent publishes it from Telegram,
and runs only start when the parent asks (/quiz generate). Generated files live under quiz.extra_bank_dir, outside
git, laid out as bank/ (live), reviews/, pending/ and archive/.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import random
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ltp.config import QuizSettings
from ltp.quiz import mistakes as mk
from ltp.quiz.bank import SUBJECTS, QuizBank, _validate_template, review_problem
from ltp.quiz.chesscheck import CHESS_KINDS, chess_problem
from ltp.store import Store

log = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parents[3]
VARIANTS_PER_MISTAKE = 2
TOP_UP_PER_SUBJECT = 5
MISTAKE_HORIZON = timedelta(days=60)

Chat = Callable[[str, str], str]  # (prompt, model) -> reply


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def bank(self) -> Path:
        return self.root / "bank"

    @property
    def reviews(self) -> Path:
        return self.root / "reviews"

    @property
    def pending(self) -> Path:
        return self.root / "pending"

    @property
    def archive(self) -> Path:
        return self.root / "archive"


@dataclass
class Target:
    reason: str               # "mistake" | "low_stock"
    subject: str
    examples: list[dict]
    count: int
    variant_of: str | None = None


@dataclass
class Result:
    batch: str
    pending: list[dict] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def read_templates(*dirs: Path) -> list[dict]:
    out: list[dict] = []
    for d in dirs:
        for path in sorted(Path(d).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                out.extend(t for t in data if isinstance(t, dict))
    return out


# -- planning -----------------------------------------------------------------
def plan(templates: list[dict], store: Store, devices: list[str], now: datetime,
         cfg: QuizSettings, rng: random.Random) -> list[Target]:
    """What to write this run, mistakes first, capped at cfg.generate_max templates."""
    by_id = {t["id"]: t for t in templates if isinstance(t.get("id"), str)}
    covered = {t.get("variant_of") for t in templates if t.get("variant_of")}
    targets: list[Target] = []
    for device in devices:
        book = mk.book(store.attempts_since(device, "regular", now - MISTAKE_HORIZON))
        for e in sorted(book.values(), key=lambda e: e.last_utc, reverse=True):
            t = by_id.get(e.template_id)
            if e.mastered or t is None or e.template_id in covered:
                continue
            covered.add(e.template_id)
            targets.append(Target("mistake", t["subject"], [t], VARIANTS_PER_MISTAKE,
                                  e.template_id))
    regular = [t for t in templates if t.get("tier") == "regular"]
    seen_by_device = [store.last_seen_by_template(device, "regular",
                                                  now - timedelta(days=cfg.dedup_days))
                      for device in devices]
    for subject in sorted(SUBJECTS):  # the bank is shared: one top-up per subject
        pool = [t for t in regular if t.get("subject") == subject]
        if not pool or not seen_by_device:
            continue
        unseen = min(sum(1 for t in pool if t["id"] not in seen) for seen in seen_by_device)
        if unseen >= cfg.generate_low_water:
            continue
        top = max(t.get("grade", 0) for t in pool)
        examples = [t for t in pool if t.get("grade") == top] or pool
        need = min(TOP_UP_PER_SUBJECT, cfg.generate_low_water - unseen)
        targets.append(Target("low_stock", subject,
                              rng.sample(examples, min(3, len(examples))), need))
    capped, total = [], 0
    for t in targets:
        if total >= cfg.generate_max:
            break
        t.count = min(t.count, cfg.generate_max - total)
        total += t.count
        capped.append(t)
    return capped


# -- prompting ------------------------------------------------------------------
RULES = """Rules (a strict validator checks every one):
- Output ONLY a JSON array of new templates. No prose, no code fences.
- Copy the exact JSON shape of the example: same subject, tier, answer_type and field names.
- "id": any short string (the system renames it). Instances need unique ids too.
- Plain English, British/Australian spelling. Curly braces only for {param} placeholders.
- integer/decimal: "verify" is an arithmetic expression over the params using numbers,
  + - * / ** and parentheses only, and it must equal each instance's answer exactly;
  "plausible" {min, max, step} allows at least 20 values and contains every answer;
  give 3 instances with different params, each {"id", "params", "answer"} with the exact
  answer as a string. Hints are not filled in, so do not put {param} in hints.
- choice: at least 5 options {"id", "text"}, exactly one correct; one instance with
  "params": {} and "answer" set to the correct option id.
- passage: same structure as the example, every vocab "sentence" copied word for word
  from "text", parts with 4 options each.
- chess: "figure" is a JSON string {"type": "chess", "fen": ...}; options are legal moves
  in algebraic notation without + or #; exactly one option wins.
- 1-3 hints that guide without giving the answer; an explanation of the answer.
- Exactly one defensible answer. Child-appropriate. Facts must be correct.
- Do not copy the example: new story, new numbers, new wording."""


def build_prompt(target: Target) -> str:
    grade = max(int(t.get("grade", 5)) for t in target.examples)
    if target.reason == "mistake":
        kind = target.examples[0].get("kind")
        goal = (f"The child got the example question wrong. Write {target.count} new questions "
                f"that practise the same skill (kind \"{kind}\") at the same difficulty, with "
                "a different story, so the child can learn the idea properly.")
    else:
        goal = (f"Write {target.count} new {target.subject} questions similar in kind and "
                "difficulty to the examples, on new topics.")
    examples = "\n".join(json.dumps(t, ensure_ascii=False) for t in target.examples)
    return (f"You write quiz questions for a child in Year {grade} (about age {grade + 5}).\n"
            f"{goal}\n\n{RULES}\n\nExample templates:\n{examples}\n")


def parse_reply(text: str) -> list[dict]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


# -- checking ---------------------------------------------------------------------
def _rename(t: dict, new_id: str) -> dict:
    t = {**t, "id": new_id}
    if isinstance(t.get("instances"), list):
        t["instances"] = [{**i, "id": f"{new_id}-{k}"} if isinstance(i, dict) else i
                          for k, i in enumerate(t["instances"], 1)]
    return t


def check(t: dict, seen_instances: dict[str, str], seen_text: set[str]) -> str | None:
    """Why a candidate cannot be used, or None. Registers its ids/text when accepted."""
    key = t.get("text") if t.get("answer_type") == "passage" else t.get("prompt")
    if not isinstance(key, str) or key in seen_text:
        return "duplicate or missing prompt"
    if t.get("figure") is not None and t.get("kind") not in CHESS_KINDS:
        return "generated figures are only allowed for chess"
    trial = dict(seen_instances)
    problems, built = _validate_template(t, trial)
    if problems:
        return problems[0]
    if t.get("kind") in CHESS_KINDS:
        for inst in built:
            if (why := chess_problem(inst)) is not None:
                return why
    seen_instances.clear()
    seen_instances.update(trial)
    seen_text.add(key)
    return None


def auto_publishable(t: dict) -> bool:
    """Answers a machine can prove: numeric verify expressions and chess."""
    return t.get("answer_type") in ("integer", "decimal") or t.get("kind") in CHESS_KINDS


def _review_module():
    spec = importlib.util.spec_from_file_location("review_bank",
                                                  SERVER_DIR / "scripts" / "review_bank.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def review(cands: list[dict], reviews_dir: Path, chat: Chat, model: str) -> list[dict]:
    """LLM review (every pass the template needs); returns the candidates that passed."""
    if not cands:
        return []
    rb = _review_module()

    def runner(prompt: str) -> str:
        return chat(prompt, model) + f"\nmodel: {model}\n"
    for review_pass in (1, 2):
        todo = [t for t in cands if review_pass in _passes(t)]
        if todo:
            rb.review_templates(todo, reviews_dir, review_pass, runner, reviewer="deepseek")
    return [t for t in cands
            if all(review_problem(t, reviews_dir, p) is None for p in _passes(t))]


def _passes(t: dict) -> tuple[int, ...]:
    """Generated questions are all reviewed, numeric ones too (the model also wrote the
    verify formula); challenge ones twice."""
    return (1, 2) if t.get("tier") == "challenge" else (1,)


# -- the run ----------------------------------------------------------------------
def run(cfg: QuizSettings, store: Store, devices: list[str], now: datetime, chat: Chat,
        rng: random.Random | None = None, dry_run: bool = False) -> Result:
    rng = rng or random.Random()
    paths = Paths(cfg.extra_bank_dir)
    batch = f"gen-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"  # never reuse a batch name
    result = Result(batch)
    templates = read_templates(cfg.bank_dir, paths.bank, paths.pending)
    bank = QuizBank.load(cfg.bank_dir, Path(cfg.bank_dir).parent / "reviews",
                         extra=((paths.bank, paths.reviews),))
    seen_instances = {i.instance_id: i.template_id
                      for tier in ("regular", "challenge") for i in bank.instances(tier)}
    seen_text = {t.get("text") if t.get("answer_type") == "passage" else t.get("prompt")
                 for t in templates}
    targets = plan(templates, store, devices, now, cfg, rng)
    log.info("generation %s: %d targets", batch, len(targets))
    accepted: list[dict] = []
    n = 0
    for target in targets:
        if dry_run:
            continue
        try:
            reply = chat(build_prompt(target), cfg.generate_model)
        except Exception as exc:  # one failed call must not sink the whole run
            log.warning("generation call failed for %s: %s", target.subject, exc)
            result.rejected.append(f"{target.subject}: call failed ({exc})")
            continue
        for cand in parse_reply(reply)[:target.count]:
            n += 1
            t = _rename(cand, f"{batch}-{n:02d}")
            t["generated"] = batch
            if target.variant_of:
                t["variant_of"] = target.variant_of
            if (why := check(t, seen_instances, seen_text)) is not None:
                result.rejected.append(f"{t['id']}: {why}")
                continue
            accepted.append(t)
    passed = review(accepted, paths.reviews, chat, cfg.review_model)
    passed_ids = {t["id"] for t in passed}
    result.rejected += [f"{t['id']}: review failed" for t in accepted if t["id"] not in passed_ids]
    # Nothing goes live on its own: the same model wrote the question, its formula and the
    # review, so a parent looks at every batch before /quiz publish.
    result.pending = passed
    if passed:
        _write(paths.pending / f"{batch}.json", passed)
    return result


def _write(path: Path, templates: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(templates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)


def _load_with_extra(cfg: QuizSettings) -> QuizBank:
    paths = Paths(cfg.extra_bank_dir)
    return QuizBank.load(cfg.bank_dir, Path(cfg.bank_dir).parent / "reviews",
                         extra=((paths.bank, paths.reviews),))


# -- reminder (no LLM call) -----------------------------------------------------------
def suggestion(cfg: QuizSettings, store: Store, devices: list[str], now: datetime) -> str | None:
    """One line for the daily summary when new questions would help, or None."""
    paths = Paths(cfg.extra_bank_dir)
    try:
        targets = plan(read_templates(cfg.bank_dir, paths.bank, paths.pending), store, devices,
                       now, cfg, random.Random(0))
    except Exception as exc:  # the summary must never fail because of this
        log.warning("generation planning failed: %s", exc)
        return None
    if not targets:
        return None
    mistakes = sum(1 for t in targets if t.reason == "mistake")
    low = sorted({t.subject for t in targets if t.reason == "low_stock"})
    parts = []
    if mistakes:
        parts.append(f"{mistakes} 道错题还没有同类练习题")
    if low:
        parts.append("快做完的科目：" + "、".join(low))
    total = sum(t.count for t in targets)
    return (f"💡 建议补题：{'；'.join(parts)}。回复 /quiz generate 生成约 {total} 题"
            "（用 DeepSeek，生成后要你确认才上线）")


# -- parent actions (called by the daemon) ------------------------------------------
def _batch_path(cfg: QuizSettings, batch: str) -> Path | None:
    if not batch or any(c in batch for c in "/\\.") or not batch.startswith("gen-"):
        return None
    path = Paths(cfg.extra_bank_dir).pending / f"{batch}.json"
    return path if path.is_file() else None


def pending_batches(cfg: QuizSettings) -> dict[str, list[dict]]:
    paths = Paths(cfg.extra_bank_dir)
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(paths.pending.glob("gen-*.json"))}


def publish(cfg: QuizSettings, batch: str) -> tuple[int, str | None]:
    """Move a pending batch live. Returns (count, problem); the batch stays pending when the
    bank would not load with it."""
    src = _batch_path(cfg, batch)
    if src is None:
        return 0, "找不到这个批次"
    paths = Paths(cfg.extra_bank_dir)
    count = len(json.loads(src.read_text(encoding="utf-8")))
    dst = paths.bank / f"{batch}.json"
    _move(src, dst)
    try:
        _load_with_extra(cfg)
    except Exception as exc:
        _move(dst, src)
        return 0, f"题库加载失败，已保留在待确认：{str(exc)[:200]}"
    return count, None


def drop(cfg: QuizSettings, batch: str) -> bool:
    src = _batch_path(cfg, batch)
    if src is None:
        return False
    _move(src, Paths(cfg.extra_bank_dir).archive / f"{batch}.dropped.json")
    return True


def show(t: dict) -> str:
    """A template with its answer, for the parent to check in Telegram."""
    tag = " [已机器验算]" if auto_publishable(t) else ""
    head = f"{t['id']} {t.get('subject')}/{t.get('kind')} Y{t.get('grade')}{tag}"
    if t.get("answer_type") == "passage":
        lines = [head, (t.get("text") or "")[:300]]
        for part in t.get("parts", []):
            ans = next((o["text"] for o in part.get("options", []) if o.get("id") == part.get("answer")), "?")
            lines.append(f"  Q {part.get('prompt')} → {ans}")
        return "\n".join(lines)
    inst = (t.get("instances") or [{}])[0]
    try:
        prompt = t["prompt"].format_map(inst.get("params", {}))
    except (KeyError, ValueError, AttributeError, IndexError):
        prompt = t.get("prompt", "")
    ans = inst.get("answer")
    if t.get("answer_type") == "choice":
        ans = next((o["text"] for o in t.get("options", []) if o.get("id") == ans), ans)
    return f"{head}\n{prompt}\n  → {ans}"


def batch_buttons(batch: str, count: int) -> list[list[tuple[str, str]]]:
    """One-tap publish/drop for a batch (callback data becomes /quiz publish|drop <batch>)."""
    tag = batch.rsplit("-", 1)[-1]
    return [[(f"✅ 上线 {tag}（{count} 题）", f"quiz:publish {batch}"),
             (f"🗑 丢弃 {tag}", f"quiz:drop {batch}")]]


def summary(result: Result) -> str:
    lines = [f"🧠 新题生成 {result.batch}：待你确认 {len(result.pending)} 题，"
             f"未通过检查 {len(result.rejected)} 题"]
    if result.pending:
        lines.append(f"/quiz pending 查看题目和答案；/quiz publish {result.batch} 上线；"
                     f"/quiz drop {result.batch} 丢弃")
    return "\n".join(lines)
