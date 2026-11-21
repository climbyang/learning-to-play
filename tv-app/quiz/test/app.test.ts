import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NetworkError, RejectedError, UnavailableError, VersionError, type AttemptView } from "../src/api";
import { App } from "../src/app";
import { KeyFilter } from "../src/keys";
import { RetryController } from "../src/retry";
import { fakeApi, passageView, status, view } from "./helpers";

let root: HTMLElement;
beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = '<div id="app"></div>';
  root = document.getElementById("app")!;
});
afterEach(() => { vi.useRealTimers(); });

async function boot(api = fakeApi()) {
  const app = new App(root, api, { retry: new RetryController(() => 0.5), keys: new KeyFilter(() => Date.now()) });
  app.start(document);
  await vi.advanceTimersByTimeAsync(0);
  return { app, api };
}
const key = (app: App, keyCode: number) => app.handleKey({ keyCode, repeat: false });
const flush = () => vi.advanceTimersByTimeAsync(0);

describe("app lifecycle", () => {
  it("load renders home from status + openAttempts only, never issue", async () => {
    const { api } = await boot();
    expect(root.querySelector("#home-start")!.textContent).toBe("Start quiz");
    expect(api.status).toHaveBeenCalledTimes(1);
    expect(api.openAttempts).toHaveBeenCalledTimes(1);
    expect(api.issue).not.toHaveBeenCalled();
  });
  it("visibility visible and relaunch refresh with only status/openAttempts", async () => {
    const { app, api } = await boot();
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    app.refresh();
    await flush();
    expect(api.status).toHaveBeenCalledTimes(3);
    expect(api.openAttempts).toHaveBeenCalledTimes(3);
    for (const k of ["issue", "get", "submit", "hint", "skip", "review"] as const) expect(api[k]).not.toHaveBeenCalled();
  });
  it("shows Continue labels for open attempts", async () => {
    const api = fakeApi();
    api.openAttempts.mockResolvedValue([view()]);
    await boot(api);
    expect(root.querySelector("#home-start")!.textContent).toBe("Continue quiz");
  });
  it("unavailable status shows the reason and retries", async () => {
    const api = fakeApi();
    api.status.mockResolvedValueOnce(status({ available: false, reason: "curfew" }));
    await boot(api);
    expect(root.textContent).toMatch(/bedtime/);
    await vi.advanceTimersByTimeAsync(2500);
    expect(root.querySelector("#home-start")).not.toBeNull();
  });
  it("network error shows retry text then recovers", async () => {
    const api = fakeApi();
    api.status.mockRejectedValueOnce(new NetworkError("x"));
    await boot(api);
    expect(root.textContent).toMatch(/Can't reach the server/);
    await vi.advanceTimersByTimeAsync(2500);
    expect(root.querySelector("#home-start")).not.toBeNull();
  });
  it("503 paused shows a countdown", async () => {
    const api = fakeApi();
    api.status.mockRejectedValueOnce(new UnavailableError("paused", 90));
    await boot(api);
    expect(root.querySelector("#countdown")!.textContent).toBe("1:30");
    await vi.advanceTimersByTimeAsync(1000);
    expect(root.querySelector("#countdown")!.textContent).toBe("1:29");
  });
  it("version error shows update text and stops retrying", async () => {
    const api = fakeApi();
    api.status.mockRejectedValue(new VersionError());
    await boot(api);
    expect(root.textContent).toContain("Please update the quiz app");
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.status).toHaveBeenCalledTimes(1);
  });
});

describe("keys and routing", () => {
  it("OK on Start quiz issues and shows the question; Back returns home; Back on home is a no-op", async () => {
    const { app, api } = await boot();
    expect(document.activeElement!.id).toBe("home-start");
    key(app, 13);
    await flush();
    expect(api.issue).toHaveBeenCalledWith("regular");
    expect(root.querySelector(".question")).not.toBeNull();
    key(app, 461);
    await flush();
    expect(root.querySelector(".home")).not.toBeNull();
    const calls = api.status.mock.calls.length;
    vi.advanceTimersByTime(500);
    key(app, 461);
    await flush();
    expect(root.querySelector(".home")).not.toBeNull();
    expect(api.status.mock.calls.length).toBe(calls);
  });
  it("remote digits type into the numeric field", async () => {
    const { app } = await boot();
    key(app, 13);
    await flush();
    key(app, 52);
    key(app, 50);
    expect(root.querySelector("#answer-field")!.textContent).toBe("42");
  });
  it("double OK on Submit triggers exactly one submit", async () => {
    const { app, api } = await boot();
    key(app, 13);
    await flush();
    key(app, 52);
    root.querySelector<HTMLElement>("#act-submit")!.focus();
    key(app, 13);
    key(app, 13);
    await flush();
    expect(api.submit).toHaveBeenCalledTimes(1);
    expect(api.submit).toHaveBeenCalledWith("a1", 1, "4");
    expect(root.querySelector(".result")).not.toBeNull();
  });
  it("hint re-renders with the hint text and keeps typed digits", async () => {
    const { app, api } = await boot();
    key(app, 13);
    await flush();
    key(app, 55);
    root.querySelector<HTMLElement>("#act-hint")!.click();
    await flush();
    expect(api.hint).toHaveBeenCalledWith("a1", 1);
    expect(root.textContent).toContain("Hint: h1");
    expect(root.querySelector("#answer-field")!.textContent).toBe("7");
  });
  it("Back from the result page returns home via status/openAttempts", async () => {
    const { app, api } = await boot();
    key(app, 13);
    await flush();
    key(app, 52);
    root.querySelector<HTMLElement>("#act-submit")!.click();
    await flush();
    key(app, 461);
    await flush();
    expect(root.querySelector(".home")).not.toBeNull();
    expect(api.status).toHaveBeenCalledTimes(2);
  });
  it("cooldown rejection shows a notice and stays on the question", async () => {
    const api = fakeApi();
    api.submit.mockRejectedValueOnce(new RejectedError("cooldown", 20, 409));
    const { app } = await boot(api);
    key(app, 13);
    await flush();
    key(app, 52);
    root.querySelector<HTMLElement>("#act-submit")!.click();
    await flush();
    expect(root.querySelector(".notice")!.textContent).toMatch(/wait 0:20/);
    expect(root.querySelector(".question")).not.toBeNull();
  });
  it("review_required reloads the attempt and review is callable", async () => {
    const api = fakeApi();
    api.openAttempts.mockResolvedValue([passageView()]);
    api.submit.mockRejectedValueOnce(new RejectedError("review_required", null, 409));
    api.get.mockResolvedValueOnce(
      passageView({ stage: "vocab", vocab: [{ word: "w", meaning: "m", sentence: "a w b", check_prompt: "c", options: [], done: false, need_review: true }] }),
    );
    await boot(api);
    root.querySelector<HTMLElement>("#home-start")!.click();
    await flush();
    expect(root.querySelector(".passage")).not.toBeNull();
    root.querySelector<HTMLElement>("#opt-b")!.click();
    await flush();
    expect(api.submit).toHaveBeenCalledWith("p1", 1, "b", 1);
    expect(root.querySelector(".vocab.review")).not.toBeNull();
    root.querySelector<HTMLElement>("#vocab-read")!.click();
    await flush();
    expect(api.review).toHaveBeenCalledTimes(1);
  });
  it("skip goes to the skipped result", async () => {
    const { app } = await boot();
    key(app, 13);
    await flush();
    root.querySelector<HTMLElement>("#act-skip")!.click();
    await flush();
    expect(root.querySelector(".outcome")!.textContent).toMatch(/Skipped/);
  });
  it("a timed-out write reconciles with get(id) and never re-sends", async () => {
    const api = fakeApi();
    api.submit.mockRejectedValueOnce(new NetworkError("request timed out", true));
    api.get.mockResolvedValueOnce(view({ state: "correct", outcome: "credited", credited_minutes: 3 }));
    const { app } = await boot(api);
    key(app, 13);
    await flush();
    key(app, 52);
    root.querySelector<HTMLElement>("#act-submit")!.click();
    await flush();
    expect(api.get).toHaveBeenCalledWith("a1");
    expect(api.submit).toHaveBeenCalledTimes(1);
    expect(root.querySelector(".result")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.submit).toHaveBeenCalledTimes(1);
  });
});

describe("passage flow", () => {
  const answered = (over: Partial<AttemptView> = {}) => {
    const v = passageView({ rev: 2, step: 2, parts_right: 1, ...over });
    v.parts[1] = { ...v.parts[1], status: "wrong", given: "b", correct: "a", explanation: "It says yes." };
    v.parts[2] = { ...v.parts[2], status: "current" };
    return v;
  };
  async function openPassage(api = fakeApi()) {
    api.openAttempts.mockResolvedValue([passageView()]);
    const booted = await boot(api);
    root.querySelector<HTMLElement>("#home-start")!.click();
    await flush();
    return booted;
  }

  it("resuming a set shows the current part, not old feedback", async () => {
    await openPassage();
    expect(root.querySelector(".passage.feedback")).toBeNull();
    expect(root.textContent).toContain("Q2?");
  });
  it("after a part submit shows right/wrong, correct option and explanation, then Next", async () => {
    const api = fakeApi();
    api.submit.mockResolvedValueOnce(answered());
    await openPassage(api);
    root.querySelector<HTMLElement>("#opt-b")!.click();
    await flush();
    expect(api.submit).toHaveBeenCalledWith("p1", 1, "b", 1);
    expect(root.querySelector(".passage.feedback")).not.toBeNull();
    expect(root.querySelector(".verdict")!.textContent).toMatch(/Not quite/);
    expect(root.querySelector(".answer")!.textContent).toBe("Answer: Yes");
    expect(root.querySelector(".explanation")!.textContent).toBe("It says yes.");
    expect(document.activeElement!.id).toBe("part-next");
    expect(root.textContent).not.toContain("Q3?"); // next part waits for Next
    root.querySelector<HTMLElement>("#part-next")!.click();
    await flush();
    expect(root.querySelector(".passage.feedback")).toBeNull();
    expect(root.textContent).toContain("Q3?");
    expect(document.activeElement!.id).toBe("opt-a");
  });
  it("a timed-out part submit reconciled via get(id) still shows its feedback", async () => {
    const api = fakeApi();
    api.submit.mockRejectedValueOnce(new NetworkError("request timed out", true));
    api.get.mockResolvedValueOnce(answered());
    await openPassage(api);
    root.querySelector<HTMLElement>("#opt-b")!.click();
    await flush();
    expect(api.get).toHaveBeenCalledWith("p1");
    expect(root.querySelector(".verdict")!.textContent).toMatch(/Not quite/);
  });
  it("feedback for the last part comes before the first vocab card", async () => {
    const api = fakeApi();
    const v = answered({ stage: "vocab", step: 3, vocab: [{ word: "w", meaning: "m", sentence: "a w b", check_prompt: "c?", options: [{ id: "x", text: "X" }], done: false, need_review: false }] });
    v.parts[2] = { ...v.parts[2], status: "right", correct: "a", explanation: "Last." };
    v.parts[1] = { ...v.parts[1] };
    api.openAttempts.mockResolvedValue([answered()]);
    api.submit.mockResolvedValueOnce(v);
    await boot(api);
    root.querySelector<HTMLElement>("#home-start")!.click();
    await flush();
    root.querySelector<HTMLElement>("#opt-a")!.click();
    await flush();
    expect(root.querySelector(".verdict")!.textContent).toMatch(/Right/);
    expect(root.querySelector(".explanation")!.textContent).toBe("Last.");
    root.querySelector<HTMLElement>("#part-next")!.click();
    await flush();
    expect(root.querySelector(".vocab")).not.toBeNull();
    expect(root.querySelector("#act-skip")).not.toBeNull();
  });
  it("a set settled by its last part shows that part's feedback before the result", async () => {
    const api = fakeApi();
    const v = answered({ state: "correct", stage: "done", step: null, outcome: "credited", credited_minutes: 3 });
    v.parts[2] = { ...v.parts[2], status: "right", correct: "a", explanation: "Last." };
    api.openAttempts.mockResolvedValue([answered()]);
    api.submit.mockResolvedValueOnce(v);
    await boot(api);
    root.querySelector<HTMLElement>("#home-start")!.click();
    await flush();
    root.querySelector<HTMLElement>("#opt-a")!.click();
    await flush();
    expect(root.querySelector(".explanation")!.textContent).toBe("Last.");
    root.querySelector<HTMLElement>("#part-next")!.click();
    await flush();
    expect(root.querySelector(".passage.feedback")).toBeNull();
    expect(root.textContent).toContain("right");
  });
  it("hint and skip on the passage page call the server with the current rev", async () => {
    const api = fakeApi();
    api.hint.mockResolvedValueOnce(passageView({ rev: 2, hints: ["Look at line 2."] }));
    api.skip.mockResolvedValueOnce(passageView({ rev: 3, state: "skipped", outcome: "skipped" }));
    await openPassage(api);
    root.querySelector<HTMLElement>("#act-hint")!.click();
    await flush();
    expect(api.hint).toHaveBeenCalledWith("p1", 1);
    expect(root.textContent).toContain("Hint: Look at line 2.");
    root.querySelector<HTMLElement>("#act-skip")!.click();
    await flush();
    expect(api.skip).toHaveBeenCalledWith("p1", 2);
    expect(root.querySelector(".outcome")!.textContent).toMatch(/Skipped/);
  });
});

describe("pause handling", () => {
  it("a write refused with 503 paused shows the countdown, never reconciles, and returns home when it ends", async () => {
    const api = fakeApi();
    api.submit.mockRejectedValueOnce(new UnavailableError("paused", 90));
    api.status
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(status({ available: false, reason: "paused", retry_after_s: 90 }));
    const { app } = await boot(api);
    key(app, 13);
    await flush();
    key(app, 52);
    root.querySelector<HTMLElement>("#act-submit")!.click();
    await flush();
    expect(root.textContent).toMatch(/paused/);
    expect(root.querySelector("#countdown")!.textContent).toBe("1:30");
    expect(api.get).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(root.querySelector("#countdown")!.textContent).toBe("1:00");
    await vi.advanceTimersByTimeAsync(60_000);
    await flush();
    expect(root.querySelector(".home")).not.toBeNull();
    expect(api.submit).toHaveBeenCalledTimes(1);
    expect(api.get).not.toHaveBeenCalled();
  });
  it("a write refused with 503 locked shows the lock page without a countdown", async () => {
    const api = fakeApi();
    api.hint.mockRejectedValueOnce(new UnavailableError("locked", null));
    api.status
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(status({ available: false, reason: "locked" }));
    const { app } = await boot(api);
    key(app, 13);
    await flush();
    root.querySelector<HTMLElement>("#act-hint")!.click();
    await flush();
    expect(root.textContent).toMatch(/locked/);
    expect(root.querySelector("#countdown")).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });
  it("opened during a pause: status retry_after_s drives the countdown, then home", async () => {
    const api = fakeApi();
    api.status.mockResolvedValueOnce(status({ available: false, reason: "paused", retry_after_s: 5 }));
    await boot(api);
    expect(root.querySelector("#countdown")!.textContent).toBe("0:05");
    await vi.advanceTimersByTimeAsync(2000);
    expect(root.querySelector("#countdown")!.textContent).toBe("0:03");
    expect(api.status).toHaveBeenCalledTimes(1); // waits for the pause, no early polling
    await vi.advanceTimersByTimeAsync(3000);
    await flush();
    expect(root.querySelector(".home")).not.toBeNull();
    expect(api.issue).not.toHaveBeenCalled();
  });
});
