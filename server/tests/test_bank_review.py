import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ltp.quiz.bank import BankError, QuizBank
from test_quiz_bank import choice_template, review_record, write_bank

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_bank.py"
spec = importlib.util.spec_from_file_location("review_bank", SCRIPT)
review_bank = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_bank)


def setup(tmp_path, templates):
    write_bank(tmp_path, templates)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    return reviews


def put(reviews, t, review_pass=1, **changes):
    rec = review_record(t, review_pass)
    rec.update(changes)
    name = f"{t['id']}.json" if review_pass == 1 else f"{t['id']}.pass2.json"
    (reviews / name).write_text(json.dumps(rec), encoding="utf-8")


def load_error(tmp_path, reviews):
    with pytest.raises(BankError) as exc:
        QuizBank.load(tmp_path, reviews)
    return str(exc.value)


def test_missing_review_rejected(tmp_path):
    reviews = setup(tmp_path, [choice_template("c1")])
    msg = load_error(tmp_path, reviews)
    assert "c1" in msg and "missing review" in msg


def test_stale_hash_rejected(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t)
    t["prompt"] = "Changed after review."
    write_bank(tmp_path, [t])
    assert "stale" in load_error(tmp_path, reviews)


def test_failed_verdict_rejected(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t, verdict="fail")
    assert "not pass" in load_error(tmp_path, reviews)


def test_unnamed_reviewer_rejected(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t, reviewer=" ")
    assert "does not name its reviewer" in load_error(tmp_path, reviews)


def test_any_named_reviewer_accepted(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t, reviewer="human")
    assert QuizBank.load(tmp_path, reviews).get("c1-1")


def test_challenge_needs_pass2(tmp_path):
    t = choice_template("c1")
    t["tier"] = "challenge"
    reviews = setup(tmp_path, [t])
    put(reviews, t)
    msg = load_error(tmp_path, reviews)
    assert "c1.pass2.json" in msg
    put(reviews, t, review_pass=2)
    assert QuizBank.load(tmp_path, reviews).get("c1-1")


def test_valid_review_accepted_and_math_needs_none(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t)
    assert QuizBank.load(tmp_path, reviews).get("c1-1")


def test_inline_review_rejected(tmp_path):
    t = choice_template("c1")
    t["review"] = {"by": "codex", "content_hash": "x"}
    reviews = setup(tmp_path, [t])
    put(reviews, t)
    assert "inline review is no longer used" in load_error(tmp_path, reviews)


# -- script ---------------------------------------------------------------

def good_reply(batch, model="gpt-test"):
    verdicts = {t["id"]: {"verdict": "pass", "checks": {k: True for k in review_bank.check_names(t)},
                          "notes": "ok"} for t in batch}
    return f"model: {model}\nthinking...\nREVIEW_JSON: {json.dumps(verdicts)}\n"


def test_script_writes_review_file_from_canned_reply(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return good_reply([t])

    rc = review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)], runner=runner)
    assert rc == 0 and len(prompts) == 1 and '"id": "c1"' in prompts[0]
    rec = json.loads((reviews / "c1.json").read_text(encoding="utf-8"))
    assert rec["model"] == "gpt-test" and rec["pass"] == 1 and rec["verdict"] == "pass"
    assert rec["reviewer"] == "codex" and rec["utc"].endswith("Z")
    assert QuizBank.load(tmp_path, reviews).get("c1-1")
    # second run: nothing stale or missing
    assert review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)],
                            runner=lambda p: pytest.fail("no reviewer call expected")) == 0


def test_script_failed_verdict_exit_nonzero_and_file_written(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    verdicts = {"c1": {"verdict": "fail", "checks": {k: k != "reading_level" for k in review_bank.check_names(t)},
                       "notes": "too hard"}}
    rc = review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)],
                          runner=lambda p: f"REVIEW_JSON: {json.dumps(verdicts)}")
    assert rc == 1
    rec = json.loads((reviews / "c1.json").read_text(encoding="utf-8"))
    assert rec["verdict"] == "fail" and rec["model"] == "unknown"


@pytest.mark.parametrize("reply", [
    "no marker here",
    "REVIEW_JSON: {not json",
    'REVIEW_JSON: {"other": {}}',
    'REVIEW_JSON: {"c1": {"verdict": "maybe", "checks": {}}}',
    'REVIEW_JSON: {"c1": {"verdict": "pass", "checks": {"answer_unique_correct": true}}}',
])
def test_script_unparseable_reply_writes_nothing(tmp_path, reply):
    reviews = setup(tmp_path, [choice_template("c1")])
    rc = review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)],
                          runner=lambda p: reply)
    assert rc == 1
    assert not list(reviews.iterdir())


def test_script_rejects_pass_with_false_check(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    checks = {k: False for k in review_bank.check_names(t)}
    reply = "REVIEW_JSON: " + json.dumps({"c1": {"verdict": "pass", "checks": checks, "notes": ""}})
    assert review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)],
                            runner=lambda p: reply) == 1
    assert not list(reviews.iterdir())


def test_script_batches_five_and_pass2_only_challenge(tmp_path):
    templates = [choice_template(f"c{i}") for i in range(7)]
    for i, t in enumerate(templates):
        t["prompt"] = f"Pick one {i}."
        if i < 6:
            t["tier"] = "challenge"
    reviews = setup(tmp_path, templates)
    sizes = []

    def runner(prompt):
        batch = [t for t in templates if f'"id": "{t["id"]}"' in prompt]
        sizes.append(len(batch))
        return good_reply(batch)

    args = ["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews)]
    assert review_bank.main(args, runner=runner) == 0
    assert sizes == [5, 2]
    sizes.clear()
    assert review_bank.main(args + ["--pass", "2"], runner=runner) == 0
    assert sizes == [5, 1]
    assert (reviews / "c0.pass2.json").is_file() and not (reviews / "c6.pass2.json").exists()
    assert json.loads((reviews / "c0.pass2.json").read_text(encoding="utf-8"))["pass"] == 2
    assert QuizBank.load(tmp_path, reviews).get("c0-1")


def test_script_stale_selection_and_never_edits_templates(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    put(reviews, t)
    t["prompt"] = "Edited."
    write_bank(tmp_path, [t])
    before = (tmp_path / "b.json").read_bytes()
    assert review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews), "--stale"],
                            runner=lambda p: good_reply([t])) == 0
    assert (tmp_path / "b.json").read_bytes() == before
    assert QuizBank.load(tmp_path, reviews).get("c1-1")


def test_pass2_prompt_is_adversarial_and_differs():
    t = choice_template("c1")
    assert "adversarial" in review_bank.build_prompt([t], 2)
    assert "adversarial" not in review_bank.build_prompt([t], 1)


def test_script_cmd_reviewer_records_its_name(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    reply = tmp_path / "reply.txt"
    reply.write_text(good_reply([t]), encoding="utf-8")
    script = tmp_path / "fake_llm.py"
    script.write_text(
        "import sys\nsys.stdin.read()\n"
        f"sys.stdout.write(open({str(reply)!r}, encoding='utf-8').read())\n", encoding="utf-8")
    cmd = f'"{sys.executable}" "{script}"'
    rc = review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews),
                           "--cmd", cmd, "--reviewer", "fake-llm"])
    assert rc == 0
    rec = json.loads((reviews / "c1.json").read_text(encoding="utf-8"))
    assert rec["reviewer"] == "fake-llm" and rec["model"] == "gpt-test"


def test_script_rejects_blank_reviewer(tmp_path):
    t = choice_template("c1")
    reviews = setup(tmp_path, [t])
    with pytest.raises(SystemExit):
        review_bank.main(["--bank-dir", str(tmp_path), "--reviews-dir", str(reviews), "--reviewer", " "],
                         runner=lambda p: pytest.fail("no reviewer call expected"))
    assert not (reviews / "c1.json").exists()
