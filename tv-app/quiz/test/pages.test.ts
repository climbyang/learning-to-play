import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderFigure } from "../src/figure";
import { renderHome } from "../src/pages/home";
import { renderPartFeedback, renderPassage } from "../src/pages/passage";
import { appendChar, renderQuestion, type Entry } from "../src/pages/question";
import { outcomeText, renderResult } from "../src/pages/result";
import { renderUnavailable } from "../src/pages/unavailable";
import { highlightSentence, renderVocab } from "../src/pages/vocab";
import { passageView, status, view } from "./helpers";

beforeEach(() => { document.body.innerHTML = ""; });
const acts = () => ({ submit: vi.fn(), hint: vi.fn(), skip: vi.fn() });
const pacts = () => ({ answer: vi.fn(), hint: vi.fn(), skip: vi.fn() });
const vacts = () => ({ answer: vi.fn(), review: vi.fn(), skip: vi.fn() });
const entry = (): Entry => ({ key: "k", text: "" });
const text = (e: HTMLElement) => e.textContent ?? "";

describe("home", () => {
  it("shows stats and start labels", () => {
    const p = renderHome(status(), [], { start: vi.fn() });
    expect(text(p.el)).toContain("Time left: 25 min");
    expect(text(p.el)).toContain("Correct today: 2");
    expect(text(p.el)).toContain("Earned today: 6 min");
    expect(p.el.querySelector("#home-start")!.textContent).toBe("Start quiz");
    expect(p.el.querySelector("#home-challenge")!.textContent).toBe("Daily challenge");
  });
  it("shows continue labels and challenge done", () => {
    const open = [view(), view({ id: "c", tier: "challenge" })];
    const p = renderHome(status({ challenge_state: "issued" }), open, { start: vi.fn() });
    expect(p.el.querySelector("#home-start")!.textContent).toBe("Continue quiz");
    expect(p.el.querySelector("#home-challenge")!.textContent).toBe("Continue challenge");
    const d = renderHome(status({ challenge_state: "correct" }), [], { start: vi.fn() });
    const b = d.el.querySelector<HTMLButtonElement>("#home-challenge")!;
    expect(b.textContent).toBe("Challenge done");
    expect(b.disabled).toBe(true);
  });
});

describe("question", () => {
  it("numeric: keypad, field, hint/submit/skip with skips left", () => {
    const a = acts();
    const e = entry();
    const p = renderQuestion(view({ tries_left: 2 }), e, a);
    document.body.appendChild(p.el);
    expect(text(p.el)).toContain("What is 6 x 7?");
    expect(text(p.el)).toContain("Tries left: 2");
    expect(p.el.querySelectorAll(".key").length).toBe(12); // 0-9 + del + minus, no dot for integer
    p.el.querySelector<HTMLElement>("#key-4")!.click();
    p.el.querySelector<HTMLElement>("#key-2")!.click();
    p.onDigit!(7);
    expect(p.el.querySelector("#answer-field")!.textContent).toBe("427");
    p.el.querySelector<HTMLElement>("#key-del")!.click();
    expect(e.text).toBe("42");
    expect(p.el.querySelector("#act-skip")!.textContent).toBe("Skip (1 left)");
    p.el.querySelector<HTMLElement>("#act-submit")!.click();
    expect(a.submit).toHaveBeenCalledWith("42");
  });
  it("empty answer is not submitted", () => {
    const a = acts();
    const p = renderQuestion(view(), entry(), a);
    p.el.querySelector<HTMLElement>("#act-submit")!.click();
    expect(a.submit).not.toHaveBeenCalled();
  });
  it("decimal has a dot key; challenge hides skip", () => {
    const p = renderQuestion(view({ answer_type: "decimal", tier: "challenge" }), entry(), acts());
    expect(p.el.querySelector("#key-dot")).not.toBeNull();
    expect(p.el.querySelector("#act-skip")).toBeNull();
  });
  it("appendChar rules", () => {
    expect(appendChar("", "-", true)).toBe("-");
    expect(appendChar("1", "-", true)).toBe("1");
    expect(appendChar("1.5", ".", true)).toBe("1.5");
    expect(appendChar("1", ".", false)).toBe("1");
    expect(appendChar("123456", "7", true)).toBe("123456");
  });
  it("choice: options are buttons that submit their id", () => {
    const a = acts();
    const p = renderQuestion(
      view({ answer_type: "choice", options: [{ id: "x", text: "Cat" }, { id: "y", text: "Dog" }], hints: ["h"] }),
      entry(), a);
    expect(p.el.querySelectorAll(".option").length).toBe(2);
    p.el.querySelector<HTMLElement>("#opt-y")!.click();
    expect(a.submit).toHaveBeenCalledWith("y");
    expect(text(p.el)).toContain("Hint: h");
    expect(p.el.querySelector("#key-1")).toBeNull();
  });
  it("hint disabled when all shown; skip disabled when none left", () => {
    const p = renderQuestion(view({ hints: ["a", "b"], skips_left: 0 }), entry(), acts());
    expect(p.el.querySelector<HTMLButtonElement>("#act-hint")!.disabled).toBe(true);
    expect(p.el.querySelector<HTMLButtonElement>("#act-skip")!.disabled).toBe(true);
  });
  it("renders a bar figure and hides an unknown one", () => {
    const bar = JSON.stringify({ type: "bar", data: [{ label: "A", value: 3 }, { label: "B", value: 5 }] });
    const p = renderQuestion(view({ figure: bar }), entry(), acts());
    expect(p.el.querySelectorAll("rect").length).toBe(2);
    const q = renderQuestion(view({ figure: JSON.stringify({ type: "pie" }) }), entry(), acts());
    expect(q.el.querySelector(".figure")).toBeNull();
    expect(q.el.textContent).toContain("What is 6 x 7?");
  });
});

describe("figure", () => {
  it("table and malformed input", () => {
    const t = renderFigure(JSON.stringify({ type: "table", headers: ["x", "y"], rows: [[1, 2], [3, 4]] }))!;
    expect(t.querySelectorAll("td").length).toBe(4);
    expect(renderFigure("not json")).toBeNull();
    expect(renderFigure(null)).toBeNull();
    expect(renderFigure(JSON.stringify({ type: "bar", data: [] }))).toBeNull();
  });
  it("chess board from the side to move", () => {
    const w = renderFigure(JSON.stringify({ type: "chess", fen: "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1" }))!;
    expect(w.querySelectorAll("rect").length).toBe(64);
    expect(w.querySelectorAll(".chess-piece").length).toBe(6);
    const labels = (e: HTMLElement) => [...e.querySelectorAll("text:not(.chess-piece)")].map((t) => t.textContent);
    expect(labels(w)[0]).toBe("a");
    const b = renderFigure(JSON.stringify({ type: "chess", fen: "6k1/5ppp/8/8/8/8/8/R5K1 b - - 0 1" }))!;
    expect(labels(b)[0]).toBe("h");
    expect(renderFigure(JSON.stringify({ type: "chess", fen: "bad" }))).toBeNull();
    expect(renderFigure(JSON.stringify({ type: "chess", fen: "9/8/8/8/8/8/8/8 w" }))).toBeNull();
  });
});

describe("passage", () => {
  it("mid-parts: markers, scrollable passage, current part options", () => {
    const act = pacts();
    const p = renderPassage(passageView(), act);
    const marks = [...p.el.querySelectorAll(".marker")].map((m) => m.textContent);
    expect(marks).toEqual(["✓", "•", "•"]);
    expect(p.el.querySelector("[data-scroll]")!.textContent).toContain("Once upon a time.");
    expect(text(p.el)).toContain("Q2?");
    expect(p.initialFocus).toBe("opt-a");
    p.el.querySelector<HTMLElement>("#opt-b")!.click();
    expect(act.answer).toHaveBeenCalledWith("b");
  });
  it("wrong part is marked with a cross", () => {
    const v = passageView();
    v.parts[0].status = "wrong";
    const p = renderPassage(v, pacts());
    expect(p.el.querySelector(".marker")!.textContent).toBe("✗");
  });
  it("hint and skip: revealed hints, counts, callbacks", () => {
    const act = pacts();
    const p = renderPassage(passageView({ hints: ["Reread line 1."], hints_total: 2, skips_left: 2 }), act);
    expect(text(p.el)).toContain("Hint: Reread line 1.");
    const hint = p.el.querySelector<HTMLButtonElement>("#act-hint")!;
    expect(hint.textContent).toBe("Hint (1/2)");
    expect(hint.disabled).toBe(false);
    hint.click();
    expect(act.hint).toHaveBeenCalledTimes(1);
    const skip = p.el.querySelector<HTMLButtonElement>("#act-skip")!;
    expect(skip.textContent).toBe("Skip (2 left)");
    skip.click();
    expect(act.skip).toHaveBeenCalledTimes(1);
  });
  it("hint disabled when used up or absent; skip disabled at 0; challenge has no skip", () => {
    const used = renderPassage(passageView({ hints: ["h"], hints_total: 1, skips_left: 0 }), pacts());
    expect(used.el.querySelector<HTMLButtonElement>("#act-hint")!.disabled).toBe(true);
    expect(used.el.querySelector<HTMLButtonElement>("#act-skip")!.disabled).toBe(true);
    const none = renderPassage(passageView({ hints_total: 0 }), pacts());
    expect(none.el.querySelector<HTMLButtonElement>("#act-hint")!.disabled).toBe(true);
    const ch = renderPassage(passageView({ tier: "challenge", skips_left: 0 }), pacts());
    expect(ch.el.querySelector("#act-skip")).toBeNull();
    expect(ch.el.querySelector("#act-hint")).not.toBeNull();
  });
  it("feedback shows verdict, correct option text and explanation for an answered part", () => {
    const v = passageView();
    v.parts[0] = {
      prompt: "Q1?", options: [{ id: "a", text: "A1" }, { id: "b", text: "B1" }],
      status: "wrong", given: "b", correct: "a", explanation: "Line 1 says so.",
    };
    const next = vi.fn();
    const p = renderPartFeedback(v, 0, next);
    expect(p.el.querySelector(".verdict.wrong")!.textContent).toMatch(/Not quite/);
    expect(p.el.querySelector(".answer")!.textContent).toBe("Answer: A1");
    expect(p.el.querySelector(".explanation")!.textContent).toBe("Line 1 says so.");
    expect(p.initialFocus).toBe("part-next");
    p.el.querySelector<HTMLElement>("#part-next")!.click();
    expect(next).toHaveBeenCalledTimes(1);
    v.parts[0].status = "right";
    expect(renderPartFeedback(v, 0, next).el.querySelector(".verdict.right")!.textContent).toMatch(/Right/);
  });
  it("feedback renders nothing about an unanswered part", () => {
    const v = passageView();
    // Even if a (buggy) view carried data for the current part, it is not shown.
    v.parts[1] = { ...v.parts[1], correct: "a", explanation: "secret" };
    const p = renderPartFeedback(v, 1, vi.fn());
    expect(p.el.querySelector(".verdict")).toBeNull();
    expect(text(p.el)).not.toContain("secret");
    expect(p.el.querySelector("#part-next")).not.toBeNull();
  });
});

describe("vocab", () => {
  const item = (over = {}) => ({
    word: "gleam", meaning: "to shine softly", sentence: "The lake began to Gleam at dawn.",
    check_prompt: "Which word means shine?", options: [{ id: "a", text: "glow" }, { id: "b", text: "run" }],
    done: false, need_review: false, ...over,
  });
  it("shows word, meaning, highlighted sentence and options", () => {
    const act = vacts();
    const v = passageView({ stage: "vocab", vocab: [item({ done: true, word: "old" }), item()] });
    const p = renderVocab(v, act);
    expect(p.el.querySelector(".word")!.textContent).toBe("gleam");
    expect(p.el.querySelector("mark")!.textContent).toBe("Gleam");
    p.el.querySelector<HTMLElement>("#opt-a")!.click();
    expect(act.answer).toHaveBeenCalledWith("a");
  });
  it("need_review shows Look again and calls review", () => {
    const act = vacts();
    const p = renderVocab(passageView({ stage: "vocab", vocab: [item({ need_review: true })] }), act);
    expect(text(p.el)).toContain("Look again");
    expect(p.el.querySelector(".option")).toBeNull();
    const b = p.el.querySelector<HTMLElement>("#vocab-read")!;
    expect(b.textContent).toBe("I've read it");
    b.click();
    expect(act.review).toHaveBeenCalledTimes(1);
  });
  it("regular vocab can be skipped (also while reviewing); challenge cannot", () => {
    const act = vacts();
    const p = renderVocab(passageView({ stage: "vocab", skips_left: 3, vocab: [item()] }), act);
    const skip = p.el.querySelector<HTMLButtonElement>("#act-skip")!;
    expect(skip.textContent).toBe("Skip (3 left)");
    skip.click();
    expect(act.skip).toHaveBeenCalledTimes(1);
    const rv = renderVocab(passageView({ stage: "vocab", vocab: [item({ need_review: true })] }), vacts());
    expect(rv.el.querySelector("#act-skip")).not.toBeNull();
    const ch = renderVocab(passageView({ tier: "challenge", stage: "vocab", vocab: [item()] }), vacts());
    expect(ch.el.querySelector("#act-skip")).toBeNull();
  });
  it("highlight tolerates a missing word", () => {
    expect(highlightSentence("abc", "zzz").querySelector("mark")).toBeNull();
  });
});

describe("result", () => {
  const cases: [string, string, RegExp][] = [
    ["correct", "credited", /Great job/],
    ["correct", "half", /half the time/],
    ["correct", "hint", /hint/],
    ["correct", "cap", /limit/],
    ["correct", "cap_full", /no more time/],
    ["correct", "too_fast", /very quick/],
    ["correct", "lost", /wrong answer earlier/],
    ["correct", "parts_wrong", /Too many parts/],
    ["correct", "kind_cap", /practice/],
    ["correct", "practice", /review/],
    ["exhausted", "exhausted", /No tries left/],
    ["skipped", "skipped", /Skipped/],
    ["expired", "expired", /expired/],
  ];
  it.each(cases)("state %s outcome %s", (state, outcome, re) => {
    const p = renderResult(view({ state: state as never, outcome }), vi.fn());
    expect(p.el.querySelector(".outcome")!.textContent).toMatch(re);
  });
  it("shows credited minutes, explanation and correct answer", () => {
    const p = renderResult(
      view({ state: "exhausted", outcome: "exhausted", credited_minutes: 0, explanation: "Because 6x7=42", correct_answer: 42 }),
      vi.fn());
    expect(text(p.el)).toContain("Answer: 42");
    expect(text(p.el)).toContain("Because 6x7=42");
    const c = renderResult(view({ state: "correct", outcome: "credited", credited_minutes: 3 }), vi.fn());
    expect(c.el.querySelector(".credited")!.textContent).toBe("+3 min");
  });
  it("passage shows x/y and OK calls done", () => {
    const done = vi.fn();
    const p = renderResult(passageView({ state: "correct", outcome: "half", parts_right: 2, parts_total: 3 }), done);
    expect(text(p.el)).toContain("2/3 right");
    p.el.querySelector<HTMLElement>("#result-ok")!.click();
    expect(done).toHaveBeenCalled();
  });
  it("half with a hint says the time was halved twice", () => {
    const p = renderResult(passageView({ state: "correct", outcome: "half", hints: ["h"], credited_minutes: 2 }), vi.fn());
    expect(p.el.querySelector(".outcome")!.textContent).toMatch(/used a hint.*halved twice/);
    expect(outcomeText({ state: "correct", outcome: "half", hints: [] })).toMatch(/half the time/);
  });
  it("expired wording depends on the tier", () => {
    expect(outcomeText({ state: "expired", outcome: "expired", tier: "challenge" })).toMatch(/challenge has expired/);
    expect(outcomeText({ state: "expired", outcome: "", tier: "regular" })).toMatch(/question has expired/);
  });
  it("falls back by state for an unknown outcome", () => {
    expect(outcomeText({ state: "correct", outcome: "weird" })).toMatch(/Great job/);
  });
});

describe("unavailable", () => {
  const cases: [string, RegExp][] = [
    ["locked", /locked/], ["curfew", /bedtime/], ["paused", /paused/], ["disabled", /switched off/],
    ["version", /Please update the quiz app/], ["network", /Can't reach the server, retrying/],
  ];
  it.each(cases)("reason %s", (reason, re) => {
    expect(text(renderUnavailable(reason, null).el)).toMatch(re);
  });
  it("paused shows a countdown", () => {
    expect(renderUnavailable("paused", 125).el.querySelector("#countdown")!.textContent).toBe("2:05");
    expect(renderUnavailable("locked", 125).el.querySelector("#countdown")).toBeNull();
  });
});
