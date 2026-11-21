import type { AttemptView } from "../api";
import { button, el, page, type Page } from "../dom";
import { renderFigure } from "../figure";

export const MAX_ANSWER_CHARS = 6;

export interface Entry {
  key: string;
  text: string;
}

export interface QuestionActions {
  submit(answer: string): void;
  hint(): void;
  skip(): void;
}

/** Apply one typed character to the numeric field: digits, one dot, leading minus, max 6 chars. */
export function appendChar(cur: string, ch: string, allowDot: boolean): string {
  if (cur.length >= MAX_ANSWER_CHARS) return cur;
  if (ch === "-") return cur === "" ? "-" : cur;
  if (ch === ".") return allowDot && !cur.includes(".") ? cur + "." : cur;
  if (/^[0-9]$/.test(ch)) return cur + ch;
  return cur;
}

export function backspace(cur: string): string {
  return cur.slice(0, -1);
}

function actionRow(v: AttemptView, row: number, act: QuestionActions, submit: (() => void) | null): HTMLElement {
  const bar = el("div", "actions");
  const hint = button("act-hint", `Hint (${v.hints.length}/${v.hints_total})`, row, 0, act.hint);
  if (v.hints.length >= v.hints_total) hint.disabled = true;
  bar.appendChild(hint);
  if (submit) bar.appendChild(button("act-submit", "Submit", row, 1, submit));
  if (v.tier !== "challenge") {
    const skip = button("act-skip", `Skip (${v.skips_left} left)`, row, 2, act.skip);
    if (v.skips_left <= 0) skip.disabled = true;
    bar.appendChild(skip);
  }
  return bar;
}

function statusLine(v: AttemptView): HTMLElement | null {
  if (v.tries_left === null || v.tries_left === undefined) return null;
  return el("div", "tries", `Tries left: ${v.tries_left}`);
}

function hintList(v: AttemptView): HTMLElement | null {
  if (v.hints.length === 0) return null;
  const box = el("div", "hints");
  v.hints.forEach((h) => box.appendChild(el("div", "hint", "Hint: " + h)));
  return box;
}

export function renderQuestion(v: AttemptView, entry: Entry, act: QuestionActions): Page {
  const root = page("question");
  const left = el("div", "left");
  left.appendChild(el("div", "prompt", v.prompt));
  const fig = renderFigure(v.figure);
  if (fig) left.appendChild(fig);
  const hints = hintList(v);
  if (hints) left.appendChild(hints);
  const tries = statusLine(v);
  if (tries) left.appendChild(tries);
  root.appendChild(left);

  const right = el("div", "right");
  root.appendChild(right);

  if (v.answer_type === "choice") {
    const list = el("div", "options");
    v.options.forEach((o, i) => {
      list.appendChild(button("opt-" + o.id, o.text, i, 0, () => act.submit(o.id), "btn option"));
    });
    right.appendChild(list);
    right.appendChild(actionRow(v, v.options.length, act, null));
    return { el: root };
  }

  const allowDot = v.answer_type === "decimal";
  const field = el("div", "field", entry.text);
  field.id = "answer-field";
  const refresh = () => {
    field.textContent = entry.text;
  };
  const type = (ch: string) => {
    entry.text = appendChar(entry.text, ch, allowDot);
    refresh();
  };
  right.appendChild(field);

  const pad = el("div", "keypad");
  const layout: string[][] = [
    ["1", "2", "3", "del"],
    ["4", "5", "6", "-"],
    ["7", "8", "9", allowDot ? "." : ""],
    ["0"],
  ];
  layout.forEach((keys, r) => {
    keys.forEach((k, c) => {
      if (k === "") return;
      const label = k === "del" ? "⌫" : k;
      const b = button("key-" + (k === "del" ? "del" : k === "." ? "dot" : k === "-" ? "minus" : k), label, r, c, () => {
        if (k === "del") {
          entry.text = backspace(entry.text);
          refresh();
        } else type(k);
      }, "btn key");
      pad.appendChild(b);
    });
  });
  right.appendChild(pad);
  right.appendChild(
    actionRow(v, layout.length, act, () => {
      if (entry.text !== "" && entry.text !== "-" && entry.text !== ".") act.submit(entry.text);
    }),
  );
  return { el: root, onDigit: (d) => type(String(d)) };
}
