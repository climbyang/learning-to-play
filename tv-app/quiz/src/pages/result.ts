import type { AttemptView } from "../api";
import { button, el, page, type Page } from "../dom";

const OUTCOME_TEXT: Record<string, string> = {
  credited: "Great job! Your time has been added.",
  half: "Nearly there! One part was wrong, so you get half the time.",
  hint: "You used a hint, so you get half the time.",
  cap: "Correct! You've reached today's quiz limit, so only some of the time was added.",
  cap_full: "Correct! You've reached today's quiz limit, so no more time can be added today.",
  kind_cap: "Correct! You've already earned today's time for this kind of question, so this one was practice.",
  too_fast: "Correct, but that was very quick, so no time was added.",
  lost: "Not this time. You chose a wrong answer earlier, so no time was added.",
  parts_wrong: "Too many parts were wrong, so no time was added.",
  exhausted: "No tries left. Here is the answer.",
  skipped: "Skipped. No time was added.",
  expired: "This challenge has expired.",
};
const STATE_FALLBACK: Record<string, string> = {
  correct: OUTCOME_TEXT.credited,
  exhausted: OUTCOME_TEXT.exhausted,
  skipped: OUTCOME_TEXT.skipped,
  expired: OUTCOME_TEXT.expired,
};

const HALF_AND_HINT = "Nearly there! One part was wrong and you used a hint, so the time was halved twice.";
const EXPIRED_REGULAR = "This question has expired.";

export function outcomeText(v: Pick<AttemptView, "outcome" | "state"> & Partial<Pick<AttemptView, "hints" | "tier">>): string {
  if (v.outcome === "half" && v.hints && v.hints.length > 0) return HALF_AND_HINT;
  const text = OUTCOME_TEXT[v.outcome] ?? STATE_FALLBACK[v.state] ?? "All done.";
  return text === OUTCOME_TEXT.expired && v.tier === "regular" ? EXPIRED_REGULAR : text;
}

export function renderResult(v: AttemptView, onDone: () => void): Page {
  const root = page("result");
  root.appendChild(el("h1", "outcome", outcomeText(v)));
  if (v.credited_minutes > 0) {
    root.appendChild(el("div", "credited", `+${v.credited_minutes} min`));
  }
  if (v.answer_type === "passage") {
    root.appendChild(el("div", "score", `${v.parts_right}/${v.parts_total} right`));
  } else if (v.correct_answer !== null && v.correct_answer !== undefined) {
    root.appendChild(el("div", "answer", `Answer: ${String(v.correct_answer)}`));
  }
  if (v.explanation) root.appendChild(el("div", "explanation", v.explanation));
  root.appendChild(button("result-ok", "OK", 0, 0, onDone));
  return { el: root };
}
