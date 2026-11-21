import type { AttemptView, Part } from "../api";
import { button, el, page, type Page } from "../dom";

export interface PassageActions {
  answer(optionId: string): void;
  hint(): void;
  skip(): void;
}

const MARKS: Record<Part["status"], string> = {
  right: "✓",
  wrong: "✗",
  current: "•",
  pending: "•",
};

export function currentPartIndex(v: AttemptView): number {
  const byStatus = v.parts.findIndex((p) => p.status === "current");
  if (byStatus >= 0) return byStatus;
  return v.step !== null && v.step !== undefined && v.step < v.parts.length ? v.step : 0;
}

/** Number of parts the server reports as answered (right or wrong). */
export function answeredParts(v: AttemptView): number {
  return v.parts.filter((p) => p.status === "right" || p.status === "wrong").length;
}

function markers(v: AttemptView): HTMLElement {
  const box = el("div", "markers");
  v.parts.forEach((p, i) => {
    const m = el("span", "marker " + p.status, MARKS[p.status] ?? "•");
    m.setAttribute("aria-label", `Part ${i + 1}`);
    box.appendChild(m);
  });
  return box;
}

function passageText(v: AttemptView): HTMLElement {
  const text = el("div", "passage-text", v.passage ?? "");
  // Focusable so Up/Down can scroll it (FocusGrid scrolls the closest data-scroll region).
  text.setAttribute("data-scroll", "");
  text.setAttribute("data-focus", "");
  text.dataset.row = "0";
  text.dataset.col = "0";
  text.id = "passage-text";
  return text;
}

/** Revealed hints (the server only sends the ones already used). */
export function hintList(v: AttemptView): HTMLElement | null {
  if (v.hints.length === 0) return null;
  const box = el("div", "hints");
  v.hints.forEach((h) => box.appendChild(el("div", "hint", "Hint: " + h)));
  return box;
}

/** Skip button for regular sets only; challenge sets cannot be skipped. */
export function skipButton(v: AttemptView, row: number, col: number, onSkip: () => void): HTMLButtonElement | null {
  if (v.tier === "challenge") return null;
  const skip = button("act-skip", `Skip (${v.skips_left} left)`, row, col, onSkip);
  if (v.skips_left <= 0) skip.disabled = true;
  return skip;
}

export function renderPassage(v: AttemptView, act: PassageActions): Page {
  const root = page("passage");
  root.appendChild(markers(v));

  const body = el("div", "body");
  body.appendChild(passageText(v));

  const right = el("div", "right");
  const part = v.parts[currentPartIndex(v)];
  const nOpts = part ? part.options.length : 0;
  if (part) {
    right.appendChild(el("div", "prompt", part.prompt));
    part.options.forEach((o, i) => {
      right.appendChild(button("opt-" + o.id, o.text, i, 1, () => act.answer(o.id), "btn option"));
    });
  }
  const hints = hintList(v);
  if (hints) right.appendChild(hints);
  const bar = el("div", "actions");
  const hint = button("act-hint", `Hint (${v.hints.length}/${v.hints_total})`, nOpts, 1, act.hint);
  if (v.hints.length >= v.hints_total) hint.disabled = true;
  bar.appendChild(hint);
  const skip = skipButton(v, nOpts, 2, act.skip);
  if (skip) bar.appendChild(skip);
  right.appendChild(bar);

  body.appendChild(right);
  root.appendChild(body);
  return { el: root, initialFocus: part && part.options.length ? "opt-" + part.options[0].id : undefined };
}

/**
 * Feedback for an answered part: right/wrong, the correct option and its explanation.
 * Renders only what the server sent for that part; an unanswered part shows nothing.
 */
export function renderPartFeedback(v: AttemptView, index: number, onNext: () => void): Page {
  const root = page("passage feedback");
  root.appendChild(markers(v));
  const body = el("div", "body");
  body.appendChild(passageText(v));

  const right = el("div", "right");
  const part = v.parts[index];
  const answered = part !== undefined && (part.status === "right" || part.status === "wrong");
  if (answered) {
    right.appendChild(el("div", "prompt", part.prompt));
    const ok = part.status === "right";
    right.appendChild(el("div", "verdict " + (ok ? "right" : "wrong"), ok ? "✓ Right!" : "✗ Not quite."));
    if (part.correct !== undefined && part.correct !== null) {
      const opt = part.options.find((o) => o.id === part.correct);
      right.appendChild(el("div", "answer", "Answer: " + (opt ? opt.text : String(part.correct))));
    }
    if (part.explanation) right.appendChild(el("div", "explanation", part.explanation));
  }
  right.appendChild(button("part-next", "Next", 0, 1, onNext));
  body.appendChild(right);
  root.appendChild(body);
  return { el: root, initialFocus: "part-next" };
}
