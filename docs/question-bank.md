# Question bank

The bank lives in `server/quiz/bank/` as six JSON files: maths, logic and English, each with a regular and a challenge tier. It's written for children aged about 9–11 (Year 4–5). The server validates the whole bank on start-up. If validation fails, only the quiz is turned off: monitoring keeps running, and the parents get one Telegram alert with the first problems found.

## Templates and instances

Each entry is a **template**. Its stable `id` is the unit for "not seen in 30 days". A template has a `subject` (`math`, `logic` or `english`), a `kind`, a `tier` (`regular` or `challenge`), a `grade`, `est_minutes` and an `answer_type`. Look at any template in the bank files for the full shape. The loader in `server/src/ltp/quiz/bank.py` is the reference for every rule below.

- **Numeric (`integer`, `decimal`).** A `prompt` with `{param}` placeholders, 1–3 `hints`, an `explanation` and a list of `instances`, each with its own `id`, `params` and `answer`. Two more fields are required:
  - `verify`: an arithmetic expression over the params. The loader evaluates it and refuses any instance whose stored answer doesn't match.
  - `plausible`: a `min`/`max`/`step` range. It must allow at least 20 reasonable answers, so guessing doesn't pay.
- **Multiple choice (`choice`).** A prompt, at least 5 options with fixed ids, and the id of the correct option.
- **Reading set (`passage`).** Used for English. It has a `text`, a list of comprehension `parts` (each with a prompt, at least 4 options and an answer), optional hints, and one or two `vocab` cards. Each card has a word, its meaning, the `sentence` from the passage that uses it (which must appear word for word in the text), a check prompt, at least 3 options and an answer.
- **Figures (optional).** A question can carry a figure drawn on the TV: either `{"type": "bar", "data": [{"label", "value"}…]}` or `{"type": "table", "headers": […], "rows": [[…]…]}`.

## Review records

Answers for maths and numeric logic questions are checked by `verify`. English and multiple-choice templates need a stored review instead, kept in `server/quiz/reviews/`:

- One file per template, `<template-id>.json`. Challenge templates also need a second, adversarial review in `<template-id>.pass2.json`.
- Each record names its reviewer and model, and holds a verdict, a set of checks and the template's content hash. The checks are: the keyed answer is the only correct one, the distractors are unambiguous, the reading level fits, the content suits a child, the facts are accurate, and vocab sentences appear in the passage.
- The loader refuses any template whose review is missing, failed or **stale**. Editing a template changes its content hash, so every edit needs a fresh review.

`server/scripts/review_bank.py` writes these records. It sends templates in batches to an LLM reviewer, parses a strict one-line JSON verdict and writes one file per template. It never edits templates.

- By default it uses the Codex CLI in read-only mode.
- With `--cmd`, it uses any command that reads the prompt on stdin and prints the reply on stdout, such as another LLM's CLI. Name the reviewer with `--reviewer`.
- `--pass 2` runs the adversarial pass, `--only <id>…` reviews specific templates and `--stale` re-reviews only templates that changed.
- A human reviewer can write a record by hand in the same format, using their own name as the reviewer.

The records shipped in this repository were produced by an LLM reviewer. Treat them as a first filter, not a guarantee.

## Changing the bank

1. Edit or add templates in `server/quiz/bank/`.
2. Run `uv run python scripts/review_bank.py` (plus `--pass 2` for challenge templates) in `server/`, and fix anything it fails.
3. Run `uv run pytest tests/test_production_bank.py` to confirm that the bank loads and still meets its size and mix targets.
4. Restart the service.

Parents can use `/quiz check` in Telegram to spot-check a random live question with its answer, and `/quiz bad <id>` to pull one immediately.

Automated tests use their own small bank in `server/tests/sample_bank/`, so changes to the real bank never break them.
