"""The production bank (server/quiz/bank + reviews) loads cleanly and meets size targets.

Every other test uses the test-only sample bank in tests/sample_bank; this is the one place that
checks production content.
"""
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from ltp.quiz.bank import QuizBank
from ltp.quiz.chesscheck import chess_problem, fen_problem

PROD_BANK = Path(__file__).resolve().parents[1] / "quiz" / "bank"
PROD_REVIEWS = PROD_BANK.parent / "reviews"


@pytest.fixture(scope="module")
def bank():
    # QuizBank.load raises BankError listing every problem, so a return means zero errors.
    return QuizBank.load(PROD_BANK, PROD_REVIEWS)


def templates(bank, tier, pred=lambda i: True):
    return {i.template_id for i in bank.instances(tier) if pred(i)}


def english_passage(i):
    return i.subject == "english" and i.answer_type == "passage"


def test_loads_with_minimum_template_counts(bank):
    assert len(templates(bank, "regular")) >= 100
    assert len(templates(bank, "challenge")) >= 30


def test_minimum_english_passage_sets(bank):
    assert len(templates(bank, "regular", english_passage)) >= 35
    assert len(templates(bank, "challenge", english_passage)) >= 10


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def figure_problem(raw):
    """Why the TV app (tv-app/quiz/src/figure.ts) could not render this figure, or None."""
    try:
        spec = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return f"not JSON: {exc}"
    if not isinstance(spec, dict):
        return "not an object"
    if spec.get("type") == "bar":
        if "data" in spec:
            data = spec["data"]
            ok = (isinstance(data, list) and data
                  and all(isinstance(d, dict) and "label" in d and _number(d.get("value"))
                          for d in data))
            return None if ok else "bar data must be a non-empty list of {label, value>=0}"
        labels, values = spec.get("labels"), spec.get("values")
        ok = (isinstance(labels, list) and isinstance(values, list) and labels
              and len(labels) == len(values) and all(_number(v) for v in values))
        return None if ok else "bar needs data[] or equal-length labels[]/values[>=0]"
    if spec.get("type") == "chess":
        return fen_problem(spec.get("fen"))
    if spec.get("type") == "table":
        headers, rows = spec.get("headers"), spec.get("rows")
        ok = (isinstance(headers, list) and headers and isinstance(rows, list) and rows
              and all(isinstance(r, list) for r in rows))
        return None if ok else "table needs headers[] and rows[[...]]"
    return f"unknown type {spec.get('type')!r}"


def test_every_figure_renders_in_the_app(bank):
    bad = {}
    for tier in ("regular", "challenge"):
        for i in bank.instances(tier):
            if i.figure is not None:
                problem = figure_problem(i.figure)
                if problem:
                    bad[i.instance_id] = problem
    assert bad == {}


def test_chess_answers_are_the_only_winning_move(bank):
    chess_items = [i for tier in ("regular", "challenge") for i in bank.instances(tier)
                   if i.kind.startswith("chess_")]
    bad = {i.instance_id: p for i in chess_items if (p := chess_problem(i))}
    assert bad == {}


def test_chess_checker_catches_wrong_key():
    from ltp.quiz.bank import Instance
    fen = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"  # Ra8 is mate
    opts = (("a", "Ra8"), ("b", "Ra7"), ("c", "Kf2"), ("d", "Ra2"), ("e", "Kg2"))
    good = Instance("t", "t-1", "logic", "chess_mate1", "regular", "", json.dumps(
        {"type": "chess", "fen": fen}), (), "", "choice", "a", opts, "h")
    assert chess_problem(good) is None
    assert chess_problem(replace(good, answer="b")) is not None
    assert chess_problem(replace(good, options=opts[:4] + (("e", "Ra8"),))) is not None


def test_figure_checker_rejects_bad_shapes():
    assert figure_problem('{"type":"bar","data":[{"label":"a","value":2}]}') is None
    assert figure_problem('{"type":"bar","labels":["a"],"values":[1]}') is None
    assert figure_problem('{"type":"table","headers":["x"],"rows":[[1]]}') is None
    assert figure_problem('{"type":"chess","fen":"6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"}') is None
    for raw in ('{"type":"chess","fen":"bad"}', '{"type":"chess"}', "nope", "[]", '{"type":"pie"}', '{"type":"bar","data":[]}',
                '{"type":"bar","labels":["a"],"values":[-1]}', '{"type":"table","headers":["x"]}'):
        assert figure_problem(raw) is not None, raw
