import copy
import json
from pathlib import Path

import pytest

from ltp.quiz.bank import BankError, QuizBank, check_answer, content_hash, required_passes

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_bank" / "bank"  # test-only sample bank
SAMPLE_REVIEWS = SAMPLE_DIR.parent / "reviews"


def review_record(t, review_pass=1, verdict="pass"):
    return {"template_id": t["id"], "content_hash": content_hash(t), "reviewer": "codex",
            "model": "test-model", "utc": "2026-11-21T00:00:00Z", "pass": review_pass,
            "verdict": verdict, "checks": {}, "notes": ""}


def write_reviews(bank_dir, reviews_dir):
    """Write valid review files for every template in bank_dir that needs them."""
    reviews_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(bank_dir.glob("*.json")):
        for t in json.loads(path.read_text(encoding="utf-8")):
            if not isinstance(t.get("id"), str):
                continue
            for n in required_passes(t):
                name = f"{t['id']}.json" if n == 1 else f"{t['id']}.pass2.json"
                (reviews_dir / name).write_text(
                    json.dumps(review_record(t, n)), encoding="utf-8")


def load_reviewed(bank_dir):
    """Load a tmp bank after writing matching review files next to it."""
    reviews = bank_dir / "reviews"
    write_reviews(bank_dir, reviews)
    return QuizBank.load(bank_dir, reviews)


def base_template(tid="t1"):
    return {
        "id": tid,
        "subject": "math",
        "kind": "multi_step",
        "tier": "regular",
        "grade": 4,
        "est_minutes": 3,
        "prompt": "A box holds {a} pens. Sam buys {b} boxes and gives away {c} pens. How many are left?",
        "hints": ["Find the total first."],
        "explanation": "a*b - c.",
        "answer_type": "integer",
        "plausible": {"min": 0, "max": 1000, "step": 1},
        "verify": "a * b - c",
        "instances": [{"id": f"{tid}-1", "params": {"a": 12, "b": 7, "c": 9}, "answer": "75"}],
    }


def write_bank(tmp_path, templates):
    (tmp_path / "b.json").write_text(json.dumps(templates), encoding="utf-8")
    return tmp_path


def choice_template(tid):
    t = {
        "id": tid,
        "subject": "logic",
        "kind": "deduction",
        "tier": "regular",
        "grade": 5,
        "est_minutes": 3,
        "prompt": "Pick one.",
        "hints": ["Think."],
        "explanation": "Because.",
        "answer_type": "choice",
        "options": [{"id": k, "text": k.upper()} for k in "abcde"],
        "instances": [{"id": f"{tid}-1", "params": {}, "answer": "c"}],
    }
    return t


def test_sample_bank_loads():
    bank = QuizBank.load(SAMPLE_DIR, SAMPLE_REVIEWS)
    assert bank.templates()
    regular = bank.instances("regular")
    challenge = bank.instances("challenge")
    assert len(challenge) >= 2 and regular
    subjects = {i.subject for i in regular + challenge}
    assert subjects == {"math", "logic", "english"}
    assert any(i.answer_type == "decimal" for i in regular + challenge)
    assert any(len(i.options) >= 5 for i in regular + challenge)
    first = regular[0]
    assert bank.get(first.instance_id) == first
    assert bank.get("nope") is None
    assert "{" not in first.prompt


def test_parameter_substitution_and_hash(tmp_path):
    t = base_template()
    bank = load_reviewed(write_bank(tmp_path, [t]))
    inst = bank.get("t1-1")
    assert "12 pens" in inst.prompt and "7 boxes" in inst.prompt
    assert inst.content_hash == content_hash(t)
    assert inst.answer == "75"


def test_good_choice_with_review(tmp_path):
    t = choice_template("c1")
    bank = load_reviewed(write_bank(tmp_path, [t]))
    assert len(bank.get("c1-1").options) == 5


def _verify_mismatch(t):
    t["instances"][0]["answer"] = "76"


def _range_19(t):
    t["plausible"] = {"min": 0, "max": 18, "step": 1}
    t["instances"][0]["params"] = {"a": 2, "b": 5, "c": 1}
    t["instances"][0]["answer"] = "9"


def _out_of_range(t):
    t["plausible"] = {"min": 0, "max": 50, "step": 1}


def _four_options(t):
    t.clear()
    t.update(choice_template("tpl-zq"))
    t["options"] = t["options"][:4]


def _import_call(t):
    t["verify"] = "__import__('os').getcwd()"


def _pow_big(t):
    t["verify"] = "a ** 11"


def _missing_param(t):
    t["prompt"] = "Uses {zzz}."


def _dup_instance(t):
    t["instances"].append(copy.deepcopy(t["instances"][0]))


def _float_str(t):
    t["verify"] = "'5'"


def _max_inf(t):
    t["plausible"] = {"min": 0, "max": float("inf"), "step": 1}


def _step_nan(t):
    t["plausible"] = {"min": 0, "max": 50, "step": float("nan")}


def _min_nan(t):
    t["plausible"] = {"min": float("nan"), "max": 50, "step": 1}


def _param_inf(t):
    t["instances"][0]["params"]["a"] = float("inf")


def _param_nan(t):
    t["instances"][0]["params"]["b"] = float("nan")


@pytest.mark.parametrize(
    "mutate",
    [
        _max_inf, _step_nan, _min_nan, _param_inf, _param_nan,
        _verify_mismatch, _range_19, _out_of_range, _four_options,
        _import_call, _pow_big, _missing_param, _dup_instance, _float_str,
    ],
)
def test_bank_rejects(tmp_path, mutate):
    t = base_template("tpl-zq")
    mutate(t)
    t["id"] = "tpl-zq"
    with pytest.raises(BankError) as ei:
        load_reviewed(write_bank(tmp_path, [t]))
    assert any(p.startswith("tpl-zq:") for p in ei.value.problems)


def test_range_20_accepted(tmp_path):
    t = base_template("tpl-zq")
    t["plausible"] = {"min": 0, "max": 19, "step": 1}
    t["instances"][0]["params"] = {"a": 2, "b": 5, "c": 1}
    t["instances"][0]["answer"] = "9"
    load_reviewed(write_bank(tmp_path, [t]))


def test_duplicate_instance_id_across_templates(tmp_path):
    a, b = base_template("a"), base_template("b")
    b["instances"][0]["id"] = "a-1"
    with pytest.raises(BankError) as ei:
        load_reviewed(write_bank(tmp_path, [a, b]))
    assert any("a-1" in p for p in ei.value.problems)


def test_decimal_verify_exact(tmp_path):
    t = base_template("d")
    t["answer_type"] = "decimal"
    t["verify"] = "a / b"
    t["plausible"] = {"min": 0, "max": 10, "step": 0.1}
    t["instances"][0].update(params={"a": 3, "b": 2, "c": 0}, answer="1.5")
    load_reviewed(write_bank(tmp_path, [t]))
    t["instances"][0]["answer"] = "1.4"
    with pytest.raises(BankError):
        load_reviewed(write_bank(tmp_path, [t]))


def test_empty_directory_rejected(tmp_path):
    with pytest.raises(BankError):
        load_reviewed(tmp_path)


@pytest.mark.parametrize(
    "answer_type,expected,given,result",
    [
        ("integer", "12", " 012 ", True),
        ("integer", "-3", "-3", True),
        ("integer", "12", "１２", None),
        ("integer", "12", "", None),
        ("integer", "12", "9" * 65, None),
        ("integer", "12", "13", False),
        ("decimal", "1.5", "1.50", True),
        ("decimal", "1.5", "1,5", None),
        ("decimal", "1.5", "1.49", False),
        ("choice", "c", "c", True),
        ("choice", "c", "d", False),
    ],
)
def test_check_answer(answer_type, expected, given, result):
    assert check_answer(answer_type, expected, given) is result


@pytest.mark.parametrize("old,new", [('"a": 12', '"a": 1e999'), ('"max": 1000', '"max": 1e999')])
def test_bank_rejects_1e999_literal(tmp_path, old, new):
    text = json.dumps([base_template("tpl-zq")]).replace(old, new)
    assert "1e999" in text
    (tmp_path / "b.json").write_text(text, encoding="utf-8")
    with pytest.raises(BankError):
        load_reviewed(tmp_path)


@pytest.mark.parametrize("value,ok", [(8, True), (5, True), (600, True), (4, False),
                                      (601, False), ("8", False), (True, False), (8.5, False)])
def test_min_seconds_is_optional_and_bounded(tmp_path, value, ok):
    from ltp.quiz.bank import _validate_template
    t = base_template()
    t["min_seconds"] = value
    problems, built = _validate_template(t, {})
    if ok:
        assert problems == [] and built[0].min_seconds == value
    else:
        assert any("min_seconds" in p for p in problems)
    problems, built = _validate_template(base_template("t2"), {})
    assert problems == [] and built[0].min_seconds is None
