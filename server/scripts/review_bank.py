"""Write LLM review files for question-bank templates.

Run from server/:
  uv run python scripts/review_bank.py [--pass 1|2] [--only ID ...] [--stale] [--cmd "..."]

Each batch of templates is sent to a reviewer and the verdicts are stored as one file per
template in quiz/reviews/. The bank loader refuses english and choice templates without a
passing, up-to-date review file. This script never edits templates. Exit status is non-zero
when any template failed or errored.

Reviewer: by default the Codex CLI (`codex exec -s read-only`). With --cmd, any command that
reads the prompt on stdin and prints the reply on stdout (for example another LLM CLI); its
program name is recorded as the reviewer unless --reviewer is given.
"""

from __future__ import annotations

import argparse
import json
import locale
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ltp.quiz.bank import content_hash, required_passes, review_path, review_problem

SERVER_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BANK_DIR = SERVER_DIR / "quiz" / "bank"
DEFAULT_REVIEWS_DIR = SERVER_DIR / "quiz" / "reviews"
BATCH_SIZE = 5
REVIEW_TIMEOUT_SECONDS = 900
MARKER = "REVIEW_JSON:"

CHECKS_COMMON = {
    "answer_unique_correct": "the keyed answer is correct and the only correct option",
    "distractors_unambiguous": "no distractor could also be argued correct; options are clearly distinct",
    "reading_level": "wording suits a reader in the school year given by the template's grade field "
                     "(grade 5 = Year 5, about age 10)",
    "child_appropriate": "content is safe and suitable for a child",
    "factual_accuracy": "any nonfiction statements in the passage or prompt are accurate "
                        "(flag claims that are false or outdated for a 2026 reader)",
}
CHECK_PASSAGE = {"sentence_in_passage": "every vocab sentence appears verbatim in the passage text"}

PROMPT_PASS1 = (
    "You are reviewing quiz questions for a primary-school child (each template's grade field is "
    "its target school year). For each template below, check:\n{rubric}\n"
    "Templates use {{param}} placeholders filled from each instance's params; judge the filled-in question. "
    "Do not modify any file. Write any notes in English."
)
PROMPT_PASS2 = (
    "You are an adversarial second reviewer of challenge-tier quiz questions for a primary-school child "
    "(each template's grade field is its target school year). "
    "Try hard to break each template: look for a second defensible answer, an ambiguous or trick option, "
    "a wrong key, a hint or explanation that leaks or contradicts the answer, wording above the reading level, "
    "or unsuitable content. Mark a check false if you can build any reasonable counter-argument. "
    "Checks:\n{rubric}\nDo not modify any file. Write any notes in English."
)
REPLY_FORMAT = (
    "\n\nEnd your reply with ONE final line, exactly in this form (valid single-line JSON, keys are template ids):\n"
    '{marker} {{"<template_id>": {{"verdict": "pass" or "fail", "checks": {{"<check>": true or false, ...}}, '
    '"notes": "short reason"}}, ...}}\n'
    "Use exactly these check names: {names}. Verdict is pass only if every check is true.\n\nTemplates:\n"
)

Runner = Callable[[str], str]


class ReviewError(Exception):
    """A reviewer reply could not be turned into review records."""


def check_names(template: dict) -> list[str]:
    names = list(CHECKS_COMMON)
    if template.get("answer_type") == "passage":
        names += list(CHECK_PASSAGE)
    return names


def build_prompt(batch: list[dict], review_pass: int) -> str:
    rubric_items = dict(CHECKS_COMMON)
    if any(t.get("answer_type") == "passage" for t in batch):
        rubric_items.update(CHECK_PASSAGE)
    rubric = "\n".join(f"- {k}: {v}" for k, v in rubric_items.items())
    head = (PROMPT_PASS2 if review_pass == 2 else PROMPT_PASS1).format(rubric=rubric)
    head += REPLY_FORMAT.format(marker=MARKER, names=", ".join(rubric_items))
    body = "\n".join(json.dumps(t, ensure_ascii=False, sort_keys=True) for t in batch)
    return head + body


def parse_reply(text: str, batch: list[dict]) -> dict[str, dict]:
    """Strictly parse the final REVIEW_JSON line; returns {template_id: verdict dict}."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(MARKER)]
    if not lines:
        raise ReviewError(f"no {MARKER} line in reviewer output")
    try:
        data = json.loads(lines[-1][len(MARKER):].strip())
    except ValueError as exc:
        raise ReviewError(f"{MARKER} line is not valid JSON ({exc})") from exc
    ids = [t["id"] for t in batch]
    if not isinstance(data, dict) or set(data) != set(ids):
        raise ReviewError(f"{MARKER} keys must be exactly {ids}")
    for t in batch:
        rec = data[t["id"]]
        names = check_names(t)
        if not isinstance(rec, dict) or rec.get("verdict") not in ("pass", "fail"):
            raise ReviewError(f"{t['id']}: verdict must be 'pass' or 'fail'")
        checks = rec.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(names) or not all(
            isinstance(v, bool) for v in checks.values()
        ):
            raise ReviewError(f"{t['id']}: checks must be booleans named exactly {names}")
        if rec["verdict"] == "pass" and not all(checks.values()):
            raise ReviewError(f"{t['id']}: verdict pass contradicts a false check")
        if not isinstance(rec.get("notes", ""), str):
            raise ReviewError(f"{t['id']}: notes must be a string")
    return data


def parse_model(text: str) -> str:
    m = re.search(r"^model:\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    return m.group(1) if m else "unknown"


def codex_command() -> list[str]:
    """argv prefix for codex. On Windows the npm .cmd shim mangles newlines and shell
    metacharacters in the prompt argument, so run its node entry point directly."""
    exe = shutil.which("codex")
    if exe is None:
        raise ReviewError("codex executable not found on PATH")
    if exe.lower().endswith((".cmd", ".bat")):
        base = Path(exe).parent
        entry = base / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = base / "node.exe"
        node_exe = str(node) if node.is_file() else shutil.which("node")
        if not entry.is_file() or node_exe is None:
            raise ReviewError(f"cannot locate the node entry point behind {exe}")
        return [node_exe, str(entry)]
    return [exe]


def decode_output(raw: bytes) -> str:
    """Decode console output: UTF-8 when valid, else the locale code page (Windows consoles)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def codex_runner(prompt: str) -> str:
    """Run `codex exec -s read-only` with stdin from the null device and a hard timeout."""
    try:
        proc = subprocess.run(
            [*codex_command(), "exec", "-s", "read-only", prompt],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=REVIEW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"codex timed out after {REVIEW_TIMEOUT_SECONDS}s") from exc
    stdout, stderr = decode_output(proc.stdout), decode_output(proc.stderr)
    if proc.returncode != 0:
        raise ReviewError(f"codex exited {proc.returncode}: {stderr.strip()[-300:]}")
    return f"{stdout}\n{stderr}"


def command_runner(cmd: str) -> Runner:
    """Runner for any reviewer command: prompt on stdin, reply on stdout, hard timeout."""
    argv = split_cmd(cmd)

    def run(prompt: str) -> str:
        try:
            proc = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True,
                                  timeout=REVIEW_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ReviewError(f"{argv[0]} timed out after {REVIEW_TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise ReviewError(f"cannot run {argv[0]}: {exc}") from exc
        stdout, stderr = decode_output(proc.stdout), decode_output(proc.stderr)
        if proc.returncode != 0:
            raise ReviewError(f"{argv[0]} exited {proc.returncode}: {stderr.strip()[-300:]}")
        return f"{stdout}\n{stderr}"

    return run


def split_cmd(cmd: str) -> list[str]:
    argv = [a.strip('"') for a in shlex.split(cmd, posix=sys.platform != "win32")]
    if not argv:
        raise ReviewError("--cmd is empty")
    return argv


def load_templates(bank_dir: Path) -> list[dict]:
    templates: list[dict] = []
    for path in sorted(Path(bank_dir).glob("*.json")):
        templates.extend(json.loads(path.read_text(encoding="utf-8")))
    return templates


def _is_stale(t: dict, reviews_dir: Path, review_pass: int) -> bool:
    return "stale" in (review_problem(t, reviews_dir, review_pass) or "")


def select(templates: list[dict], reviews_dir: Path, review_pass: int,
           only: list[str] | None, stale_only: bool) -> list[dict]:
    """Templates needing this pass: file missing or stale (or any named by --only)."""
    chosen = []
    for t in templates:
        if review_pass not in required_passes(t):
            continue
        exists = review_path(reviews_dir, t["id"], review_pass).is_file()
        if only is not None:
            wanted = t["id"] in only
        elif stale_only:
            wanted = exists and _is_stale(t, reviews_dir, review_pass)
        else:
            wanted = not exists or _is_stale(t, reviews_dir, review_pass)
        if wanted:
            chosen.append(t)
    return chosen


def review_templates(templates: list[dict], reviews_dir: Path, review_pass: int, runner: Runner,
                     reviewer: str = "codex",
                     now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                     ) -> tuple[list[str], list[str]]:
    """Review in batches; returns (failed ids, errored ids). Writes one file per parsed verdict."""
    failed: list[str] = []
    errored: list[str] = []
    reviews_dir.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(templates), BATCH_SIZE):
        batch = templates[i:i + BATCH_SIZE]
        try:
            reply = runner(build_prompt(batch, review_pass))
            verdicts = parse_reply(reply, batch)
        except ReviewError as exc:
            print(f"ERROR batch {[t['id'] for t in batch]}: {exc}", file=sys.stderr)
            errored.extend(t["id"] for t in batch)
            continue
        model = parse_model(reply)
        for t in batch:
            rec = verdicts[t["id"]]
            record = {
                "template_id": t["id"], "content_hash": content_hash(t), "reviewer": reviewer,
                "model": model, "utc": now().strftime("%Y-%m-%dT%H:%M:%SZ"), "pass": review_pass,
                "verdict": rec["verdict"], "checks": rec["checks"], "notes": rec.get("notes", ""),
            }
            review_path(reviews_dir, t["id"], review_pass).write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{t['id']}: {rec['verdict']} (pass {review_pass})")
            if rec["verdict"] != "pass":
                failed.append(t["id"])
    return failed, errored


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pass", dest="review_pass", type=int, choices=(1, 2), default=1)
    ap.add_argument("--only", nargs="+", metavar="ID", help="review exactly these template ids")
    ap.add_argument("--stale", action="store_true", help="only re-review templates whose file is stale")
    ap.add_argument("--bank-dir", type=Path, default=DEFAULT_BANK_DIR)
    ap.add_argument("--reviews-dir", type=Path, default=DEFAULT_REVIEWS_DIR)
    ap.add_argument("--cmd", help="reviewer command reading the prompt on stdin (default: codex exec)")
    ap.add_argument("--reviewer", help="name recorded in review files (default: codex, or the --cmd program)")
    args = ap.parse_args(argv)
    if runner is None:
        runner = command_runner(args.cmd) if args.cmd else codex_runner
    reviewer = args.reviewer if args.reviewer is not None else (
        Path(split_cmd(args.cmd)[0]).stem if args.cmd else "codex")
    if not reviewer.strip():
        ap.error("--reviewer must not be blank")
    templates = load_templates(args.bank_dir)
    todo = select(templates, args.reviews_dir, args.review_pass, args.only, args.stale)
    if not todo:
        print("nothing to review")
        return 0
    failed, errored = review_templates(todo, args.reviews_dir, args.review_pass, runner, reviewer)
    if failed or errored:
        print(f"FAILED: {failed}  ERRORED: {errored}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
