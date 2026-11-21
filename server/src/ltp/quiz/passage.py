"""Pure helpers for passage-set attempts.

A passage attempt keeps its per-part and per-card state in `Attempt.progress`:
`{"v": 1, "parts": [{"given", "correct"}...], "vocab": [{"wrong", "done", "need_review",
"last_utc"}...], "parts_done_utc"}`. `Attempt.answer` holds the correct ids
(`{"parts": [...], "vocab": [...]}`) and `Attempt.option_order` the shuffled option ids
per part and per card. Everything here is side-effect free; the service applies changes.
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from ltp.quiz.bank import Instance
from ltp.store import Attempt

PROGRESS_VERSION = 1
PASSAGE = "passage"


def is_passage(a: Attempt) -> bool:
    return a.snapshot.get("answer_type") == PASSAGE


def _opts(pairs) -> list[dict]:
    return [{"id": oid, "text": text} for oid, text in pairs]


def build(inst: Instance, rng: random.Random) -> tuple[dict, dict, dict, dict]:
    """Snapshot additions, answer, option order and initial progress for a new attempt."""
    p = inst.passage or {}
    parts = [{"id": x["id"], "prompt": x["prompt"], "options": _opts(x["options"]),
              "explanation": x["explanation"]} for x in p["parts"]]
    vocab = [{"id": c["id"], "word": c["word"], "meaning": c["meaning"],
              "sentence": c["sentence"], "check_prompt": c["check_prompt"],
              "options": _opts(c["options"])} for c in p["vocab"]]
    answer = {"parts": [x["answer"] for x in p["parts"]],
              "vocab": [c["answer"] for c in p["vocab"]]}

    def shuffled(items: list[dict]) -> list[list[str]]:
        out = []
        for item in items:
            ids = [o["id"] for o in item["options"]]
            rng.shuffle(ids)
            out.append(ids)
        return out

    order = {"parts": shuffled(parts), "vocab": shuffled(vocab)}
    progress = {"v": PROGRESS_VERSION, "parts": [],
                "vocab": [{"wrong": 0, "done": False, "need_review": False, "last_utc": None}
                          for _ in vocab],
                "parts_done_utc": None}
    return {"text": p["text"], "parts": parts, "vocab": vocab}, answer, order, progress


def parse_utc(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


def parts_left(a: Attempt) -> bool:
    return len(a.progress["parts"]) < len(a.snapshot["parts"])


def current_card(a: Attempt) -> int | None:
    """Index of the first vocab card not yet done, or None when all are done."""
    return next((i for i, c in enumerate(a.progress["vocab"]) if not c["done"]), None)


def stage(a: Attempt) -> str:
    if a.state != "issued":
        return "done"
    if parts_left(a):
        return "parts"
    return "vocab" if current_card(a) is not None else "done"


def current_step(a: Attempt) -> int:
    """Set-wide step: parts first, then vocab cards; a wrong card answer keeps the step."""
    n_parts = len(a.snapshot["parts"])
    if parts_left(a):
        return len(a.progress["parts"])
    card = current_card(a)
    return n_parts + (len(a.progress["vocab"]) if card is None else card)


def wrong_parts(a: Attempt) -> int:
    return sum(1 for p in a.progress["parts"] if not p["correct"])


def last_vocab_utc(a: Attempt) -> datetime | None:
    stamps = [parse_utc(c["last_utc"]) for c in a.progress["vocab"] if c["last_utc"]]
    return max(stamps) if stamps else None


def with_part(progress: dict, given: str, correct: bool, n_parts: int,
              now: datetime) -> dict:
    new = {**progress, "parts": [*progress["parts"], {"given": given, "correct": correct}]}
    if len(new["parts"]) == n_parts:
        new["parts_done_utc"] = now.isoformat()
    return new


def with_card(progress: dict, index: int, **changes: Any) -> dict:
    cards = [dict(c) for c in progress["vocab"]]
    cards[index].update(changes)
    return {**progress, "vocab": cards}


def _ordered(options: list[dict], order: list[str]) -> list[dict]:
    by_id = {o["id"]: o for o in options}
    return [dict(by_id[oid]) for oid in order]


def view_fields(a: Attempt) -> dict[str, Any]:
    """Passage-specific AttemptView fields. Never leaks an unanswered part's answer while
    issued, nor vocab cards before the vocab stage; vocab answers are never returned."""
    snap, prog, order = a.snapshot, a.progress, a.option_order
    terminal = a.state != "issued"
    st = stage(a)
    answered = prog["parts"]
    parts = []
    for i, part in enumerate(snap["parts"]):
        item: dict[str, Any] = {"prompt": part["prompt"],
                                "options": _ordered(part["options"], order["parts"][i])}
        if i < len(answered):
            item["status"] = "right" if answered[i]["correct"] else "wrong"
            item["given"] = answered[i]["given"]
        else:
            item["status"] = "current" if (i == len(answered) and not terminal) else "pending"
        if i < len(answered) or terminal:
            item["correct"] = a.answer["parts"][i]
            item["explanation"] = part["explanation"]
        parts.append(item)
    if st == "parts":
        shown = 0
    elif st == "vocab":
        shown = (current_card(a) or 0) + 1
    else:
        shown = len(snap["vocab"])
    vocab = [{"word": c["word"], "meaning": c["meaning"], "sentence": c["sentence"],
              "check_prompt": c["check_prompt"],
              "options": _ordered(c["options"], order["vocab"][i]),
              "done": prog["vocab"][i]["done"], "need_review": prog["vocab"][i]["need_review"]}
             for i, c in enumerate(snap["vocab"][:shown])]
    return {"passage": snap["text"], "stage": st, "step": current_step(a), "parts": parts,
            "vocab": vocab, "parts_right": sum(1 for p in answered if p["correct"]),
            "parts_total": len(snap["parts"])}
