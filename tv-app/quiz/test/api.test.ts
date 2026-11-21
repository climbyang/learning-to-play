import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiClient, BusyError, NetworkError, RateLimitError, RejectedError, UnavailableError, VersionError,
} from "../src/api";

const TOKEN = "secret-token-xyz";
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
const client = (f: unknown, timeoutMs?: number) =>
  new ApiClient({ baseUrl: "http://h:1/", token: TOKEN, fetchImpl: f as typeof fetch, timeoutMs });

afterEach(() => { vi.useRealTimers(); });

describe("ApiClient", () => {
  it("sends auth + version headers and parses open attempts", async () => {
    const f = vi.fn(() => json(200, { attempts: [{ id: "a", rev: 1 }] }));
    const r = await client(f).openAttempts();
    expect(r).toEqual([{ id: "a", rev: 1 }]);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://h:1/api/attempts/open");
    const h = init.headers as Record<string, string>;
    expect(h["Authorization"]).toBe("Bearer " + TOKEN);
    expect(h["X-LTP-API"]).toBe("2");
  });
  it("posts submit with step only when given", async () => {
    const f = vi.fn(() => json(200, { id: "a", rev: 2 }));
    const c = client(f);
    await c.submit("a", 1, "x");
    await c.submit("a", 2, "y", 3);
    const calls = f.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls[0][0]).toBe("http://h:1/api/attempt/a/submit");
    expect(JSON.parse(calls[0][1].body as string)).toEqual({ rev: 1, answer: "x" });
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ rev: 2, answer: "y", step: 3 });
  });
  it("issue passes tier query", async () => {
    const f = vi.fn(() => json(200, { id: "a", rev: 0 }));
    await client(f).issue("challenge");
    expect((f.mock.calls[0] as unknown as [string])[0]).toBe("http://h:1/api/attempt?tier=challenge");
  });
  it("maps statuses to typed errors", async () => {
    await expect(client(() => json(426, { error: "v" })).status()).rejects.toBeInstanceOf(VersionError);
    const u = await client(() => json(503, { error: "x", reason: "quiet_hours", retry_after_s: 60 }))
      .status().catch((e) => e);
    expect(u).toBeInstanceOf(UnavailableError);
    expect(u.reason).toBe("quiet_hours");
    expect(u.retryAfter).toBe(60);
    for (const s of [409, 400, 404]) {
      const e = await client(() => json(s, { error: "e", code: "stale_rev" })).get("a").catch((x) => x);
      expect(e).toBeInstanceOf(RejectedError);
      expect(e.code).toBe("stale_rev");
    }
    const r = await client(() => json(429, { error: "rl", retry_after_s: 5 })).status().catch((e) => e);
    expect(r).toBeInstanceOf(RateLimitError);
    expect(r.retryAfter).toBe(5);
  });
  it("timeout becomes NetworkError", async () => {
    vi.useFakeTimers();
    const f = (_u: string, init: RequestInit) =>
      new Promise((_res, rej) => {
        init.signal!.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      });
    const p = client(f, 10_000).status().catch((e) => e);
    await vi.advanceTimersByTimeAsync(10_001);
    const e = await p;
    expect(e).toBeInstanceOf(NetworkError);
    expect(e.timedOut).toBe(true);
  });
  it("timeout while reading the body is reported as a timeout", async () => {
    vi.useFakeTimers();
    const f = (_u: string, init: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          new Promise((_res, rej) => {
            init.signal!.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
          }),
      } as unknown as Response);
    const p = client(f, 10_000).status().catch((e) => e);
    await vi.advanceTimersByTimeAsync(10_001);
    const e = await p;
    expect(e).toBeInstanceOf(NetworkError);
    expect(e.timedOut).toBe(true);
  });
  it("rejects a second concurrent write with BusyError, then frees the slot", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const f = vi.fn(async () => {
      await gate;
      return new Response(JSON.stringify({ id: "a", rev: 2 }), { status: 200 });
    });
    const c = client(f);
    const first = c.hint("a", 1);
    await expect(c.skip("a", 1)).rejects.toBeInstanceOf(BusyError);
    release();
    await first;
    await expect(c.skip("a", 2)).resolves.toBeDefined();
  });
  it("frees the write slot after a failure and does not retry", async () => {
    const f = vi.fn(() => json(409, { error: "e", code: "stale" }));
    const c = client(f);
    await expect(c.hint("a", 1)).rejects.toBeInstanceOf(RejectedError);
    await expect(c.hint("a", 1)).rejects.toBeInstanceOf(RejectedError);
    expect(f).toHaveBeenCalledTimes(2);
  });
  it("never leaks the token in error messages", async () => {
    const errs: Error[] = [];
    const fails = [
      () => Promise.reject(new TypeError("failed " + TOKEN)),
      () => json(426, {}), () => json(401, {}), () => json(500, {}),
      () => json(503, { reason: "r" }), () => json(409, { code: "c" }), () => json(429, {}),
    ];
    for (const fn of fails) errs.push(await client(fn).status().catch((e) => e));
    for (const e of errs) expect(e.message).not.toContain(TOKEN);
  });
});
