import { vi } from "vitest";
import type { AttemptView, StatusView } from "../src/api";

export function view(over: Partial<AttemptView> = {}): AttemptView {
  return {
    id: "a1", tier: "regular", state: "issued", rev: 1, prompt: "What is 6 x 7?", figure: null,
    answer_type: "integer", options: [], hints: [], hints_total: 2, tries_left: 3, skips_left: 1,
    outcome: "", credited_minutes: 0, nominal_minutes: 3, explanation: null, correct_answer: null,
    passage: null, stage: null, step: null, parts: [], vocab: [], parts_right: 0, parts_total: 0,
    ...over,
  };
}

export function status(over: Partial<StatusView> = {}): StatusView {
  return { remaining_s: 1500, quiz_today: 2, earned_minutes: 6, challenge_state: "none", available: true, reason: null, retry_after_s: null, ...over };
}

export function passageView(over: Partial<AttemptView> = {}): AttemptView {
  return view({
    id: "p1", answer_type: "passage", passage: "Once upon a time.\nThe end.", stage: "parts", step: 1,
    parts_total: 3, parts_right: 1,
    parts: [
      { prompt: "Q1?", options: [{ id: "a", text: "A1" }], status: "right" },
      { prompt: "Q2?", options: [{ id: "a", text: "Yes" }, { id: "b", text: "No" }], status: "current" },
      { prompt: "Q3?", options: [{ id: "a", text: "X" }], status: "pending" },
    ],
    ...over,
  });
}

export function fakeApi() {
  return {
    status: vi.fn(async () => status()),
    openAttempts: vi.fn(async () => [] as AttemptView[]),
    issue: vi.fn(async () => view()),
    get: vi.fn(async () => view()),
    submit: vi.fn(async () => view({ state: "correct", outcome: "credited", credited_minutes: 3 })),
    hint: vi.fn(async () => view({ rev: 2, hints: ["h1"] })),
    skip: vi.fn(async () => view({ state: "skipped", outcome: "skipped" })),
    review: vi.fn(async () => view()),
  };
}
