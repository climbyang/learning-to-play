import copy
import json

import pytest

from ltp.quiz.bank import BankError, QuizBank, content_hash
from test_quiz_bank import base_template, load_reviewed, review_record, write_bank

SENTENCE = "The old lighthouse keeper was reluctant to leave his tower."


def make_text(words, sentence=SENTENCE):
    n = len(sentence.split())
    filler = " ".join(f"word{i}" for i in range(words - n))
    return f"{sentence} {filler}"


def opts(prefix, n):
    return [{"id": f"{prefix}{i}", "text": f"{prefix} option {i}"} for i in range(n)]


def part(pid, n_opts=4):
    return {"id": pid, "prompt": f"Question {pid}?", "options": opts(pid, n_opts),
            "answer": f"{pid}1", "explanation": f"Because {pid}."}


def seal(t):
    return copy.deepcopy(t)


def passage(tid="p1", tier="regular", words=150, n_parts=3, review=True):
    t = {
        "id": tid, "subject": "english", "kind": "reading", "tier": tier, "grade": 5,
        "est_minutes": 8, "answer_type": "passage", "text": make_text(words),
        "parts": [part(f"{tid}q{i}") for i in range(n_parts)],
        "vocab": [{"id": f"{tid}v1", "word": "reluctant", "meaning": "unwilling",
                   "sentence": SENTENCE, "check_prompt": "Reluctant means?",
                   "options": opts("v", 3), "answer": "v0"}],
        "hints": ["Reread the first sentence."],
    }
    return seal(t) if review else t


def load(tmp_path, templates, name="b.json"):
    (tmp_path / name).write_text(json.dumps(templates), encoding="utf-8")
    return load_reviewed(tmp_path)


def mutated(mutate, **kw):
    t = passage(review=False, **kw)
    mutate(t)
    return seal(t)


def expect_error(tmp_path, template, needle):
    with pytest.raises(BankError) as exc:
        load(tmp_path, [template])
    assert needle in str(exc.value)


def test_passage_loads_as_single_instance(tmp_path):
    bank = load(tmp_path, [passage()])
    insts = bank.instances("regular")
    assert len(insts) == 1
    inst = insts[0]
    assert inst.instance_id == inst.template_id == "p1"
    assert inst.answer_type == "passage" and inst.subject == "english"
    assert inst.passage["text"].startswith(SENTENCE)
    assert [p["id"] for p in inst.passage["parts"]] == ["p1q0", "p1q1", "p1q2"]
    assert inst.passage["parts"][0]["options"][0] == ("p1q00", "p1q0 option 0")
    assert inst.passage["vocab"][0]["options"][1] == ("v1", "v option 1")
    assert inst.passage["vocab"][0]["answer"] == "v0"
    assert inst.hints == ("Reread the first sentence.",)
    assert bank.get("p1") == inst


def test_challenge_passage_loads(tmp_path):
    for n in (4, 5):
        tid = f"c{n}"
        bank = load(tmp_path, [passage(tid, "challenge", words=300, n_parts=n)])
        assert len(bank.get(tid).passage["parts"]) == n


@pytest.mark.parametrize("words,ok", [(119, False), (120, True), (200, True), (201, False)])
def test_regular_word_bounds(tmp_path, words, ok):
    t = passage(words=words)
    if ok:
        load(tmp_path, [t])
    else:
        expect_error(tmp_path, t, "words")


@pytest.mark.parametrize("words,ok", [(249, False), (250, True), (350, True), (351, False)])
def test_challenge_word_bounds(tmp_path, words, ok):
    t = passage("c1", "challenge", words=words, n_parts=4)
    if ok:
        load(tmp_path, [t])
    else:
        expect_error(tmp_path, t, "words")


def test_word_count_uses_whitespace_tokens(tmp_path):
    t = passage(review=False)
    t["text"] = make_text(150).replace(" word1 ", "\n\nword1\t")
    load(tmp_path, [seal(t)])


@pytest.mark.parametrize("tier,n", [("regular", 2), ("regular", 4), ("challenge", 3), ("challenge", 6)])
def test_parts_count_by_tier(tmp_path, tier, n):
    words = 150 if tier == "regular" else 300
    expect_error(tmp_path, passage("x", tier, words=words, n_parts=n), "parts, got")


def test_part_needs_four_options(tmp_path):
    t = mutated(lambda t: t["parts"][0].update(options=opts("a", 3), answer="a1"))
    expect_error(tmp_path, t, "needs at least 4 options")


def test_vocab_check_needs_three_options(tmp_path):
    t = mutated(lambda t: t["vocab"][0].update(options=opts("v", 2)))
    expect_error(tmp_path, t, "needs at least 3 options")


def test_duplicate_option_ids(tmp_path):
    def m(t):
        t["parts"][0]["options"][1]["id"] = t["parts"][0]["options"][0]["id"]
    expect_error(tmp_path, mutated(m), "option ids must be unique")


def test_duplicate_option_texts_case_insensitive(tmp_path):
    def m(t):
        t["parts"][0]["options"][1]["text"] = "  " + t["parts"][0]["options"][0]["text"].upper()
    expect_error(tmp_path, mutated(m), "option texts must be unique")


def test_vocab_duplicate_option_texts(tmp_path):
    def m(t):
        t["vocab"][0]["options"][1]["text"] = t["vocab"][0]["options"][0]["text"]
    expect_error(tmp_path, mutated(m), "option texts must be unique")


def test_part_answer_not_option(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t["parts"][1].update(answer="zzz")), "not an option id")


def test_vocab_answer_not_option(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t["vocab"][0].update(answer="zzz")), "not an option id")


@pytest.mark.parametrize("key", ["prompt", "explanation"])
def test_part_empty_text_fields(tmp_path, key):
    expect_error(tmp_path, mutated(lambda t: t["parts"][0].update({key: "  "})), f"{key} must be")


def test_vocab_empty_meaning(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t["vocab"][0].update(meaning="")), "meaning must be")


def test_sentence_must_be_in_text(tmp_path):
    t = mutated(lambda t: t["vocab"][0].update(sentence="Reluctant words not in the text."))
    expect_error(tmp_path, t, "not a substring")


def test_word_must_be_in_sentence(tmp_path):
    t = mutated(lambda t: t["vocab"][0].update(word="lamp"))
    expect_error(tmp_path, t, "does not occur in its sentence")


def test_word_match_is_case_insensitive_whole_word(tmp_path):
    load(tmp_path, [mutated(lambda t: t["vocab"][0].update(word="RELUCTANT"))])
    t = mutated(lambda t: t["vocab"][0].update(word="reluct"))
    expect_error(tmp_path, t, "does not occur in its sentence")


def test_vocab_card_count(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t.update(vocab=[])), "1-2 vocab")

    def three(t):
        t["vocab"] = [dict(t["vocab"][0], id=f"v{i}") for i in range(3)]
    expect_error(tmp_path, mutated(three), "1-2 vocab")


def test_two_vocab_cards_and_zero_hints_ok(tmp_path):
    def m(t):
        t["vocab"].append(dict(t["vocab"][0], id="v2"))
        t["hints"] = []
    bank = load(tmp_path, [mutated(m)])
    assert len(bank.get("p1").passage["vocab"]) == 2 and bank.get("p1").hints == ()


def test_hints_at_most_two(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t.update(hints=["a", "b", "c"])), "hints")


def test_part_and_vocab_ids_unique_in_template(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t["parts"][1].update(id=t["parts"][0]["id"])), "ids must be unique")
    expect_error(tmp_path, mutated(lambda t: t["vocab"][0].update(id=t["parts"][0]["id"])), "ids must be unique")


def test_subject_must_be_english(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t.update(subject="logic")), "subject english")


def test_instance_id_clashing_with_other_template(tmp_path):
    other = base_template("p1-clash")
    other["instances"][0]["id"] = "p1"
    with pytest.raises(BankError) as exc:
        load(tmp_path, [other, passage("p1")])
    assert "duplicate instance id" in str(exc.value)


def test_identical_passage_text_across_files(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([passage("p1")]), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps([passage("p2")]), encoding="utf-8")
    with pytest.raises(BankError) as exc:
        load_reviewed(tmp_path)
    assert "identical passage text" in str(exc.value)


def test_identical_prompt_across_files(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([base_template("m1")]), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps([base_template("m2")]), encoding="utf-8")
    with pytest.raises(BankError) as exc:
        load_reviewed(tmp_path)
    assert "identical prompt" in str(exc.value)


def test_edit_invalidates_review(tmp_path):
    edits = [
        lambda t: t["parts"][0]["options"][2].update(text="changed text"),
        lambda t: t["hints"].append("x"),
        lambda t: t["vocab"][0].update(meaning="other"),
        lambda t: t.update(text=t["text"] + " extra"),
    ]
    for i, edit in enumerate(edits):
        d = tmp_path / f"case{i}"
        d.mkdir()
        t = passage()
        write_bank(d, [t])
        reviews = d / "reviews"
        reviews.mkdir()
        (reviews / "p1.json").write_text(json.dumps(review_record(t)), encoding="utf-8")
        edit(t)
        write_bank(d, [t])
        with pytest.raises(BankError) as exc:
            QuizBank.load(d, reviews)
        assert "stale" in str(exc.value)


def test_review_required(tmp_path):
    write_bank(tmp_path, [passage()])
    with pytest.raises(BankError) as exc:
        QuizBank.load(tmp_path, tmp_path / "reviews")
    assert "p1" in str(exc.value) and "missing review" in str(exc.value)


def test_passage_rejects_instances_key(tmp_path):
    expect_error(tmp_path, mutated(lambda t: t.update(instances=[])), "must not have instances")


@pytest.mark.parametrize("label,mutate", [
    ("part answer list", lambda t: t["parts"][0].update(answer=[1])),
    ("part answer dict", lambda t: t["parts"][0].update(answer={})),
    ("vocab answer dict", lambda t: t["vocab"][0].update(answer={})),
    ("vocab answer list", lambda t: t["vocab"][0].update(answer=["v0"])),
    ("tier list", lambda t: t.update(tier=[1])),
    ("tier dict", lambda t: t.update(tier={})),
    ("subject list", lambda t: t.update(subject=[1])),
    ("est NaN", lambda t: t.update(est_minutes=float("nan"))),
    ("est Infinity", lambda t: t.update(est_minutes=float("inf"))),
    ("part as string", lambda t: t.update(parts=["oops"] * 3)),
    ("parts not list", lambda t: t.update(parts="oops")),
    ("part options not list", lambda t: t["parts"][0].update(options="abcd")),
    ("option entry not object", lambda t: t["parts"][0].update(options=["a", "b", "c", "d"])),
    ("option id list", lambda t: t["parts"][0]["options"][0].update(id=[1])),
    ("option text number", lambda t: t["parts"][0]["options"][0].update(text=5)),
    ("part id list", lambda t: t["parts"][0].update(id=[1])),
    ("vocab not list", lambda t: t.update(vocab="oops")),
    ("vocab card string", lambda t: t.update(vocab=["oops"])),
    ("vocab id dict", lambda t: t["vocab"][0].update(id={})),
    ("vocab word number", lambda t: t["vocab"][0].update(word=7)),
    ("vocab sentence list", lambda t: t["vocab"][0].update(sentence=[1])),
    ("text number", lambda t: t.update(text=5)),
    ("hints string", lambda t: t.update(hints="oops")),
    ("hints non-string", lambda t: t.update(hints=[1])),
    ("grade string", lambda t: t.update(grade="5")),
    ("kind list", lambda t: t.update(kind=[1])),
])
def test_malformed_types_raise_bank_error(tmp_path, label, mutate):
    t = passage(review=False)
    mutate(t)
    t = seal(t) if label != "est NaN" and label != "est Infinity" else t
    with pytest.raises(BankError) as exc:
        load(tmp_path, [t])
    assert "p1" in str(exc.value)
