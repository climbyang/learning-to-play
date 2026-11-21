"""Validate bank files while drafting, before review records exist.

Run from server/:  uv run python scripts/check_bank_draft.py [file.json ...]
Checks every template's structure, verify expressions and duplicate ids/prompts the
same way the loader does, but skips the review-record requirement.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ltp.quiz.bank import _validate_template

BANK = Path(__file__).resolve().parents[1] / "quiz" / "bank"


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted(BANK.glob("*.json"))
    seen_instances: dict[str, str] = {}
    seen_ids: set[str] = set()
    seen_text: dict[str, str] = {}
    problems: list[str] = []
    count = 0
    for path in sorted(BANK.glob("*.json")):  # duplicates are checked bank-wide
        data = json.loads(path.read_text(encoding="utf-8"))
        for t in data:
            tid = t.get("id")
            key = t.get("text") if t.get("answer_type") == "passage" else t.get("prompt")
            if tid in seen_ids:
                problems.append(f"{path.name}: duplicate template id {tid}")
            seen_ids.add(tid)
            if isinstance(key, str) and key in seen_text:
                problems.append(f"{tid}: identical prompt/text to {seen_text[key]}")
            elif isinstance(key, str):
                seen_text[key] = tid
            p, _ = _validate_template(t, seen_instances)
            if path in files or path.resolve() in [f.resolve() for f in files]:
                problems.extend(f"{path.name}: {x}" for x in p)
                count += 1
    for p in problems:
        print(p)
    print(f"checked {count} templates, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
