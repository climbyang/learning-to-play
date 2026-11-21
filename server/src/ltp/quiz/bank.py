"""Quiz bank: load, validate and verify question templates."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path

SUBJECTS = {"math", "logic", "english"}
TIERS = {"regular", "challenge"}
ANSWER_TYPES = {"integer", "decimal", "choice", "passage"}
MIN_ANSWER_SPACE = 20
MIN_OPTIONS = 5
MAX_ANSWER_LEN = 64
MAX_POW_EXPONENT = 10
# passage sets: tier -> (min words, max words, min parts, max parts)
PASSAGE_LIMITS = {"regular": (120, 200, 3, 3), "challenge": (250, 350, 4, 5)}
MIN_PART_OPTIONS = 4
MIN_VOCAB_OPTIONS = 3
MAX_PASSAGE_HINTS = 2

_INT_RE = re.compile(r"-?[0-9]+")
_DEC_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")


class BankError(ValueError):
    """Raised when the bank fails validation; each problem names its template id."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Instance:
    template_id: str
    instance_id: str
    subject: str
    kind: str
    tier: str
    prompt: str
    figure: str | None
    hints: tuple[str, ...]
    explanation: str
    answer_type: str
    answer: str
    options: tuple[tuple[str, str], ...]
    content_hash: str
    passage: dict | None = None


def content_hash(template: dict) -> str:
    """Hash of a template (a legacy inline review key is excluded)."""
    body = {k: v for k, v in template.items() if k != "review"}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_answer(answer_type: str, expected: str, given: str) -> bool | None:
    """True/False for a judged answer, None when the input format is invalid."""
    if not isinstance(given, str):
        return None
    given = given.strip()
    if not given or len(given) > MAX_ANSWER_LEN:
        return None
    if answer_type == "integer":
        if not given.isascii() or not _INT_RE.fullmatch(given):
            return None
        return int(given) == int(expected)
    if answer_type == "decimal":
        if not given.isascii() or not _DEC_RE.fullmatch(given):
            return None
        return Decimal(given) == Decimal(expected)
    if answer_type == "choice":
        return given == expected
    raise ValueError(f"unknown answer_type {answer_type!r}")


# ---------------------------------------------------------------- verify

class _VerifyError(ValueError):
    pass


def _to_fraction(value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _VerifyError(f"unsupported value {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise _VerifyError(f"non-finite value {value!r}")
    return Fraction(str(value))


def _eval(node: ast.AST, params: dict) -> Fraction:
    if isinstance(node, ast.Expression):
        return _eval(node.body, params)
    if isinstance(node, ast.Constant):
        return _to_fraction(node.value)
    if isinstance(node, ast.Name):
        if node.id not in params:
            raise _VerifyError(f"unknown name {node.id!r}")
        return _to_fraction(params[node.id])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, params)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, params), _eval(node.right, params)
        op = node.op
        try:
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return Fraction(left // right)
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                if right.denominator != 1 or abs(right) > MAX_POW_EXPONENT:
                    raise _VerifyError("exponent must be an integer with |n| <= 10")
                return left ** int(right)
        except ZeroDivisionError as exc:
            raise _VerifyError("division by zero") from exc
    raise _VerifyError(f"disallowed expression node {type(node).__name__}")


def _verified_value(expr: str, params: dict) -> Fraction:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise _VerifyError(f"verify syntax error: {exc.msg}") from exc
    return _eval(tree, params)


def _fraction_to_decimal_str(value: Fraction) -> str | None:
    """Exact decimal string, or None when the value does not terminate."""
    with localcontext() as ctx:
        ctx.prec = 200
        d = Decimal(value.numerator) / Decimal(value.denominator)
        if Fraction(d) != value:
            return None
        return format(d, "f")


# ---------------------------------------------------------------- validation

def _count_answer_space(plausible: dict) -> int:
    raw = [plausible[k] for k in ("min", "max", "step")]
    if any(isinstance(v, float) and not math.isfinite(v) for v in raw):
        raise ValueError("plausible values must be finite numbers")
    lo, hi, step = (Decimal(str(v)) for v in raw)
    if step <= 0 or hi < lo:
        raise ValueError("plausible needs step > 0 and max >= min")
    return int((hi - lo) // step) + 1


def needs_review(t: dict) -> bool:
    """english and choice templates must carry stored review files (see scripts/review_bank.py)."""
    return t.get("subject") == "english" or t.get("answer_type") == "choice"


def required_passes(t: dict) -> tuple[int, ...]:
    """Review passes a template needs: pass 1 always, pass 2 as well for the challenge tier."""
    if not needs_review(t):
        return ()
    return (1, 2) if t.get("tier") == "challenge" else (1,)


def review_path(reviews_dir: Path, template_id: str, review_pass: int) -> Path:
    suffix = ".json" if review_pass == 1 else ".pass2.json"
    return Path(reviews_dir) / f"{template_id}{suffix}"


def review_problem(t: dict, reviews_dir: Path, review_pass: int) -> str | None:
    """Why the stored review for this pass is unacceptable, or None when it proves the review."""
    path = review_path(reviews_dir, t["id"], review_pass)
    if not path.is_file():
        return f"missing review file {path.name} (pass {review_pass})"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable review file {path.name} ({exc})"
    if not isinstance(rec, dict):
        return f"review file {path.name} must contain a JSON object"
    if rec.get("template_id") != t["id"] or rec.get("pass") != review_pass:
        return f"review file {path.name} does not match template id / pass {review_pass}"
    if not isinstance(rec.get("reviewer"), str) or not rec["reviewer"].strip():
        return f"review file {path.name} does not name its reviewer"
    if rec.get("content_hash") != content_hash(t):
        return f"review file {path.name} is stale (template changed after review)"
    if rec.get("verdict") != "pass":
        return f"review file {path.name} verdict is {rec.get('verdict')!r}, not pass"
    return None


def _check_reviews(t: dict, reviews_dir: Path) -> list[str]:
    problems = []
    for review_pass in required_passes(t):
        why = review_problem(t, reviews_dir, review_pass)
        if why:
            problems.append(f"{t['id']}: {why}")
    return problems


def _validate_options(raw: object, minimum: int, label: str, bad) -> list[tuple[str, str]]:
    """Validate an option list; returns (id, text) tuples (empty when malformed)."""
    if not isinstance(raw, list) or not all(
        isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"]
        and isinstance(o.get("text"), str) and o["text"].strip()
        for o in raw
    ):
        bad(f"{label}: options must be a list of {{id, text}} with non-empty strings")
        return []
    if len(raw) < minimum:
        bad(f"{label}: needs at least {minimum} options, got {len(raw)}")
    if len({o["id"] for o in raw}) != len(raw):
        bad(f"{label}: option ids must be unique")
    if len({o["text"].strip().lower() for o in raw}) != len(raw):
        bad(f"{label}: option texts must be unique")
    return [(o["id"], o["text"]) for o in raw]


def _nonempty(d: dict, key: str) -> bool:
    return isinstance(d.get(key), str) and bool(d[key].strip())


def _validate_passage(t: dict, bad, seen_instances: dict[str, str]) -> Instance | None:
    """Validate a passage-set template; build its single Instance (id == template id)."""
    tid, tier, text = t["id"], t.get("tier"), t.get("text")
    n_before = len(bad.problems)
    pmin = pmax = None
    if not isinstance(text, str) or not text.strip():
        bad("passage text must be a non-empty string")
        text = ""
    elif isinstance(tier, str) and tier in PASSAGE_LIMITS:
        lo, hi, pmin, pmax = PASSAGE_LIMITS[tier]
        words = len(text.split())
        if not lo <= words <= hi:
            bad(f"passage has {words} words, {tier} tier needs {lo}-{hi}")
    if t.get("subject") != "english":
        bad("passage templates must have subject english")
    if not isinstance(tier, str) or tier not in TIERS:
        bad(f"tier must be one of {sorted(TIERS)}")
    for key in ("instances", "params", "verify", "plausible"):
        if key in t:
            bad(f"passage templates must not have {key}")
    if not _nonempty(t, "kind"):
        bad("kind must be a non-empty string")
    if not isinstance(t.get("grade"), int) or isinstance(t.get("grade"), bool):
        bad("grade must be an integer")
    est = t.get("est_minutes")
    if isinstance(est, bool) or not isinstance(est, (int, float)) or est <= 0 or not math.isfinite(est):
        bad("est_minutes must be a positive number")

    raw_parts = t.get("parts")
    parts: list[dict] = []
    if not isinstance(raw_parts, list) or not all(isinstance(p, dict) for p in raw_parts):
        bad("parts must be a list of objects")
    else:
        if pmin is not None and not pmin <= len(raw_parts) <= pmax:
            want = str(pmin) if pmin == pmax else f"{pmin}-{pmax}"
            bad(f"{tier} passage needs {want} parts, got {len(raw_parts)}")
        for p in raw_parts:
            pid = p.get("id")
            if not isinstance(pid, str) or not pid:
                bad("part without a string id")
                continue
            for key in ("prompt", "explanation"):
                if not _nonempty(p, key):
                    bad(f"part {pid}: {key} must be a non-empty string")
            opts = _validate_options(p.get("options"), MIN_PART_OPTIONS, f"part {pid}", bad)
            if opts and p.get("answer") not in [oid for oid, _ in opts]:
                bad(f"part {pid}: answer {p.get('answer')!r} is not an option id")
            parts.append({"id": pid, "prompt": p.get("prompt"), "options": opts,
                          "answer": p.get("answer"), "explanation": p.get("explanation")})

    raw_vocab = t.get("vocab")
    vocab: list[dict] = []
    if not isinstance(raw_vocab, list) or not all(isinstance(v, dict) for v in raw_vocab):
        bad("vocab must be a list of objects")
    else:
        if not 1 <= len(raw_vocab) <= 2:
            bad(f"passage needs 1-2 vocab cards, got {len(raw_vocab)}")
        for v in raw_vocab:
            vid = v.get("id")
            if not isinstance(vid, str) or not vid:
                bad("vocab card without a string id")
                continue
            for key in ("word", "meaning", "sentence", "check_prompt"):
                if not _nonempty(v, key):
                    bad(f"vocab {vid}: {key} must be a non-empty string")
            sentence, word = v.get("sentence"), v.get("word")
            if _nonempty(v, "sentence"):
                if sentence not in text:
                    bad(f"vocab {vid}: sentence is not a substring of the passage text")
                if _nonempty(v, "word") and not re.search(
                    r"(?<!\w)" + re.escape(word.strip()) + r"(?!\w)", sentence, re.IGNORECASE
                ):
                    bad(f"vocab {vid}: word {word!r} does not occur in its sentence")
            opts = _validate_options(v.get("options"), MIN_VOCAB_OPTIONS, f"vocab {vid}", bad)
            if opts and v.get("answer") not in [oid for oid, _ in opts]:
                bad(f"vocab {vid}: answer {v.get('answer')!r} is not an option id")
            vocab.append({"id": vid, "word": word, "meaning": v.get("meaning"), "sentence": sentence,
                          "check_prompt": v.get("check_prompt"), "options": opts,
                          "answer": v.get("answer")})

    all_ids = [x["id"] for x in parts + vocab]
    if len(set(all_ids)) != len(all_ids):
        bad("part and vocab ids must be unique within the template")

    hints = t.get("hints", [])
    if not isinstance(hints, list) or len(hints) > MAX_PASSAGE_HINTS or not all(
        isinstance(h, str) and h.strip() for h in hints
    ):
        bad(f"hints must be 0-{MAX_PASSAGE_HINTS} non-empty strings")
        hints = []

    if tid in seen_instances:
        bad(f"duplicate instance id {tid!r} (also in {seen_instances[tid]})")
    else:
        seen_instances[tid] = tid
    if len(bad.problems) > n_before:
        return None
    return Instance(
        template_id=tid, instance_id=tid, subject="english", kind=t["kind"], tier=tier,
        prompt="", figure=None, hints=tuple(hints), explanation="", answer_type="passage",
        answer="", options=(), content_hash=content_hash(t),
        passage={"text": text, "parts": parts, "vocab": vocab},
    )


class _Problems:
    """Callable collector that prefixes each message with the template id."""

    def __init__(self, tid: str, sink: list[str]):
        self.tid = tid
        self.problems = sink

    def __call__(self, msg: str) -> None:
        self.problems.append(f"{self.tid}: {msg}")


def _validate_template(t: dict, seen_instances: dict[str, str]) -> tuple[list[str], list[Instance]]:
    tid = t.get("id")
    if not isinstance(tid, str) or not tid:
        return [f"<unknown template>: missing id in {str(t)[:60]}"], []
    problems: list[str] = []
    bad = _Problems(tid, problems)
    if "review" in t:
        bad(f"inline review is no longer used; review is now proven by server/quiz/reviews/{tid}.json")

    if t.get("answer_type") == "passage":
        inst = _validate_passage(t, bad, seen_instances)
        return problems, [inst] if inst else []

    subject, tier, atype = t.get("subject"), t.get("tier"), t.get("answer_type")
    if not isinstance(subject, str) or subject not in SUBJECTS:
        bad(f"subject must be one of {sorted(SUBJECTS)}")
    if not isinstance(tier, str) or tier not in TIERS:
        bad(f"tier must be one of {sorted(TIERS)}")
    if not isinstance(atype, str) or atype not in ANSWER_TYPES:
        bad(f"answer_type must be one of {sorted(ANSWER_TYPES)}")
    for key in ("kind", "prompt", "explanation"):
        if not isinstance(t.get(key), str) or not t[key].strip():
            bad(f"{key} must be a non-empty string")
    if not isinstance(t.get("grade"), int) or isinstance(t.get("grade"), bool):
        bad("grade must be an integer")
    est = t.get("est_minutes")
    if isinstance(est, bool) or not isinstance(est, (int, float)) or est <= 0 or not math.isfinite(est):
        bad("est_minutes must be a positive number")
    hints = t.get("hints")
    if not isinstance(hints, list) or not 1 <= len(hints) <= 3 or not all(
        isinstance(h, str) and h.strip() for h in hints
    ):
        bad("hints must be 1-3 non-empty strings")
    figure = t.get("figure")
    if figure is not None and not isinstance(figure, str):
        bad("figure must be a string when present")

    options: list = []
    plausible = None
    if atype == "choice":
        options = t.get("options") or []
        if not isinstance(options, list) or not all(
            isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("text"), str)
            for o in options
        ):
            bad("options must be a list of {id, text}")
            options = []
        else:
            if len(options) < MIN_OPTIONS:
                bad(f"choice questions need at least {MIN_OPTIONS} options, got {len(options)}")
            if len({o["id"] for o in options}) != len(options):
                bad("option ids must be unique")
    elif atype in ("integer", "decimal"):
        raw_plausible = t.get("plausible")
        if not isinstance(raw_plausible, dict) or not all(
            isinstance(raw_plausible.get(k), (int, float)) and not isinstance(raw_plausible.get(k), bool)
            for k in ("min", "max", "step")
        ):
            bad("numeric questions need plausible {min, max, step}")
        else:
            try:
                count = _count_answer_space(raw_plausible)
            except (ValueError, ArithmeticError) as exc:
                bad(str(exc))
            else:
                plausible = raw_plausible
                if count < MIN_ANSWER_SPACE:
                    bad(f"answer space {count} < {MIN_ANSWER_SPACE}")
        if not isinstance(t.get("verify"), str) or not t["verify"].strip():
            bad("numeric questions need a verify expression")

    raw_instances = t.get("instances")
    if not isinstance(raw_instances, list) or not raw_instances:
        bad("instances must be a non-empty list")
        return problems, []

    chash = content_hash(t)
    built: list[Instance] = []
    for inst in raw_instances:
        iid = inst.get("id") if isinstance(inst, dict) else None
        if not isinstance(iid, str) or not iid:
            bad("instance without a string id")
            continue
        if iid in seen_instances:
            bad(f"duplicate instance id {iid!r} (also in {seen_instances[iid]})")
            continue
        seen_instances[iid] = tid
        params, answer = inst.get("params", {}), inst.get("answer")
        if not isinstance(params, dict) or not isinstance(answer, str) or not answer:
            bad(f"instance {iid}: params must be an object and answer a non-empty string")
            continue
        try:
            prompt = t["prompt"].format_map(params) if isinstance(t.get("prompt"), str) else ""
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            bad(f"instance {iid}: prompt substitution failed ({exc!r})")
            continue

        if atype == "choice":
            if answer not in {o["id"] for o in options}:
                bad(f"instance {iid}: answer {answer!r} is not an option id")
                continue
        elif atype in ("integer", "decimal"):
            if check_answer(atype, answer, answer) is not True:
                bad(f"instance {iid}: answer {answer!r} is not a valid {atype}")
                continue
            if plausible is not None:
                lo, hi = Decimal(str(plausible["min"])), Decimal(str(plausible["max"]))
                try:
                    if not lo <= Decimal(answer) <= hi:
                        bad(f"instance {iid}: answer {answer} outside plausible range [{lo}, {hi}]")
                        continue
                except InvalidOperation:
                    bad(f"instance {iid}: answer {answer!r} is not numeric")
                    continue
            if isinstance(t.get("verify"), str):
                try:
                    value = _verified_value(t["verify"], params)
                except _VerifyError as exc:
                    bad(f"instance {iid}: verify failed ({exc})")
                    continue
                if value.denominator == 1:
                    text = str(value.numerator)
                elif atype == "decimal":
                    text = _fraction_to_decimal_str(value)
                else:
                    text = None
                if text is None or check_answer(atype, answer, text) is not True:
                    bad(f"instance {iid}: verify gives {value}, keyed answer is {answer}")
                    continue

        built.append(
            Instance(
                template_id=tid,
                instance_id=iid,
                subject=subject,
                kind=t.get("kind"),
                tier=tier,
                prompt=prompt,
                figure=figure,
                hints=tuple(hints) if isinstance(hints, list) else (),
                explanation=t.get("explanation"),
                answer_type=atype,
                answer=answer,
                options=tuple((o["id"], o["text"]) for o in options),
                content_hash=chash,
            )
        )
    return problems, built


class QuizBank:
    def __init__(self, instances: list[Instance], template_ids: list[str]):
        self._instances = list(instances)
        self._by_id = {i.instance_id: i for i in instances}
        self._templates = list(template_ids)

    @classmethod
    def load(cls, directory: Path, reviews_dir: Path) -> "QuizBank":
        directory = Path(directory)
        reviews_dir = Path(reviews_dir)
        files = sorted(directory.glob("*.json"))
        if not files:
            raise BankError([f"no *.json bank files in {directory}"])
        problems: list[str] = []
        instances: list[Instance] = []
        template_ids: list[str] = []
        seen_instances: dict[str, str] = {}
        seen_templates: set[str] = set()
        seen_prompts: dict[str, str] = {}
        seen_passages: dict[str, str] = {}
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"{path.name}: cannot read bank file ({exc})")
                continue
            if not isinstance(data, list) or not all(isinstance(t, dict) for t in data):
                problems.append(f"{path.name}: must contain a JSON list of templates")
                continue
            for t in data:
                tid = t.get("id")
                if isinstance(tid, str) and tid in seen_templates:
                    problems.append(f"{tid}: duplicate template id")
                    continue
                if isinstance(tid, str):
                    seen_templates.add(tid)
                if isinstance(tid, str):
                    # bank-wide duplicate content: identical prompt (non-passage) or passage text
                    if t.get("answer_type") == "passage":
                        key, seen, what = t.get("text"), seen_passages, "passage text"
                    else:
                        key, seen, what = t.get("prompt"), seen_prompts, "prompt"
                    if isinstance(key, str) and key.strip():
                        if key in seen:
                            problems.append(f"{tid}: identical {what} to template {seen[key]}")
                        else:
                            seen[key] = tid
                p, built = _validate_template(t, seen_instances)
                problems.extend(p)
                if isinstance(tid, str) and tid and needs_review(t):
                    problems.extend(_check_reviews(t, reviews_dir))
                instances.extend(built)
                if isinstance(tid, str):
                    template_ids.append(tid)
        if problems:
            raise BankError(problems)
        return cls(instances, template_ids)

    def instances(self, tier: str) -> list[Instance]:
        return [i for i in self._instances if i.tier == tier]

    def get(self, instance_id: str) -> Instance | None:
        return self._by_id.get(instance_id)

    def templates(self) -> list[str]:
        return list(self._templates)
