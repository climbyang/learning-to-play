import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RetryController } from "../src/retry";

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

const noJitter = () => 0.5;
const failing = () => vi.fn(() => Promise.reject(new Error("x")));

describe("RetryController", () => {
  it("backs off 2s,4s,8s,16s,30s,30s", async () => {
    const task = failing();
    const rc = new RetryController(noJitter);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    let calls = 1;
    for (const d of [2000, 4000, 8000, 16000, 30000, 30000]) {
      await vi.advanceTimersByTimeAsync(d - 1);
      expect(task).toHaveBeenCalledTimes(calls);
      await vi.advanceTimersByTimeAsync(1);
      calls++;
      expect(task).toHaveBeenCalledTimes(calls);
    }
    rc.cancel();
  });
  it("applies 20 percent jitter either way", async () => {
    const task = failing();
    const rc = new RetryController(() => 1);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(2399);
    expect(task).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    rc.cancel();
    const t2 = failing();
    const rc2 = new RetryController(() => 0);
    rc2.start(t2);
    await vi.advanceTimersByTimeAsync(1600);
    expect(t2).toHaveBeenCalledTimes(2);
    rc2.cancel();
  });
  it("honours retryAfter when longer than backoff", async () => {
    const task = vi.fn(() => Promise.reject(Object.assign(new Error("x"), { retryAfter: 10 })));
    const rc = new RetryController(noJitter);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(9999);
    expect(task).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    rc.cancel();
  });
  it("stops after success", async () => {
    const task = vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValue(undefined);
    const rc = new RetryController(noJitter);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(2000);
    expect(task).toHaveBeenCalledTimes(2);
    expect(rc.active).toBe(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(2);
  });
  it("pause stops the timer, resume retries immediately", async () => {
    const task = failing();
    const rc = new RetryController(noJitter);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(0);
    rc.pause();
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    rc.resume();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    rc.cancel();
  });
  it("a failure that lands while paused waits for resume", async () => {
    let fail!: () => void;
    const task = vi.fn(() => new Promise<void>((_r, rej) => (fail = () => rej(new Error("x")))));
    const rc = new RetryController(noJitter);
    rc.start(task);
    rc.pause();
    fail();
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    rc.resume();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    rc.cancel();
  });
  it("starting a new loop cancels the previous one", async () => {
    const a = failing();
    const b = failing();
    const rc = new RetryController(noJitter);
    rc.start(a);
    await vi.advanceTimersByTimeAsync(0);
    rc.start(b);
    await vi.advanceTimersByTimeAsync(10000);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b.mock.calls.length).toBeGreaterThan(1);
    rc.cancel();
    const n = b.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(b).toHaveBeenCalledTimes(n);
  });
  it("bindVisibility pauses and resumes", async () => {
    const task = failing();
    const rc = new RetryController(noJitter);
    const off = rc.bindVisibility(document);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(0);
    let hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    off();
    rc.cancel();
  });
  it("resume honours the remaining retryAfter wait", async () => {
    const task = vi.fn(() => Promise.reject(Object.assign(new Error("x"), { retryAfter: 10 })));
    const rc = new RetryController(noJitter);
    rc.start(task);
    await vi.advanceTimersByTimeAsync(3000);
    rc.pause();
    await vi.advanceTimersByTimeAsync(1000);
    rc.resume();
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(task).toHaveBeenCalledTimes(2);
    rc.cancel();
  });
  it("bound while already hidden: no attempt until the page becomes visible", async () => {
    let hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    try {
      const rc = new RetryController(noJitter);
      const off = rc.bindVisibility(document);
      const task = failing();
      rc.start(task);
      await vi.advanceTimersByTimeAsync(60000);
      expect(task).toHaveBeenCalledTimes(0);
      hidden = false;
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(task).toHaveBeenCalledTimes(1);
      off();
      rc.cancel();
    } finally {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    }
  });
  it("start while hidden keeps the paused flag and waits for resume", async () => {
    const rc = new RetryController(noJitter);
    const first = failing();
    rc.start(first);
    await vi.advanceTimersByTimeAsync(0);
    rc.pause();
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    const second = failing();
    rc.start(second);
    await vi.advanceTimersByTimeAsync(60000);
    expect(second).toHaveBeenCalledTimes(0);
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    rc.resume();
    await vi.advanceTimersByTimeAsync(0);
    expect(second).toHaveBeenCalledTimes(1);
    rc.cancel();
  });
});
